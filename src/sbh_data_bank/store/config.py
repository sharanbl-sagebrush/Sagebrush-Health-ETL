"""Database configuration, read from the environment.

No connection string, password or server name is ever stored in this
repository, and none is ever written to a log or an error message. The
previous implementation kept a SQL admin password in plaintext in
``settings.yaml`` and in 22 other files; this module exists so there is no
convenient place to do that again.

Development reads from an env file outside the repository. Production reads
from the process environment, populated from Key Vault by managed identity.
Both arrive here as the same :class:`SqlSettings`.

    SBH_SQL_SERVER     sagebrushhealth-data-analysis.database.windows.net
    SBH_SQL_DATABASE   sbh_data_bank
    SBH_SQL_USER       (omit when using managed identity)
    SBH_SQL_PASSWORD   (omit when using managed identity)
    SBH_SQL_DRIVER     ODBC Driver 18 for SQL Server
    SBH_TENANT         sagebrush
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["SqlSettings", "load_env_file"]

DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read ``KEY=value`` lines into the process environment.

    For development only, and deliberately not defaulted to a path inside the
    repository - the file holds a live credential and must live somewhere that
    cannot be committed by accident.

    Existing environment variables win, so a real deployment is never
    overridden by a stray file.
    """
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        values[key] = value
        os.environ.setdefault(key, value)
    return values


@dataclass(frozen=True, slots=True)
class SqlSettings:
    """Everything needed to reach Azure SQL, and nothing that identifies a person."""

    server: str
    database: str
    driver: str = DEFAULT_DRIVER
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    """Never appears in ``repr``, so a settings object cannot leak into a log
    line or a traceback."""
    tenant: str = "sagebrush"
    connect_timeout: int = 30
    login_timeout: int = 30

    @classmethod
    def from_env(cls) -> SqlSettings:
        missing = [k for k in ("SBH_SQL_SERVER", "SBH_SQL_DATABASE") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                f"database settings incomplete: {', '.join(missing)} not set. "
                "Set them in the environment, or load a dev env file with "
                "load_env_file() - never commit one."
            )
        return cls(
            server=os.environ["SBH_SQL_SERVER"],
            database=os.environ["SBH_SQL_DATABASE"],
            driver=os.environ.get("SBH_SQL_DRIVER", DEFAULT_DRIVER),
            user=os.environ.get("SBH_SQL_USER") or None,
            password=os.environ.get("SBH_SQL_PASSWORD") or None,
            tenant=os.environ.get("SBH_TENANT", "sagebrush"),
        )

    @property
    def uses_managed_identity(self) -> bool:
        """True when no username is supplied, so Azure AD is used instead.

        The target posture: no password anywhere, the pipeline authenticating as
        itself rather than as a person. Today the load runs under a named SQL
        login, which is worth replacing before this reaches production.
        """
        return not self.user

    def connection_string(self) -> str:
        """Build the ODBC string. Never log the result."""
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER=tcp:{self.server},1433",
            f"DATABASE={self.database}",
            "Encrypt=yes",
            "TrustServerCertificate=no",
            f"Connection Timeout={self.connect_timeout}",
        ]
        if self.uses_managed_identity:
            parts.append("Authentication=ActiveDirectoryMsi")
        else:
            parts.append(f"UID={self.user}")
            parts.append(f"PWD={self.password}")
        return ";".join(parts) + ";"

    def describe(self) -> str:
        """A form safe to print: identifies the target, reveals no credential."""
        who = "managed identity" if self.uses_managed_identity else f"login {self.user!r}"
        return f"{self.database} on {self.server} as {who} (tenant {self.tenant!r})"
