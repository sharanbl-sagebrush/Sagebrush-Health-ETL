"""``meta.blob``: every blob version the acquire step has fetched.

One row per ``(account, container, name, etag)``, written the moment the
file is in the landing root. The listing on the next run is compared
against these pairs, so a blob is downloaded once however often the step
runs, and a re-upload (new etag) is downloaded again and left to the
commit engine's content-hash check. The row also keeps the file's SHA-256,
which is the identity ``meta.source_file`` uses, so "which upload did this
file come from" is one join.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

__all__ = ["BlobLedger", "BlobRecord", "MemoryBlobLedger", "SqlBlobLedger"]


@dataclass(frozen=True, slots=True)
class BlobRecord:
    tenant_id: str
    account: str
    container: str
    blob_name: str
    etag: str
    content_length: int
    last_modified: datetime
    file_sha256: str
    fetched_at: datetime
    local_path: str


class BlobLedger(Protocol):
    def seen(self, tenant_id: str, account: str, container: str) -> set[tuple[str, str]]:
        """Every ``(blob_name, etag)`` already fetched for this container."""
        ...

    def record(self, record: BlobRecord) -> None: ...


class _Queryable(Protocol):
    def execute(self, sql: str, *params: Any) -> list[tuple]: ...


_COLUMNS = [
    "tenant_id", "account_name", "container_name", "blob_name", "etag", "content_length",
    "last_modified", "file_sha256", "fetched_at", "local_path",
]  # fmt: skip
#: A blob version is seen once a batch exists for its content (loaded,
#: duplicate, recognised or refused: the file reached the engine), or for a
#: day after it was fetched. So a fetch whose load died before writing a
#: batch is fetched again on the next pass, which is what makes an ephemeral
#: landing root (the container job's /tmp) safe; and a file no spec claims,
#: which never gets a batch, comes back once a day and is listed as
#: unclaimed each time until a spec is written for it. A zip is identified
#: by its own bytes while its members get the batches, so it too returns
#: daily; zips are the backlog's shape, not the uploads'.
SEEN_SQL = (
    "SELECT b.blob_name, b.etag FROM meta.blob AS b "
    "WHERE b.tenant_id = ? AND b.account_name = ? AND b.container_name = ? "
    "AND (b.fetched_at > DATEADD(hour, -24, SYSUTCDATETIME()) OR EXISTS ("
    "SELECT 1 FROM meta.source_file AS sf JOIN meta.batch AS bt "
    "ON bt.source_file_id = sf.source_file_id "
    "WHERE sf.tenant_id = b.tenant_id AND sf.file_sha256 = b.file_sha256))"
)
INSERT_SQL = (
    f"INSERT INTO meta.blob ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})"
)


class SqlBlobLedger:
    """Over ``store.connection.Database``; each call is its own transaction."""

    def __init__(self, db: _Queryable) -> None:
        self._db = db

    def seen(self, tenant_id: str, account: str, container: str) -> set[tuple[str, str]]:
        return {(r[0], r[1]) for r in self._db.execute(SEEN_SQL, tenant_id, account, container)}

    def record(self, record: BlobRecord) -> None:
        self._db.execute(
            INSERT_SQL,
            record.tenant_id, record.account, record.container, record.blob_name, record.etag,
            record.content_length, record.last_modified, record.file_sha256, record.fetched_at,
            record.local_path,
        )  # fmt: skip


class MemoryBlobLedger:
    """For tests: every fetched version is seen, with no batch to consult."""

    def __init__(self) -> None:
        self.records: list[BlobRecord] = []

    def seen(self, tenant_id: str, account: str, container: str) -> set[tuple[str, str]]:
        return {
            (r.blob_name, r.etag)
            for r in self.records
            if (r.tenant_id, r.account, r.container) == (tenant_id, account, container)
        }

    def record(self, record: BlobRecord) -> None:
        self.records.append(record)
