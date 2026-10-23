"""Fetch what the team uploaded, then load it.

    python scripts/acquire.py --env-file .env --actor <you>
    python scripts/acquire.py --env-file .env --actor <you> --poll 300
    python scripts/acquire.py --env-file .env --fetch-only

One pass: list the upload container (``SBH_BLOB_ACCOUNT`` /
``SBH_BLOB_CONTAINER``), copy every blob version not yet in ``meta.blob``
into the landing root under the blob's own path, record each copy, and
then run the backfill loop over the landing root so the new files commit
through the engine like any other delivery. A blob already fetched is
skipped; a file already committed is a ``DUPLICATE_FILE``; nothing is
ever loaded twice. ``--poll N`` repeats the pass every N seconds, which
is the "pick up a midweek re-upload without waiting for Sunday" the
design asks for, and ``--fetch-only`` stops after the copy.

The landing root is ``SBH_LANDING_ROOT`` or, by default, a folder under
the user's local application data, outside the repository: the files
are PHI. The upload convention is a dated folder per download day,
``inbound/MMDDYYYY/``, which is what gives each delivery its as-of (D5).

When ``SBH_ALERT_TO`` is set and something happened (files fetched, a
refusal, a failed run) the pass's report is mailed to those addresses
through ``SBH_SMTP_*``; ``--mail-dry-run`` prints the mail instead.

PHI: prints blob names, sizes, filenames, counts and outcomes. Never a
cell value.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sbh_data_bank.acquire import (  # noqa: E402
    BlobRecord,
    BlobSettings,
    Fetched,
    SqlBlobLedger,
    open_container,
    sync,
)
from sbh_data_bank.commit import SqlStore  # noqa: E402
from sbh_data_bank.commit.backfill import (  # noqa: E402
    BENIGN,
    backfill,
    print_report,
    print_unclaimed,
)
from sbh_data_bank.observe.notify import (  # noqa: E402
    PrintSender,
    Sender,
    SmtpSender,
    SmtpSettings,
    compose_lines,
    recipients_from_env,
)
from sbh_data_bank.registry.loader import SpecRegistry  # noqa: E402
from sbh_data_bank.store.config import SqlSettings, load_env_file  # noqa: E402
from sbh_data_bank.store.connection import Database  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def default_landing() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(os.environ.get("SBH_LANDING_ROOT") or Path(base, "sbh_data_bank", "landing"))


def one_pass(
    registry: SpecRegistry,
    db: Database,
    settings: SqlSettings,
    blob_settings: BlobSettings,
    landing: Path,
    *,
    actor: str,
    fast: bool,
    fetch_only: bool,
) -> tuple[list[Fetched], dict]:
    container = open_container(blob_settings)
    ledger = SqlBlobLedger(db)
    seen = ledger.seen(settings.tenant, blob_settings.account, blob_settings.container)
    print(f"{len(seen)} blob versions already fetched; listing {blob_settings.describe()}")

    def record(item: Fetched) -> None:
        ledger.record(
            BlobRecord(
                tenant_id=settings.tenant,
                account=blob_settings.account,
                container=blob_settings.container,
                blob_name=item.remote.name,
                etag=item.remote.etag,
                content_length=item.remote.size,
                last_modified=item.remote.last_modified.replace(tzinfo=None),
                file_sha256=item.file_sha256,
                fetched_at=datetime.now(),
                local_path=str(item.local_path.relative_to(landing)),
            )
        )
        print(f"  fetched {item.remote.name} ({item.remote.size:,} bytes)")

    fetched = sync(container, landing, seen, on_fetched=record)
    print(f"{len(fetched)} new file{'s' if len(fetched) != 1 else ''}")
    if fetch_only or not fetched:
        return fetched, {}
    report = backfill(
        SqlStore(db, fast_executemany=fast),
        registry,
        landing,
        tenant_id=settings.tenant,
        actor=actor,
        only=[],
        skip=[],
        operator_date=None,
        reconnect=db.close,
        trigger_kind="acquire",
    )
    print_report(report)
    print_unclaimed(registry, landing)
    return fetched, report


def _mail(sender: Sender, to: list[str], where: str, fetched: list[Fetched], report: dict) -> None:
    lines = [f"fetched {len(fetched)} new file(s):"]
    lines += [f"  {f.remote.name} ({f.remote.size:,} bytes)" for f in fetched]
    attention = 0
    if report:
        lines += ["", "load:"]
        for key, c in report.items():
            loaded = c.get("LOADED", 0)
            refused = {k: v for k, v in c.items() if k.isupper() and k not in BENIGN | {"LOADED"}}
            attention += sum(refused.values())
            summary = f"  {key}: loaded={loaded} inserted={c.get('inserted', 0)} "
            summary += f"corrected={c.get('corrected', 0)} quarantined={c.get('quarantined', 0)}"
            if refused:
                summary += " NEEDS ATTENTION " + ", ".join(f"{k}={v}" for k, v in refused.items())
            lines.append(summary)
    subject = f"[SBH ETL] {len(fetched)} file(s) fetched"
    if attention:
        subject += f", {attention} need attention"
    try:
        sender.send(compose_lines(subject, lines, where=where), to)
    except Exception as exc:  # noqa: BLE001 - the mail is the courtesy, not the record
        print(f"could not send the mail: {type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--landing", type=Path)
    parser.add_argument("--poll", type=int, metavar="SECONDS")
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--actor")
    parser.add_argument("--fast", action="store_true", default=True)
    parser.add_argument("--mail-dry-run", action="store_true")
    parser.add_argument("--specs", type=Path, default=REPO / "specs")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)
    settings = SqlSettings.from_env()
    blob_settings = BlobSettings.from_env()
    registry = SpecRegistry.from_dir(args.specs)
    landing = (args.landing or default_landing()).resolve()
    landing.mkdir(parents=True, exist_ok=True)
    actor = args.actor or settings.user or getpass.getuser()
    to = recipients_from_env()
    sender: Sender | None = None
    if args.mail_dry_run:
        sender = PrintSender()
    elif to:
        sender = SmtpSender(SmtpSettings.from_env())
    print(f"acquiring into {landing}; loading into {settings.describe()}")

    failed = False
    while True:
        with Database(settings) as db:
            fetched, report = one_pass(
                registry,
                db,
                settings,
                blob_settings,
                landing,
                actor=actor,
                fast=args.fast,
                fetch_only=args.fetch_only,
            )
        if any("FAILED" in c for c in report.values()):
            failed = True
        if fetched and sender is not None:
            _mail(sender, to, settings.describe(), fetched, report)
        if args.poll is None:
            break
        time.sleep(args.poll)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
