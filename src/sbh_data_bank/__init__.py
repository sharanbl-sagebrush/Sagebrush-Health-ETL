"""SBH_DATA_BANK: configuration-driven ingestion of vendor extracts into an
append-only ledger. Read the subpackages in this order: ``core`` (hashing and
effective dating), ``transforms`` (NDC and value parsing), ``registry`` (the
source spec model, header matching, variant binding), ``ingest`` (reading and
typing a file), ``store`` (settings, connection, ledger schema)."""
