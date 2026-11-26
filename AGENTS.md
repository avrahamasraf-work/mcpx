# AGENTS.md — MCPX Remote MCP Gateway on GCP

This repository manages a remote, HTTPS-protected Model Context Protocol (MCP) gateway hosted on Google Cloud Platform (GCP).

## Architecture Overview

- **Cloud Provider & Project:** GCP (`GCP_PROJECT_ID` stored in private `mcpx.agent.env` inside Google Drive folder `mcpx/`; currently `mcpx-515813`).
- **Compute Tier:** GCP Always Free `e2-micro` VM (`us-east1` / `us-east1-b`, Ubuntu 24.04 LTS, 30 GB `pd-standard` boot disk, 2 GB `/swapfile`).
- **Networking & TLS:**
  - Regional static external IP attached to the VM.
  - Firewall allows inbound TCP `80`, TCP/UDP `443` (`http-server`, `https-server` tags), and SSH (`22`, `mcpx-ssh` tag scoped to `SSH_SOURCE_RANGES`, defaulting to IAP `35.235.240.0/20`).
  - Domain: `mcpx.duckdns.org` (DuckDNS `A` record pointing to the static IP).
  - Reverse Proxy & Auth: `caddy:2.11.9-alpine` provides automatic Let's Encrypt HTTPS, routes OAuth 2.1 endpoints (`/.well-known/*`, `/oauth/*`) to `mcpx-oauth:9100`, and guards all MCP/health routes via `forward_auth` (`mcpx-oauth` accepts either static `MCP_BEARER_TOKEN` or OAuth 2.1 PKCE JWTs, returning `401` with RFC 9728 `WWW-Authenticate: Bearer resource_metadata=...` otherwise).
- **MCP Gateway:** `mcpx-gateway` — custom Python MCP gateway built on the official `mcp` Python SDK, Starlette, and Uvicorn (`infra/gateway/`, `python:3.13-slim` image with `uv`/`uvx` and Node.js 22/`npx`) running on internal Docker network port `9000` (never exposed directly to the public internet).
- **Enabled Servers & Groups (static config):**
  - Servers (`infra/servers/<name>.json`, controlled by `"enabled": true|false`): `context7`, `uv-time`, `gemini-sound`, `mcpx-logs`.
  - Groups (`infra/groups/<name>.json`, controlled by `"enabled": true|false`): `ops`.
- **Transport & Endpoints (stateless `streamable-http`):**
  - **Unified:** `https://<MCP_SUBDOMAIN>/mcp` — aggregates all enabled servers with `<server>__<tool>` namespaced tools and prompts.
  - **Per-server:** `https://<MCP_SUBDOMAIN>/<server-name>/mcp` — exposes a single upstream server with original unprefixed tool names.
  - **Group:** `https://<MCP_SUBDOMAIN>/groups/<group-name>/mcp` — exposes a curated `<server>__<tool>` allow-list defined in `infra/groups/<group-name>.json` (e.g., starter group `ops`).
- **Observability:** Each gateway service writes to its own log file under `/var/log/mcpx/` (`caddy.log`, `mcpx-gateway.log`, `mcpx-oauth.log`), and each repo-hosted local server writes to its own separate rotating file under `/var/log/mcpx/servers/<server-name>.log`; all are queryable over MCP via `mcpx-logs` (`list_log_sources`, `get_logs`), and client-side inspection uses `scripts/inspect.sh` (`--list` or Anthropic's MCP Inspector web UI).

## Repository Layout

- `README.md` — Human-oriented overview, architecture flow, server/group management, observability, and client connection guide.
- `.agents/environment.json`, `.agents/setup.sh` — Cloud agent session bootstrap (installs `gcloud`, `dig`, `nslookup`, `jq`, `openssl`, `pyyaml`).
- `.agents/skills/` — Repo-local agent skills:
  - `coding-principles` — Clarity, 3-4 line file-context headers, uniform naming, live execution, no backward-compatibility cruft.
  - `testing` — Integration, CLI, and E2E testing over trivial unit tests.
  - `grow-autonomy` — Improving agent instructions and system structure from root causes.
  - `audit-agent-context` — Read-only audit of repo instructions, skills, and context quality (manual invocation).
- `infra/docker-compose.yml` — Runs `mcpx-gateway` (built from `./gateway`, internal `:9000`) + `mcpx-oauth` (internal `:9100`) + `caddy` (ports `80`/`443`) with pinned image versions, memory limits, log rotation, `no-new-privileges`, shared `/var/log/mcpx` volume, and persistent `oauth-data`, `uv-cache`, `npm-cache`, and Caddy TLS volumes.
- `infra/Caddyfile` — Automatic HTTPS + `mcpx-oauth` `forward_auth` guard + OAuth 2.1 public route forwarding + streaming reverse proxy (`flush_interval -1`) to `mcpx-gateway:9000`.
- `infra/config.json` — Base `mcpx-gateway` configuration (`gateway` settings including `authTokens`, plus empty `servers` and `groups` merged at build/deploy time).
- `infra/gateway/` — Custom Python MCP gateway (`Dockerfile` based on `python:3.13-slim-bookworm` + `uv`/`uvx` + Node.js 22/`npx`, `pyproject.toml`, and `mcpx_gateway/` package: `app.py`, `config.py`, `main.py`, `router.py`, `upstream.py`).
- `infra/oauth/server.py` — Pure-stdlib Python OAuth 2.1 Authorization Server (RFC 9728, RFC 8414, RFC 7591 DCR, static Client ID/Secret, PKCE `S256`, refresh-token rotation, RFC 7009 revocation) and Caddy `forward_auth` verifier.
- `infra/servers/<name>.json` — Per-server `mcpx-gateway` definitions under `"servers"` (`context7.json`, `uv-time.json`, `gemini-sound.json`, `mcpx-logs.json`).
- `infra/groups/<name>.json` — Per-group tool allow-list definitions under `"groups"` (`ops.json`), mapping each group name to `description` and `servers` (`{ "<server>": "*" | ["<tool>", ...] }`).
- `infra/.env.example` — Non-secret environment variable template.
- `servers/gemini-sound/` — Self-contained Python `uv` MCP server for Gemini text-to-speech (`gemini_generate_sound` returning WAV `AudioContent`, default model `gemini-2.5-flash-preview-tts`, and `gemini_sound_list_voices`).
- `servers/mcpx-logs/` — Self-contained Python `uv` MCP server exposing `list_log_sources` and `get_logs(source, lines, search)` over `/var/log/mcpx`.
- `scripts/provision-gcp.sh` — Idempotently enables Compute Engine API, reserves static IP, sets firewall rules, and creates the VM.
- `scripts/mcp-servers.sh` — Config builder and deployment entrypoint (`list`, `list-groups`, `build-config`, `start`/`deploy`); discovers enabled entries from `infra/servers/*.json` and `infra/groups/*.json` via each file's `"enabled"` flag, merges them with `infra/config.json`, validates group server references, and invokes `scripts/deploy-vm.sh` with `MCPS_ENV_FILE`.
- `scripts/deploy-vm.sh` — Verifies DNS, configures 2 GB `/swapfile` + Docker, uploads `infra/gateway/`, configs, `servers/`, and `.env`, validates `config.json`, builds and starts Docker Compose, verifies `uv`/`uvx`, checks HTTPS auth (`401` vs `200`), and runs `scripts/e2e-test.py`.
- `scripts/inspect.sh` — Client-side observability tool: `--list` prints tools/descriptions across all enabled servers, and default mode launches Anthropic's interactive MCP Inspector web UI with temporary `chmod 600` config.
- `scripts/e2e-test.py` — Live E2E integration test & latency profiler against `https://<MCP_SUBDOMAIN>` (auth checks, OAuth 2.1 flow, `/_healthz` & `/_readyz`, real MCP `initialize`/`tools/list`/`tools/call` across `/mcp`, `/<server>/mcp`, and `/groups/<name>/mcp` on `uv-time`, `context7`, `mcpx-logs` including `mcpx-gateway` log source, and `gemini-sound`, plus p50/p95 latency budgets).

## Security & Secret Hygiene Rules

1. **Never commit secrets to git.** No Bearer tokens, API keys (`GEMINI_API_KEY`, `GEMINI_TOKEN`, etc.), `.env` / `*.env` files, or GCP Service Account JSON keys (`mcpx-agent-key.json`, `gcp-sa*.json`, etc.) may ever be committed or pushed.
2. **Private config & credentials live in Google Drive folder `mcpx/` (`1E0Ml9EUgTNMEZDWNqxmMXoX_VM5A6mL0`).** The folder stores three files:
   - `mcpx.agent.env` — GCP/agent infrastructure settings (`GCP_PROJECT_ID`, `DOMAIN`, `SERVICE_ACCOUNT_MAIN`).
   - `mcpx.mcps.env` — All MCP gateway & server secrets (`MCP_BEARER_TOKEN`, `GEMINI_TOKEN`, etc.).
   - `mcpx-agent-key.json` — GCP service account JSON key.
   Always download them into an isolated `chmod 700` temporary directory with `chmod 600` file permissions (never inside the git repo), and delete the temporary directory (`rm -rf`) immediately after provisioning/deployment completes.
3. **Never expose gateway or admin ports publicly.** Only Caddy binds host ports `80` and `443`. `mcpx-gateway` port `9000` (including its unauthenticated `/_healthz` and `/_readyz` endpoints) MUST stay internal to the Docker bridge network behind Caddy's `forward_auth` guard.
4. **Defense in depth:** Both `Caddyfile` (`mcpx-oauth` `forward_auth`) and `mcpx-gateway` (`gateway.authTokens`) enforce `MCP_BEARER_TOKEN`.

## Agent Workflow & Runbook

### 1. Prepare Cloud Environment & Fetch Credentials from Drive `mcpx/`
```bash
bash .agents/setup.sh
export PATH="$HOME/google-cloud-sdk/bin:$PATH"

# List Drive folder mcpx/ (1E0Ml9EUgTNMEZDWNqxmMXoX_VM5A6mL0) to confirm file IDs
gdrive readonly ls 1E0Ml9EUgTNMEZDWNqxmMXoX_VM5A6mL0

# Create a restricted temporary directory for secrets (NEVER inside the git repo)
WORK_TMP="$(mktemp -d /tmp/mcpx-secrets.XXXXXX)"
chmod 700 "$WORK_TMP"
export CLOUDSDK_CONFIG="$WORK_TMP/gcloud_cfg"

# Download mcpx-agent-key.json, mcpx.agent.env, and mcpx.mcps.env with strict permissions
gdrive readonly download <MCPX_AGENT_KEY_FILE_ID> --out "$WORK_TMP/mcpx-agent-key.json"
gdrive readonly download <MCPX_AGENT_ENV_FILE_ID> --out "$WORK_TMP/mcpx.agent.env"
gdrive readonly download <MCPX_MCPS_ENV_FILE_ID> --out "$WORK_TMP/mcpx.mcps.env"
chmod 600 "$WORK_TMP/mcpx-agent-key.json" "$WORK_TMP/mcpx.agent.env" "$WORK_TMP/mcpx.mcps.env"
```

### 2. Authenticate `gcloud` & Load Runtime Variables
```bash
gcloud auth activate-service-account --key-file="$WORK_TMP/mcpx-agent-key.json"
set -a && source "$WORK_TMP/mcpx.agent.env" && source "$WORK_TMP/mcpx.mcps.env" && set +a
```
*(Note: scripts accept either `MCP_SUBDOMAIN` or `DOMAIN` from `mcpx.agent.env`, falling back to `DOMAIN` when `MCP_SUBDOMAIN` is unset, and pass MCP server tokens from `mcpx.mcps.env` via `MCPS_ENV_FILE`.)*

### 3. Provision GCP Infrastructure
```bash
bash scripts/provision-gcp.sh
```
Pause after this step if DNS is not yet pointed: update the `A` record for `${MCP_SUBDOMAIN:-${DOMAIN:-mcpx.duckdns.org}}` to the printed Static External IP and confirm with `dig +short A "${MCP_SUBDOMAIN:-${DOMAIN:-mcpx.duckdns.org}}" @1.1.1.1`.

### 4. Deploy & Verify Gateway on VM
```bash
MCPS_ENV_FILE="$WORK_TMP/mcpx.mcps.env" bash scripts/mcp-servers.sh deploy
```
To also run a live `gemini_generate_sound` WAV verification during E2E checks:
```bash
MCPS_ENV_FILE="$WORK_TMP/mcpx.mcps.env" E2E_EXTRA_ARGS="--call-gemini-sound" bash scripts/mcp-servers.sh deploy
```

Inspect live tools from the CLI after deployment:
```bash
AGENT_ENV_FILE="$WORK_TMP/mcpx.agent.env" MCPS_ENV_FILE="$WORK_TMP/mcpx.mcps.env" \
  bash scripts/inspect.sh --list
```

Always delete temporary secret files immediately after finishing:
```bash
rm -rf "$WORK_TMP"
```

### 5. Adding New MCP Servers & Tool Groups
- **Per-server config:** create `infra/servers/<name>.json` with `"enabled": true` and the `mcpx-gateway` server definition under `"servers"` (`url` + `transportType` for remote servers, or `command: "uv"` / `"uvx"` + `args` for stdio servers). Toggle `"enabled": false` in the same file to disable it.
- **Repo-hosted `uv` servers:** place scripts in `servers/<name>/server.py` using PEP 723 inline script metadata and run via `uv run --script /servers/<name>/server.py` so `/servers` stays mounted `:ro` and dependencies cache in `uv-cache`.
- **Tool groups:** create `infra/groups/<group-name>.json` with `"groups": { "<group-name>": { "enabled": true, "description": "...", "servers": { "<server>": "*" | ["<tool>", ...] } } }`. Toggle `"enabled": false` in the same file to disable it.
- **Secrets:** reference secrets via `${ENV_VAR}` in `infra/servers/<name>.json` (`mcpx-gateway` expands env vars from the container environment) and store real values only in `mcpx.mcps.env` inside Drive folder `mcpx/` and `~/mcp-server/.env` (`chmod 600`) on the VM.

### 6. Testing & Profiling Policy (E2E / Integration Only — No Unit Tests)
This repository intentionally uses **only E2E and integration tests** (no unit tests or mocked tests — see `.agents/skills/testing/SKILL.md`).

- **Static config & script checks:**
  - `python3 -c "import yaml; yaml.safe_load(open('infra/docker-compose.yml'))"`
  - `jq empty .agents/environment.json infra/config.json infra/servers/*.json infra/groups/*.json`
  - `python3 -m py_compile infra/gateway/mcpx_gateway/*.py infra/oauth/server.py servers/*/*.py scripts/e2e-test.py`
  - `bash -n .agents/setup.sh scripts/provision-gcp.sh scripts/mcp-servers.sh scripts/deploy-vm.sh scripts/inspect.sh`
- **Live E2E integration tests & latency profiling:**
  ```bash
  python3 scripts/e2e-test.py --iterations 5
  ```
  *(Auto-detects enabled servers from `scripts/mcp-servers.sh list`; supports `--servers`, `--skip-servers`, and `--call-gemini-sound`.)*
  Verifies:
  1. Unauthenticated and invalid-token requests return `401` on `/_healthz` and `/<probe-server>/mcp`.
  2. Authorized `/_healthz` and `/_readyz` return `200` with zero unhealthy servers.
  3. Full MCP Streamable HTTP handshake (`initialize` → `tools/list` → `tools/call`) across unified (`/mcp`), per-server (`/<server>/mcp`), and group (`/groups/<name>/mcp`) endpoints on `uv-time`, `context7`, `mcpx-logs` (`list_log_sources` + `get_logs` for `mcpx-gateway` and `mcpx-logs`), and `gemini-sound` (`gemini_sound_list_voices`, plus WAV `AudioContent` check with `--call-gemini-sound`).
  4. Multi-iteration latency profile (`min` / `p50` / `avg` / `p95` / `max` in ms) with configurable p95 latency budgets.
