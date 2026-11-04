#!/bin/sh
# The entry point of the container image: one word picks the job.
#
# The scheduled jobs in deploy/deploy.sh cannot be given a command that
# carries a token starting with "-" (the Azure CLI reads it as one of its own
# options), so the flags each job needs live here and the job is created
# with `sh scripts/job.sh acquire` or `sh scripts/job.sh freshness`.
#
# A rebuild (decision D18) runs as a one-off execution of the acquire job
# with the command overridden, so the work happens next to the database:
#   python deploy/rebuild.py finance.monthly_payments [--apply] [--wait]
# (the launcher carries the job's image and environment over; a bare
# "job start" override would drop them). The spec keys follow the word.
set -eu
job="${1:-acquire}"
[ $# -gt 0 ] && shift
case "$job" in
  acquire)      exec python scripts/acquire.py --actor etl ;;
  freshness)    exec python scripts/freshness.py --email ;;
  rebuild-plan) exec python scripts/rebuild.py --actor etl --only "$@" ;;
  rebuild)      exec python scripts/rebuild.py --actor etl --apply --only "$@" ;;
  *) echo "job.sh: unknown job '$job' (acquire | freshness | rebuild-plan | rebuild)" >&2
     exit 2 ;;
esac
