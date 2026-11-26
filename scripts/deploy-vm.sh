#!/usr/bin/env bash
# Deploys the MCPX gateway stack (Caddy + tbxark/mcp-proxy) to the GCP VM over SSH.
# Bootstraps swap/Docker, builds the merged config via scripts/mcp-servers.sh when
# CONFIG_FILE is unset, syncs secrets from MCPS_ENV_FILE into ~/mcp-server/.env,
# starts Docker Compose, and runs live HTTPS and E2E verification.

set -euo pipefail

export PATH="$HOME/google-cloud-sdk/bin:$PATH"
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

GCP_PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
GCP_REGION="${GCP_REGION:-us-east1}"
GCP_ZONE="${GCP_ZONE:-${GCP_REGION}-b}"
VM_NAME="${VM_NAME:-mcpx-gateway}"
MCP_SUBDOMAIN="${MCP_SUBDOMAIN:-${DOMAIN:-}}"
: "${MCP_SUBDOMAIN:?Set MCP_SUBDOMAIN or DOMAIN (e.g. mcpx.duckdns.org)}"
MCP_BEARER_TOKEN="${MCP_BEARER_TOKEN:-}"
CONFIG_FILE="${CONFIG_FILE:-}"
MCPS_ENV_FILE="${MCPS_ENV_FILE:-}"
SKIP_DNS_CHECK="${SKIP_DNS_CHECK:-0}"
USE_IAP_SSH="${USE_IAP_SSH:-0}"

if [ -n "${CONFIG_FILE}" ]; then
  if [ ! -f "${CONFIG_FILE}" ]; then
    echo "ERROR: Config file not found: ${CONFIG_FILE}" >&2
    exit 1
  fi
  jq empty "${CONFIG_FILE}"
fi

if [ -n "${MCPS_ENV_FILE}" ]; then
  if [ ! -f "${MCPS_ENV_FILE}" ]; then
    echo "ERROR: MCPS_ENV_FILE not found: ${MCPS_ENV_FILE}" >&2
    exit 1
  fi
  if [ -z "${MCP_BEARER_TOKEN}" ]; then
    MCP_BEARER_TOKEN="$(grep -E '^(export[[:space:]]+)?MCP_BEARER_TOKEN=' "${MCPS_ENV_FILE}" | tail -n 1 | sed -E 's/^(export[[:space:]]+)?MCP_BEARER_TOKEN=//; s/\r$//; s/^["'\'']//; s/["'\'']$//' || true)"
  fi
fi

: "${MCP_BEARER_TOKEN:?Set MCP_BEARER_TOKEN (or provide it inside MCPS_ENV_FILE)}"

SSH_IAP_ARGS=()
if [ "${USE_IAP_SSH}" = "1" ]; then
  SSH_IAP_ARGS+=(--tunnel-through-iap)
fi

VM_IP="$(gcloud compute instances describe "${VM_NAME}" \
  --zone="${GCP_ZONE}" \
  --project="${GCP_PROJECT_ID}" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"

if [ -z "${VM_IP}" ]; then
  echo "ERROR: Could not determine external IP for VM ${VM_NAME}." >&2
  exit 1
fi

echo "==> VM ${VM_NAME} external IP: ${VM_IP}"

if [ "${SKIP_DNS_CHECK}" != "1" ]; then
  RESOLVED_IP="$(dig +short A "${MCP_SUBDOMAIN}" @1.1.1.1 | grep -E '^[0-9]+(\.[0-9]+){3}$' | head -n 1 || true)"
  echo "==> DNS check: ${MCP_SUBDOMAIN} -> ${RESOLVED_IP:-<none>}"
  if [ "${RESOLVED_IP}" != "${VM_IP}" ]; then
    echo "ERROR: DNS for ${MCP_SUBDOMAIN} resolves to '${RESOLVED_IP:-<none>}', expected '${VM_IP}'." >&2
    echo "Update your A record first so Let's Encrypt TLS succeeds on the first try." >&2
    exit 1
  fi
fi

ssh_vm() {
  gcloud compute ssh "${VM_NAME}" \
    --zone="${GCP_ZONE}" \
    --project="${GCP_PROJECT_ID}" \
    --quiet \
    "${SSH_IAP_ARGS[@]}" \
    --ssh-flag="-T -o StrictHostKeyChecking=accept-new" \
    --command="$1"
}

scp_vm() {
  gcloud compute scp \
    --zone="${GCP_ZONE}" \
    --project="${GCP_PROJECT_ID}" \
    --quiet \
    "${SSH_IAP_ARGS[@]}" \
    --scp-flag="-o StrictHostKeyChecking=accept-new" \
    "$@"
}

echo "==> Configuring 2 GB /swapfile and installing Docker on ${VM_NAME}..."
ssh_vm 'bash -s' <<'REMOTE_BOOTSTRAP'
set -euo pipefail

if ! sudo swapon --show | grep -q '/swapfile'; then
  if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
  fi
  sudo swapon /swapfile
fi

if ! grep -q '^/swapfile ' /etc/fstab; then
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true

mkdir -p "$HOME/mcp-server"
rm -rf "$HOME/mcp-server/servers.new" "$HOME/mcp-server/oauth.new"
mkdir -p "$HOME/mcp-server/servers.new" "$HOME/mcp-server/oauth.new"
REMOTE_BOOTSTRAP

echo "==> Preparing temporary .env and effective config locally (chmod 600)..."
TMP_ENV="$(mktemp /tmp/mcpx-deploy-env.XXXXXX)"
TMP_CONFIG=""
cleanup_tmp() {
  rm -f "${TMP_ENV}" "${TMP_CONFIG}"
}
trap cleanup_tmp EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
chmod 600 "${TMP_ENV}"

{
  printf 'MCP_SUBDOMAIN=%s\n' "${MCP_SUBDOMAIN}"
  if [ -n "${MCPS_ENV_FILE}" ]; then
    has_bearer_token=0
    while IFS= read -r raw_line || [ -n "${raw_line}" ]; do
      line="${raw_line%$'\r'}"
      case "${line}" in
        ""|\#*) continue ;;
      esac
      if [[ "${line}" =~ ^[[:space:]]*# ]] || [[ "${line}" =~ ^[[:space:]]*$ ]]; then
        continue
      fi
      line="${line#export }"
      if [[ "${line}" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
        key="${line%%=*}"
        if [ "${key}" = "MCP_SUBDOMAIN" ]; then
          continue
        fi
        if [ "${key}" = "MCP_BEARER_TOKEN" ]; then
          has_bearer_token=1
        fi
        printf '%s\n' "${line}"
      fi
    done < "${MCPS_ENV_FILE}"
    if [ "${has_bearer_token}" -eq 0 ]; then
      printf 'MCP_BEARER_TOKEN=%s\n' "${MCP_BEARER_TOKEN}"
    fi
  else
    echo "WARNING: MCPS_ENV_FILE is not set; only MCP_SUBDOMAIN and MCP_BEARER_TOKEN will be written to .env." >&2
    printf 'MCP_BEARER_TOKEN=%s\n' "${MCP_BEARER_TOKEN}"
  fi
} > "${TMP_ENV}"

if [ -n "${CONFIG_FILE}" ]; then
  EFFECTIVE_CONFIG_FILE="${CONFIG_FILE}"
else
  TMP_CONFIG="$(mktemp /tmp/mcpx-deploy-config.XXXXXX.json)"
  chmod 600 "${TMP_CONFIG}"
  bash "${SCRIPT_DIR}/mcp-servers.sh" build-config "${TMP_CONFIG}"
  EFFECTIVE_CONFIG_FILE="${TMP_CONFIG}"
fi

echo "==> Uploading gateway configs and local MCP servers to ~/mcp-server/..."
scp_vm \
  "${REPO_ROOT}/infra/docker-compose.yml" \
  "${REPO_ROOT}/infra/Caddyfile" \
  "${VM_NAME}:~/mcp-server/"

scp_vm \
  "${EFFECTIVE_CONFIG_FILE}" \
  "${VM_NAME}:~/mcp-server/config.json"

scp_vm \
  --scp-flag="-p" \
  "${TMP_ENV}" \
  "${VM_NAME}:~/mcp-server/.env"

if [ -d "${REPO_ROOT}/servers" ]; then
  SERVER_ENTRIES=()
  for entry in "${REPO_ROOT}/servers"/*; do
    if [ -e "${entry}" ]; then
      SERVER_ENTRIES+=("${entry}")
    fi
  done
  if [ "${#SERVER_ENTRIES[@]}" -gt 0 ]; then
    scp_vm \
      --recurse \
      "${SERVER_ENTRIES[@]}" \
      "${VM_NAME}:~/mcp-server/servers.new/"
  fi
fi

if [ -d "${REPO_ROOT}/infra/oauth" ]; then
  OAUTH_ENTRIES=()
  for entry in "${REPO_ROOT}/infra/oauth"/*; do
    if [ -e "${entry}" ]; then
      OAUTH_ENTRIES+=("${entry}")
    fi
  done
  if [ "${#OAUTH_ENTRIES[@]}" -gt 0 ]; then
    scp_vm \
      --recurse \
      "${OAUTH_ENTRIES[@]}" \
      "${VM_NAME}:~/mcp-server/oauth.new/"
  fi
fi

echo "==> Locking permissions, syncing servers/ and oauth/, validating config, and starting Docker Compose stack..."
ssh_vm 'bash -s' <<'REMOTE_START'
set -euo pipefail
cd "$HOME/mcp-server"
chmod 600 .env
chmod 644 docker-compose.yml Caddyfile config.json

rm -rf "$HOME/mcp-server/servers" "$HOME/mcp-server/oauth"
mv "$HOME/mcp-server/servers.new" "$HOME/mcp-server/servers"
mv "$HOME/mcp-server/oauth.new" "$HOME/mcp-server/oauth"

sudo docker compose pull </dev/null
sudo docker compose run -T --rm --no-deps mcp-proxy --config /config/config.json -check-config </dev/null
sudo docker compose up -d --force-recreate --remove-orphans </dev/null

echo "==> Verifying uv and uvx inside mcp-proxy container:"
sudo docker compose exec -T mcp-proxy uv --version </dev/null
sudo docker compose exec -T mcp-proxy uvx --version </dev/null

echo "==> Container status:"
sudo docker compose ps </dev/null
REMOTE_START

echo "==> Waiting for HTTPS TLS certificate & readiness on https://${MCP_SUBDOMAIN}/_readyz ..."
READY=0
for attempt in $(seq 1 24); do
  CODE="$(curl -sS --connect-timeout 5 --max-time 15 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${MCP_BEARER_TOKEN}" \
    "https://${MCP_SUBDOMAIN}/_readyz" 2>/dev/null)" || CODE="000"
  if [ "${CODE}" = "200" ]; then
    echo "    Ready! (HTTP 200)"
    READY=1
    break
  fi
  echo "    Attempt ${attempt}/24: HTTP ${CODE} (waiting for TLS/readiness...)"
  sleep 5
done

if [ "${READY}" -ne 1 ]; then
  echo "ERROR: Timed out waiting for https://${MCP_SUBDOMAIN}/_readyz to return HTTP 200." >&2
  echo "Inspect remote container logs with:" >&2
  echo "  gcloud compute ssh ${VM_NAME} --zone=${GCP_ZONE} --project=${GCP_PROJECT_ID} --command='cd ~/mcp-server && sudo docker compose logs --tail=50'" >&2
  exit 1
fi

echo "==> Verifying unauthorized request is blocked (expect 401):"
UNAUTH_CODE="$(curl -sS --connect-timeout 5 --max-time 15 -o /dev/null -w '%{http_code}' "https://${MCP_SUBDOMAIN}/_healthz")"
echo "    GET /_healthz without token -> HTTP ${UNAUTH_CODE}"
if [ "${UNAUTH_CODE}" != "401" ]; then
  echo "ERROR: Expected 401 without Bearer token, got ${UNAUTH_CODE}!" >&2
  exit 1
fi

echo "==> Verifying authorized health & readiness endpoints (expect 200):"
curl -fsS --connect-timeout 5 --max-time 15 -H "Authorization: Bearer ${MCP_BEARER_TOKEN}" "https://${MCP_SUBDOMAIN}/_healthz"
echo ""
curl -fsS --connect-timeout 5 --max-time 15 -H "Authorization: Bearer ${MCP_BEARER_TOKEN}" "https://${MCP_SUBDOMAIN}/_readyz"
echo ""

echo "==> Running full E2E integration tests & latency profiling..."
E2E_CMD=(python3 "${SCRIPT_DIR}/e2e-test.py" --iterations "${E2E_ITERATIONS:-5}")
if [ -n "${E2E_EXTRA_ARGS:-}" ]; then
  read -r -a EXTRA_ARR <<< "${E2E_EXTRA_ARGS}"
  if [ "${#EXTRA_ARR[@]}" -gt 0 ]; then
    E2E_CMD+=("${EXTRA_ARR[@]}")
  fi
fi
MCP_SUBDOMAIN="${MCP_SUBDOMAIN}" MCP_BEARER_TOKEN="${MCP_BEARER_TOKEN}" "${E2E_CMD[@]}"

echo "==> Deployment, E2E verification, and profiling complete!"
