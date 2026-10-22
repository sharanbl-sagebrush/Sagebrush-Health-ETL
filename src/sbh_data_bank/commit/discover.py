"""Finding a source's deliveries on disk, in the order the engine should
commit them.

The filename is a routing hint only: ``discover`` lists every file under a
root whose name a spec's patterns accept, plus every CSV member of a zip
archive whose own name they accept (Monthly Payments ships forty months that
way). The binder decides what a file really is when it is loaded, so a file
the pattern finds but the spec cannot bind is still returned and the engine
records the attempt.

``in_as_of_order`` resolves each delivery's as-of by the D5 chain and sorts
by it, so a backfill replays history in the order the vendor produced it
and a late file is recognised as late rather than treated as current. A
delivery with no as-of sorts last; the engine will refuse it with
``AS_OF_UNAVAILABLE`` and the refusal is on the record.

Folders that hold things other than vendor deliveries (the consultant's
copies, the hand-cut test subsets, the validation pack) are skipped by name.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sbh_data_bank.curate.delivery import AsOf, AsOfUnavailableError, resolve_as_of
from sbh_data_bank.registry.spec import SourceSpec

__all__ = ["DATA_SUFFIXES", "DEFAULT_SKIP_DIRS", "Found", "discover", "in_as_of_order"]

#: Folders under a root that are never vendor deliveries, matched on any
#: path component.
DEFAULT_SKIP_DIRS: frozenset[str] = frozenset(
    {"Azure App ETL Files - Consultant", "Sagebrush ETL Test Files", "DB_Validation_Pack"}
)

#: What the loader can read directly. A zip is opened only when its own
#: name matches the spec.
DATA_SUFFIXES: frozenset[str] = frozenset({".csv", ".xlsx", ".xlsm", ".xls"})


@dataclass(frozen=True, slots=True)
class Found:
    """One deliverable the engine can commit: a file, or one member of a zip."""

    path: Path
    member: str | None = None

    @property
    def filename(self) -> str:
        """The name the as-of chain and the binder see."""
        return Path(self.member).name if self.member else self.path.name

    @property
    def display(self) -> str:
        return f"{self.path.name}::{self.member}" if self.member else self.path.name


def discover(
    spec: SourceSpec, root: Path, *, skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS
) -> list[Found]:
    """Every file under ``root`` the spec's filename patterns accept, in path
    order. Zip archives whose stem matches contribute each CSV member."""
    found: list[Found] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in skip_dirs for part in path.parts):
            continue
        suffix = path.suffix.lower()
        if suffix == ".zip":
            if spec.matches_filename(path.stem):
                with zipfile.ZipFile(path) as archive:
                    found.extend(
                        Found(path, member)
                        for member in sorted(archive.namelist())
                        if member.lower().endswith(".csv")
                    )
            continue
        if suffix in DATA_SUFFIXES and spec.matches_filename(path.name):
            found.append(Found(path))
    return found


def in_as_of_order(
    spec: SourceSpec,
    found: list[Found],
    root: Path,
    *,
    operator_date: date | None = None,
) -> list[tuple[AsOf | None, Found]]:
    """Pair each delivery with its as-of and sort oldest first; a delivery
    with no as-of sorts last, so everything loadable goes before anything
    the engine will refuse."""
    dated: list[tuple[AsOf | None, Found]] = []
    for item in found:
        try:
            as_of: AsOf | None = resolve_as_of(
                item.path, spec, filename=item.filename, operator_date=operator_date, root=root
            )
        except AsOfUnavailableError:
            as_of = None
        dated.append((as_of, item))
    dated.sort(
        key=lambda pair: (
            pair[0].value if pair[0] is not None else date.max,
            str(pair[1].path),
            pair[1].member or "",
        )
    )
    return dated
