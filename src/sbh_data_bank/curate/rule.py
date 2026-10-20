"""The dedup rule, applied in memory: one rule for every source.

``store.schema.DEDUP_RULE`` names it; CLAUDE.md ("Duplicates") and
docs/architecture-and-plan.md ("Duplicates and delivery order") give the
reasoning. In one paragraph:

A row's identity is its identity columns (the spec's natural key, or every
landed column when there is none) plus an occurrence number: rows in one
delivery that share identity columns are numbered 1, 2, 3 in file order, so
nothing within a file is ever collapsed. Deliveries are applied in as-of
order. Against the current row of the same identity, an incoming row is
inserted (no current row), unchanged (newer as-of, same hash), a correction
(newer as-of, different hash: insert and supersede), late (older as-of:
history only) or a conflict (same as-of, different hash: the batch is held
for a person). After the rows, a current row that the delivery covers but no
longer carries is superseded with reason ``vanished``.

What vanish means, exactly (decisions D1 to D3): the delivery is newer than
the current row, the row's event date falls inside the delivery's coverage
window, the row's scope values (``vanish_scope``, e.g. McKesson's account
number) are among those the delivery carries, and the row's identity is
absent from the delivery. A delivery with no known coverage vanishes nothing.
A vanished row is never deleted; it is superseded and stays queryable.

How this fits
-------------
:class:`Ledger` is the in-memory curated state of one source: what the
``curated.*`` table will hold once the commit engine writes it. The layer
walk builds one per source from the real files; tests build small ones. The
commit engine will do the same per batch against the database, with the
same outcomes, and the names here are the names on ``meta.batch``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sbh_data_bank.core.identity import stable_row_hash
from sbh_data_bank.curate.delivery import Coverage
from sbh_data_bank.ingest.reader import TypedRow
from sbh_data_bank.registry.spec import SourceSpec
from sbh_data_bank.store.schema import identity_columns_for

__all__ = [
    "CurrentRow",
    "DeliveryFacts",
    "Ledger",
    "Outcomes",
    "RowFact",
    "SupersededRow",
    "facts_for",
]


@dataclass(frozen=True, slots=True)
class RowFact:
    """What the curated layer needs from one typed row. Carries no cell value
    except the event date and the scope values the rule compares on."""

    line_number: int
    identity_hash: str
    occurrence: int
    row_hash: str
    event_date: date | None
    """The row's event date; for a snapshot source, the delivery's as-of."""
    scope: tuple[Any, ...]
    """The row's ``vanish_scope`` values, in spec order. Empty when unscoped."""
    controls: tuple[Decimal, ...]
    """The row's control-total values, so current totals can be summed."""


@dataclass(frozen=True, slots=True)
class DeliveryFacts:
    """One delivery as the rule sees it: when, what window, which rows."""

    label: str
    as_of: date
    coverage: Coverage | None
    rows: Sequence[RowFact]


@dataclass(slots=True)
class CurrentRow:
    """A live curated row: the version of one identity that is current."""

    row_hash: str
    as_of: date
    delivery: str
    line_number: int
    event_date: date | None
    scope: tuple[Any, ...]
    controls: tuple[Decimal, ...]
    ref: Any = None
    """An opaque handle to the stored row, for the commit engine: the
    (batch_id, source_file_id, line_number) that keys it in ``curated.*``.
    ``None`` for rows that exist only in memory."""


@dataclass(frozen=True, slots=True)
class SupersededRow:
    """A row that stopped being current, and why. Never deleted."""

    identity_hash: str
    occurrence: int
    row: CurrentRow
    reason: str
    """``corrected`` | ``vanished``; a late row is recorded with ``late``."""
    by: str
    """The label of the delivery that retired it."""


@dataclass(slots=True)
class Outcomes:
    """What one delivery did. Every incoming row lands in exactly one of the
    first five counters; ``vanished`` counts prior rows, not incoming ones."""

    inserted: int = 0
    unchanged: int = 0
    corrected: int = 0
    reordered: int = 0
    """Of the corrections, how many only swapped the hashes of colliding
    lines within one identity: the vendor emitted them in another order."""
    late: int = 0
    conflict: int = 0
    vanished: int = 0
    held: bool = False
    """True when a conflict held the whole delivery: nothing was applied."""

    @property
    def rows(self) -> int:
        return self.inserted + self.unchanged + self.corrected + self.late + self.conflict

    def as_counter(self) -> Counter:
        return Counter(
            inserted=self.inserted,
            unchanged=self.unchanged,
            corrected=self.corrected,
            late=self.late,
            conflict=self.conflict,
            vanished=self.vanished,
        )


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - a non-numeric control cell counts as zero
        return Decimal(0)


def facts_for(
    spec: SourceSpec,
    rows: Iterable[TypedRow],
    *,
    as_of: date,
    controls: Sequence[str] = (),
) -> list[RowFact]:
    """Reduce one delivery's typed rows to :class:`RowFact`s.

    Assigns the occurrence number: rows sharing an identity hash are numbered
    in the order given, which must be file order. ``as_of`` stands in as the
    event date of every row of a snapshot source. ``controls`` names the
    control-total columns to carry.
    """
    identity = identity_columns_for(spec)
    event_column = spec.event_date_column
    counts: Counter = Counter()
    facts: list[RowFact] = []
    for row in rows:
        identity_hash = stable_row_hash(row.values, identity)
        counts[identity_hash] += 1
        if spec.is_snapshot:
            event_date: date | None = as_of
        else:
            event_date = row.values.get(event_column) if event_column else None
        facts.append(
            RowFact(
                line_number=row.line_number,
                identity_hash=identity_hash,
                occurrence=counts[identity_hash],
                row_hash=row.values["_row_hash"],
                event_date=event_date,
                scope=tuple(row.values.get(c) for c in spec.vanish_scope),
                controls=tuple(_decimal(row.values.get(c)) for c in controls),
            )
        )
    return facts


class Ledger:
    """The curated state of one source, in memory.

    ``current`` maps (identity hash, occurrence) to the live version;
    ``history`` holds every row that was superseded or arrived late, with the
    reason. Apply deliveries in any order: as-of decides, not arrival.
    """

    __slots__ = ("current", "history", "_vanish", "_scoped")

    def __init__(self, *, vanish: bool = False, scoped: bool = False) -> None:
        """``vanish`` is ``SourceSpec.vanishes``; ``scoped`` is whether the
        spec declares a ``vanish_scope``, so an empty scope tuple on every row
        is not mistaken for "every row is in scope"."""
        self.current: dict[tuple[str, int], CurrentRow] = {}
        self.history: list[SupersededRow] = []
        self._vanish = vanish
        self._scoped = scoped

    @classmethod
    def for_spec(cls, spec: SourceSpec) -> Ledger:
        return cls(vanish=spec.vanishes, scoped=bool(spec.vanish_scope))

    def _outcome_for(self, fact: RowFact, as_of: date) -> str:
        current = self.current.get((fact.identity_hash, fact.occurrence))
        if current is None:
            return "inserted"
        if as_of > current.as_of:
            return "unchanged" if fact.row_hash == current.row_hash else "corrected"
        if as_of == current.as_of:
            return "unchanged" if fact.row_hash == current.row_hash else "conflict"
        return "late"

    def decide(self, delivery: DeliveryFacts) -> list[tuple[RowFact, str]]:
        """Every incoming row's outcome against the current state, in file
        order, without changing anything. ``apply`` starts from this; the
        commit engine reads it to know which outcome to write on each row."""
        return [(fact, self._outcome_for(fact, delivery.as_of)) for fact in delivery.rows]

    def apply(self, delivery: DeliveryFacts) -> Outcomes:
        """Apply one delivery and return what it did.

        Decides every row's outcome first and mutates nothing if any row is a
        conflict: a batch with a conflict is held for a person, whole. That is
        the commit engine's transaction boundary, reproduced here.
        """
        out = Outcomes()
        decided = self.decide(delivery)
        for _fact, outcome in decided:
            setattr(out, outcome, getattr(out, outcome) + 1)
        if out.conflict:
            out.held = True
            return out

        # Hashes already current for each identity, so a correction that only
        # reorders colliding lines can be told from a real edit.
        sibling_hashes: dict[str, set[str]] = defaultdict(set)
        for (identity_hash, _occ), row in self.current.items():
            sibling_hashes[identity_hash].add(row.row_hash)

        for fact, outcome in decided:
            key = (fact.identity_hash, fact.occurrence)
            incoming = CurrentRow(
                row_hash=fact.row_hash,
                as_of=delivery.as_of,
                delivery=delivery.label,
                line_number=fact.line_number,
                event_date=fact.event_date,
                scope=fact.scope,
                controls=fact.controls,
            )
            if outcome == "inserted":
                self.current[key] = incoming
            elif outcome == "corrected":
                previous = self.current[key]
                if fact.row_hash in sibling_hashes[fact.identity_hash]:
                    out.reordered += 1
                self.history.append(SupersededRow(*key, previous, "corrected", delivery.label))
                self.current[key] = incoming
            elif outcome == "late":
                # Stored as history behind the current row; current untouched.
                self.history.append(SupersededRow(*key, incoming, "late", delivery.label))
            # unchanged: nothing written, counted only.

        if self._vanish and delivery.coverage is not None:
            out.vanished = self._vanish_absent(delivery)
        return out

    def _vanish_absent(self, delivery: DeliveryFacts) -> int:
        """Supersede current rows this delivery covers but no longer carries."""
        carried = {(f.identity_hash, f.occurrence) for f in delivery.rows}
        scopes = {f.scope for f in delivery.rows} if self._scoped else None
        coverage = delivery.coverage
        assert coverage is not None
        gone: list[tuple[str, int]] = []
        for key, row in self.current.items():
            if key in carried or row.as_of >= delivery.as_of:
                continue  # carried, or not older than this delivery
            if row.event_date is None or not coverage.covers(row.event_date):
                continue  # outside the window this delivery speaks for
            if scopes is not None and row.scope not in scopes:
                continue  # another account's, site's or program's rows
            gone.append(key)
        for key in gone:
            row = self.current.pop(key)
            self.history.append(SupersededRow(*key, row, "vanished", delivery.label))
        return len(gone)

    def control_sums(self, width: int) -> list[Decimal]:
        """Sum of each control column over current rows."""
        sums = [Decimal(0)] * width
        for row in self.current.values():
            for i, value in enumerate(row.controls):
                sums[i] += value
        return sums

    def snapshot(self) -> dict[tuple[str, int], str]:
        """Identity -> current row hash, for "did anything move" comparisons."""
        return {key: row.row_hash for key, row in self.current.items()}
