# Running the ETL on Azure

Decided 2026-10-23 (developer: "I want to host a function app or similar on
Azure so that the computation is handed there, not on my computer").

## What runs where

Two scheduled jobs on Azure Container Apps, in the resource group
`sbh-data-bank-rg`, from one image built from this repository's `Dockerfile`:

| Job | Schedule (UTC) | Command | What it does |
|---|---|---|---|
| `sbh-acquire` | every 30 minutes | `scripts/acquire.py --actor etl` | lists the `sbh-data-bank` upload container, copies every unseen blob into `/tmp/landing`, records it in `meta.blob`, runs the load over the landing root, mails the report when it fetched anything |
| `sbh-freshness` | daily 14:00 | `scripts/freshness.py --email` | the freshness check; mails the findings when there are any |

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

## The landing root is ephemeral

`/tmp/landing` does not survive the run. That is why `meta.blob` decides
what to fetch: a blob version is skipped once a batch exists for its
content (loaded, duplicate, recognised or refused), and is fetched again
on the next pass otherwise, so a load that died before writing its batch
is retried, and a file no spec claims is listed again on the next pass
until a spec is written for it.
