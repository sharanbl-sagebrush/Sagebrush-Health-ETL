"""Azure SQL access. ``config`` reads ``SBH_SQL_*`` settings from the
environment; ``connection`` opens a single-threaded ``Database`` with retry
and transactions; ``schema`` generates the metadata tables and one typed
``raw.*`` table per source spec."""
