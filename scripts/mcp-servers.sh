#!/usr/bin/env bash
# Registry and config builder for MCP servers deployed to the MCPX gateway.
# Declares the enabled server list, merges infra/config.json with each
# infra/servers/<name>.json definition, and delegates deployment to deploy-vm.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BASE_CONFIG="${REPO_ROOT}/infra/config.json"
SERVERS_DIR="${REPO_ROOT}/infra/servers"

# Single source of truth for which servers in infra/servers/ are deployed.
# Override at runtime with space-separated MCP_SERVERS="context7 uv-time".
ENABLED_SERVERS=(
  context7
  uv-time
  gemini-sound
  mcpx-logs
)

if [ -n "${MCP_SERVERS:-}" ]; then
  read -r -a ENABLED_SERVERS <<< "${MCP_SERVERS}"
fi

usage() {
  cat <<'EOF'
Usage: scripts/mcp-servers.sh <command> [args]

Manage enabled MCP servers and build the merged mcp-proxy config.

Commands:
  list                        Print enabled server names (one per line)
  build-config [output_path]  Merge infra/config.json + infra/servers/<name>.json
                              and write validated JSON to output_path or stdout
  start | deploy              Build config to a temp file and run scripts/deploy-vm.sh
  -h | --help                 Show this help message

Environment:
  MCP_SERVERS                 Optional space-separated list overriding ENABLED_SERVERS
EOF
}

cmd_list() {
  local name
  for name in "${ENABLED_SERVERS[@]}"; do
    printf '%s\n' "${name}"
  done
}

cmd_build_config() {
  local output_path="${1:-}"
  if [ ! -f "${BASE_CONFIG}" ]; then
    echo "ERROR: Base config not found: ${BASE_CONFIG}" >&2
    exit 1
  fi

  local files=("${BASE_CONFIG}")
  local name server_file
  for name in "${ENABLED_SERVERS[@]}"; do
    server_file="${SERVERS_DIR}/${name}.json"
    if [ ! -f "${server_file}" ]; then
      echo "ERROR: Server config not found: ${server_file}" >&2
      exit 1
    fi
    jq empty "${server_file}"
    files+=("${server_file}")
  done

  if [ -n "${output_path}" ]; then
    jq -s 'reduce .[] as $item ({}; . * $item)' "${files[@]}" > "${output_path}"
    jq empty "${output_path}"
  else
    jq -s 'reduce .[] as $item ({}; . * $item)' "${files[@]}"
  fi
}

TMP_CONFIG=""
cleanup_tmp_config() {
  if [ -n "${TMP_CONFIG}" ]; then
    rm -f "${TMP_CONFIG}"
  fi
}

cmd_deploy() {
  TMP_CONFIG="$(mktemp /tmp/mcpx-config.XXXXXX.json)"
  trap cleanup_tmp_config EXIT
  chmod 600 "${TMP_CONFIG}"

  cmd_build_config "${TMP_CONFIG}"
  CONFIG_FILE="${TMP_CONFIG}" bash "${SCRIPT_DIR}/deploy-vm.sh" "$@"
}

COMMAND="${1:-}"
case "${COMMAND}" in
  list)
    cmd_list
    ;;
  build-config)
    shift
    cmd_build_config "${1:-}"
    ;;
  start|deploy)
    shift
    cmd_deploy "$@"
    ;;
  -h|--help|"")
    usage
    if [ -z "${COMMAND}" ]; then
      exit 1
    fi
    ;;
  *)
    echo "ERROR: Unknown command '${COMMAND}'." >&2
    usage >&2
    exit 1
    ;;
esac
