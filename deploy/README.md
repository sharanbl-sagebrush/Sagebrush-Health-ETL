# Running the ETL on Azure

Decided 2026-10-23 (developer: "I want to host a function app or similar on
Azure so that the computation is handed there, not on my computer").

## What runs where

Two scheduled jobs on Azure Container Apps, in the resource group
`sbh-data-bank-rg`, from one image built from this repository's `Dockerfile`:

| Job | Schedule (UTC) | Command | What it does |
|---|---|---|---|
| `sbh-acquire` | every 30 minutes | `sh scripts/job.sh acquire` (runs `scripts/acquire.py --actor etl`) | lists the `sbh-data-bank` upload container, copies every unseen blob into `/tmp/landing`, records it in `meta.blob`, runs the load over the landing root, mails the report when it fetched anything |
| `sbh-freshness` | daily 14:00 | `sh scripts/job.sh freshness` (runs `scripts/freshness.py --email`) | the freshness check; mails the findings when there are any |

The jobs were deployed 2026-11-03 (both identities hold `AcrPull` on the
registry; `sbh-acquire` also holds Storage Blob Data Reader on the
container). Two things the CLI taught: a job command may not carry a
token that starts with `-` (the CLI reads it as its own option), which
is why `scripts/job.sh` exists, and creating a job with `--registry-identity
system` can fail its image pull before the fresh identity's `AcrPull` has
propagated, leaving the job on the placeholder image; the fix is
`az containerapp job registry set ... --identity system` followed by
`job update --image`. **A `Failed` execution of `sbh-freshness` is
expected whenever the check finds an alert**: the script exits 1 by
design and has mailed the findings; read the log before assuming a
crash. The acquire job exits 0 whether or not it found files.

**A one-off rebuild** (decision D18) runs next to the database as an
execution of the acquire job with its command overridden, after the image
has been rebuilt from the commit that carries the spec change
(`deploy/deploy.sh`):

```
python deploy/rebuild.py finance.monthly_payments                # plan, writes nothing
python deploy/rebuild.py finance.monthly_payments --apply --wait # write, print the log
python deploy/rebuild.py <key> --image e978868 --apply --wait    # on an image the jobs are not on yet
```

`--image` takes the registry tag, which is the commit; it runs that
image for the one execution and leaves the scheduled jobs on theirs.
Moving the jobs themselves is `deploy/deploy.sh --no-build` after the
image is built, or one `az containerapp job update --image` per job;
until then the half-hourly acquire run types files with the specs of
the image it runs.

The launcher reads the job's image and environment and passes them back
with the new command, because a start with a command override replaces
the execution's whole container template: the first attempt (2026-11-04)
passed only the command and ran without its SQL settings, failing before
it connected. The scheduled acquire runs are not affected.

A job runs to completion and exits; nothing is billed between runs. A
pass that finds nothing new ends in seconds. The largest load seen so far
(Monthly Payments, 1.3 million rows) fits a 4 vCPU / 8 GiB replica, which
is what the jobs ask for.

## Why Container Apps jobs and not a Function App

A Function App on the consumption plan stops a run at ten minutes, and the
loads run for up to forty. The ETL also needs Microsoft's ODBC Driver 18 in
the image, which a container gives us and a function runtime does not
promise. A Container Apps job is the same idea (code that runs on a
schedule, billed while it runs) without those two limits, and the image is
the one that runs on a laptop, so what is tested is what is deployed.

## Identity and secrets

- The jobs carry a system-assigned managed identity with **Storage Blob
  Data Reader** on the upload container and nothing else; the acquire
  step reads blobs with it (`DefaultAzureCredential`).
- The SQL login and the SMTP password are Container Apps **secrets**,
  referenced by the environment variables the settings classes read.
  `deploy.sh` takes them from `.env` and never prints them. Managed
  identity for SQL (omit `SBH_SQL_USER`) is the target posture and needs a
  contained user created for the job's identity by the server's Entra
  admin; until then the interim SQL login is used.
- The image is pulled from the registry with the same managed identity.

## Deploy, redeploy, operate

```bash
bash deploy/deploy.sh                 # build the image at HEAD and (re)create both jobs
bash deploy/deploy.sh --no-build      # change settings only
az containerapp job start -n sbh-acquire -g sbh-data-bank-rg          # run a pass now
az containerapp job execution list -n sbh-acquire -g sbh-data-bank-rg -o table
az containerapp job logs show -n sbh-acquire -g sbh-data-bank-rg --container sbh-acquire
az group delete -n sbh-data-bank-rg   # remove everything the deploy made
```

The upload convention is unchanged: the team uploads each download day's
files into `inbound/MMDDYYYY/` over SFTP; the dated folder is the as-of.

**Image tags.** Every build is tagged with the commit it was built from
and nothing else, so a tag names exact code and an old tag can be
deleted once no job or execution record needs it. Delete a tag with
`az acr repository delete --image sbh-data-bank:<tag>`, and only after
`az acr repository show-manifests` says no other tag shares its
manifest: deleting one tag of a shared manifest deletes the build
under every tag. The deploy script used to add `latest` to each build
for that reason it no longer does (2026-11-06: removing the stale
`latest` removed e7a5161, the image both jobs ran, and it was rebuilt
from the commit's tree with `git archive` before the next run).

## The landing root is ephemeral

`/tmp/landing` does not survive the run. That is why `meta.blob` decides
what to fetch: a blob version is skipped once a batch exists for its
content (loaded, duplicate, recognised or refused), and is fetched again
on the next pass otherwise, so a load that died before writing its batch
is retried, and a file no spec claims is listed again on the next pass
until a spec is written for it.
