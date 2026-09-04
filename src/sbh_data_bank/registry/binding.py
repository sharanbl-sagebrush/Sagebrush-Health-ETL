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
        parts = []
        if self.missing:
            parts.append(f"missing {list(self.missing)}")
        if self.forbidden_present:
            parts.append(f"unexpectedly has {list(self.forbidden_present)}")
        return f"{self.variant}: " + "; ".join(parts)


class NoVariantMatchedError(ValueError):
    """The file's headers match no known shape of this source."""

    def __init__(self, spec_key: str, rejections: list[VariantRejection], headers: tuple[str, ...]):
        self.spec_key = spec_key
        self.rejections = rejections
        self.headers = headers
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
    """
    matched: list[SchemaVariant] = []
    rejections: list[VariantRejection] = []

    for variant in spec.variants:
        missing = tuple(n for n in variant.detect.require_all if n not in headers)
        forbidden = tuple(n for n in variant.detect.require_none if n in headers)
        if missing or forbidden:
            rejections.append(VariantRejection(variant.name, missing, forbidden))
        else:
            matched.append(variant)

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
        return tuple(b for b in self.present if b.column.pii is not PiiClass.PROHIBITED)

    @property
    def prohibited(self) -> tuple[ColumnBinding, ...]:
        return tuple(b for b in self.present if b.column.pii is PiiClass.PROHIBITED)

    @property
    def renamed(self) -> tuple[ColumnBinding, ...]:
        return tuple(b for b in self.present if b.is_renamed)

    @property
    def has_drift(self) -> bool:
        return bool(self.missing_required or self.new_columns)

    def drift_report(self) -> str:
        """Human-readable summary of everything that did not line up."""
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
    variant = select_variant(spec, headers)

    bindings: list[ColumnBinding] = []
    matched_positions: set[int] = set()
    missing_required: list[str] = []
    missing_optional: list[str] = []

    for column in variant.columns:
        if column.is_injected:
            # Supplied by the loader - sheet_name_column today. It has no
            # vendor header, so it is neither matched nor missing.
            bindings.append(ColumnBinding(column=column, match=None))
            continue
        match = headers.find(column.target, column.sources)
        bindings.append(ColumnBinding(column=column, match=match))
        if match is None:
            (missing_required if column.required else missing_optional).append(column.target)
        else:
            matched_positions.add(match.position)

    return Binding(
        spec=spec,
        variant=variant,
        columns=tuple(bindings),
        missing_required=tuple(missing_required),
        missing_optional=tuple(missing_optional),
        new_columns=tuple(sorted(headers.unmatched_headers(matched_positions))),
    )
