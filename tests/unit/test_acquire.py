"""The acquire step (``acquire.blob``, ``acquire.ledger``).

A fake container stands in for Azure: the tests hold what ``sync`` does
with a listing and a ledger (fetch what is new, mirror the blob's path,
record each copy as it lands, skip what was fetched, fetch a re-upload
again), and that the ledger's SQL names only columns ``meta.blob`` has.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from sbh_data_bank.acquire import (
    BlobRecord,
    BlobSettings,
    MemoryBlobLedger,
    RemoteFile,
    SqlBlobLedger,
    sync,
)
from sbh_data_bank.acquire.ledger import INSERT_SQL, SEEN_SQL
from sbh_data_bank.store.schema import meta_statements

T0 = datetime(2026, 10, 26, 9, 0)


class FakeContainer:
    def __init__(self, blobs: dict[str, bytes], *, etags: dict[str, str] | None = None):
        self.blobs = blobs
        self.etags = etags or {}
        self.downloads: list[str] = []

    def list_files(self) -> list[RemoteFile]:
        return [
            RemoteFile(name, len(data), self.etags.get(name, f"etag-{name}"), T0)
            for name, data in self.blobs.items()
        ]

    def download(self, name: str, target: Path) -> None:
        self.downloads.append(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.blobs[name])


def _record(item, landing: Path) -> BlobRecord:
    return BlobRecord(
        tenant_id="t1",
        account="acct",
        container="box",
        blob_name=item.remote.name,
        etag=item.remote.etag,
        content_length=item.remote.size,
        last_modified=item.remote.last_modified,
        file_sha256=item.file_sha256,
        fetched_at=T0,
        local_path=str(item.local_path.relative_to(landing)),
    )


class TestSync:
    def test_new_blobs_are_mirrored_under_their_path_and_recorded(self, tmp_path):
        container = FakeContainer(
            {"inbound/10262026/a.csv": b"ID\n1\n", "inbound/10262026/b.xlsx": b"xx"}
        )
        ledger = MemoryBlobLedger()
        fetched = sync(
            container, tmp_path, ledger.seen("t1", "acct", "box"),
            on_fetched=lambda item: ledger.record(_record(item, tmp_path)),
        )  # fmt: skip

        assert [f.remote.name for f in fetched] == [
            "inbound/10262026/a.csv",
            "inbound/10262026/b.xlsx",
        ]
        assert (tmp_path / "inbound" / "10262026" / "a.csv").read_bytes() == b"ID\n1\n"
        assert fetched[0].file_sha256 == hashlib.sha256(b"ID\n1\n").hexdigest()
        assert not list(tmp_path.rglob("*.part"))
        assert ledger.seen("t1", "acct", "box") == {
            ("inbound/10262026/a.csv", "etag-inbound/10262026/a.csv"),
            ("inbound/10262026/b.xlsx", "etag-inbound/10262026/b.xlsx"),
        }
        assert ledger.records[0].local_path == str(Path("inbound/10262026/a.csv"))

    def test_a_fetched_blob_is_not_fetched_again_but_a_reupload_is(self, tmp_path):
        container = FakeContainer({"inbound/x.csv": b"1"})
        ledger = MemoryBlobLedger()
        first = sync(
            container, tmp_path, ledger.seen("t1", "acct", "box"),
            on_fetched=lambda item: ledger.record(_record(item, tmp_path)),
        )  # fmt: skip
        again = sync(container, tmp_path, ledger.seen("t1", "acct", "box"))
        assert len(first) == 1 and again == []
        assert container.downloads == ["inbound/x.csv"]

        container.blobs["inbound/x.csv"] = b"2"
        container.etags["inbound/x.csv"] = "etag-v2"
        reupload = sync(container, tmp_path, ledger.seen("t1", "acct", "box"))
        assert [f.remote.etag for f in reupload] == ["etag-v2"]
        assert (tmp_path / "inbound" / "x.csv").read_bytes() == b"2"

    def test_the_ledger_is_per_container(self):
        ledger = MemoryBlobLedger()
        ledger.record(BlobRecord("t1", "acct", "box", "n", "e", 1, T0, "0" * 64, T0, "n"))
        assert ledger.seen("t1", "acct", "box") == {("n", "e")}
        assert ledger.seen("t1", "acct", "other") == set()
        assert ledger.seen("t2", "acct", "box") == set()


class TestSettings:
    def test_from_env_and_the_sas_never_shows(self, monkeypatch):
        monkeypatch.setenv("SBH_BLOB_ACCOUNT", "sbhsftp01")
        monkeypatch.setenv("SBH_BLOB_CONTAINER", "sbh-data-bank")
        monkeypatch.setenv("SBH_BLOB_SAS", "sv=secret")
        s = BlobSettings.from_env()
        assert s.account_url == "https://sbhsftp01.blob.core.windows.net"
        assert "secret" not in repr(s) and "secret" not in s.describe()
        assert "SAS" in s.describe()
        monkeypatch.delenv("SBH_BLOB_SAS")
        assert "Entra" in BlobSettings.from_env().describe()
        monkeypatch.delenv("SBH_BLOB_CONTAINER")
        with pytest.raises(ValueError, match="SBH_BLOB_CONTAINER"):
            BlobSettings.from_env()


class _FakeDb:
    def __init__(self):
        self.calls = []

    def execute(self, sql, *params):
        self.calls.append((sql, params))
        return [("n", "e")] if sql == SEEN_SQL else []


class TestSqlLedger:
    def test_the_sql_names_only_columns_the_table_has(self):
        (create,) = [s for s in meta_statements() if "CREATE TABLE meta.blob" in s]
        columns = INSERT_SQL.split("(", 1)[1].split(")")[0].split(", ")
        for name in columns:
            assert f"    {name} " in create, name
        assert "uq_blob UNIQUE (tenant_id, account_name, container_name, blob_name, etag)" in create

    def test_seen_and_record_go_through_the_database(self):
        db = _FakeDb()
        ledger = SqlBlobLedger(db)
        assert ledger.seen("t1", "acct", "box") == {("n", "e")}
        ledger.record(BlobRecord("t1", "acct", "box", "n", "e", 1, T0, "0" * 64, T0, "n"))
        assert db.calls[0] == (SEEN_SQL, ("t1", "acct", "box"))
        # Seen means "reached the engine" or "fetched today", so an
        # ephemeral landing root never loses a file whose load died.
        assert "JOIN meta.batch" in SEEN_SQL and "DATEADD(hour, -24" in SEEN_SQL
        assert db.calls[1][0] == INSERT_SQL and len(db.calls[1][1]) == 10
