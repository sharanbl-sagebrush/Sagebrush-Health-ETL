#!/bin/sh
# The entry point of the container image: one word picks the job.
#
# The scheduled jobs in deploy/deploy.sh cannot be given a command that
# carries a token starting with "-" (the Azure CLI reads it as one of its own
# options), so the flags each job needs live here and the job is created
# with `sh scripts/job.sh acquire` or `sh scripts/job.sh freshness`.
set -eu
case "${1:-acquire}" in
  acquire)   exec python scripts/acquire.py --actor etl ;;
  freshness) exec python scripts/freshness.py --email ;;
  *) echo "job.sh: unknown job '$1' (acquire | freshness)" >&2; exit 2 ;;
esac
