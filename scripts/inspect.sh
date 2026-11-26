#!/usr/bin/env bash
# Client-side observability entrypoint for the deployed MCPX HTTPS gateway.
# Fits into MCPX alongside `scripts/mcp-servers.sh` (enabled server list) and
# `scripts/e2e-test.py` (automated E2E verification) to let operators inspect live
# tools via `--list` or launch Anthropic's interactive MCP Inspector web UI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage:
  scripts/inspect.sh [--list [server ...]]
  scripts/inspect.sh [server]

Modes:
  --list [server ...]   Query each enabled MCPX server over Streamable HTTP and
                        print its tools and descriptions (uses stdlib python3).
  [server]              Launch Anthropic's MCP Inspector web UI preconfigured
                        with all enabled MCPX servers and Bearer auth (default).

Environment:
  MCP_SUBDOMAIN / DOMAIN   Gateway hostname (e.g. mcpx.duckdns.org)
  MCP_BEARER_TOKEN         Gateway Bearer token
  AGENT_ENV_FILE           Optional path to mcpx.agent.env to source
  MCPS_ENV_FILE            Optional path to mcpx.mcps.env to source
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

if [ -n "${AGENT_ENV_FILE:-}" ]; then
  if [ ! -f "${AGENT_ENV_FILE}" ]; then
    echo "ERROR: AGENT_ENV_FILE not found: ${AGENT_ENV_FILE}" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "${AGENT_ENV_FILE}"
  set +a
fi

if [ -n "${MCPS_ENV_FILE:-}" ]; then
  if [ ! -f "${MCPS_ENV_FILE}" ]; then
    echo "ERROR: MCPS_ENV_FILE not found: ${MCPS_ENV_FILE}" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "${MCPS_ENV_FILE}"
  set +a
fi

MCP_SUBDOMAIN="${MCP_SUBDOMAIN:-${DOMAIN:-}}"
: "${MCP_SUBDOMAIN:?Set MCP_SUBDOMAIN or DOMAIN (or pass AGENT_ENV_FILE)}"
: "${MCP_BEARER_TOKEN:?Set MCP_BEARER_TOKEN (or pass MCPS_ENV_FILE)}"

BASE_URL="${MCP_SUBDOMAIN%/}"
if [[ ! "${BASE_URL}" =~ ^https?:// ]]; then
  BASE_URL="https://${BASE_URL}"
fi

load_enabled_servers() {
  local helper="${SCRIPT_DIR}/mcp-servers.sh"
  if [ -f "${helper}" ]; then
    bash "${helper}" list
  else
    printf '%s\n' context7 uv-time gemini-sound mcpx-logs
  fi
}

mapfile -t ENABLED_SERVERS < <(load_enabled_servers | sed '/^[[:space:]]*$/d')
if [ "${#ENABLED_SERVERS[@]}" -eq 0 ]; then
  echo "ERROR: No enabled servers found." >&2
  exit 1
fi

if [ "${1:-}" = "--list" ]; then
  shift
  if [ "$#" -gt 0 ]; then
    TARGET_SERVERS=("$@")
  else
    TARGET_SERVERS=("${ENABLED_SERVERS[@]}")
  fi

  BASE_URL="${BASE_URL}" MCP_BEARER_TOKEN="${MCP_BEARER_TOKEN}" \
    python3 - "${TARGET_SERVERS[@]}" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

base_url = os.environ["BASE_URL"].rstrip("/")
token = os.environ["MCP_BEARER_TOKEN"]
servers = sys.argv[1:]


def parse_mcp_body(raw: str) -> dict:
    text = raw.strip()
    if not text:
        return {}
    if text.startswith("{"):
        return json.loads(text)
    last = {}
    for line in text.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                try:
                    parsed = json.loads(chunk)
                    if isinstance(parsed, dict):
                        last = parsed
                except json.JSONDecodeError:
                    pass
    return last


def post_rpc(url: str, payload: dict, session_id: str | None) -> tuple[dict, str | None]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        new_session = resp.headers.get("Mcp-Session-Id") or session_id
        body = resp.read().decode("utf-8", errors="replace")
        return parse_mcp_body(body), new_session


failed = False
for server in servers:
    url = f"{base_url}/{server}/mcp"
    print(f"=== {server} ({url}) ===")
    try:
        _, sid = post_rpc(
            url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "mcpx-inspect", "version": "1.0.0"},
                },
            },
            None,
        )
        try:
            post_rpc(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        except urllib.error.HTTPError:
            pass

        resp, _ = post_rpc(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, sid)
        if "error" in resp:
            print(f"  ERROR: {resp['error']}")
            failed = True
            continue

        tools = resp.get("result", {}).get("tools", [])
        if not tools:
            print("  (no tools reported)")
        for tool in tools:
            name = tool.get("name", "<unnamed>")
            desc = " ".join(str(tool.get("description") or "").split())
            if len(desc) > 120:
                desc = desc[:117] + "..."
            print(f"  - {name}: {desc}" if desc else f"  - {name}")
    except Exception as exc:
        print(f"  ERROR: {exc}")
        failed = True
    print()

sys.exit(1 if failed else 0)
PY
  exit $?
fi

if ! command -v npx >/dev/null 2>&1; then
  echo "ERROR: 'npx' is required to launch the MCP Inspector UI (install Node.js 18+)." >&2
  exit 1
fi

SELECTED_SERVER="${1:-${ENABLED_SERVERS[0]}}"
CONFIG_SERVERS=("${ENABLED_SERVERS[@]}")
FOUND=0
for srv in "${CONFIG_SERVERS[@]}"; do
  if [ "${srv}" = "${SELECTED_SERVER}" ]; then
    FOUND=1
    break
  fi
done
if [ "${FOUND}" -eq 0 ]; then
  CONFIG_SERVERS+=("${SELECTED_SERVER}")
fi

TMP_CFG="$(mktemp /tmp/mcpx-inspector-config.XXXXXX.json)"
cleanup() {
  rm -f "${TMP_CFG}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
chmod 600 "${TMP_CFG}"

BASE_URL="${BASE_URL}" MCP_BEARER_TOKEN="${MCP_BEARER_TOKEN}" TMP_CFG="${TMP_CFG}" \
  python3 - "${CONFIG_SERVERS[@]}" <<'PY'
import json
import os
import sys

base_url = os.environ["BASE_URL"].rstrip("/")
token = os.environ["MCP_BEARER_TOKEN"]
out_path = os.environ["TMP_CFG"]
servers = sys.argv[1:]

config = {
    "mcpServers": {
        name: {
            "type": "streamable-http",
            "url": f"{base_url}/{name}/mcp",
            "headers": {
                "Authorization": f"Bearer {token}",
            },
        }
        for name in servers
    }
}

with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(config, fh, indent=2)
PY

echo "==> Launching MCP Inspector UI for '${SELECTED_SERVER}' (${BASE_URL}/${SELECTED_SERVER}/mcp)"
echo "    Configured servers: ${CONFIG_SERVERS[*]}"
npx --yes @modelcontextprotocol/inspector --web --config "${TMP_CFG}" --server "${SELECTED_SERVER}"
