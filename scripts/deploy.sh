#!/usr/bin/env bash
#
# deploy.sh — one-command deploy for the Fashion Assistant agent.
#
# Provisions the data plane (S3 buckets + S3 Vectors + IAM) with CDK, populates
# the S3 Vectors index with the demo catalog, and launches the local Streamlit
# app. Safe to re-run: each step is idempotent.
#
# Flow (matches README.md):
#   venv + deps  ->  preflight (AWS creds, CDK CLI/bootstrap, Bedrock access)
#   ->  cdk deploy --outputs-file variables.json  ->  ingest  ->  streamlit run
#
# Usage:
#   scripts/deploy.sh                 # full deploy + ingest + run app
#   INGEST_LIMIT=100 scripts/deploy.sh   # ingest only the first 100 images
#   SKIP_INGEST=1 scripts/deploy.sh      # deploy + run, skip ingest (index must exist)
#   SKIP_APP=1 scripts/deploy.sh         # deploy (+ingest) but don't launch the UI
#   NO_DEPLOY=1 scripts/deploy.sh        # skip cdk deploy (reuse existing stack)
#
# Environment overrides:
#   PRIMARY_REGION   (default us-east-1)  primary/embeddings region
#   IMAGE_REGION     (default us-west-2)  Stability text-to-image region
#   STREAMLIT_PORT   (default 8501)       port for the local app
#   INGEST_LIMIT     (default 0 = all)    number of catalog images to ingest
#
set -euo pipefail

# ----------------------------------------------------------------------------
# Resolve paths and config
# ----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

PRIMARY_REGION="${PRIMARY_REGION:-us-east-1}"
IMAGE_REGION="${IMAGE_REGION:-us-west-2}"
STREAMLIT_PORT="${STREAMLIT_PORT:-8501}"
INGEST_LIMIT="${INGEST_LIMIT:-0}"
VENV_DIR="${ROOT_DIR}/.venv"
PY="${VENV_DIR}/bin/python"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------------------
# 1. Toolchain preflight
# ----------------------------------------------------------------------------
log "Checking toolchain"
command -v python3 >/dev/null || die "python3 not found."
command -v cdk >/dev/null || die "AWS CDK CLI not found. Install it with: npm install -g aws-cdk"
command -v aws >/dev/null || die "AWS CLI not found. Install it: https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-install.html"
ok "python3, cdk, aws present"

# ----------------------------------------------------------------------------
# 2. Virtualenv + dependencies
# ----------------------------------------------------------------------------
if [[ ! -d "${VENV_DIR}" ]]; then
  log "Creating virtualenv at .venv"
  python3 -m venv "${VENV_DIR}"
fi
log "Installing dependencies (app + infra + dev)"
"${PY}" -m pip install --quiet --upgrade pip
"${PY}" -m pip install --quiet -e '.[infra,dev]'
ok "dependencies installed"

# ----------------------------------------------------------------------------
# 3. AWS credentials + account/region
# ----------------------------------------------------------------------------
log "Verifying AWS credentials (region ${PRIMARY_REGION})"
CALLER_JSON="$(aws sts get-caller-identity --region "${PRIMARY_REGION}" 2>/dev/null)" \
  || die "AWS credentials not valid for ${PRIMARY_REGION}. Run 'aws sso login' or 'aws configure'."
ACCOUNT_ID="$(printf '%s' "${CALLER_JSON}" | "${PY}" -c 'import sys,json;print(json.load(sys.stdin)["Account"])')"
CALLER_ARN="$(printf '%s' "${CALLER_JSON}" | "${PY}" -c 'import sys,json;print(json.load(sys.stdin)["Arn"])')"
ok "account ${ACCOUNT_ID} as ${CALLER_ARN}"

# ----------------------------------------------------------------------------
# 4. CDK bootstrap check
# ----------------------------------------------------------------------------
log "Checking CDK bootstrap in ${PRIMARY_REGION}"
if aws cloudformation describe-stacks --stack-name CDKToolkit --region "${PRIMARY_REGION}" >/dev/null 2>&1; then
  ok "CDK is bootstrapped"
else
  warn "CDKToolkit stack not found — bootstrapping ${ACCOUNT_ID}/${PRIMARY_REGION}"
  CDK_DEFAULT_ACCOUNT="${ACCOUNT_ID}" CDK_DEFAULT_REGION="${PRIMARY_REGION}" AWS_REGION="${PRIMARY_REGION}" \
    cdk bootstrap "aws://${ACCOUNT_ID}/${PRIMARY_REGION}" --app "${PY} app.py"
  ok "bootstrap complete"
fi

# ----------------------------------------------------------------------------
# 5. Bedrock model-access reminder (console-only; cannot be enabled via CLI)
# ----------------------------------------------------------------------------
log "Bedrock model access (enable once in the console if not already)"
cat <<EOF
  Required model access — Amazon Bedrock console -> Model access:
    ${PRIMARY_REGION}:
      - us.anthropic.claude-sonnet-4-5-20250929-v1:0   (brain, cross-region profile)
      - amazon.nova-2-multimodal-embeddings-v1:0        (embeddings)
      - us.stability.stable-image-inpaint-v1:0          (inpaint)
      - us.stability.stable-outpaint-v1:0               (outpaint)
      - us.stability.stable-image-search-replace-v1:0   (replace an item, mask-free)
      - us.stability.stable-image-search-recolor-v1:0   (recolor an item, mask-free)
    ${IMAGE_REGION}:
      - stability.stable-image-core-v1:1                (text-to-image generate)
      - stability.stable-image-ultra-v1:1               (optional, higher quality)
  Model access is account/region config and cannot be set from this script.
EOF

# ----------------------------------------------------------------------------
# 6. cdk deploy  (writes variables.json consumed by the app + ingest)
# ----------------------------------------------------------------------------
if [[ "${NO_DEPLOY:-0}" == "1" ]]; then
  warn "NO_DEPLOY=1 — skipping cdk deploy (reusing existing stack)"
  [[ -f "${ROOT_DIR}/variables.json" ]] || die "variables.json missing; run a real deploy first."
else
  log "Deploying data plane (cdk deploy)"
  CDK_DEFAULT_ACCOUNT="${ACCOUNT_ID}" CDK_DEFAULT_REGION="${PRIMARY_REGION}" AWS_REGION="${PRIMARY_REGION}" \
    cdk deploy \
      --app "${PY} app.py" \
      --require-approval never \
      --outputs-file variables.json
  ok "stack deployed; outputs written to variables.json"
fi

# ----------------------------------------------------------------------------
# 7. Ingest the catalog into the S3 Vectors index (creates the index)
# ----------------------------------------------------------------------------
if [[ "${SKIP_INGEST:-0}" == "1" ]]; then
  warn "SKIP_INGEST=1 — skipping ingest (the index must already exist)"
else
  log "Ingesting catalog into the S3 Vectors index (LIMIT=${INGEST_LIMIT:-all})"
  AWS_REGION="${PRIMARY_REGION}" LIMIT="${INGEST_LIMIT}" "${PY}" "${SCRIPT_DIR}/run_ingest.py"
  ok "ingest complete"
fi

# ----------------------------------------------------------------------------
# 8. Launch the local Streamlit app
# ----------------------------------------------------------------------------
if [[ "${SKIP_APP:-0}" == "1" ]]; then
  log "SKIP_APP=1 — not launching the UI."
  ok "Deploy complete. Start the app later with:"
  echo "    AWS_REGION=${PRIMARY_REGION} ${PY} -m streamlit run frontend/app.py"
else
  log "Launching Streamlit on http://localhost:${STREAMLIT_PORT}"
  echo "    (Ctrl-C to stop; re-run this script any time — it is idempotent.)"
  exec env AWS_REGION="${PRIMARY_REGION}" "${PY}" -m streamlit run frontend/app.py \
    --server.port "${STREAMLIT_PORT}"
fi
