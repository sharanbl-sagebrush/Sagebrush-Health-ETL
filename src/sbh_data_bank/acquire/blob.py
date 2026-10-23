"""The upload container, and mirroring it to a local landing root.

How files arrive (decided 2026-10-07, ``docs/architecture-and-plan.md``):
the team downloads each vendor's extract by hand and uploads it, over
SFTP, to the ``sbh-data-bank`` container of the ``sbhsftp01`` storage
account (set up 2026-10-23; the SFTP user's home is ``inbound/``). The
ETL never speaks SFTP. It reads the same container through the Blob API,
as an Entra ID identity, and copies every blob it has not seen before
into a local landing root under the blob's own path. From there the
commit engine does what it does for any folder: the dated upload folder
in the path is the delivery's as-of (D5), a file already committed is a
``DUPLICATE_FILE``, and everything else goes through the layers.

So the upload convention is the folder convention: a dated folder,
``inbound/MMDDYYYY/`` or ``inbound/YYYY-MM-DD/``, holding that day's
downloads. A corrected re-pull goes in a new dated folder for the day it
was pulled, never over the old file.

Identity of a blob version is ``(name, etag)``. A re-upload of the same
bytes to the same name has a new etag and is fetched again, which costs a
download and then a ``DUPLICATE_FILE`` batch, and nothing else. The
download is written to a temporary name and renamed into place, so a
half-fetched file is never where the engine looks.

Credentials: a SAS token in ``SBH_BLOB_SAS`` when set (container-scoped,
read and list, for a developer machine), otherwise ``DefaultAzureCredential``,
which is ``az login`` on a developer machine and the managed identity in
production. Either needs Storage Blob Data Reader on the container; the
account key is never used.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

__all__ = ["BlobSettings", "Container", "Fetched", "RemoteFile", "open_container", "sync"]


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """One blob as listed: its path inside the container and its version."""

    name: str
    size: int
    etag: str
    last_modified: datetime


class Container(Protocol):
    """What the acquire step needs from a container: a listing, and a
    download of one blob to one local path."""

    def list_files(self) -> list[RemoteFile]: ...

    def download(self, name: str, target: Path) -> None: ...


@dataclass(frozen=True)
class BlobSettings:
    """``SBH_BLOB_ACCOUNT``, ``SBH_BLOB_CONTAINER`` and, optionally,
    ``SBH_BLOB_SAS``. The SAS is excluded from ``repr``."""

    account: str
    container: str
    sas: str | None = None

    def __repr__(self) -> str:
        return f"BlobSettings(account={self.account!r}, container={self.container!r})"

    @property
    def account_url(self) -> str:
        return f"https://{self.account}.blob.core.windows.net"

    def describe(self) -> str:
        how = "SAS token" if self.sas else "Entra ID (DefaultAzureCredential)"
        return f"{self.container} on {self.account} via {how}"

    @classmethod
    def from_env(cls) -> BlobSettings:
        missing = [k for k in ("SBH_BLOB_ACCOUNT", "SBH_BLOB_CONTAINER") if not os.environ.get(k)]
        if missing:
            raise ValueError(f"missing environment settings: {', '.join(missing)}")
        return cls(
            account=os.environ["SBH_BLOB_ACCOUNT"],
            container=os.environ["SBH_BLOB_CONTAINER"],
            sas=os.environ.get("SBH_BLOB_SAS") or None,
        )


class AzureContainer:
    """A ``Container`` over ``azure.storage.blob.ContainerClient``. The SDK
    is imported here so nothing else in the package needs it installed."""

    def __init__(self, settings: BlobSettings) -> None:
        from azure.storage.blob import ContainerClient

        if settings.sas:
            credential: Any = settings.sas.lstrip("?")
        else:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
        self._client = ContainerClient(settings.account_url, settings.container, credential)

    def list_files(self) -> list[RemoteFile]:
        found = []
        for blob in self._client.list_blobs(include=["metadata"]):
            # A hierarchical-namespace account lists a directory as a
            # zero-length blob flagged hdi_isfolder; it is not a file.
            if (blob.metadata or {}).get("hdi_isfolder") == "true":
                continue
            if blob.name.endswith("/"):
                continue
            found.append(
                RemoteFile(blob.name, int(blob.size or 0), str(blob.etag), blob.last_modified)
            )
        return found

    def download(self, name: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as handle:
            self._client.download_blob(name).readinto(handle)


def open_container(settings: BlobSettings) -> Container:
    return AzureContainer(settings)


@dataclass(frozen=True, slots=True)
class Fetched:
    """One blob version copied into the landing root."""

    remote: RemoteFile
    local_path: Path
    file_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sync(
    container: Container,
    landing: Path,
    seen: Iterable[tuple[str, str]],
    *,
    on_fetched: Callable[[Fetched], None] | None = None,
) -> list[Fetched]:
    """Copy every blob version not in ``seen`` (pairs of name and etag) into
    ``landing`` under the blob's path, oldest first, and return what was
    fetched. ``on_fetched`` is called after each file is in place, which is
    where the ledger records it, so a crash between two files loses
    nothing already recorded."""
    known = set(seen)
    fetched: list[Fetched] = []
    for remote in sorted(container.list_files(), key=lambda r: (r.last_modified, r.name)):
        if (remote.name, remote.etag) in known:
            continue
        target = landing / Path(*remote.name.split("/"))
        partial = target.with_name(target.name + ".part")
        container.download(remote.name, partial)
        partial.replace(target)
        item = Fetched(remote, target, _sha256(target))
        fetched.append(item)
        if on_fetched is not None:
            on_fetched(item)
    return fetched
