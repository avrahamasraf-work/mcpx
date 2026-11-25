#!/usr/bin/env bash
set -euo pipefail

# Idempotently provision GCP resources for the HTTPS-protected MCP gateway:
#   1. Configure active project & enable Compute Engine API
#   2. Reserve a regional static external IP
#   3. Ensure firewall rules for HTTP (80), HTTPS (443), and SSH (22)
#   4. Create an Ubuntu 24.04 LTS Always Free e2-micro VM with 30 GB pd-standard disk
#   5. Wait until SSH is ready and print the reserved static IP for DNS configuration

export PATH="$HOME/google-cloud-sdk/bin:$PATH"

GCP_PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID (e.g. from mcpx.env)}"
GCP_REGION="${GCP_REGION:-us-east1}"
GCP_ZONE="${GCP_ZONE:-${GCP_REGION}-b}"
VM_NAME="${VM_NAME:-mcpx-gateway}"
STATIC_IP_NAME="${STATIC_IP_NAME:-mcpx-gateway-ip}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-micro}"
DISK_SIZE="${DISK_SIZE:-30GB}"
DISK_TYPE="${DISK_TYPE:-pd-standard}"

echo "==> Setting GCP project: ${GCP_PROJECT_ID}"
gcloud config set project "${GCP_PROJECT_ID}" --quiet

echo "==> Ensuring Compute Engine API is enabled..."
gcloud services enable compute.googleapis.com --project="${GCP_PROJECT_ID}" --quiet

echo "==> Ensuring static external IP '${STATIC_IP_NAME}' in ${GCP_REGION}..."
if ! gcloud compute addresses describe "${STATIC_IP_NAME}" \
  --region="${GCP_REGION}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute addresses create "${STATIC_IP_NAME}" \
    --region="${GCP_REGION}" \
    --project="${GCP_PROJECT_ID}" \
    --quiet
fi

STATIC_IP="$(gcloud compute addresses describe "${STATIC_IP_NAME}" \
  --region="${GCP_REGION}" \
  --project="${GCP_PROJECT_ID}" \
  --format='get(address)')"
echo "    Static External IP: ${STATIC_IP}"

echo "==> Ensuring firewall rules (HTTP 80, HTTPS 443, SSH 22)..."
if ! gcloud compute firewall-rules describe default-allow-http --project="${GCP_PROJECT_ID}" >/dev/null 2>&1 \
  && ! gcloud compute firewall-rules describe mcpx-allow-http --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute firewall-rules create mcpx-allow-http \
    --project="${GCP_PROJECT_ID}" \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=tcp:80 \
    --source-ranges=0.0.0.0/0 \
    --target-tags=http-server \
    --quiet
fi

if ! gcloud compute firewall-rules describe default-allow-https --project="${GCP_PROJECT_ID}" >/dev/null 2>&1 \
  && ! gcloud compute firewall-rules describe mcpx-allow-https --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute firewall-rules create mcpx-allow-https \
    --project="${GCP_PROJECT_ID}" \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=tcp:443,udp:443 \
    --source-ranges=0.0.0.0/0 \
    --target-tags=https-server \
    --quiet
fi

if ! gcloud compute firewall-rules describe default-allow-ssh --project="${GCP_PROJECT_ID}" >/dev/null 2>&1 \
  && ! gcloud compute firewall-rules describe mcpx-allow-ssh --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute firewall-rules create mcpx-allow-ssh \
    --project="${GCP_PROJECT_ID}" \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges=0.0.0.0/0 \
    --quiet
fi

echo "==> Ensuring VM '${VM_NAME}' (${MACHINE_TYPE}, ${DISK_SIZE} ${DISK_TYPE}) in ${GCP_ZONE}..."
if ! gcloud compute instances describe "${VM_NAME}" \
  --zone="${GCP_ZONE}" --project="${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute instances create "${VM_NAME}" \
    --project="${GCP_PROJECT_ID}" \
    --zone="${GCP_ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size="${DISK_SIZE}" \
    --boot-disk-type="${DISK_TYPE}" \
    --address="${STATIC_IP}" \
    --tags=http-server,https-server \
    --quiet
else
  echo "    VM '${VM_NAME}' already exists."
fi

echo "==> Waiting for SSH to become ready on '${VM_NAME}'..."
for attempt in $(seq 1 30); do
  if gcloud compute ssh "${VM_NAME}" \
    --zone="${GCP_ZONE}" \
    --project="${GCP_PROJECT_ID}" \
    --quiet \
    --ssh-flag="-o ConnectTimeout=10 -o StrictHostKeyChecking=no" \
    --command="echo ssh-ready" >/dev/null 2>&1; then
    echo "    SSH is ready!"
    break
  fi
  if [ "${attempt}" -eq 30 ]; then
    echo "ERROR: Timed out waiting for SSH on ${VM_NAME}." >&2
    exit 1
  fi
  sleep 5
done

echo ""
echo "============================================================"
echo "GCP VM provisioned successfully!"
echo "  Project:   ${GCP_PROJECT_ID}"
echo "  Zone:      ${GCP_ZONE}"
echo "  VM Name:   ${VM_NAME}"
echo "  Static IP: ${STATIC_IP}"
echo "============================================================"
echo "Next step: point your DNS A record (e.g. ${MCP_SUBDOMAIN:-mcpx.duckdns.org}) to ${STATIC_IP}"
echo "and verify with: dig +short ${MCP_SUBDOMAIN:-mcpx.duckdns.org}"
