"""Bind every shipped spec against the real extracts, and report what happens.

Not a test - the extracts contain PHI and live outside the repository, so this
cannot run in CI. It is the evidence that the specs in ``specs/`` still describe
the files that actually arrive, and it is what to run after any vendor change.

Reads header rows only. No cell values are read, printed or written anywhere.

    python scripts/bind_real_files.py [root]

``root`` defaults to the Sagebrush ETL Files folder.

How to run
    ``python scripts/bind_real_files.py``            use SBH_SOURCE_ROOT / default
    ``python scripts/bind_real_files.py D:\\drops``   another folder

Inputs
    ``root``            optional positional path, searched recursively.
    ``SBH_SOURCE_ROOT`` environment variable. Unlike ``audit_files.py`` this
                        root is used as-is, not joined with ``Original Files``.
    ``specs/``          every YAML spec in the repository.
    No database, no credentials.

Outputs
    One block per spec on stdout. For each file whose name matches the spec's
    ``filename_patterns`` it prints which variant matched, how many of the
    variant's columns were found, how many will be landed, whether the load
    would proceed, and any drift (new or missing columns). The last line is
    either "OK - every recognised file binds and loads" or a count of files
    that were recognised but blocked. Exit code 1 if anything is blocked.

What "bind" proves
    That for every real file on disk, exactly one variant's detector matches
    its header, every header the spec names resolves to one real column, and
    nothing a required column needs is missing. That is the whole contract
    between a spec and a file, short of reading values.

What it does NOT prove
    - Natural-key uniqueness. ``docs/data-findings.md`` section 13 records
      that every declared key was probed against the full extract (for
      example 6,163 rows for McKesson Purchasing Detail), but the probe that
      did that is not in this repository. This script reads headers only, so
      a key that stops being unique in a new delivery would not be caught
      here; it would surface as a correction at ledger time.
    - Value parsing. Dates, money and NDCs are not parsed; that is what
      ``sbh_data_bank.ingest.load`` does when the commit engine runs.
    - Files that match no spec's filename pattern are not reported at all.

Where it fits
    After ``audit_files.py`` says a file's shape is sane and before any
    load. Re-run after editing any spec or after any vendor change.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.ingest.reader import (  # noqa: E402
    HeaderNotFoundError,
    open_source,
    resolve_sheet,
)
from sbh_data_bank.registry.binding import (  # noqa: E402
    AmbiguousVariantError,
    NoVariantMatchedError,
    bind,
)
from sbh_data_bank.registry.headers import AmbiguousHeaderError, HeaderIndex  # noqa: E402
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.registry.spec import SourceSpec  # noqa: E402

#: Where the vendor extracts live. They are PHI and stay outside the
#: repository, so the location is per-machine: set SBH_SOURCE_ROOT, or pass
#: a path argument. The fallback is the layout this was first written against.
DEFAULT_ROOT = Path(
    os.environ.get(
        "SBH_SOURCE_ROOT",
        r"C:\Users\SharanLokeshwar\OneDrive - Sagebrush Healthcare\Sagebrush ETL Files",
    )
)
REPO = Path(__file__).resolve().parents[1]

#: Loose files the script will try. Zips are handled by name match only.
DATA_SUFFIXES = {".csv", ".xlsx", ".xls"}


def _headers_via_reader(
    source: Path | io.BufferedIOBase, spec: SourceSpec, sheet: str | None = None
) -> list[str]:
    """Read the header the way the loader does.

    Every variant is tried, because finding the header can depend on the
    variant - ABC locates it by marker, Athena by a fixed preamble count. The
    script deliberately does not implement its own header search: it existed
    once, and it disagreed with the loader, reporting ABC workbooks as
    unrecognised while the loader read all 4,513 rows.
    """
    last: Exception | None = None
    for variant in spec.variants:
        # An in-memory stream (a zip member) has to be rewound for each
        # variant's attempt; a Path is simply re-opened by open_source.
        if isinstance(source, Path):
            handle: Path | io.BufferedIOBase = source
        else:
            source.seek(0)
            handle = source
        try:
            # open_source applies this variant's ReadSpec: sheet selection,
            # preamble count or header marker, encoding. Only the header is
            # kept; the row iterator is discarded unread.
            header, _ = open_source(handle, variant.read, sheet=sheet)
        except (HeaderNotFoundError, StopIteration, KeyError, UnicodeDecodeError) as exc:
            # This variant's read settings did not fit the file. Remember
            # the error and try the next variant.
            last = exc
            continue
        if header:
            return header
    # No variant produced a header. Re-raise the last failure so the caller
    # can report it, rather than silently treating the file as empty.
    if last is not None:
        raise last
    return []


def _sheets_for(path: Path, spec: SourceSpec) -> list[str]:
    """Every worksheet any variant of the spec would select, in order, no repeats.

    Variants can name different sheets (or a pattern), so the union is what
    needs checking. ``resolve_sheet`` applies the variant's ``sheet`` or
    ``sheet_pattern``, with case-insensitive matching because the same report
    arrives as ``Results``, ``sheet1`` and ``Sheet1``.
    """
    names: list[str] = []
    for variant in spec.variants:
        for name in resolve_sheet(path, variant.read):
            if name not in names:
                names.append(name)
    return names


def _report(label: str, spec: SourceSpec, headers: list[str]) -> str:
    """Bind one header set to the spec and render a one-line verdict.

    ``bind`` does the real work: select exactly one variant, match every
    spec column to a real header, and compute drift. Three outcomes come
    back as text rather than exceptions so the run continues past them.
    """
    try:
        # HeaderIndex normalizes the vendor names (case, punctuation,
        # whitespace) so "Days` Supply" and "Days Supply" are the same column.
        binding = bind(spec, HeaderIndex(headers))
    except NoVariantMatchedError:
        # Not necessarily wrong. The filename pattern is a hint, so a derived
        # or analysis file whose name resembles a vendor extract lands here -
        # and refusing it is the correct outcome.
        return f"    {label}: not recognised, {len(headers)} columns - refused"
    except (AmbiguousVariantError, AmbiguousHeaderError) as exc:
        return f"    {label}: {type(exc).__name__}: {exc}"

    # A binding can succeed and still be unloadable: a required column
    # missing, or a missing column under on_missing_column: fail. That is a
    # BLOCKED, and it is what the exit code counts.
    blocking = binding.blocking_problem()
    status = "loads" if blocking is None else f"BLOCKED - {blocking}"
    line = (
        f"    {label}: variant={binding.variant.name} "
        f"bound={len(binding.present)}/{len(binding.variant.columns)} "
        f"landed={len(binding.landed)} {status}"
    )
    # Drift is reported even when the file loads: a new vendor column under
    # on_new_column: warn is exactly the thing a person should go and look at.
    if binding.has_drift or binding.missing_optional:
        line += f"\n        drift: {binding.drift_report()}"
    return line


def main(root: Path) -> int:
    """For every spec, find its files under ``root`` by name and bind each one."""
    # Loading the registry validates every spec against the pydantic model,
    # so a malformed spec fails here before any file is touched.
    registry = SpecRegistry.from_dir(REPO / "specs")
    print(f"{len(registry)} specs, root {root}\n")

    problems = 0
    for spec in registry:
        print(f"{spec.key}  ({spec.name})")
        seen = 0

        # Outer loop is spec, inner loop is file, so the output groups by
        # source. The filename pattern is only a routing hint: a file that
        # matches by name but not by header is reported as refused, which is
        # the right answer for a derived or analysis file (see _report).
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue

            # Zip archives (Monthly Payments) are opened in memory and the
            # first two members are checked; the 40 members share one header.
            if path.suffix.lower() == ".zip" and spec.matches_filename(path.name):
                with zipfile.ZipFile(path) as archive:
                    members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
                    for member in members[:2]:  # shape is identical across members
                        with archive.open(member) as raw:
                            headers = _headers_via_reader(io.BytesIO(raw.read()), spec)
                        seen += 1
                        line = _report(Path(member).name, spec, headers)
                        problems += "BLOCKED" in line
                        print(line)
                    if len(members) > 2:
                        print(f"    ... and {len(members) - 2} more members, same shape")
                continue

            if path.suffix.lower() not in DATA_SUFFIXES:
                continue
            if not spec.matches_filename(path.name):
                continue

            seen += 1
            try:
                if path.suffix.lower() == ".csv":
                    line = _report(path.name, spec, _headers_via_reader(path, spec))
                    problems += "BLOCKED" in line
                    print(line)
                else:
                    # A workbook is bound once per selected worksheet.
                    for sheet_name in _sheets_for(path, spec):
                        headers = _headers_via_reader(path, spec, sheet_name)
                        line = _report(f"{path.name} [{sheet_name}]", spec, headers)
                        problems += "BLOCKED" in line
                        print(line)
            except HeaderNotFoundError as exc:
                print(f"    {path.name}: header not found - {exc}")

        # A spec with no file under root is not an error here - BuyBill is
        # discontinued and Athena has no 2023 data - but it is worth seeing.
        if seen == 0:
            print("    (no matching files found)")
        print()

    print(
        "OK - every recognised file binds and loads"
        if problems == 0
        else f"{problems} file(s) recognised but blocked"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ROOT))
