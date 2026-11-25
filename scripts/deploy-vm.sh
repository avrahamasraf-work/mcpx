#!/usr/bin/env bash
set -euo pipefail

# Provision the VM over SSH and deploy the Caddy + tbxark/mcp-proxy stack:
#   1. Verify DNS A record resolves to the VM's static external IP
#   2. Create and enable a 2 GB /swapfile persisted in /etc/fstab
#   3. Install Docker and Docker Compose plugin
#   4. Upload infra files (docker-compose.yml, Caddyfile, config.json) + servers/ + secure .env
#   5. Validate mcp-proxy config, start containers, verify uv works inside mcp-proxy,
#      and test HTTPS auth (401 without token, 200 with Bearer token)

export PATH="$HOME/google-cloud-sdk/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

GCP_PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
GCP_REGION="${GCP_REGION:-us-east1}"
GCP_ZONE="${GCP_ZONE:-${GCP_REGION}-b}"
VM_NAME="${VM_NAME:-mcpx-gateway}"
MCP_SUBDOMAIN="${MCP_SUBDOMAIN:-${DOMAIN:-}}"
: "${MCP_SUBDOMAIN:?Set MCP_SUBDOMAIN or DOMAIN (e.g. mcpx.duckdns.org)}"
MCP_BEARER_TOKEN="${MCP_BEARER_TOKEN:?Set MCP_BEARER_TOKEN}"
CONFIG_FILE="${CONFIG_FILE:-${REPO_ROOT}/infra/config.json}"
SKIP_DNS_CHECK="${SKIP_DNS_CHECK:-0}"

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
    --ssh-flag="-T -o StrictHostKeyChecking=no" \
    --command="$1"
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

mkdir -p "$HOME/mcp-server/servers"
REMOTE_BOOTSTRAP

echo "==> Preparing temporary .env file locally (chmod 600)..."
GEMINI_API_KEY="${GEMINI_API_KEY:-${GEMINI_TOKEN:-}}"
TMP_ENV="$(mktemp /tmp/mcpx-deploy-env.XXXXXX)"
trap 'rm -f "${TMP_ENV}"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
chmod 600 "${TMP_ENV}"
{
  printf 'MCP_SUBDOMAIN=%s\n' "${MCP_SUBDOMAIN}"
  printf 'MCP_BEARER_TOKEN=%s\n' "${MCP_BEARER_TOKEN}"
  if [ -n "${GEMINI_API_KEY:-}" ]; then
    printf 'GEMINI_API_KEY=%s\n' "${GEMINI_API_KEY}"
    printf 'GEMINI_DEFAULT_MODEL=%s\n' "${GEMINI_DEFAULT_MODEL:-gemini-3.8-flash}"
  fi
} > "${TMP_ENV}"

echo "==> Uploading gateway configs and local MCP servers to ~/mcp-server/..."
gcloud compute scp \
  --zone="${GCP_ZONE}" \
  --project="${GCP_PROJECT_ID}" \
  --quiet \
  "${REPO_ROOT}/infra/docker-compose.yml" \
  "${REPO_ROOT}/infra/Caddyfile" \
  "${VM_NAME}:~/mcp-server/"

gcloud compute scp \
  --zone="${GCP_ZONE}" \
  --project="${GCP_PROJECT_ID}" \
  --quiet \
  "${CONFIG_FILE}" \
  "${VM_NAME}:~/mcp-server/config.json"

gcloud compute scp \
  --zone="${GCP_ZONE}" \
  --project="${GCP_PROJECT_ID}" \
  --quiet \
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
    gcloud compute scp \
      --zone="${GCP_ZONE}" \
      --project="${GCP_PROJECT_ID}" \
      --quiet \
      --recurse \
      "${SERVER_ENTRIES[@]}" \
      "${VM_NAME}:~/mcp-server/servers/"
  fi
fi

echo "==> Locking permissions, validating config, and starting Docker Compose stack..."
ssh_vm 'bash -s' <<'REMOTE_START'
set -euo pipefail
cd "$HOME/mcp-server"
chmod 600 .env
chmod 644 docker-compose.yml Caddyfile config.json

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
if jq -e '.mcpServers.gemini' "${CONFIG_FILE}" >/dev/null 2>&1; then
  E2E_CMD+=(--include-gemini)
fi
if [ -n "${E2E_EXTRA_ARGS:-}" ]; then
  read -r -a EXTRA_ARR <<< "${E2E_EXTRA_ARGS}"
  if [ "${#EXTRA_ARR[@]}" -gt 0 ]; then
    E2E_CMD+=("${EXTRA_ARR[@]}")
  fi
fi
MCP_SUBDOMAIN="${MCP_SUBDOMAIN}" MCP_BEARER_TOKEN="${MCP_BEARER_TOKEN}" "${E2E_CMD[@]}"

echo "==> Deployment, E2E verification, and profiling complete!"
