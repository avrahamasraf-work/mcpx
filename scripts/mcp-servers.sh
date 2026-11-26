#!/usr/bin/env bash
# Registry and config builder for MCP servers and tool groups in mcpx-gateway.
# Declares enabled servers and groups, merges infra/config.json with each
# infra/servers/<name>.json and infra/groups/<name>.json, and delegates deploy.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BASE_CONFIG="${REPO_ROOT}/infra/config.json"
SERVERS_DIR="${REPO_ROOT}/infra/servers"
GROUPS_DIR="${REPO_ROOT}/infra/groups"

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

# Single source of truth for which tool groups in infra/groups/ are deployed.
# Override at runtime with space-separated MCP_GROUPS="ops" or MCP_GROUPS="none".
ENABLED_GROUPS=(
  ops
)

if [ -n "${MCP_GROUPS:-}" ]; then
  if [ "${MCP_GROUPS}" = "none" ]; then
    ENABLED_GROUPS=()
  else
    read -r -a ENABLED_GROUPS <<< "${MCP_GROUPS}"
  fi
fi

usage() {
  cat <<'EOF'
Usage: scripts/mcp-servers.sh <command> [args]

Manage enabled MCP servers and tool groups and build the merged mcpx-gateway config.

Commands:
  list                        Print enabled server names (one per line)
  list-groups                 Print enabled group names (one per line)
  build-config [output_path]  Merge infra/config.json + infra/servers/<name>.json
                              + infra/groups/<name>.json, validate group server
                              references, and write JSON to output_path or stdout
  start | deploy              Build config to a temp file and run scripts/deploy-vm.sh
  -h | --help                 Show this help message

Environment:
  MCP_SERVERS                 Optional space-separated list overriding ENABLED_SERVERS
  MCP_GROUPS                  Optional space-separated list overriding ENABLED_GROUPS
                              (use "none" to disable all groups)
EOF
}

cmd_list() {
  local name
  for name in "${ENABLED_SERVERS[@]}"; do
    printf '%s\n' "${name}"
  done
}

cmd_list_groups() {
  local name
  if [ "${#ENABLED_GROUPS[@]}" -gt 0 ]; then
    for name in "${ENABLED_GROUPS[@]}"; do
      printf '%s\n' "${name}"
    done
  fi
}

cmd_build_config() {
  local output_path="${1:-}"
  if [ ! -f "${BASE_CONFIG}" ]; then
    echo "ERROR: Base config not found: ${BASE_CONFIG}" >&2
    exit 1
  fi
  jq empty "${BASE_CONFIG}"

  local files=("${BASE_CONFIG}")
  local name server_file group_file
  for name in "${ENABLED_SERVERS[@]}"; do
    server_file="${SERVERS_DIR}/${name}.json"
    if [ ! -f "${server_file}" ]; then
      echo "ERROR: Server config not found: ${server_file}" >&2
      exit 1
    fi
    jq empty "${server_file}"
    files+=("${server_file}")
  done

  if [ "${#ENABLED_GROUPS[@]}" -gt 0 ]; then
    for name in "${ENABLED_GROUPS[@]}"; do
      group_file="${GROUPS_DIR}/${name}.json"
      if [ ! -f "${group_file}" ]; then
        echo "ERROR: Group config not found: ${group_file}" >&2
        exit 1
      fi
      jq empty "${group_file}"
      files+=("${group_file}")
    done
  fi

  local merged
  merged="$(jq -s 'reduce .[] as $item ({}; . * $item)' "${files[@]}")"

  local missing
  missing="$(printf '%s\n' "${merged}" | jq -r '
    (.servers // {}) as $servers |
    (.groups // {}) | to_entries[] |
    .key as $group |
    (.value.servers // {}) | keys[] |
    . as $srv |
    select(($servers | has($srv)) | not) |
    "\($group):\($srv)"
  ')"
  if [ -n "${missing}" ]; then
    echo "ERROR: Group config references unknown or disabled server(s): ${missing}" >&2
    exit 1
  fi

  if [ -n "${output_path}" ]; then
    printf '%s\n' "${merged}" > "${output_path}"
    jq empty "${output_path}"
  else
    printf '%s\n' "${merged}"
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
  list-groups)
    cmd_list_groups
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
