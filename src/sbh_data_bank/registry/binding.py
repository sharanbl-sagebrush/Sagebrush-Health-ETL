"""Deciding what a file is, and wiring its columns to a spec.

Two steps, kept separate because they fail for different reasons and need
different diagnostics.

**Variant selection** asks which shape this file is, using its header set
alone. Not the filename: a Cardinal file named ``01012023-…`` contains no 2023
data, WeInfuse switched the same export from CSV to XLSX, and sixteen of
eighteen sources in the previous pipeline never matched their glob at all -
logged as a warning while the job reported success. Headers are what a file
actually is.

**Column binding** wires each spec field to a column position and reports what
did not line up: required columns the vendor stopped sending, and columns the
vendor started sending that the spec does not know about. The previous pipeline
dropped unknown columns silently, so a vendor could add a corrected value in a
new column and nobody would find out.

Both failures are deliberately verbose. An operator looking at a file that will
not load needs to know which variants were considered and what each one wanted,
not that "no parser matched".

How this fits
-------------
``ingest.load`` calls ``bind`` once per file after ``ingest.reader`` has found
the header row and built a ``HeaderIndex`` from it. The ``Binding`` that comes
back is then handed to the reader, which uses each ``ColumnBinding.match``'s
position to pull the right cell out of every data row. ``scripts/bind_real_files.py``
calls the same function against every real extract to prove each spec still
fits its files. Inputs come from ``registry.spec`` and ``registry.headers``;
nothing in this module reads a file or touches a database.
"""

from __future__ import annotations

from dataclasses import dataclass

from sbh_data_bank.registry.headers import HeaderIndex, HeaderMatch
from sbh_data_bank.registry.spec import (
    ColumnSpec,
    DriftPolicy,
    PiiClass,
    SchemaVariant,
    SourceSpec,
)

__all__ = [
    "AmbiguousVariantError",
    "Binding",
    "ColumnBinding",
    "NoVariantMatchedError",
    "VariantRejection",
    "bind",
    "select_variant",
]


@dataclass(frozen=True, slots=True)
class VariantRejection:
    """Why one variant did not match. Collected for the error message."""

    variant: str
    missing: tuple[str, ...]
    """Headers the detector required but the file does not have."""
    forbidden_present: tuple[str, ...]
    """Headers the detector required to be absent but the file has."""

    def describe(self) -> str:
        """One line for the operator: the variant name and what disqualified it."""
        # At least one of the two tuples is non-empty, or the variant would
        # have matched rather than been rejected.
        parts = []
        if self.missing:
            parts.append(f"missing {list(self.missing)}")
        if self.forbidden_present:
            parts.append(f"unexpectedly has {list(self.forbidden_present)}")
        return f"{self.variant}: " + "; ".join(parts)


class NoVariantMatchedError(ValueError):
    """The file's headers match no known shape of this source."""

    def __init__(self, spec_key: str, rejections: list[VariantRejection], headers: tuple[str, ...]):
        # Keep the structured inputs on the exception so a caller (the bind
        # script, a future triage screen) can inspect them without parsing
        # the message.
        self.spec_key = spec_key
        self.rejections = rejections
        self.headers = headers
        # The message lists every variant with its reason, then the first
        # dozen headers the file actually has, so the usual cause - a vendor
        # renamed a column the detector keys on - is visible at a glance.
        detail = "\n  ".join(r.describe() for r in rejections)
        super().__init__(
            f"{spec_key}: no variant matches this file.\n"
            f"  {detail}\n"
            f"  file has {len(headers)} columns: {list(headers[:12])}"
            f"{' ...' if len(headers) > 12 else ''}"
        )


class AmbiguousVariantError(ValueError):
    """More than one variant matched.

    A spec bug rather than a data problem - two detectors overlap and need a
    ``require_none`` to separate them, exactly as the Cardinal summary and
    detail reports do.
    """

    def __init__(self, spec_key: str, matched: list[str]):
        self.spec_key = spec_key
        self.matched = matched
        super().__init__(
            f"{spec_key}: {len(matched)} variants match the same file: {matched}. "
            "Detectors must be mutually exclusive; add require_none to separate them."
        )


def select_variant(spec: SourceSpec, headers: HeaderIndex) -> SchemaVariant:
    """Choose the one variant whose detector the file satisfies.

    Exactly one must match. Zero and two are both errors, and neither is
    resolved by falling back to a default - guessing which shape a file is,
    when the answer decides how every column is interpreted, is how a
    9-column summary gets read as a 17-column detail report.

    Raises ``NoVariantMatchedError`` when nothing matches and
    ``AmbiguousVariantError`` when more than one does.
    """
    matched: list[SchemaVariant] = []
    rejections: list[VariantRejection] = []

    # Every variant is tested; there is no short-circuit on the first hit,
    # because a second hit is an error that must be reported, not hidden.
    # ``n in headers`` goes through HeaderIndex.__contains__, so the detector's
    # names are compared after the same normalisation as everything else.
    for variant in spec.variants:
        # require_all: each listed header must be present.
        missing = tuple(n for n in variant.detect.require_all if n not in headers)
        # require_none: each listed header must be absent. This is what keeps
        # the Cardinal summary (no NDC) from matching the detail detector.
        forbidden = tuple(n for n in variant.detect.require_none if n in headers)
        if missing or forbidden:
            rejections.append(VariantRejection(variant.name, missing, forbidden))
        else:
            matched.append(variant)

    # Exactly one is the only acceptable outcome.
    if len(matched) == 1:
        return matched[0]
    if not matched:
        raise NoVariantMatchedError(spec.key, rejections, headers.headers)
    raise AmbiguousVariantError(spec.key, [v.name for v in matched])


@dataclass(frozen=True, slots=True)
class ColumnBinding:
    """One spec field, and the file column it resolved to.

    ``match`` is ``None`` when the file does not have the column. For an
    optional column that is normal and the value is null; for a required one it
    is drift; for a reader-injected column it is expected, because no vendor
    header supplies it.
    """

    column: ColumnSpec
    match: HeaderMatch | None

    @property
    def is_injected(self) -> bool:
        """True when the loader supplies the value rather than a file column."""
        return self.column.is_injected

    @property
    def is_present(self) -> bool:
        """Whether this column will carry a value.

        An injected column always will, even though it has no match - it is
        never absent, so it must never be reported as drift.
        """
        return self.match is not None or self.column.is_injected

    @property
    def is_renamed(self) -> bool:
        """True when the file used an older accepted name rather than the first.

        Worth surfacing: it says a vendor has moved on, and the deprecated
        alias can eventually be retired from the spec.
        """
        # sources[0] is the preferred (current) header by convention; a match
        # on anything later in the list means the file is on an older name.
        # Injected columns have no sources, so they are excluded before the
        # index is read.
        return (
            self.match is not None
            and not self.column.is_injected
            and self.match.matched_alias != self.column.sources[0]
        )


@dataclass(frozen=True, slots=True)
class Binding:
    """The complete wiring of one file to one variant, plus what did not fit."""

    spec: SourceSpec
    variant: SchemaVariant
    columns: tuple[ColumnBinding, ...]
    # One ColumnBinding per spec column, in spec order, matched or not.
    missing_required: tuple[str, ...]
    missing_optional: tuple[str, ...]
    new_columns: tuple[str, ...]
    """Real columns in the file that the spec does not claim. The drift signal."""

    @property
    def present(self) -> tuple[ColumnBinding, ...]:
        """Bindings that resolved to an actual column."""
        return tuple(b for b in self.columns if b.is_present)

    @property
    def landed(self) -> tuple[ColumnBinding, ...]:
        """Bindings whose values are written.

        Excludes PROHIBITED columns, which are matched so that drift and
        column-count checks still see them, and then discarded without their
        values being read into anything that persists.
        """
        # ingest.load uses this list to pick control-total columns and the
        # fields that go into the row hash, so a prohibited value can never
        # influence either. ingest.reader walks ``columns`` directly and skips
        # PROHIBITED itself before reading the cell.
        return tuple(b for b in self.present if b.column.pii is not PiiClass.PROHIBITED)

    @property
    def prohibited(self) -> tuple[ColumnBinding, ...]:
        """Bindings that matched a column the spec forbids from landing."""
        return tuple(b for b in self.present if b.column.pii is PiiClass.PROHIBITED)

    @property
    def renamed(self) -> tuple[ColumnBinding, ...]:
        """Bindings that matched on a deprecated alias rather than the first name."""
        return tuple(b for b in self.present if b.is_renamed)

    @property
    def has_drift(self) -> bool:
        """Whether anything structural changed. Optional columns going missing
        and alias renames are reported but are not drift."""
        return bool(self.missing_required or self.new_columns)

    def drift_report(self) -> str:
        """Human-readable summary of everything that did not line up."""
        # Each kind of mismatch gets its own clause, so the report reads as a
        # checklist. Renames are shown as target<-vendor header.
        lines = []
        if self.missing_required:
            lines.append(f"required columns absent: {list(self.missing_required)}")
        if self.missing_optional:
            lines.append(f"optional columns absent: {list(self.missing_optional)}")
        if self.new_columns:
            lines.append(f"columns the spec does not know: {list(self.new_columns)}")
        if self.renamed:
            lines.append(
                "matched on a deprecated alias: "
                + str([f"{b.column.target}<-{b.match.source_header}" for b in self.renamed])
            )
        return "; ".join(lines) or "no drift"

    def blocking_problem(self) -> str | None:
        """The reason this file must not load, or ``None`` if it may.

        Policy lives on the spec so a source that routinely gains optional
        columns can be tolerant while one that must not change is strict.
        """
        # WARN is the only policy that lets the file through; FAIL and
        # QUARANTINE_FILE both block here. Missing required columns are
        # checked first because they are the more serious of the two: a load
        # would quarantine every row anyway.
        if self.missing_required and self.spec.on_missing_column is not DriftPolicy.WARN:
            return f"required columns absent: {list(self.missing_required)}"
        if self.new_columns and self.spec.on_new_column is not DriftPolicy.WARN:
            return f"unknown columns present: {list(self.new_columns)}"
        return None


def bind(spec: SourceSpec, headers: HeaderIndex) -> Binding:
    """Select the variant and wire every column, reporting drift.

    Does not raise on drift; it reports it, and the caller applies policy via
    :meth:`Binding.blocking_problem`. Raises only when the file cannot be
    identified at all, because there is nothing useful to report about a file
    whose shape is unknown.
    """
    # Step 1: which shape is this file. Raises if the answer is not exactly one.
    variant = select_variant(spec, headers)

    bindings: list[ColumnBinding] = []
    matched_positions: set[int] = set()
    missing_required: list[str] = []
    missing_optional: list[str] = []

    # Step 2: one lookup per spec column, in spec order.
    for column in variant.columns:
        if column.is_injected:
            # Supplied by the loader - sheet_name_column today. It has no
            # vendor header, so it is neither matched nor missing.
            bindings.append(ColumnBinding(column=column, match=None))
            continue
        # First accepted name present in the file wins; None if none is.
        match = headers.find(column.target, column.sources)
        bindings.append(ColumnBinding(column=column, match=match))
        if match is None:
            # Absent. Which list it lands in depends on ``required``, and
            # that in turn decides whether blocking_problem can block the file.
            (missing_required if column.required else missing_optional).append(column.target)
        else:
            # Remember the position so step 3 can tell claimed columns from
            # unclaimed ones. Prohibited columns are claimed here too, which
            # is the point: SSN must not show up as a "new column" every week.
            matched_positions.add(match.position)

    # Step 3: every real column the spec did not claim is new-column drift.
    # Sorted so the report and any test against it are deterministic.
    return Binding(
        spec=spec,
        variant=variant,
        columns=tuple(bindings),
        missing_required=tuple(missing_required),
        missing_optional=tuple(missing_optional),
        new_columns=tuple(sorted(headers.unmatched_headers(matched_positions))),
    )
