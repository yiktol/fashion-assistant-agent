#!/usr/bin/env bash
#
# destroy.sh — tear down the Fashion Assistant data plane.
#
# Runs `cdk destroy`, which removes the S3 buckets (auto-delete enabled) and the
# S3 Vectors custom resource best-effort deletes the index then the vector
# bucket. Use this to avoid ongoing cost when you are done.
#
# Usage:
#   scripts/destroy.sh            # prompts for confirmation
#   FORCE=1 scripts/destroy.sh    # no prompt (non-interactive)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
PY="${ROOT_DIR}/.venv/bin/python"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

[[ -x "${PY}" ]] || die "Run scripts/deploy.sh first (missing .venv)."
command -v cdk >/dev/null || die "AWS CDK CLI not found."

CALLER_JSON="$(aws sts get-caller-identity --region "${PRIMARY_REGION}" 2>/dev/null)" \
  || die "AWS credentials not valid for ${PRIMARY_REGION}."
ACCOUNT_ID="$(printf '%s' "${CALLER_JSON}" | "${PY}" -c 'import sys,json;print(json.load(sys.stdin)["Account"])')"

log "This will DESTROY FashionAgentStack in ${ACCOUNT_ID}/${PRIMARY_REGION}"
log "The image bucket, access-log bucket, and S3 Vectors bucket + index will be deleted."

if [[ "${FORCE:-0}" != "1" ]]; then
  read -r -p "Type 'destroy' to confirm: " reply
  [[ "${reply}" == "destroy" ]] || die "Aborted."
fi

CDK_DEFAULT_ACCOUNT="${ACCOUNT_ID}" CDK_DEFAULT_REGION="${PRIMARY_REGION}" AWS_REGION="${PRIMARY_REGION}" \
  cdk destroy --app "${PY} app.py" --force

log "Teardown complete. (variables.json is left in place; delete it manually if desired.)"
