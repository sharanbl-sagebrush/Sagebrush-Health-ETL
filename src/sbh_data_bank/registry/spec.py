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

How this fits
-------------
This module is the bottom of the registry package: it imports nothing from the
rest of ``sbh_data_bank``. ``registry.loader`` turns a YAML file into a
``SourceSpec``; ``registry.binding`` and ``ingest.reader`` read the model to
decide what a file is and how to parse each cell; ``store.schema`` reads it to
emit a ``CREATE TABLE`` per source. The shape of a YAML spec is, top to bottom:

    SourceSpec                 one file in specs/
      variants: [SchemaVariant]   one per shape the file can take
        detect: VariantDetector     which headers mark this shape
        read:   ReadSpec            how to find the header and data rows
        columns: [ColumnSpec]       one per destination column

Every model is frozen (immutable once built) and forbids unknown keys, so a
misspelt key in a YAML file is a load error rather than a silently ignored
setting.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "EVENT_DATE_AS_OF",
    "RESERVED_COLUMN_NAMES",
    "AsOfSource",
    "ColumnSpec",
    "CoverageSource",
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

#: The ``event_date`` value for a snapshot source, whose rows have no date of
#: their own: eligibility, the wholesaler account register, patients. A newer
#: snapshot covers every row of an older one, so the delivery's as-of stands
#: in for the event date (decision D1, docs/decision-record.md).
EVENT_DATE_AS_OF = "as_of"

#: Target names a spec may not use, because every generated table carries a
#: column of that name for the ledger itself (``store/schema.py``, which
#: re-exports this set as ``LEDGER_COLUMN_NAMES``). Found 2026-10-21 when the
#: first migration failed: WeInfuse ships a ``Created At`` column that had
#: been mapped to ``created_at``, the audit column. A vendor stamp with such a
#: name is mapped to ``vendor_<name>`` instead.
RESERVED_COLUMN_NAMES = frozenset(
    {
        "tenant_id", "source_file_id", "line_number", "extra_cells", "spec_sha256",
        "row_hash", "batch_id", "dedup_rule", "identity_hash", "occurrence", "as_of",
        "outcome", "superseded_by", "superseded_at", "supersede_reason",
        "created_at", "created_by", "updated_at", "updated_by",
    }
)  # fmt: skip

# Two string shapes used throughout, enforced by pydantic at load time.
#
# Identifier is a snake_case name: lowercase, starts with a letter, digits and
# underscores allowed. It is used for target column names, variant names and
# constant names, all of which end up as SQL column or object names, so the
# pattern deliberately matches what is safe to put in a CREATE TABLE unquoted.
#
# SourceKey is a dotted identifier with at least two segments, for example
# ``wholesaler.cardinal.invoice``. The dots carry a convention, not a rule:
# category, vendor, report.
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", min_length=1, max_length=128)]
SourceKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=128)]


# The ``type:`` value of a column in a YAML spec. ``ingest.reader`` dispatches
# on this to pick a parser from ``transforms.values`` or ``transforms.ndc``, and
# ``store.schema`` maps it to a SQL column type.
class FieldType(StrEnum):
    """How a column's raw text becomes a typed value."""

    TEXT = "text"
    # Stored as text after trimming. Also the right choice for identifiers that
    # look numeric but carry leading zeros, such as Cardinal's "000010" line
    # number, which an integer type would silently destroy.
    INTEGER = "integer"
    # Whole numbers only; a value with a fractional part fails to parse.
    DECIMAL = "decimal"
    # Non-money numbers such as quantities, parsed to Decimal so a unit count of
    # 0.1 is exact.
    CURRENCY = "currency"
    """Money. Parsed to Decimal and never through float, and understands
    accounting parentheses."""
    # The parentheses rule matters: the old parser nulled all 227,284
    # parenthesised amounts in Monthly Payments, overstating by $39,967,197.61.
    DATE = "date"
    # Requires ``date_formats`` on the column; see the validator below.
    BOOLEAN = "boolean"
    # Vendor spellings such as True/False, Y/N, 1/0 become a bool.
    NDC = "ndc"
    """Normalized to 11-digit 5-4-2, resolved against the NDC dimension when the
    source gives an unhyphenated 10-digit code."""
    # Not a left-pad: zfill(11) was wrong on 54.5% of distinct NDCs because it
    # implements only the FDA 4-4-2 rule. Ambiguous codes quarantine instead.


# The ``pii:`` value of a column. It never changes how a value is parsed, only
# whether and how it is stored. The reader uses PROHIBITED to drop a column
# before any value is kept; the other classes are a storage-policy label for
# the warehouse to act on.
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
    # Also SSN in Curve Monthly Services (data finding 14). Matching the column
    # and then discarding it is deliberate: if the spec simply omitted it, the
    # column would show up as "new column" drift on every delivery.


# The ``on_error:`` value of a column: what one unparseable cell does to its row.
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


# The ``on_new_column:`` and ``on_missing_column:`` values at the top of a
# spec. Applied by ``Binding.blocking_problem`` in ``registry.binding`` after
# the file's headers have been compared with the spec's columns.
class DriftPolicy(StrEnum):
    """What happens when the vendor's column set changes."""

    FAIL = "fail"
    """Stop. Nothing lands until a human has looked."""

    QUARANTINE_FILE = "quarantine_file"
    """Land nothing, keep the file, raise an exception for triage."""

    WARN = "warn"
    """Load anyway and record the drift. Appropriate only where a vendor adds
    optional columns routinely."""
    # Note that only WARN lets the file through. FAIL and QUARANTINE_FILE both
    # block it today; the difference between them is intent recorded for the
    # commit engine, which is not yet built.


# The ``action:`` value of a variant. Almost always the default, ``load``.
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

    The same action also hands a shape to another source. Since 2026-10-22
    the Cardinal summary is loaded by its own spec, so the detail spec
    recognises the summary with ``loaded_by`` naming that source, and the
    summary spec recognises the detail the same way. Nothing is dropped and
    nothing is loaded twice.
    """


# The ``read.footer:`` value of a variant. Consumed by ``ingest.load``, which
# detects the totals row, keeps it out of the data, and compares its figures
# with the sums of every ``control_total`` column.
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
    # The McKesson Purchasing Detail report has one too (file audit, finding
    # 1): its footer declares Extended Price $20,394,456.72 on line 6165.


# The ``lifecycle:`` value at the top of a spec. Only ACTIVE sources are
# expected to deliver, so only they can raise a "nothing received" alert.
class SourceLifecycle(StrEnum):
    """Whether a source is expected to deliver."""

    ACTIVE = "active"
    # Delivering now; silence past ``expected_cadence_days`` is an alert.
    DISCONTINUED = "discontinued"
    """The vendor stopped producing it. WeInfuse BuyBill, as of the June 2026
    reporting-module change. Historical data stays; absence is not an alert."""
    NO_ACCESS = "no_access"
    """Known to exist but not obtainable. CuraScript, Rosemont and Trinity are
    listed in the source catalogue as "no access on reporting tool"."""
    PLANNED = "planned"
    # Expected in future; no spec columns need to be real yet.


# The ``coverage:`` value at the top of a spec: where the date window a
# delivery claims to cover comes from. The vanish rule retires a current row
# only when a newer delivery covers the row's date and no longer carries it,
# so the window has to be stated, not assumed (decision D2).
class CoverageSource(StrEnum):
    """Where a delivery's covered date window comes from."""

    FILENAME = "filename"
    """The date range in the filename. Right for most sources; every real
    file carries one. Cardinal's window ends one day before its as-of and
    WeInfuse's starts one day early, which the offset fields express."""

    DATA = "data"
    """The earliest and latest value of the coverage column in the typed
    rows. For a vendor whose filename window is unreliable: Wellpartner's
    D3 invoices file claims January but holds only March."""

    AS_OF = "as_of"
    """Everything up to and including the as-of date. For snapshot sources,
    where a newer delivery replaces every row of an older one."""


# The ``as_of_source:`` value at the top of a spec. Normally absent: the
# as-of of a delivery is resolved the same way for every source (decision D5;
# ``curate.delivery.resolve_as_of``). Set only to override that chain for the
# backlog files that have no dated upload folder.
class AsOfSource(StrEnum):
    """Where a delivery's as-of date (vendor extraction date) came from."""

    FOLDER = "folder"
    """The upload folder is named for the Sunday it was uploaded. The rule
    going forward, for every source; never set on a spec, since it is tried
    first whenever the file sits in such a folder."""

    XLSX_CREATED = "xlsx_created"
    """The workbook's internal creation stamp, written by the vendor portal."""

    XLSX_MODIFIED = "xlsx_modified"
    """The workbook's internal modified stamp. For a hand-edited register
    whose creation stamp never moves: the Master COGS List."""

    FILENAME = "filename"
    """The end of the date range in the filename. The only option for CSV,
    and the right one where the xlsx stamp is an Excel re-save (ABC)."""

    OPERATOR = "operator"
    """A date the operator supplies. The Allscripts June 2026 file."""


# One entry under ``columns:`` in a variant. The reader walks these in order
# for every data row: find the cell at the bound position, parse it per
# ``type``, apply ``required`` and ``on_error``, and write it to ``target``.
class ColumnSpec(BaseModel):
    """One column: where it comes from, what it means, how it is typed."""

    # frozen: a spec cannot be mutated after loading, so two readers of the
    # same spec cannot disagree. extra="forbid": an unknown key in the YAML is
    # an error, not an ignored setting.
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
        # No vendor header means nothing to match; the value is supplied by the
        # reader (today: the worksheet name). SourceSpec._validate makes sure
        # this is only ever true for the declared sheet_name_column.
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

    # Rejects a column whose type and type-specific settings disagree. Runs
    # after pydantic has typed every field, so ``self.type`` is already an
    # enum member here.
    @model_validator(mode="after")
    def _check_type_specific_fields(self) -> Self:
        # A date column with no formats would fall back to inference somewhere,
        # and inference is the defect: Cardinal ships ISO in one pull and US in
        # the next, so 01/02/2025 has no single right answer.
        if self.type is FieldType.DATE and not self.date_formats:
            raise ValueError(
                f"column {self.target!r} is a date and must declare date_formats. "
                "Inferring date formats is how 01/02/2025 becomes 1 February in one "
                "file and 2 January in the next."
            )
        # The reverse: formats on a non-date column would be silently ignored,
        # which usually means the author forgot to set the type.
        if self.type is not FieldType.DATE and self.date_formats:
            raise ValueError(f"column {self.target!r} is {self.type} and cannot have date_formats")
        # max_length only means something for text; on a number it would be
        # a no-op that looks like a constraint.
        if self.max_length is not None and self.type is not FieldType.TEXT:
            raise ValueError(f"column {self.target!r} is {self.type} and cannot have max_length")
        return self


# The ``read:`` block of a variant. Everything here is about locating the
# header row and the data rows inside the physical file; nothing here is about
# what the columns mean. ``ingest.reader`` consumes it.
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
    """Fixed number of lines to discard before the header.

    Use when the count is genuinely stable. Athena exports carry exactly one
    title row in every delivery, so a fixed count is honest there.
    """

    header_marker: str | None = None
    """A column name that identifies the header row, scanned for from the top.

    Preferred over ``preamble_lines`` wherever the preamble is not stable, and
    it is not stable more often than one would like. ABC delivers the same 26
    columns as a workbook with one title row above the header, and as a CSV
    with a ``sep=,`` declaration *and* a title - two lines. A single fixed count
    cannot serve both, and two variants cannot be told apart because their
    headers are identical, so the count has to stop being the thing that
    locates the header.

    This stays declarative. It says "the header is the first row containing a
    column called X", which is a statement about the file's structure that a
    human wrote down - not a sniffer guessing where the data starts. If the
    marker is absent from the first ``max_preamble_scan`` rows the read fails
    loudly rather than picking a row.
    """

    max_preamble_scan: int = Field(default=30, ge=1, le=200)
    """How far to look for ``header_marker`` before giving up."""

    # utf-8-sig strips a UTF-8 byte-order mark if one is present and is a
    # plain utf-8 read otherwise. Allscripts eRx ships with a BOM (file
    # audit); without this the mark would be glued to the first header and
    # that column would never match.
    encoding: str = "utf-8-sig"
    delimiter: str = ","
    skip_trailing_blank_rows: bool = True
    """Cardinal detail exports end with a fully empty row."""

    footer: FooterPolicy = FooterPolicy.NONE
    """Whether the last row is a totals line, and its figures a control check."""

    # Rejects a read block that locates the header two different ways.
    @model_validator(mode="after")
    def _check_header_location(self) -> Self:
        # preamble_lines of 0 is the default and means "header is row 0", so it
        # is compatible with a marker; any positive count is a second answer.
        if self.header_marker is not None and self.preamble_lines:
            raise ValueError(
                "give either preamble_lines or header_marker, not both - two answers to "
                "'where is the header' is one too many"
            )
        return self

    # Rejects an inconsistent worksheet selection.
    @model_validator(mode="after")
    def _check_sheet_selection(self) -> Self:
        # One tab or many tabs, not both.
        if self.sheet is not None and self.sheet_pattern is not None:
            raise ValueError("give either sheet or sheet_pattern, not both")
        if self.sheet_pattern is not None:
            # Compile now so a bad regex fails at spec load, not on the first
            # workbook that arrives at 2am.
            try:
                re.compile(self.sheet_pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"bad sheet_pattern: {exc}") from exc
            # Rows from several tabs are concatenated, so each must carry its
            # origin or the Master Sheet and a vendor tab become identical.
            if self.sheet_name_column is None:
                raise ValueError(
                    "sheet_pattern requires sheet_name_column, so a row's origin tab "
                    "survives concatenation"
                )
        return self

    def selects_sheet(self, name: str) -> bool:
        """Whether this worksheet should be read."""
        # Precedence: a pattern wins, then an exact (case-folded) name, and with
        # neither set every sheet is selected - the reader takes the first.
        if self.sheet_pattern is not None:
            return re.search(self.sheet_pattern, name, re.IGNORECASE) is not None
        if self.sheet is not None:
            return name.strip().lower() == self.sheet.strip().lower()
        return True


# The ``detect:`` block of a variant: a fingerprint of column names. The
# binder tests every variant's detector against the file's header set and
# requires exactly one to pass. Names here are vendor headers as shipped,
# compared after the same normalisation ``registry.headers`` applies.
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

    # Rejects an empty detector, which would match every file and make the
    # variant ambiguous with all its siblings.
    @model_validator(mode="after")
    def _needs_a_condition(self) -> Self:
        if not self.require_all and not self.require_none:
            raise ValueError("a detector must require at least one column present or absent")
        return self


# One entry under ``variants:`` in a spec. A source has one variant unless
# the vendor has shipped more than one layout, in which case each layout gets
# its own detector, read settings and column list, and all of them land on
# the same target names so nothing downstream can tell which layout a row
# came from.
class SchemaVariant(BaseModel):
    """One shape a source's file can take."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Identifier
    detect: VariantDetector
    read: ReadSpec = Field(default_factory=ReadSpec)
    columns: list[ColumnSpec] = Field(min_length=1)
    action: VariantAction = VariantAction.LOAD
    superseded_by: str | None = None
    """Name of the variant that replaces this one. One of this and
    ``loaded_by`` is required when the action is recognise_only, so the
    reason is recorded next to the decision rather than in a commit message."""
    loaded_by: SourceKey | None = None
    """Key of the source whose spec loads this shape. The other way a
    recognise_only variant states its reason: the file is somebody else's.
    The registry checks the key exists."""
    description: str = ""

    @property
    def loads(self) -> bool:
        # Convenience for "will rows from this variant be written".
        return self.action is VariantAction.LOAD

    # Rejects a mismatch between ``action`` and its reason fields: the three
    # describe one decision and must agree.
    @model_validator(mode="after")
    def _check_action(self) -> Self:
        reasons = [r for r in (self.superseded_by, self.loaded_by) if r]
        # Not loading needs exactly one stated reason: the sibling variant
        # that covers the same data, or the source that loads this shape.
        if self.action is VariantAction.RECOGNISE_ONLY and len(reasons) != 1:
            raise ValueError(
                f"variant {self.name!r} is recognise_only and must name either the variant "
                "that supersedes it (superseded_by) or the source that loads it (loaded_by)"
            )
        # A loading variant that also claims to be superseded, or to belong to
        # another source, is contradictory.
        if self.action is VariantAction.LOAD and reasons:
            raise ValueError(
                f"variant {self.name!r} loads, so it cannot be superseded or loaded elsewhere"
            )
        return self

    # Rejects two columns writing to the same destination, which would make
    # the later one silently overwrite the earlier.
    @model_validator(mode="after")
    def _targets_are_unique(self) -> Self:
        seen: set[str] = set()
        for column in self.columns:
            if column.target in seen:
                raise ValueError(f"variant {self.name!r} maps {column.target!r} more than once")
            seen.add(column.target)
        return self

    def column(self, target: str) -> ColumnSpec | None:
        # Lookup by destination name; None when this variant has no such column.
        return next((c for c in self.columns if c.target == target), None)

    @property
    def landed_columns(self) -> list[ColumnSpec]:
        """Columns whose values are written. Excludes PROHIBITED, which is read
        for drift purposes and then discarded."""
        return [c for c in self.columns if c.pii is not PiiClass.PROHIBITED]


# The whole YAML file. Top-level keys are identity and policy for the source
# as a whole; everything about a particular file layout lives in ``variants``.
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
    """Target columns that identify a business row.

    Empty is allowed and sometimes correct. Several of these feeds are
    denormalised report extracts rather than transaction logs - Athena
    Prescriptions repeats one order once per ICD-10 diagnosis, so no subset of
    its columns identifies a row - and claiming a key that is not unique is how
    the previous pipeline came to MERGE four tables on keys that collided.

    An empty key is not a shrug, though: it changes row identity to the row hash
    and it has to be justified, so ``notes`` becomes mandatory and must record
    what was tested and what the residual duplicate count was.
    """
    # The key decides the dedup mode in store.schema: same key and same row
    # hash on re-delivery is a skip, same key and a different hash is a
    # correction that supersedes the earlier row. Fourteen of nineteen sources
    # have one; the five without are listed in data finding 13.

    constants: dict[Identifier, str] = Field(default_factory=dict)
    """Values injected into every row. One Pharmaforce spec serves Fire Mesa and
    Southington, differing only by this."""

    on_new_column: DriftPolicy = DriftPolicy.WARN
    on_missing_column: DriftPolicy = DriftPolicy.FAIL
    # Asymmetric defaults on purpose: a vendor adding a column is usually
    # harmless and worth a warning, while a required column going missing
    # means the file is not what the spec describes.

    max_quarantine_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    """The fraction of a file's rows that may quarantine before the engine
    holds the whole file instead of loading what parsed (D16). A wrong date
    format or a column that changed type quarantines every row; loading that
    file commits its hash with nothing behind it, and the corrected spec can
    never load it again. Every source loaded so far quarantined nothing; 5%
    is the line between a few bad rows and a file that is not what the spec
    describes."""
    expected_cadence_days: int | None = None
    """Used by freshness alerting. None means irregular."""
    # A staleness threshold ("alert if nothing seen in N days"), not a
    # schedule; files arrive on Sundays with ad hoc midweek re-uploads. See
    # CLAUDE.md, "How files arrive". Nothing consumes it yet.

    coverage_start: str | None = None
    """ISO date before which this source has no data, so absence is not a gap.
    ABC and Athena have no 2023 data."""

    # --- Delivery semantics: decisions D1 to D6 (docs/decision-record.md) ---
    # Everything below describes how deliveries of this source relate to each
    # other in time. None of it changes how a file is read or typed; all of
    # it is consumed typed-to-curated (``curate.rule``), so a wrong value is a
    # re-run in the database, never a re-read of files.

    event_date: Identifier | None = None
    """The target column that says when the business event happened: invoice
    date, fill date, appointment time. ``as_of`` (:data:`EVENT_DATE_AS_OF`)
    for a snapshot source whose rows carry no date of their own.

    Used by the as-of floor (a delivery may not be extracted before its own
    data), by coverage, and by the vanish outcome. A spec without one cannot
    vanish rows, because nothing says which dates a newer delivery covers.
    """

    coverage: CoverageSource = CoverageSource.FILENAME
    """Where the date window a delivery covers comes from. See
    :class:`CoverageSource`."""

    coverage_column: Identifier | None = None
    """The column the vendor's own report filter runs on, when it is not the
    event date. Wellpartner claims are filtered on processed date while the
    event is the dispensed date, which precedes the window on hundreds of
    rows per file. The coverage window is compared against this column."""

    coverage_start_offset_days: int = 0
    coverage_end_offset_days: int = 0
    """Days added to the window's start and end after it is resolved. Cardinal
    excludes the extraction day (end offset -1); WeInfuse includes the whole
    day before the filename start (start offset -1)."""

    vanish: bool | None = None
    """Whether a current row that a newer, covering delivery no longer carries
    is superseded with reason ``vanished``. ``None`` (the default) means yes
    when an event date is declared and no otherwise; ``false`` switches it off
    for a source that has one but must not retire rows (patients, Pharmaforce
    until its report-scope question is answered). See :attr:`vanishes`."""

    vanish_scope: list[Identifier] = Field(default_factory=list)
    """Columns naming the slice a delivery covers, beyond its date window. A
    McKesson extract for one account must never retire another account's rows:
    the June 2026 file carries two accounts absent from earlier extracts for
    the same dates. Empty means the window alone defines the slice."""

    row_hash_exclude: list[Identifier] = Field(default_factory=list)
    """Columns stored but left out of the row hash, because the vendor
    recomputes them at extraction time: reference prices, balances, referral
    flags. A change in one is not a correction to the business row. Never a
    key column, the event date or a control total."""

    as_of_source: AsOfSource | None = None
    """Overrides where the as-of comes from, for backlog files only. ``None``
    uses the standard chain: upload-folder date, xlsx creation stamp, filename
    end date, operator-supplied date. See :class:`AsOfSource`."""

    notes: str = ""

    # The cross-field checks that no single nested model can make, because
    # they relate the top-level settings to every variant. Each block rejects
    # one specific authoring mistake; the error names the spec and variant.
    @model_validator(mode="after")
    def _validate(self) -> Self:
        # Variant names are how superseded_by refers to a sibling, so they
        # must be unique within the spec.
        names = [v.name for v in self.variants]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.key}: variant names must be unique, got {names}")

        # A superseded_by must point at a real sibling.
        for variant in self.variants:
            if variant.superseded_by and variant.superseded_by not in names:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} is superseded by "
                    f"{variant.superseded_by!r}, which does not exist"
                )
        # A shape handed to another source must not be handed to this one.
        for variant in self.variants:
            if variant.loaded_by == self.key:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} says it is loaded by this very "
                    "source; use action: load or name another source"
                )
        # A spec made only of recognise_only variants would never land a row.
        if not any(v.loads for v in self.variants):
            raise ValueError(f"{self.key}: no variant loads anything")

        # Compile every filename regex now, so a typo fails at startup rather
        # than on the first file routed through it.
        for pattern in self.filename_patterns:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"{self.key}: bad filename pattern {pattern!r}: {exc}") from exc

        # Same for the period regex, which additionally must capture the two
        # named groups the loader reads out of it.
        if self.period_from_filename is not None:
            try:
                compiled = re.compile(self.period_from_filename, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"{self.key}: bad period pattern: {exc}") from exc
            if "year" not in compiled.groupindex or "month" not in compiled.groupindex:
                raise ValueError(
                    f"{self.key}: period_from_filename needs named groups 'year' and 'month'"
                )

        # No key is a finding about the data and must read like one. This is
        # the rule from CLAUDE.md: "a spec with no natural key must justify
        # itself in notes".
        if not self.natural_key and not self.notes.strip():
            raise ValueError(
                f"{self.key}: declares no natural key, so notes must record which "
                "candidates were tested and why none is unique. A keyless source "
                "is a finding about the data, not an omission."
            )

        # A natural key must exist in every variant, or a file of the wrong
        # shape would land keyless and silently duplicate on re-delivery.
        for variant in self.variants:
            targets = {c.target for c in variant.columns}
            # Every generated table carries the ledger columns, so a vendor
            # column with one of their names would make CREATE TABLE fail
            # (or, worse, be shadowed by the audit value).
            reserved = sorted(targets & RESERVED_COLUMN_NAMES)
            if reserved:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} maps {reserved}, which are "
                    "ledger column names on every generated table; map a vendor "
                    "column of that name to vendor_<name>"
                )
            missing = [k for k in self.natural_key if k not in targets]
            if missing:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} lacks natural-key columns {missing}"
                )
            # A column with no sources is only legitimate when the reader
            # injects it, and the only injected column today is the worksheet
            # name. Anything else sourceless is an authoring slip that would
            # otherwise land as a permanently null column.
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
            # The reverse: naming a sheet_name_column that is not declared
            # would inject a value with nowhere to go.
            injected = variant.read.sheet_name_column
            if injected is not None and injected not in targets:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} sets sheet_name_column "
                    f"{injected!r} but does not declare it as a column"
                )
            # Constants are written after the columns, so a constant named like
            # a column would silently replace the vendor's value on every row.
            clashes = set(self.constants) & targets
            if clashes:
                raise ValueError(
                    f"{self.key}: variant {variant.name!r} maps {sorted(clashes)}, which "
                    "constants would overwrite"
                )
        self._validate_delivery_semantics()
        return self

    def _date_column_in_every_variant(self, target: str, role: str) -> None:
        """Reject ``target`` unless every loading variant types it as a date.

        The event date and the coverage column are compared with as-of dates
        and coverage windows. A text column would compare as strings and a
        missing one would make every row look undated, so both are errors at
        spec load rather than surprises at curation.
        """
        for variant in self.variants:
            if not variant.loads:
                continue
            column = variant.column(target)
            if column is None:
                raise ValueError(
                    f"{self.key}: {role} {target!r} is not a column of variant {variant.name!r}"
                )
            if column.type is not FieldType.DATE:
                raise ValueError(
                    f"{self.key}: {role} {target!r} is typed {column.type} in variant "
                    f"{variant.name!r}; it must be a date"
                )

    def _validate_delivery_semantics(self) -> None:
        """The D1 to D6 fields must agree with each other and with the columns."""
        loading = [v for v in self.variants if v.loads]
        if self.event_date is not None and not self.is_snapshot:
            self._date_column_in_every_variant(self.event_date, "event_date")
        if self.coverage_column is not None:
            if self.event_date is None:
                raise ValueError(f"{self.key}: coverage_column needs an event_date")
            self._date_column_in_every_variant(self.coverage_column, "coverage_column")
        # A snapshot has no per-row dates, so the only window it can claim is
        # "everything up to the as-of". Any other coverage is a contradiction.
        if self.is_snapshot and self.coverage is not CoverageSource.AS_OF:
            raise ValueError(
                f"{self.key}: event_date is {EVENT_DATE_AS_OF!r} (a snapshot), so coverage "
                f"must be {CoverageSource.AS_OF.value!r}, not {self.coverage.value!r}"
            )
        # Vanish needs to know which dates a delivery covers, and only the
        # event date says that.
        if self.vanish and self.event_date is None:
            raise ValueError(
                f"{self.key}: vanish is true but no event_date is declared, so nothing "
                "says which dates a newer delivery covers"
            )
        if self.vanish_scope and not self.vanishes:
            raise ValueError(f"{self.key}: vanish_scope is set but this source never vanishes")
        for target in self.vanish_scope:
            for variant in loading:
                if variant.column(target) is None:
                    raise ValueError(
                        f"{self.key}: vanish_scope column {target!r} is not a column of "
                        f"variant {variant.name!r}"
                    )
        # Excluding a column from the hash says "a change here is not a
        # correction". That can never be true of the identity, the event date
        # or a control total, and a prohibited column has no value to exclude.
        protected = {*self.natural_key, self.event_date, self.coverage_column}
        for target in self.row_hash_exclude:
            if target in protected:
                raise ValueError(
                    f"{self.key}: row_hash_exclude names {target!r}, which is a key, "
                    "event-date or coverage column and must stay in the hash"
                )
            columns = [c for v in loading if (c := v.column(target)) is not None]
            if not columns:
                raise ValueError(
                    f"{self.key}: row_hash_exclude names {target!r}, which no loading "
                    "variant declares"
                )
            if any(c.control_total for c in columns):
                raise ValueError(
                    f"{self.key}: row_hash_exclude names {target!r}, a control total; a "
                    "change in money or quantity is always a correction"
                )
            if any(c.pii is PiiClass.PROHIBITED for c in columns):
                raise ValueError(
                    f"{self.key}: row_hash_exclude names {target!r}, which is prohibited "
                    "and never hashed anyway"
                )

    def variant(self, name: str) -> SchemaVariant | None:
        # Lookup by variant name; None when the spec has no such variant.
        return next((v for v in self.variants if v.name == name), None)

    @property
    def is_expected_to_deliver(self) -> bool:
        """Whether silence from this source should raise a freshness alert."""
        return self.lifecycle is SourceLifecycle.ACTIVE

    def matches_filename(self, filename: str) -> bool:
        """Whether the filename hints at this source. Never the sole basis for
        deciding what a file is."""
        # An unanchored search, so "cardinal.*invoice" matches anywhere in the
        # name. A spec with no patterns matches nothing here and relies
        # entirely on header detection. All nineteen shipped specs declare at
        # least one pattern, but only as a routing hint.
        return any(re.search(p, filename, re.IGNORECASE) for p in self.filename_patterns)

    # --- Derived delivery semantics ----------------------------------------

    @property
    def is_snapshot(self) -> bool:
        """Whether rows carry no event date of their own (``event_date: as_of``)."""
        return self.event_date == EVENT_DATE_AS_OF

    @property
    def event_date_column(self) -> str | None:
        """The event-date target column, or ``None`` for a snapshot or an
        undeclared event date."""
        return None if self.event_date is None or self.is_snapshot else self.event_date

    @property
    def coverage_target(self) -> str | None:
        """The column a delivery's coverage window is compared against: the
        coverage column when the vendor filters on one, else the event date."""
        return self.coverage_column or self.event_date_column

    @property
    def vanishes(self) -> bool:
        """Whether the vanish outcome applies to this source.

        Explicit ``vanish`` wins; otherwise a source vanishes exactly when it
        declares an event date, because that is what makes coverage knowable.
        """
        if self.vanish is not None:
            return self.vanish
        return self.event_date is not None

    @property
    def landed_targets(self) -> tuple[str, ...]:
        """Every column that is written, across every loading variant.

        The widest loading variant's columns in its order, then anything only
        a narrower variant has. Prohibited columns are absent, as in every
        layer. One list serves the DDL (``store.schema``) and the row hash
        (:attr:`row_hash_columns`), so the two cannot disagree on what a row
        is made of.
        """
        loading = [v for v in self.variants if v.loads]
        widest = max(loading, key=lambda v: len(v.columns))
        order: list[str] = [c.target for c in widest.landed_columns]
        for variant in loading:
            order += [c.target for c in variant.landed_columns if c.target not in order]
        return tuple(order)

    @property
    def row_hash_columns(self) -> tuple[str, ...]:
        """The columns the row hash runs over, in a fixed order.

        The union of every loading variant's landed columns (decision D6),
        nulls included, so a column one variant lacks does not make every row
        from the other variant look corrected; minus ``row_hash_exclude``.
        """
        excluded = set(self.row_hash_exclude)
        return tuple(t for t in self.landed_targets if t not in excluded)
