"""Re-run the typed layer from the landed layer and say what would change.

For every file a source has loaded, the landed rows and the recorded
header are read back from the store, typed again under the spec as it is
now (``ingest.replay``), and compared line by line with what the original
load wrote to ``typed.*`` and ``stg.quarantine``:

=====================  ===================================================
``unchanged``          typed then and now, same row hash
``rehashed``           typed then and now, different row hash: a parsing
                       rule changed what the value is
``now_quarantined``    typed then, quarantined now
``now_typed``          quarantined then, typed now
``still_quarantined``  quarantined both times (reasons may differ)
=====================  ===================================================

plus whether the replay's control totals equal the ones the batch
recorded, and whether the replay reached ``LOADED`` at all. Under an
unchanged spec every line is ``unchanged`` and the totals agree; that is
the proof the landed layer is complete, and ``scripts/retype.py`` ran it
over the whole database on 2026-10-22 (``docs/decision-record.md``).

Nothing in the layers is written. Replacing the typed rows and
re-curating from them is decision D18, not yet taken: the curated ledger
is append-only, so a rebuild has to say what becomes of the rows curated
from the old typing. The one thing a run records is its own verdict, one
``meta.spec_check`` row per source (:func:`record_check`): the spec
digest it ran under and how many files would change. A verdict of none
is what lets the freshness check stop raising ``SPEC_CHANGED`` for that
digest, since the rows already typed are exactly what the spec would
produce.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from sbh_data_bank.commit.engine import spec_sha256
from sbh_data_bank.commit.records import LayerReader, LoadedFile
from sbh_data_bank.ingest.replay import load_from_landed
from sbh_data_bank.registry.spec import SourceSpec

__all__ = ["SPEC_CHECK_INSERT", "FileReplay", "record_check", "replay_source"]

#: How many line numbers a replay keeps per difference kind, for the report.
SAMPLE = 5


@dataclass(slots=True)
class FileReplay:
    """One loaded file typed again from the store, against its original."""

    file: LoadedFile
    outcome: str
    detail: str
    lines: int = 0
    unchanged: int = 0
    rehashed: int = 0
    now_quarantined: int = 0
    now_typed: int = 0
    still_quarantined: int = 0
    totals_agree: bool = True
    samples: dict[str, list[int]] = field(default_factory=dict)
    """Up to :data:`SAMPLE` line numbers per difference kind."""

    @property
    def identical(self) -> bool:
        """The replay reproduces the original load exactly."""
        return (
            self.outcome == "LOADED"
            and self.totals_agree
            and not (self.rehashed or self.now_quarantined or self.now_typed)
        )

    def summary(self) -> str:
        name = self.file.file_name
        if self.file.sheet_name:
            name = f"{name} [{self.file.sheet_name}]"
        parts = [name, f"[{self.outcome}]", f"lines={self.lines}"]
        parts.append(
            f"unchanged={self.unchanged} rehashed={self.rehashed} "
            f"now_quarantined={self.now_quarantined} now_typed={self.now_typed} "
            f"still_quarantined={self.still_quarantined}"
        )
        parts.append("totals agree" if self.totals_agree else "TOTALS DIFFER")
        for kind, lines in sorted(self.samples.items()):
            parts.append(f"{kind} at lines {lines}")
        if self.detail:
            parts.append(f"- {self.detail}")
        return " ".join(parts)


def _note(replay: FileReplay, kind: str, line: int) -> None:
    sample = replay.samples.setdefault(kind, [])
    if len(sample) < SAMPLE:
        sample.append(line)


def _recorded_sums(control_totals: str | None) -> dict[str, Decimal]:
    if not control_totals:
        return {}
    sums = json.loads(control_totals).get("sums") or {}
    return {k: Decimal(v) for k, v in sums.items()}


def replay_file(reader: LayerReader, spec: SourceSpec, file: LoadedFile) -> FileReplay:
    """Type one loaded file again from its landed rows and compare."""
    load = load_from_landed(
        spec,
        file.header_json,
        reader.landed_lines(spec, file.source_file_id),
        filename=file.file_name,
        sheet=file.sheet_name,
    )
    replay = FileReplay(file, load.outcome, load.detail, lines=len(load.landed))
    if load.outcome != "LOADED":
        return replay
    before_typed = reader.typed_hashes(spec, file.source_file_id)
    before_quarantined = reader.quarantined_lines(file.batch_id)
    for row in load.rows:
        line = row.line_number
        if line in before_typed:
            if before_typed[line] == row.values["_row_hash"]:
                replay.unchanged += 1
            else:
                replay.rehashed += 1
                _note(replay, "rehashed", line)
        else:
            replay.now_typed += 1
            _note(replay, "now_typed", line)
    for q in load.quarantined:
        if q.line_number in before_quarantined:
            replay.still_quarantined += 1
        else:
            replay.now_quarantined += 1
            _note(replay, "now_quarantined", q.line_number)
    recorded = _recorded_sums(file.control_totals)
    computed = {k: v for k, v in load.counters.control_totals.items()}
    replay.totals_agree = all(computed.get(k, Decimal(0)) == v for k, v in recorded.items())
    return replay


def replay_source(reader: LayerReader, spec: SourceSpec, tenant_id: str) -> Iterator[FileReplay]:
    """Every loaded file of one source, in the order they were registered."""
    for file in reader.loaded_files(tenant_id, spec.key):
        yield replay_file(reader, spec, file)


class _Queryable(Protocol):
    def execute(self, sql: str, *params: Any) -> list[tuple]: ...


_SPEC_CHECK_COLUMNS = [
    "tenant_id", "source_key", "spec_sha256", "checked_at", "checked_by", "files", "lines",
    "files_changed",
]  # fmt: skip
SPEC_CHECK_INSERT = (
    f"INSERT INTO meta.spec_check ({', '.join(_SPEC_CHECK_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _SPEC_CHECK_COLUMNS)})"
)


def record_check(
    db: _Queryable,
    spec: SourceSpec,
    replays: list[FileReplay],
    *,
    tenant_id: str,
    actor: str,
    now: datetime | None = None,
) -> None:
    """One ``meta.spec_check`` row for a completed replay of one source."""
    db.execute(
        SPEC_CHECK_INSERT,
        tenant_id,
        spec.key,
        spec_sha256(spec),
        now or datetime.now(),
        actor,
        len(replays),
        sum(r.lines for r in replays),
        sum(1 for r in replays if not r.identical),
    )
