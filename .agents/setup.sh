#!/usr/bin/env bash
set -euo pipefail

# Ensure non-interactive bash shells execute full ~/.bashrc exports.
if [ -f "$HOME/.bashrc" ]; then
  sed -i '/case \$- in/,/esac/d' "$HOME/.bashrc" 2>/dev/null || true
fi

# Ensure core CLI tools required for GCP VM provisioning, DNS checks, and TLS verification exist.
missing_pkgs=()
command -v dig >/dev/null 2>&1 || missing_pkgs+=(dnsutils)
command -v nslookup >/dev/null 2>&1 || missing_pkgs+=(dnsutils)
command -v jq >/dev/null 2>&1 || missing_pkgs+=(jq)
command -v curl >/dev/null 2>&1 || missing_pkgs+=(curl)
command -v openssl >/dev/null 2>&1 || missing_pkgs+=(openssl)
command -v ssh >/dev/null 2>&1 || missing_pkgs+=(openssh-client)

if [ "${#missing_pkgs[@]}" -gt 0 ]; then
  # Deduplicate package list.
  mapfile -t unique_pkgs < <(printf '%s\n' "${missing_pkgs[@]}" | sort -u)
  sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    ca-certificates "${unique_pkgs[@]}"
fi

# Ensure PyYAML is available to the active python3 for validating generated docker-compose.yml files.
if ! python3 -c "import yaml" >/dev/null 2>&1; then
  if python3 -m pip --version >/dev/null 2>&1; then
    python3 -m pip install --user --quiet pyyaml 2>/dev/null \
      || python3 -m pip install --user --break-system-packages --quiet pyyaml
  else
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends python3-yaml
  fi
fi

# Install Google Cloud CLI into $HOME/google-cloud-sdk so it persists across idle/wake cycles.
if [ ! -x "$HOME/google-cloud-sdk/bin/gcloud" ]; then
  arch="$(uname -m)"
  case "$arch" in
    x86_64) gcloud_arch="x86_64" ;;
    aarch64|arm64) gcloud_arch="arm" ;;
    *)
      echo "Unsupported architecture for Google Cloud CLI: $arch" >&2
      exit 1
      ;;
  esac

  tmp_tar="$(mktemp /tmp/google-cloud-cli.XXXXXX.tar.gz)"
  trap 'rm -f "$tmp_tar"' EXIT

  curl -fsSL \
    "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-${gcloud_arch}.tar.gz" \
    -o "$tmp_tar"
  rm -rf "$HOME/google-cloud-sdk"
  tar -xzf "$tmp_tar" -C "$HOME"
  "$HOME/google-cloud-sdk/install.sh" \
    --quiet \
    --usage-reporting=false \
    --path-update=false \
    --command-completion=false
fi

# Persist Google Cloud CLI on PATH idempotently in ~/.bashrc.
touch "$HOME/.bashrc"
path_line='export PATH="$HOME/google-cloud-sdk/bin:$PATH"'
grep -qxF "$path_line" "$HOME/.bashrc" || echo "$path_line" >> "$HOME/.bashrc"
export PATH="$HOME/google-cloud-sdk/bin:$PATH"

# Verify required tools are ready.
gcloud --version >/dev/null
dig -v >/dev/null 2>&1
openssl version >/dev/null
jq --version >/dev/null
python3 -c "import yaml"
