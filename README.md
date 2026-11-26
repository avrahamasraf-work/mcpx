# MCPX — Remote MCP Gateway on GCP

MCPX is a self-hosted, HTTPS-protected Model Context Protocol (MCP) gateway running on a GCP Always Free `e2-micro` VM. Powered by `mcpx-gateway` — a custom Python gateway built on the official `mcp` Python SDK, Starlette, and Uvicorn (`python:3.13-slim` with `uv`/`uvx` and Node.js 22/`npx`) — it aggregates remote and local `uv`-based MCP servers over stateless Streamable HTTP behind automatic Let's Encrypt TLS, supporting both **MCP Authorization (OAuth 2.1)** for web/mobile AI apps (Gemini, Claude.ai, ChatGPT) and static Bearer token auth for CLI/IDE clients.

## Architecture

```text
  MCP Client / Gemini / Claude.ai / ChatGPT / MCP Inspector
                      │
                      │ HTTPS + OAuth 2.1 JWT or Bearer <MCP_BEARER_TOKEN>
                      ▼
       Caddy (ports 80/443, Let's Encrypt TLS, forward_auth)
          │                                     │
          │ /.well-known/*, /oauth/*            │ /mcp, /<server>/mcp, /groups/<group>/mcp
          ▼                                     ▼
   mcpx-oauth (:9100)              mcpx-gateway (:9000, stateless Streamable HTTP)
   (OAuth 2.1 + PKCE + DCR)          ┌──────────┼──────────┬────────────┐
                                     ▼          ▼          ▼            ▼
                                 context7    uv-time  gemini-sound  mcpx-logs
                               (remote HTTP)(uvx stdio)(uv stdio)   (uv stdio)
                                                           │            │
                                                           └─► /var/log/mcpx ◄─┘
                                                              (shared log volume)
```

### Endpoints

`mcpx-gateway` exposes three Streamable HTTP endpoint modes behind Caddy:

| Mode | URL | Behavior |
| :--- | :--- | :--- |
| **Unified** | `https://<MCP_SUBDOMAIN>/mcp` | All enabled servers combined with `<server>__<tool>` namespaced tools and prompts |
| **Per-server** | `https://<MCP_SUBDOMAIN>/<server-name>/mcp` | Single upstream server with its original unprefixed tool names |
| **Group** | `https://<MCP_SUBDOMAIN>/groups/<group-name>/mcp` | Curated `<server>__<tool>` allow-list defined in `infra/groups/<group-name>.json` (e.g., `ops`) |

### Included MCP Servers

| Server | Type | Purpose & Key Tools |
| :--- | :--- | :--- |
| `context7` | Remote Streamable HTTP | Up-to-date library documentation (`resolve-library-id`, `query-docs`) |
| `uv-time` | `uvx` stdio | Current time & timezone conversion (`get_current_time`, `convert_time`) |
| `gemini-sound` | Local `uv` stdio | Gemini text-to-speech WAV audio generation (`gemini_generate_sound`, `gemini_sound_list_voices`; default model `gemini-2.5-flash-preview-tts`) |
| `mcpx-logs` | Local `uv` stdio | Gateway & server log inspection (`list_log_sources`, `get_logs`) |

---

## Controlling MCPX

### 1. Manage MCP Servers & Tool Groups

Each MCP server is defined in `infra/servers/<name>.json` and each tool group is defined in `infra/groups/<name>.json`.
Every file is the single source of truth for its entry—toggle `"enabled": true` or `"enabled": false` directly in the JSON file (defaults to `true` if omitted):

- **List enabled servers / groups:**
  ```bash
  bash scripts/mcp-servers.sh list
  bash scripts/mcp-servers.sh list-groups
  ```
- **Preview merged gateway config:**
  ```bash
  bash scripts/mcp-servers.sh build-config
  ```
- **Enable or disable a server or group:** set `"enabled": true` or `"enabled": false` in `infra/servers/<name>.json` or `infra/groups/<name>.json`, then redeploy.
- **Add a new server:**
  1. Create `infra/servers/<name>.json` with `"enabled": true` and its `mcpx-gateway` server definition under `"servers"` (remote `url` + `transportType`, or `uv`/`uvx` stdio `command` + `args`).
  2. If it is a repo-hosted Python server, place the script in `servers/<name>/server.py` (PEP 723 inline metadata) and invoke it via `uv run --script /servers/<name>/server.py`.
  3. Reference any required secrets via `${ENV_VAR}` in the JSON and store the real values in `mcpx.mcps.env`.
  4. Redeploy.
- **Add a new tool group:**
  1. Create `infra/groups/<group-name>.json` with `"enabled": true` mapping enabled server names to either `"*"` (all tools) or an explicit list of tool names:
     ```json
     {
       "groups": {
         "ops": {
           "enabled": true,
           "description": "Operations tools (current time and gateway/server logs)",
           "servers": {
             "uv-time": ["get_current_time"],
             "mcpx-logs": "*"
           }
         }
       }
     }
     ```
  2. Redeploy.

### 2. Manage Secrets (`mcpx.mcps.env`)

Never commit secrets to git. All MCP gateway and server tokens live in `mcpx.mcps.env` (stored in Google Drive folder `mcpx/` alongside `mcpx.agent.env` and `mcpx-agent-key.json`):

- `MCP_BEARER_TOKEN` — required Bearer token enforced by both Caddy (`mcpx-oauth` `forward_auth`) and `mcpx-gateway`.
- `GEMINI_TOKEN` (or `GEMINI_API_KEY`) — API key used by `gemini-sound`.
- Any additional tokens referenced via `${ENV_VAR}` by custom servers in `infra/servers/<name>.json`.

### 3. Deploy to GCP

With `gcloud` authenticated and `mcpx.agent.env` loaded (for `GCP_PROJECT_ID` and `DOMAIN` / `MCP_SUBDOMAIN`):

```bash
MCPS_ENV_FILE=/path/to/mcpx.mcps.env bash scripts/mcp-servers.sh deploy
```

This merges `infra/config.json` with the active `infra/servers/<name>.json` and `infra/groups/<name>.json` definitions, uploads `infra/gateway/`, configs, and `servers/` to the VM, builds and restarts Docker Compose, and runs `scripts/e2e-test.py`.

To also run a live Gemini audio generation check during deployment:

```bash
MCPS_ENV_FILE=/path/to/mcpx.mcps.env E2E_EXTRA_ARGS="--call-gemini-sound" bash scripts/mcp-servers.sh deploy
```

### 4. Client-Side Observability (`scripts/inspect.sh`)

Inspect the live gateway directly from your terminal or browser:

- **Print every enabled server's tools and descriptions (CLI catalog):**
  ```bash
  AGENT_ENV_FILE=/path/to/mcpx.agent.env MCPS_ENV_FILE=/path/to/mcpx.mcps.env \
    bash scripts/inspect.sh --list
  ```
- **Launch Anthropic's interactive MCP Inspector web UI:**
  ```bash
  AGENT_ENV_FILE=/path/to/mcpx.agent.env MCPS_ENV_FILE=/path/to/mcpx.mcps.env \
    bash scripts/inspect.sh              # opens with all enabled servers
  bash scripts/inspect.sh gemini-sound   # opens focused on gemini-sound
  ```
  `scripts/inspect.sh` writes a `chmod 600` temporary config with Bearer auth, launches `@modelcontextprotocol/inspector`, and deletes the temporary file on exit.

### 5. View Logs

- **Via MCP (`mcpx-logs`):** from any connected MCP client or MCP Inspector, call:
  - `list_log_sources` — lists available log files under `/var/log/mcpx` (`mcpx-gateway`, `caddy`, `mcpx-oauth`, `mcpx-logs`, `gemini-sound`, etc.).
  - `get_logs(source, lines, search)` — tails and optionally filters recent log lines (gateway logs write to `/var/log/mcpx/mcpx-gateway.log`).
- **Via SSH (Docker Compose logs):**
  ```bash
  gcloud compute ssh mcpx-gateway --zone=us-east1-b --project="$GCP_PROJECT_ID" \
    --command='cd ~/mcp-server && sudo docker compose logs --tail=100'
  ```

### 6. Connect Any MCP Client

Choose whichever Streamable HTTP endpoint fits your client:

- **Unified (all enabled servers, `<server>__<tool>` namespaced):** `https://<MCP_SUBDOMAIN>/mcp`
- **Per-server (original unprefixed tool names):** `https://<MCP_SUBDOMAIN>/<server-name>/mcp` (for example, `https://mcpx.duckdns.org/uv-time/mcp`)
- **Group (curated `<server>__<tool>` allow-list):** `https://<MCP_SUBDOMAIN>/groups/<group-name>/mcp` (for example, `https://mcpx.duckdns.org/groups/ops/mcp`)

#### Option A — OAuth 2.1 (Gemini Custom Connected Apps, Claude.ai, ChatGPT)
1. Paste `https://<MCP_SUBDOMAIN>/mcp`, `https://<MCP_SUBDOMAIN>/<server-name>/mcp`, or `https://<MCP_SUBDOMAIN>/groups/<group-name>/mcp` as the MCP Server URL.
2. Leave Client ID / Client Secret blank to use Dynamic Client Registration (RFC 7591), **or** enter the optional pre-registered `MCP_OAUTH_CLIENT_ID` and `MCP_OAUTH_CLIENT_SECRET` configured in `mcpx.mcps.env`.
3. When redirected to the MCPX consent screen, enter your `MCP_OAUTH_PASSWORD` (defaults to `MCP_BEARER_TOKEN`) and click **Authorize**.

#### Option B — Static Bearer Token (CLIs, IDEs, MCP Inspector)
- **Header:** `Authorization: Bearer <MCP_BEARER_TOKEN>`

```json
{
  "mcpServers": {
    "mcpx-all": {
      "type": "streamable-http",
      "url": "https://mcpx.duckdns.org/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_BEARER_TOKEN>"
      }
    },
    "mcpx-ops": {
      "type": "streamable-http",
      "url": "https://mcpx.duckdns.org/groups/ops/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_BEARER_TOKEN>"
      }
    },
    "context7": {
      "type": "streamable-http",
      "url": "https://mcpx.duckdns.org/context7/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_BEARER_TOKEN>"
      }
    }
  }
}
```
