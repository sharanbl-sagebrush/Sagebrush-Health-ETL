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
SEEN_SQL = (
    "SELECT blob_name, etag FROM meta.blob "
    "WHERE tenant_id = ? AND account_name = ? AND container_name = ?"
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
