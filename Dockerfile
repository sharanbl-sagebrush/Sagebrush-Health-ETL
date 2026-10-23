# The ETL as a container image, for the scheduled Container Apps jobs in
# deploy/ (acquire every half hour, freshness once a day). One image, the
# command decides the job. Python 3.11 on Debian, Microsoft's ODBC Driver 18
# for the Azure SQL connection, and this package installed from source.
#
# Nothing secret is baked in: settings and secrets reach the container as
# environment variables (CLAUDE.md, "Credentials"). The landing root is
# /tmp/landing, ephemeral by design; meta.blob is what remembers what was
# fetched, and a file whose load did not reach a batch is fetched again.

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ACCEPT_EULA=Y \
    SBH_SQL_DRIVER="ODBC Driver 18 for SQL Server" \
    SBH_LANDING_ROOT=/tmp/landing

# ODBC Driver 18 from Microsoft's apt repository, plus unixODBC for pyodbc.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg2 ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && apt-get purge -y curl gnupg2 \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY specs ./specs
COPY scripts ./scripts
RUN pip install --no-cache-dir . \
    && mkdir -p /tmp/landing

# A non-root user; the jobs need no privileges.
RUN useradd --create-home --uid 1000 etl && chown -R etl:etl /app /tmp/landing
USER etl

# The acquire pass by default; the freshness job overrides the command.
CMD ["python", "scripts/acquire.py", "--actor", "etl"]
