"""Database configuration, read from the environment.

No connection string, password or server name is ever stored in this
repository, and none is ever written to a log or an error message. The
previous implementation kept a SQL admin password in plaintext in
``settings.yaml`` and in 22 other files; this module exists so there is no
convenient place to do that again.

Development reads from a git-ignored ``.env`` in the repository root. Production reads
from the process environment, populated from Key Vault by managed identity.
Both arrive here as the same :class:`SqlSettings`.

    SBH_SQL_SERVER     sagebrushhealth-data-analysis.database.windows.net
    SBH_SQL_DATABASE   sbh_data_bank
    SBH_SQL_USER       (omit when using managed identity)
    SBH_SQL_PASSWORD   (omit when using managed identity)
    SBH_SQL_DRIVER     ODBC Driver 18 for SQL Server
    SBH_TENANT         sagebrush

What each setting is for:

``SBH_SQL_SERVER``    the Azure SQL logical server's fully qualified host name.
``SBH_SQL_DATABASE``  the database on that server - ``sbh_data_bank`` today.
``SBH_SQL_USER``      a SQL login. Present only in the interim; its absence is
                      the switch that selects managed identity.
``SBH_SQL_PASSWORD``  that login's password. Never defaulted, never printed.
``SBH_SQL_DRIVER``    the ODBC driver name as Windows/unixODBC registers it.
                      Defaults to Driver 18, which enforces encryption.
``SBH_TENANT``        the ``tenant_id`` stamped on every row this process
                      writes. Multi-tenancy is a first-day constraint of the
                      ledger, so even the single-tenant deployment names one.

How this fits
-------------
``SqlSettings.from_env()`` is the only way settings are built. Today it is
called by ``scripts/migrate.py`` (after ``load_env_file`` for a developer's
``--env-file``) and consumed by ``store.connection.connect``. Nothing in
``ingest`` imports this module: loading a file needs no database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["SqlSettings", "load_env_file"]

#: Driver 18 is the current Microsoft ODBC driver. It defaults to Encrypt=yes
#: and validates the server certificate, which is why the connection string
#: below can say TrustServerCertificate=no without further setup.
DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read ``KEY=value`` lines into the process environment.

    For development only, and deliberately not defaulted to a path: the file
    holds a live credential. The convention is ``.env`` in the repository root,
    which ``.gitignore`` excludes (decided 2026-10-21) so it cannot be
    committed by accident.

    Existing environment variables win, so a real deployment is never
    overridden by a stray file.

    Returns the values read from the file (not the merged environment), which
    is useful for a caller that wants to report which keys the file supplied.
    """
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # Blank lines and '#' comments are allowed; a line with no '=' is
        # ignored rather than an error so a stray note does not stop a dev run.
        if not line or line.startswith("#") or "=" not in line:
            continue
        # Split on the first '=' only: a password may itself contain '='.
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        values[key] = value
        # setdefault, not assignment: the process environment wins.
        os.environ.setdefault(key, value)
    return values


@dataclass(frozen=True, slots=True)
class SqlSettings:
    """Everything needed to reach Azure SQL, and nothing that identifies a person.

    Frozen so a settings object cannot be mutated after construction, and
    ``slots`` so no stray attribute (say, a cached connection string) can be
    hung on it and later logged.
    """

    server: str
    database: str
    driver: str = DEFAULT_DRIVER
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    """Never appears in ``repr``, so a settings object cannot leak into a log
    line or a traceback."""
    tenant: str = "sagebrush"
    connect_timeout: int = 30
    """Seconds the driver waits to establish the TCP/TLS connection."""
    login_timeout: int = 30
    """Seconds pyodbc waits for the login handshake. Passed separately because
    pyodbc takes it as a keyword, not in the connection string."""

    @classmethod
    def from_env(cls) -> SqlSettings:
        """Build settings from ``SBH_SQL_*`` and ``SBH_TENANT``.

        Raises ``RuntimeError`` naming the missing variables when server or
        database is unset, so a misconfigured deployment fails at startup with
        a message rather than at the first query with an ODBC error.
        """
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
            # `or None` turns an empty string into None, so SBH_SQL_USER=""
            # means "managed identity" rather than "a login with no name".
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
        # Encrypt=yes and TrustServerCertificate=no together mean TLS with a
        # validated certificate, which Azure SQL requires. Port 1433 is the
        # only port Azure SQL listens on; tcp: makes the protocol explicit.
        parts = [
            f"DRIVER={{{self.driver}}}",
            f"SERVER=tcp:{self.server},1433",
            f"DATABASE={self.database}",
            "Encrypt=yes",
            "TrustServerCertificate=no",
            f"Connection Timeout={self.connect_timeout}",
        ]
        if self.uses_managed_identity:
            # The driver obtains a token from the Azure Instance Metadata
            # Service on the host. No credential in the process at all.
            parts.append("Authentication=ActiveDirectoryMsi")
        else:
            # The interim path: a SQL login. This is the only place the
            # password is ever interpolated, and the caller never prints it.
            parts.append(f"UID={self.user}")
            parts.append(f"PWD={self.password}")
        return ";".join(parts) + ";"

    def describe(self) -> str:
        """A form safe to print: identifies the target, reveals no credential."""
        who = "managed identity" if self.uses_managed_identity else f"login {self.user!r}"
        return f"{self.database} on {self.server} as {who} (tenant {self.tenant!r})"
