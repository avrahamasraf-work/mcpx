#!/usr/bin/env bash
# Config builder and deployment entrypoint for MCP servers and tool groups.
# Discovers enabled servers in infra/servers/*.json and enabled groups in
# infra/groups/*.json (via each file's "enabled" flag), merges them with
# infra/config.json, validates group references, and delegates to deploy-vm.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BASE_CONFIG="${REPO_ROOT}/infra/config.json"
SERVERS_DIR="${REPO_ROOT}/infra/servers"
GROUPS_DIR="${REPO_ROOT}/infra/groups"

usage() {
  cat <<'EOF'
Usage: scripts/mcp-servers.sh <command> [args]

Build the merged mcpx-gateway config from static JSON files in infra/servers/
and infra/groups/ (controlled by each entry's "enabled" boolean, default true).

Commands:
  list                        Print enabled server names (one per line)
  list-groups                 Print enabled group names (one per line)
  build-config [output_path]  Merge infra/config.json + enabled infra/servers/*.json
                              + enabled infra/groups/*.json, validate group server
                              references, and write JSON to output_path or stdout
  start | deploy              Build config to a temp file and run scripts/deploy-vm.sh
  -h | --help                 Show this help message
EOF
}

collect_json_files() {
  local dir="$1"
  local file
  if [ -d "${dir}" ]; then
    for file in "${dir}"/*.json; do
      [ -f "${file}" ] || continue
      jq empty "${file}"
      printf '%s\n' "${file}"
    done
  fi
}

cmd_list() {
  local files=()
  local file
  while IFS= read -r file; do
    [ -n "${file}" ] && files+=("${file}")
  done < <(collect_json_files "${SERVERS_DIR}")

  if [ "${#files[@]}" -eq 0 ]; then
    return 0
  fi

  jq -rs '
    reduce .[] as $item ({}; . * ($item.servers // {})) |
    to_entries[] |
    select((.value.enabled // true) == true) |
    .key
  ' "${files[@]}"
}

cmd_list_groups() {
  local files=()
  local file
  while IFS= read -r file; do
    [ -n "${file}" ] && files+=("${file}")
  done < <(collect_json_files "${GROUPS_DIR}")

  if [ "${#files[@]}" -eq 0 ]; then
    return 0
  fi

  jq -rs '
    reduce .[] as $item ({}; . * ($item.groups // {})) |
    to_entries[] |
    select((.value.enabled // true) == true) |
    .key
  ' "${files[@]}"
}

cmd_build_config() {
  local output_path="${1:-}"
  if [ ! -f "${BASE_CONFIG}" ]; then
    echo "ERROR: Base config not found: ${BASE_CONFIG}" >&2
    exit 1
  fi
  jq empty "${BASE_CONFIG}"

  local files=("${BASE_CONFIG}")
  local file
  while IFS= read -r file; do
    [ -n "${file}" ] && files+=("${file}")
  done < <(collect_json_files "${SERVERS_DIR}")
  while IFS= read -r file; do
    [ -n "${file}" ] && files+=("${file}")
  done < <(collect_json_files "${GROUPS_DIR}")

  local merged
  merged="$(jq -s '
    reduce .[] as $item ({}; . * $item) |
    .servers = ((.servers // {}) | with_entries(select((.value.enabled // true) == true))) |
    .groups  = ((.groups  // {}) | with_entries(select((.value.enabled // true) == true)))
  ' "${files[@]}")"

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
