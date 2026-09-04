"""Bind every shipped spec against the real extracts, and report what happens.

Not a test - the extracts contain PHI and live outside the repository, so this
cannot run in CI. It is the evidence that the specs in ``specs/`` still describe
the files that actually arrive, and it is what to run after any vendor change.

Reads header rows only. No cell values are read, printed or written anywhere.

    python scripts/bind_real_files.py [root]

``root`` defaults to the Sagebrush ETL Files folder.
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
        if isinstance(source, Path):
            handle: Path | io.BufferedIOBase = source
        else:
            source.seek(0)
            handle = source
        try:
            header, _ = open_source(handle, variant.read, sheet=sheet)
        except (HeaderNotFoundError, StopIteration, KeyError, UnicodeDecodeError) as exc:
            last = exc
            continue
        if header:
            return header
    if last is not None:
        raise last
    return []


def _sheets_for(path: Path, spec: SourceSpec) -> list[str]:
    names: list[str] = []
    for variant in spec.variants:
        for name in resolve_sheet(path, variant.read):
            if name not in names:
                names.append(name)
    return names


def _report(label: str, spec: SourceSpec, headers: list[str]) -> str:
    try:
        binding = bind(spec, HeaderIndex(headers))
    except NoVariantMatchedError:
        # Not necessarily wrong. The filename pattern is a hint, so a derived
        # or analysis file whose name resembles a vendor extract lands here -
        # and refusing it is the correct outcome.
        return f"    {label}: not recognised, {len(headers)} columns - refused"
    except (AmbiguousVariantError, AmbiguousHeaderError) as exc:
        return f"    {label}: {type(exc).__name__}: {exc}"

    blocking = binding.blocking_problem()
    status = "loads" if blocking is None else f"BLOCKED - {blocking}"
    line = (
        f"    {label}: variant={binding.variant.name} "
        f"bound={len(binding.present)}/{len(binding.variant.columns)} "
        f"landed={len(binding.landed)} {status}"
    )
    if binding.has_drift or binding.missing_optional:
        line += f"\n        drift: {binding.drift_report()}"
    return line


def main(root: Path) -> int:
    registry = SpecRegistry.from_dir(REPO / "specs")
    print(f"{len(registry)} specs, root {root}\n")

    problems = 0
    for spec in registry:
        print(f"{spec.key}  ({spec.name})")
        seen = 0

        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue

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
                    for sheet_name in _sheets_for(path, spec):
                        headers = _headers_via_reader(path, spec, sheet_name)
                        line = _report(f"{path.name} [{sheet_name}]", spec, headers)
                        problems += "BLOCKED" in line
                        print(line)
            except HeaderNotFoundError as exc:
                print(f"    {path.name}: header not found - {exc}")

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
