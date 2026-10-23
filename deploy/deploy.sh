#!/usr/bin/env bash
# Build the image and create (or update) the two scheduled jobs on Azure
# Container Apps. Idempotent: every step creates what is missing and
# updates what exists, so it is also the way to ship a new version.
#
#     bash deploy/deploy.sh            # build + deploy, secrets from .env
#     bash deploy/deploy.sh --no-build # redeploy the jobs on the last image
#
# Needs: az login as an Owner of the subscription (the jobs get a managed
# identity and a role on the upload container), the containerapp
# extension, and .env in the repository root with SBH_SQL_USER,
# SBH_SQL_PASSWORD and, for mail, SBH_SMTP_PASSWORD. Secrets are read from
# that file and handed to the jobs as Container Apps secrets; they are
# never printed and never baked into the image.
#
# What it makes, all in one resource group so it can be removed in one
# command (az group delete -n sbh-data-bank-rg):
#   sbhdatabankacr        a Basic container registry holding the image
#   sbh-data-bank-env     a consumption Container Apps environment
#   sbh-acquire           job, every 30 minutes: fetch uploads, load them
#   sbh-freshness         job, daily 14:00 UTC: the freshness check, mailed
set -euo pipefail
cd "$(dirname "$0")/.."
# The CLI streams build logs through the console encoding and crashes on a
# Windows console when the log carries a character cp1252 lacks.
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1

SUB="${SBH_SUBSCRIPTION:-ac055e67-4bab-4943-8e81-15573482d0cb}"
RG="${SBH_RG:-sbh-data-bank-rg}"
LOC="${SBH_LOCATION:-eastus2}"
ACR="${SBH_ACR:-sbhdatabankacr}"
ENV_NAME="${SBH_ACA_ENV:-sbh-data-bank-env}"
STORAGE_RG="Admin"
STORAGE="sbhsftp01"
CONTAINER="sbh-data-bank"
TAG="$(git rev-parse --short HEAD)"
IMAGE="$ACR.azurecr.io/sbh-data-bank:$TAG"
ALERT_TO="${SBH_ALERT_TO:-Sharan.Lokeshwar@sagebrushhealth.com}"

env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- ; }
SQL_USER="$(env_value SBH_SQL_USER)"
SQL_PASSWORD="$(env_value SBH_SQL_PASSWORD)"
SMTP_PASSWORD="$(env_value SBH_SMTP_PASSWORD || true)"
[ -n "$SQL_USER" ] && [ -n "$SQL_PASSWORD" ] || { echo "SBH_SQL_USER / SBH_SQL_PASSWORD missing from .env"; exit 2; }

az account set --subscription "$SUB"
echo "== resource group $RG in $LOC"
az group create -n "$RG" -l "$LOC" -o none

echo "== registry $ACR"
az acr show -n "$ACR" -g "$RG" -o none 2>/dev/null || az acr create -n "$ACR" -g "$RG" --sku Basic -o none

if [ "${1:-}" != "--no-build" ]; then
  echo "== building $IMAGE in the registry"
  az acr build -r "$ACR" -t "sbh-data-bank:$TAG" -t "sbh-data-bank:latest" --no-logs . -o none
  az acr task list-runs -r "$ACR" --top 1 --query "[0].{status:status, started:startTime}" -o tsv
fi

echo "== environment $ENV_NAME"
az containerapp env show -n "$ENV_NAME" -g "$RG" -o none 2>/dev/null \
  || az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOC" -o none

SECRETS=(sql-user="$SQL_USER" sql-password="$SQL_PASSWORD")
[ -n "$SMTP_PASSWORD" ] && SECRETS+=(smtp-password="$SMTP_PASSWORD")
ENVVARS=(
  SBH_SQL_SERVER=sagebrushhealth-data-analysis.database.windows.net
  SBH_SQL_DATABASE=sbh_data_bank
  SBH_TENANT=sagebrush
  SBH_SQL_USER=secretref:sql-user
  SBH_SQL_PASSWORD=secretref:sql-password
  SBH_BLOB_ACCOUNT="$STORAGE"
  SBH_BLOB_CONTAINER="$CONTAINER"
  SBH_LANDING_ROOT=/tmp/landing
  SBH_SMTP_HOST=smtp.office365.com
  SBH_SMTP_PORT=587
  SBH_SMTP_USER=automation@sagebrushhealth.com
  SBH_MAIL_FROM=automation@sagebrushhealth.com
  SBH_ALERT_TO="$ALERT_TO"
)
[ -n "$SMTP_PASSWORD" ] && ENVVARS+=(SBH_SMTP_PASSWORD=secretref:smtp-password)

job() {  # name, cron, command...
  local name="$1" cron="$2"; shift 2
  echo "== job $name ($cron)"
  if az containerapp job show -n "$name" -g "$RG" -o none 2>/dev/null; then
    az containerapp job update -n "$name" -g "$RG" --image "$IMAGE" --cron-expression "$cron" \
      --cpu 4 --memory 8Gi --replica-timeout 14400 \
      --secrets "${SECRETS[@]}" --set-env-vars "${ENVVARS[@]}" --command "$@" -o none
  else
    az containerapp job create -n "$name" -g "$RG" --environment "$ENV_NAME" \
      --trigger-type Schedule --cron-expression "$cron" \
      --replica-timeout 14400 --replica-retry-limit 0 --parallelism 1 --replica-completion-count 1 \
      --image "$IMAGE" --registry-server "$ACR.azurecr.io" --registry-identity system --mi-system-assigned \
      --cpu 4 --memory 8Gi \
      --secrets "${SECRETS[@]}" --env-vars "${ENVVARS[@]}" --command "$@" -o none
  fi
  # The job's identity reads the upload container; nothing else.
  local principal scope
  principal="$(az containerapp job show -n "$name" -g "$RG" --query identity.principalId -o tsv)"
  scope="/subscriptions/$SUB/resourceGroups/$STORAGE_RG/providers/Microsoft.Storage/storageAccounts/$STORAGE/blobServices/default/containers/$CONTAINER"
  az role assignment create --role "Storage Blob Data Reader" --assignee-object-id "$principal" \
    --assignee-principal-type ServicePrincipal --scope "$scope" -o none 2>/dev/null || true
}

job sbh-acquire   "*/30 * * * *" python scripts/acquire.py --actor etl
job sbh-freshness "0 14 * * *"    python scripts/freshness.py --email

echo "== done: image $IMAGE"
echo "run one now:   az containerapp job start -n sbh-acquire -g $RG"
echo "executions:    az containerapp job execution list -n sbh-acquire -g $RG -o table"
echo "logs:          az containerapp job logs show -n sbh-acquire -g $RG --container sbh-acquire"
