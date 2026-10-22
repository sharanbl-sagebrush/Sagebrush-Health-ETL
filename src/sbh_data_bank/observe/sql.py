"""The metadata queries behind the freshness check.

Reads ``meta.source_file``, ``meta.batch``, ``meta.run`` and
``stg.quarantine`` through anything with ``Database.execute``'s shape and
builds the :class:`~sbh_data_bank.observe.freshness.SourceStatus` and
:class:`~sbh_data_bank.observe.freshness.RunStatus` inputs that
:func:`~sbh_data_bank.observe.freshness.assess` judges. Counts, dates,
outcomes and filenames only; no layered table is touched.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from sbh_data_bank.observe.freshness import BENIGN_OUTCOMES, RunStatus, SourceStatus

__all__ = ["collect"]


class _Queryable(Protocol):
    def execute(self, sql: str, *params: Any) -> list[tuple]: ...


#: Newest as-of and file count over the files with a LOADED batch.
LOADED_BY_SOURCE = (
    "SELECT sf.source_key, MAX(sf.as_of), COUNT(*) FROM meta.source_file AS sf "
    "WHERE sf.tenant_id = ? AND EXISTS (SELECT 1 FROM meta.batch AS b "
    "WHERE b.source_file_id = sf.source_file_id AND b.outcome = 'LOADED') "
    "GROUP BY sf.source_key"
)
#: When the pipeline last registered any file, loaded or not.
SEEN_BY_SOURCE = (
    "SELECT source_key, MAX(first_seen_at) FROM meta.source_file "
    "WHERE tenant_id = ? GROUP BY source_key"
)
#: Files whose latest attempt is neither a load nor benign. The subquery
#: picks each file's most recent batch; a file refused once and loaded
#: later is closed, a file refused on every attempt is open.
OPEN_REFUSALS = (
    "SELECT sf.source_key, sf.file_name, sf.sheet_name, b.outcome, b.started_at "
    "FROM meta.source_file AS sf JOIN meta.batch AS b ON b.source_file_id = sf.source_file_id "
    "WHERE sf.tenant_id = ? AND b.started_at = (SELECT MAX(b2.started_at) FROM meta.batch AS b2 "
    "WHERE b2.source_file_id = sf.source_file_id) AND b.outcome NOT IN ({benign}) "
    "ORDER BY sf.source_key, b.started_at"
).format(benign=", ".join(f"'{o}'" for o in sorted(BENIGN_OUTCOMES)))
OPEN_QUARANTINE = (
    "SELECT source_key, COUNT(*) FROM stg.quarantine "
    "WHERE tenant_id = ? AND resolved_at IS NULL GROUP BY source_key"
)
#: Runs inside the window, plus any run still open whenever it started.
RUNS = (
    "SELECT run_id, trigger_kind, started_at, finished_at, outcome, detail FROM meta.run "
    "WHERE tenant_id = ? AND (started_at >= ? OR outcome IS NULL) ORDER BY started_at"
)
#: Which sources each run in the window reached, from its batches.
RUN_SOURCES = (
    "SELECT b.run_id, b.source_key FROM meta.batch AS b JOIN meta.run AS r ON r.run_id = b.run_id "
    "WHERE r.tenant_id = ? AND r.started_at >= ? GROUP BY b.run_id, b.source_key"
)


def collect(
    db: _Queryable, tenant_id: str, *, since: datetime
) -> tuple[dict[str, SourceStatus], list[RunStatus]]:
    """Every source's status and the runs since ``since`` (open runs
    regardless), from the metadata tables."""
    loaded = {r[0]: (r[1], int(r[2])) for r in db.execute(LOADED_BY_SOURCE, tenant_id)}
    seen = {r[0]: r[1] for r in db.execute(SEEN_BY_SOURCE, tenant_id)}
    refusals: dict[str, list[tuple[str, str, datetime]]] = {}
    for key, name, sheet, outcome, started in db.execute(OPEN_REFUSALS, tenant_id):
        shown = f"{name} [{sheet}]" if sheet else name
        refusals.setdefault(key, []).append((shown, outcome, started))
    quarantine = {r[0]: int(r[1]) for r in db.execute(OPEN_QUARANTINE, tenant_id)}
    statuses = {}
    for key in set(loaded) | set(seen) | set(refusals) | set(quarantine):
        as_of, count = loaded.get(key, (None, 0))
        statuses[key] = SourceStatus(
            key,
            latest_loaded_as_of=as_of,
            latest_seen_at=seen.get(key),
            loaded_files=count,
            open_refusals=tuple(refusals.get(key, [])),
            open_quarantine=quarantine.get(key, 0),
        )
    sources: dict[str, set[str]] = {}
    for run_id, key in db.execute(RUN_SOURCES, tenant_id, since):
        sources.setdefault(run_id, set()).add(key)
    runs = [
        RunStatus(*r, sources=frozenset(sources.get(r[0], ())))
        for r in db.execute(RUNS, tenant_id, since)
    ]
    return statuses, runs
