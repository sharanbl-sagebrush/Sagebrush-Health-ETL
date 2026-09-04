"""The source spec - a declarative description of one vendor extract.

A spec says how to recognise a file, how to read it, what its columns mean, how
to type them, what identifies a row, and what to do when the vendor changes
something. It is data, not code: today it is authored as YAML and validated
here, and it will later live in the database as tenant-scoped rows so a new
source can be onboarded without a deployment.

The previous implementation had eighteen hand-written parser classes and a YAML
file whose only per-source knobs were a glob, a row offset and a table name.
Everything that actually varies between vendors - column mappings, date
formats, schema variants, junk columns, injected constants - lived in Python.
Every axis modelled below is one that was verified in a real Sagebrush extract,
not one imagined for generality:

``variants``
    Cardinal ships a 9-column invoice summary and a 17-column invoice detail
    report under similar names. WeInfuse moved to a "new reporting module" and
    its Administered export went from 33 CSV columns to 35 XLSX ones.
``ColumnSpec.sources`` (a list, not a string)
    Wellpartner ships a backtick in ``Days` Supply``; Allscripts renamed
    ``LOCATION_NAME`` to ``LOCATION_NAME1``.
``ColumnSpec.date_formats`` (required for dates)
    The Cardinal detail report ships ISO in one pull and US in the next.
``ReadSpec.preamble_lines``
    The ABC CSV opens with a ``sep=,`` line and a report title before its
    header; Athena exports carry a title row.
``ReadSpec.sheet``
    Workbook sheets are named ``Results``, ``sheet1`` and ``Sheet1`` across
    vendors - matched case-insensitively.
``constants``
    One Pharmaforce spec serves Fire Mesa and Southington, differing only by an
    injected location. The previous code special-cased this for Pharmaforce
    alone.
``pii``
    Monthly Payments carries SSN on 69% of rows. Some columns must never land.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ColumnSpec",
    "FooterPolicy",
    "VariantAction",
    "DriftPolicy",
    "FieldType",
    "OnError",
    "PiiClass",
    "ReadSpec",
    "SchemaVariant",
    "SourceLifecycle",
    "SourceSpec",
    "VariantDetector",
]

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", min_length=1, max_length=128)]
SourceKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=128)]


class FieldType(StrEnum):
    """How a column's raw text becomes a typed value."""

    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    CURRENCY = "currency"
    """Money. Parsed to Decimal and never through float, and understands
    accounting parentheses."""
    DATE = "date"
    BOOLEAN = "boolean"
    NDC = "ndc"
    """Normalized to 11-digit 5-4-2, resolved against the NDC dimension when the
    source gives an unhyphenated 10-digit code."""


class PiiClass(StrEnum):
    """What kind of identifier a column holds, and therefore how it is stored."""

    NONE = "none"
    """Not identifying. Stored in clear."""

    JOIN_KEY = "join_key"
    """An internal identifier such as PatNum or MRN. Stored in clear because
    reconciliation joins on it, but it is still PHI in context."""

    QUASI = "quasi"
    """Indirect identifier - postcode, state, gender. Stored in clear; risky
    only in combination."""

    DIRECT = "direct"
    """Name, date of birth, address, email, telephone. Encrypted at rest with
    column-level access control and an access audit trail."""

    PROHIBITED = "prohibited"
    """Never lands, in any form. The column is read and discarded so that
    column-count and drift checks still see it, but no value is written
    anywhere - not to the raw layer, not to quarantine, not to a log.

    Currently: SSN in the Monthly Payments extract, which is populated on 69%
    of rows and is never needed to reconcile a payment.
    """


class OnError(StrEnum):
    """What happens when one cell fails to parse."""

    QUARANTINE = "quarantine"
    """The row goes to quarantine with a reason code. The default, because a
    value that cannot be parsed is a value nobody has decided about yet."""

    NULL = "null"
    """The value becomes null and the row proceeds. Must be chosen explicitly -
    this is the behaviour that let the previous pipeline lose 17% of every
    payments file without anyone noticing."""

    FAIL = "fail"
    """Abort the whole file. For columns where a parse failure means the file
    is not what it claims to be."""


class DriftPolicy(StrEnum):
    """What happens when the vendor's column set changes."""

    FAIL = "fail"
    """Stop. Nothing lands until a human has looked."""

    QUARANTINE_FILE = "quarantine_file"
    """Land nothing, keep the file, raise an exception for triage."""

    WARN = "warn"
    """Load anyway and record the drift. Appropriate only where a vendor adds
    optional columns routinely."""


class VariantAction(StrEnum):
    """What to do with a file once its variant is known."""

    LOAD = "load"
    """Read it and land the rows. The normal case."""

    RECOGNISE_ONLY = "recognise_only"
    """Identify the file, then land nothing.

    For a report the vendor still produces but which has been superseded by a
    better one covering the same ground. Cardinal is the live example: the
    9-column invoice summary carries the same invoices as the 17-column detail
    report but without NDC, line number or quantity, and the April 2026 detail
    pull covers the whole period. Loading both would double-count every
    purchase.

    Recognising it still matters. Dropping the variant entirely would leave the
    file unidentifiable, and an unidentified file is either an alert somebody
    has to chase or - worse, if a detector were loosened to accommodate it - a
    summary read as though it were detail. This says "we know exactly what this
    is, and we are deliberately not loading it", which is a different and much
    safer statement.
    """


class FooterPolicy(StrEnum):
    """How to treat a trailing summary row."""

    NONE = "none"
    """No footer expected. A trailing row that looks like one still
    quarantines, because it is not data."""

    TOTALS_ROW = "totals_row"
    """The last row is a totals line: key columns empty, numeric columns
    populated.

    The Cardinal detail export ends with one, and it is worth far more than an
    annoyance to skip. The vendor is stating what the file should sum to, so
    the footer becomes an independent control total - arithmetic we did not
    perform, checked against arithmetic we did. Landing it instead would have
    added $20,147,607.39 of phantom purchases to a $20,147,607.39 file,
    doubling it exactly.
    """


class SourceLifecycle(StrEnum):
    """Whether a source is expected to deliver."""

    ACTIVE = "active"
    DISCONTINUED = "discontinued"
    """The vendor stopped producing it. WeInfuse BuyBill, as of the June 2026
    reporting-module change. Historical data stays; absence is not an alert."""
    NO_ACCESS = "no_access"
    """Known to exist but not obtainable. CuraScript, Rosemont and Trinity are
    listed in the source catalogue as "no access on reporting tool"."""
    PLANNED = "planned"


class ColumnSpec(BaseModel):
    """One column: where it comes from, what it means, how it is typed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: Identifier
    """Destination column name. Stable; vendors rename, we do not."""

    sources: list[str] = Field(default_factory=list)
    """Accepted vendor headers, in preference order. Matched on a normalized
    form, so case and punctuation differences do not need listing.

    Empty only for a reader-injected column - one the file does not contain and
    the loader supplies, currently just ``sheet_name_column``. Validated at the
    source level so an ordinary column cannot be left sourceless by accident.
    """

    @property
    def is_injected(self) -> bool:
        """True when the value comes from the loader rather than from a column."""
        return not self.sources

    type: FieldType = FieldType.TEXT
    required: bool = False
    """Two things, deliberately: the column must exist in the file, and each
    row must have a value for it.

    Column-absent is drift and blocks the file per the source's policy.
    Value-absent quarantines that one row. Splitting these into two flags reads
    tidier and gets used wrongly - a field declared "required" that silently
    accepts blanks is worse than no declaration at all, and it is how a
    completely empty row lands as a purchase with null everything.
    """

    date_formats: list[str] = Field(default_factory=list)
    """strptime formats, tried in order. Required for DATE columns and rejected
    for every other type - there is no inference anywhere in this system."""

    max_length: int | None = None
    """Over-length values fail rather than truncate."""

    pii: PiiClass = PiiClass.NONE
    on_error: OnError = OnError.QUARANTINE

    control_total: bool = False
    """Whether this column is additive and worth totalling as a control figure.

    Only for extended amounts, never for rates. Summing a unit price produces a
    number with no meaning, and a meaningless control total is worse than none
    because somebody will eventually try to reconcile against it.
    """

    description: str = ""

    @model_validator(mode="after")
    def _check_type_specific_fields(self) -> Self:
        if self.type is FieldType.DATE and not self.date_formats:
            raise ValueError(
                f"column {self.target!r} is a date and must declare date_formats. "
                "Inferring date formats is how 01/02/2025 becomes 1 February in one "
                "file and 2 January in the next."
            )
        if self.type is not FieldType.DATE and self.date_formats:
            raise ValueError(f"column {self.target!r} is {self.type} and cannot have date_formats")
        if self.max_length is not None and self.type is not FieldType.TEXT:
            raise ValueError(f"column {self.target!r} is {self.type} and cannot have max_length")
        return self


class ReadSpec(BaseModel):
    """How to get a header row and data rows out of the file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet: str | None = None
    """Worksheet name, matched case-insensitively. None takes the first sheet.
    Observed names differ only by case across vendors - Results, sheet1, Sheet1."""

    sheet_pattern: str | None = None
    """Regex selecting several worksheets, all read and concatenated.

    Finance workbooks commonly carry one tab per entity with an identical
    shape. The Master COGS List has a tab per wholesaler - Cardinal, R&S,
    Mckesson, Cura, ABC - alongside its consolidated Master Sheet. Without
    this each tab would need its own near-identical spec.
    """

    sheet_name_column: str | None = None
    """Target column receiving the worksheet name each row came from.

    Required with ``sheet_pattern``, because once rows from several tabs are
    concatenated their origin is otherwise unrecoverable - and for the Master
    COGS List the origin tab is exactly what distinguishes the authoritative
    consolidated view from a per-vendor working copy.

    Also allowed with a single ``sheet``, where it lands as a constant. That
    keeps a natural key that includes the sheet name the same shape across
    variants that read one tab and variants that read many.
    """

    preamble_lines: int = Field(default=0, ge=0, le=100)
    """Lines to discard before the header. The ABC CSV opens with ``sep=,`` and
    a report title; Athena exports carry a title row."""

    encoding: str = "utf-8-sig"
    delimiter: str = ","
    skip_trailing_blank_rows: bool = True
    """Cardinal detail exports end with a fully empty row."""

    footer: FooterPolicy = FooterPolicy.NONE
    """Whether the last row is a totals line, and its figures a control check."""

    @model_validator(mode="after")
    def _check_sheet_selection(self) -> Self:
        if self.sheet is not None and self.sheet_pattern is not None:
            raise ValueError("give either sheet or sheet_pattern, not both")
        if self.sheet_pattern is not None:
            try:
                re.compile(self.sheet_pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"bad sheet_pattern: {exc}") from exc
            if self.sheet_name_column is None:
                raise ValueError(
                    "sheet_pattern requires sheet_name_column, so a row's origin tab "
                    "survives concatenation"
                )
        return self

    def selects_sheet(self, name: str) -> bool:
        """Whether this worksheet should be read."""
        if self.sheet_pattern is not None:
            return re.search(self.sheet_pattern, name, re.IGNORECASE) is not None
        if self.sheet is not None:
            return name.strip().lower() == self.sheet.strip().lower()
        return True


class VariantDetector(BaseModel):
    """How to tell which variant a file is, from its header set alone.

    Detection is by column presence rather than by filename or date, because
    filenames lie - a Cardinal file named ``01012023-…`` contains no 2023 data -
    and because a vendor can switch formats without warning.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    require_all: list[str] = Field(default_factory=list)
    """Every one of these headers must be present."""

    require_none: list[str] = Field(default_factory=list)
    """None of these may be present. This is what separates the Cardinal
    summary from the detail report: the summary is the one with no NDC."""

    @model_validator(mode="after")
    def _needs_a_condition(self) -> Self:
        if not self.require_all and not self.require_none:
            raise ValueError("a detector must require at least one column present or absent")
        return self


class SchemaVariant(BaseModel):
    """One shape a source's file can take."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Identifier
    detect: VariantDetector
    read: ReadSpec = Field(default_factory=ReadSpec)
    columns: list[ColumnSpec] = Field(min_length=1)
    action: VariantAction = VariantAction.LOAD
    superseded_by: str | None = None
    """Name of the variant that replaces this one. Required when the action is
    recognise_only, so the reason is recorded next to the decision rather than
    in a commit message."""
    description: str = ""

    @property
    def loads(self) -> bool:
        return self.action is VariantAction.LOAD

    @model_validator(mode="after")
    def _check_action(self) -> Self:
        if self.action is VariantAction.RECOGNISE_ONLY and not self.superseded_by:
            raise ValueError(
                f"variant {self.name!r} is recognise_only and must name the variant "
                "that supersedes it"
            )
        if self.action is VariantAction.LOAD and self.superseded_by:
            raise ValueError(f"variant {self.name!r} loads, so it cannot be superseded")
        return self

    @model_validator(mode="after")
    def _targets_are_unique(self) -> Self:
        seen: set[str] = set()
        for column in self.columns:
            if column.target in seen:
                raise ValueError(f"variant {self.name!r} maps {column.target!r} more than once")
            seen.add(column.target)
        return self

    def column(self, target: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.target == target), None)

    @property
    def landed_columns(self) -> list[ColumnSpec]:
        """Columns whose values are written. Excludes PROHIBITED, which is read
        for drift purposes and then discarded."""
        return [c for c in self.columns if c.pii is not PiiClass.PROHIBITED]


class SourceSpec(BaseModel):
    """Everything needed to turn one vendor's file into rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: SourceKey
    """Stable identifier, e.g. ``wholesaler.cardinal.invoice``. Never changes;
    it is what history is keyed on."""

    name: str
    vendor: str
    group: str = ""
    """The delivery folder, e.g. "1 Wholesalers"."""

    catalog_names: list[str] = Field(default_factory=list)
    """What the finance team calls this in the source catalogue. "R and S"
    means the McKesson Custom report, which nothing else would tell you."""

    lifecycle: SourceLifecycle = SourceLifecycle.ACTIVE
    filename_patterns: list[str] = Field(default_factory=list)
    """Case-insensitive regexes matched against the filename. A hint for
    routing only - the variant detector decides what a file actually is, so a
    renamed file still loads and a misnamed one does not load wrongly.
    Deliberately not anchored on extension: the same source arrives as CSV in
    one drop and XLSX in the next."""

    period_from_filename: str | None = None
    """Optional regex with named groups ``year`` and ``month`` for sources
    delivered one file per period, e.g. ``Monthly Payments 2025-06.csv``. Must
    also tolerate ``Monthly Payments 2026-07 upto 07212026.csv``."""

    variants: list[SchemaVariant] = Field(min_length=1)

    natural_key: list[Identifier] = Field(default_factory=list)
    """Target columns that identify a business row. Empty means the source has
    no known key and rows are append-only - honest, and better than the
    previous pipeline's habit of MERGE-ing on a key that was not unique."""

    constants: dict[Identifier, str] = Field(default_factory=dict)
    """Values injected into every row. One Pharmaforce spec serves Fire Mesa and
    Southington, differing only by this."""

    on_new_column: DriftPolicy = DriftPolicy.WARN
    on_missing_column: DriftPolicy = DriftPolicy.FAIL
    expected_cadence_days: int | None = None
    """Used by freshness alerting. None means irregular."""

    coverage_start: str | None = None
    """ISO date before which this source has no data, so absence is not a gap.
    ABC and Athena have no 2023 data."""

    notes: str = ""

    @model_validator(mode="after")
    def _validate(self) -> Self:
        names = [v.name for v in self.variants]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.key}: variant names must be unique, got {names}")

        for variant in self.variants:
            if variant.superseded_by and variant.superseded_by not in names:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} is superseded by "
                    f"{variant.superseded_by!r}, which does not exist"
                )
        if not any(v.loads for v in self.variants):
            raise ValueError(f"{self.key}: no variant loads anything")

        for pattern in self.filename_patterns:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"{self.key}: bad filename pattern {pattern!r}: {exc}") from exc

        if self.period_from_filename is not None:
            try:
                compiled = re.compile(self.period_from_filename, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"{self.key}: bad period pattern: {exc}") from exc
            if "year" not in compiled.groupindex or "month" not in compiled.groupindex:
                raise ValueError(
                    f"{self.key}: period_from_filename needs named groups 'year' and 'month'"
                )

        # A natural key must exist in every variant, or a file of the wrong
        # shape would land keyless and silently duplicate on re-delivery.
        for variant in self.variants:
            targets = {c.target for c in variant.columns}
            missing = [k for k in self.natural_key if k not in targets]
            if missing:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} lacks natural-key columns {missing}"
                )
            sourceless = [c.target for c in variant.columns if c.is_injected]
            allowed_sourceless = (
                {variant.read.sheet_name_column} if variant.read.sheet_name_column else set()
            )
            stray = sorted(set(sourceless) - allowed_sourceless)
            if stray:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} columns {stray} declare no "
                    "sources and are not reader-injected"
                )
            injected = variant.read.sheet_name_column
            if injected is not None and injected not in targets:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} sets sheet_name_column "
                    f"{injected!r} but does not declare it as a column"
                )
            clashes = set(self.constants) & targets
            if clashes:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} maps {sorted(clashes)}, which "
                    "constants would overwrite"
                )
        return self

    def variant(self, name: str) -> SchemaVariant | None:
        return next((v for v in self.variants if v.name == name), None)

    @property
    def is_expected_to_deliver(self) -> bool:
        """Whether silence from this source should raise a freshness alert."""
        return self.lifecycle is SourceLifecycle.ACTIVE

    def matches_filename(self, filename: str) -> bool:
        """Whether the filename hints at this source. Never the sole basis for
        deciding what a file is."""
        return any(re.search(p, filename, re.IGNORECASE) for p in self.filename_patterns)
