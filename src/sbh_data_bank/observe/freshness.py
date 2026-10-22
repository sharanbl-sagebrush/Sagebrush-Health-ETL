"""Source freshness and the open items in the metadata tables.

``expected_cadence_days`` on a spec is a staleness threshold, not a
schedule (decided 2026-10-07, ``docs/architecture-and-plan.md``, "How
files arrive"): "alert if no new file for this source has been seen in N
days". Files are uploaded by hand on Sundays, with midweek re-uploads when
data needs correcting, so a check that runs before Sunday's upload must
not fire on its own; the grace period here is what stops that.

What "seen" means. A delivery counts from its **as-of date** (D5: the
dated upload folder, which is the day the team downloaded it), and only
once it has **loaded**. ``first_seen_at`` is when the pipeline registered
the file, which for everything backfilled on 2026-10-22 is that day and
says nothing about how current the data is; the as-of does. A file that
arrived and was held or refused is not a delivery for this purpose: it is
an open refusal, listed separately, until a later attempt on the same
file loads or recognises it.

Findings, in the order an operator should read them:

``STALE``             an active source with a cadence has no loaded
                      delivery with an as-of inside cadence + grace days
``NEVER_LOADED``      an active source with a cadence and no loaded
                      delivery at all
``OPEN_REFUSAL``      a file whose latest attempt neither loaded nor was
                      benign (held, refused, unrecognised): someone has to
                      fix the spec or the file and run it again
``RUN_FAILED``        a run marked FAILED (a store exception, so one file
                      is missing) inside the reporting window, unless a
                      later run that loaded covered the same sources
``RUN_UNFINISHED``    a run with no outcome after ``unfinished_hours``;
                      its batches each committed on their own, the
                      process just ended before closing it
``OPEN_QUARANTINE``   rows in ``stg.quarantine`` nobody has resolved

The first five are alerts; open quarantine is a note. :func:`assess` is
pure, so the rules are tested without a database; ``scripts/freshness.py``
fills the inputs from the metadata tables and prints the findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sbh_data_bank.registry.spec import SourceSpec

__all__ = [
    "ALERTS",
    "BENIGN_OUTCOMES",
    "DEFAULT_GRACE_DAYS",
    "DEFAULT_UNFINISHED_HOURS",
    "DEFAULT_WINDOW_DAYS",
    "Finding",
    "RunStatus",
    "SourceStatus",
    "assess",
    "stale_after",
]

#: Days added to every cadence before silence counts as staleness. Two
#: covers a check run on Sunday morning or Monday before the week's upload
#: has landed; a weekly source is stale on the Tuesday after a missed Sunday.
DEFAULT_GRACE_DAYS = 2

#: How far back ``RUN_FAILED`` looks. A failure inside the window is
#: closed by a later LOADED run over the same sources (the backfill's
#: text-width failure on 2026-10-22 was re-run twenty minutes later); one
#: older than the window is history either way.
DEFAULT_WINDOW_DAYS = 7

#: A run still open after this long was not interrupted a moment ago.
DEFAULT_UNFINISHED_HOURS = 6

#: Batch outcomes that need nobody's attention: rows were written, the
#: same bytes came again, or the spec recognised a sheet it deliberately
#: hands to a sibling variant or another source.
BENIGN_OUTCOMES = frozenset({"LOADED", "DUPLICATE_FILE", "RECOGNISED_NOT_LOADED"})

ALERTS = frozenset({"STALE", "NEVER_LOADED", "OPEN_REFUSAL", "RUN_FAILED", "RUN_UNFINISHED"})


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """What the metadata tables say about one source. Counts and dates only."""

    source_key: str
    latest_loaded_as_of: date | None = None
    """The newest as-of among this source's LOADED deliveries."""
    latest_seen_at: datetime | None = None
    """When the pipeline last registered any file for the source."""
    loaded_files: int = 0
    open_refusals: tuple[tuple[str, str, datetime], ...] = ()
    """``(file_name, outcome, started_at)`` for each file whose latest
    attempt is not in :data:`BENIGN_OUTCOMES`."""
    open_quarantine: int = 0


@dataclass(frozen=True, slots=True)
class RunStatus:
    """One ``meta.run`` row, as far as the check needs it."""

    run_id: str
    trigger_kind: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    detail: str | None = None
    sources: frozenset[str] = frozenset()
    """The source keys of the run's batches. A run that failed on its first
    file has none, and any later successful run then closes it."""


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    source_key: str | None
    message: str

    @property
    def is_alert(self) -> bool:
        return self.kind in ALERTS

    def __str__(self) -> str:
        who = self.source_key or "-"
        return f"{self.kind:<16} {who:<36} {self.message}"


def stale_after(spec: SourceSpec, grace_days: int = DEFAULT_GRACE_DAYS) -> int | None:
    """Days of silence after which this source is stale, or ``None`` when
    its silence is never an alert: a discontinued, no-access or planned
    source, or one with no cadence (the reference register, BuyBill)."""
    if not spec.is_expected_to_deliver or spec.expected_cadence_days is None:
        return None
    return spec.expected_cadence_days + grace_days


@dataclass(slots=True)
class _Collector:
    findings: list[Finding] = field(default_factory=list)

    def add(self, kind: str, source_key: str | None, message: str) -> None:
        self.findings.append(Finding(kind, source_key, message))


def _resolves(later: RunStatus, failed: RunStatus) -> bool:
    """Whether ``later`` is a successful run after ``failed`` over at least
    the sources the failed run had reached."""
    return (
        later.outcome == "LOADED"
        and later.started_at > failed.started_at
        and failed.sources <= later.sources
    )


def assess(
    specs: list[SourceSpec],
    statuses: dict[str, SourceStatus],
    runs: list[RunStatus],
    *,
    today: date,
    now: datetime | None = None,
    grace_days: int = DEFAULT_GRACE_DAYS,
    window_days: int = DEFAULT_WINDOW_DAYS,
    unfinished_hours: int = DEFAULT_UNFINISHED_HOURS,
) -> list[Finding]:
    """The findings for one check: sources in spec-key order, then runs.

    ``today`` is the date staleness is measured from, so a test can pin it
    and an operator can ask what would have fired last Tuesday. ``now``
    defaults to midnight of ``today`` and only matters for unfinished runs
    and the failed-run window.
    """
    now = now or datetime(today.year, today.month, today.day)
    out = _Collector()
    for spec in sorted(specs, key=lambda s: s.key):
        status = statuses.get(spec.key, SourceStatus(spec.key))
        limit = stale_after(spec, grace_days)
        if limit is not None:
            if status.latest_loaded_as_of is None:
                out.add(
                    "NEVER_LOADED",
                    spec.key,
                    f"expected every {spec.expected_cadence_days} days and nothing has loaded",
                )
            else:
                silent = (today - status.latest_loaded_as_of).days
                if silent >= limit:
                    out.add(
                        "STALE",
                        spec.key,
                        f"latest loaded delivery is as of {status.latest_loaded_as_of} "
                        f"({silent} days); expected every {spec.expected_cadence_days} days "
                        f"plus {grace_days} grace",
                    )
        for file_name, outcome, started_at in status.open_refusals:
            out.add(
                "OPEN_REFUSAL",
                spec.key,
                f"{file_name}: {outcome} on {started_at:%Y-%m-%d %H:%M}, no later attempt loaded",
            )
        if status.open_quarantine:
            out.add(
                "OPEN_QUARANTINE",
                spec.key,
                f"{status.open_quarantine} quarantined rows unresolved",
            )
    since = now - timedelta(days=window_days)
    ordered = sorted(runs, key=lambda r: r.started_at)
    for run in ordered:
        if run.outcome == "FAILED" and run.started_at >= since:
            if any(_resolves(later, run) for later in ordered):
                continue
            detail = f": {run.detail}" if run.detail else ""
            out.add(
                "RUN_FAILED",
                None,
                f"{run.run_id} ({run.trigger_kind}) started {run.started_at:%Y-%m-%d %H:%M}"
                f"{detail}",
            )
        elif run.finished_at is None and run.outcome is None:
            if now - run.started_at >= timedelta(hours=unfinished_hours):
                out.add(
                    "RUN_UNFINISHED",
                    None,
                    f"{run.run_id} ({run.trigger_kind}) started "
                    f"{run.started_at:%Y-%m-%d %H:%M} and was never closed; "
                    "scripts/close_runs.py records it as INTERRUPTED",
                )
    return out.findings
