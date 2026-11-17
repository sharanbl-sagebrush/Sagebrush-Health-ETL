"""``meta.blob``: every blob version the acquire step has fetched.

One row per ``(account, container, name, etag)``, written the moment the
file is in the landing root (and given the new fetch time when the same
version is fetched again). The listing on the next run is compared
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
#: The batch outcomes that settle a file: its rows are in, its content was
#: already in, or another spec owns its shape. Every other outcome is a
#: refusal or a hold, which a spec edit may yet turn into a load. The same
#: set as ``observe.freshness.BENIGN_OUTCOMES`` (a test holds them equal).
SETTLED_OUTCOMES = ("DUPLICATE_FILE", "LOADED", "RECOGNISED_NOT_LOADED")
#: A blob version is seen once a batch has settled its content, or for a
#: day after it was last fetched. So a fetch whose load died before writing
#: a batch is fetched again on the next pass, which is what makes an
#: ephemeral landing root (the container job's /tmp) safe; a file no spec
#: claims, which never gets a batch, comes back once a day and is listed as
#: unclaimed each time until a spec is written for it; and a file the
#: engine held or refused comes back once a day too, so a corrected spec
#: in the image is enough to load it, with no re-upload and no load from a
#: laptop (2026-11-17; before, any batch made a version seen for good, and
#: the two Monthly Payments files held on 2026-11-06 had to be re-uploaded
#: under a new folder). Each retry that is refused again leaves one more
#: batch on the file, and the freshness check reads the newest. A zip is
#: identified by its own bytes while its members get the batches, so it
#: too returns daily; zips are the backlog's shape, not the uploads'.
SEEN_SQL = (
    "SELECT b.blob_name, b.etag FROM meta.blob AS b "
    "WHERE b.tenant_id = ? AND b.account_name = ? AND b.container_name = ? "
    "AND (b.fetched_at > DATEADD(hour, -24, SYSUTCDATETIME()) OR EXISTS ("
    "SELECT 1 FROM meta.source_file AS sf JOIN meta.batch AS bt "
    "ON bt.source_file_id = sf.source_file_id "
    "WHERE sf.tenant_id = b.tenant_id AND sf.file_sha256 = b.file_sha256 "
    f"AND bt.outcome IN ({', '.join(repr(o) for o in SETTLED_OUTCOMES)})))"
)
_KEY = ["tenant_id", "account_name", "container_name", "blob_name", "etag"]
#: A version fetched again (its day is up and nothing settled it) keeps its
#: one row, with the new fetch time, which is what starts its next day.
#: Parameters: the non-key columns, then the key.
REFETCH_SQL = (
    f"UPDATE meta.blob SET {', '.join(f'{c} = ?' for c in _COLUMNS if c not in _KEY)} "
    f"WHERE {' AND '.join(f'{c} = ?' for c in _KEY)}"
)
#: The first fetch of a version; writes nothing when the row is there.
#: Parameters: every column, then the key.
INSERT_SQL = (
    f"INSERT INTO meta.blob ({', '.join(_COLUMNS)}) SELECT {', '.join('?' for _ in _COLUMNS)} "
    f"WHERE NOT EXISTS (SELECT 1 FROM meta.blob WHERE {' AND '.join(f'{c} = ?' for c in _KEY)})"
)


class SqlBlobLedger:
    """Over ``store.connection.Database``; each call is its own transaction."""

    def __init__(self, db: _Queryable) -> None:
        self._db = db

    def seen(self, tenant_id: str, account: str, container: str) -> set[tuple[str, str]]:
        return {(r[0], r[1]) for r in self._db.execute(SEEN_SQL, tenant_id, account, container)}

    def record(self, record: BlobRecord) -> None:
        key = (
            record.tenant_id, record.account, record.container, record.blob_name, record.etag,
        )  # fmt: skip
        rest = (
            record.content_length, record.last_modified, record.file_sha256, record.fetched_at,
            record.local_path,
        )  # fmt: skip
        self._db.execute(REFETCH_SQL, *rest, *key)
        self._db.execute(INSERT_SQL, *key, *rest, *key)


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
        same = (record.tenant_id, record.account, record.container, record.blob_name, record.etag)
        self.records = [
            r
            for r in self.records
            if (r.tenant_id, r.account, r.container, r.blob_name, r.etag) != same
        ]
        self.records.append(record)
