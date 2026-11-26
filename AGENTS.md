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
- **MCP Gateway:** `ghcr.io/tbxark/mcp-proxy:v0.58.0` running on internal Docker network port `9000` (never exposed directly to the public internet). The upstream image includes `uv`, `uvx`, Python 3.13, `node`, and `npx` out of the box.
- **Enabled Servers (default):** `context7`, `uv-time`, `gemini-sound`, `mcpx-logs` (managed via `ENABLED_SERVERS` in `scripts/mcp-servers.sh` and per-server JSON definitions in `infra/servers/<name>.json`).
- **Transport:** `streamable-http` — each configured server `<name>` is exposed at `https://<MCP_SUBDOMAIN>/<name>/mcp`.
- **Observability:** Gateway (`caddy`, `mcp-proxy`, `mcpx-oauth`) and local server logs write to shared `/var/log/mcpx` and are queryable over MCP via `mcpx-logs` (`list_log_sources`, `get_logs`); client-side inspection uses `scripts/inspect.sh` (`--list` or Anthropic's MCP Inspector web UI).

## Repository Layout

- `README.md` — Human-oriented overview, architecture flow, server management, observability, and client connection guide.
- `.agents/environment.json`, `.agents/setup.sh` — Cloud agent session bootstrap (installs `gcloud`, `dig`, `nslookup`, `jq`, `openssl`, `pyyaml`).
- `.agents/skills/` — Repo-local agent skills:
  - `coding-principles` — Clarity, 3-4 line file-context headers, uniform naming, live execution, no backward-compatibility cruft.
  - `testing` — Integration, CLI, and E2E testing over trivial unit tests.
  - `grow-autonomy` — Improving agent instructions and system structure from root causes.
  - `audit-agent-context` — Read-only audit of repo instructions, skills, and context quality (manual invocation).
- `infra/docker-compose.yml` — Runs `mcp-proxy` (internal `:9000`) + `mcpx-oauth` (internal `:9100`) + `caddy` (ports `80`/`443`) with pinned image versions, memory limits, log rotation, `no-new-privileges`, shared `/var/log/mcpx` volume, and persistent `oauth-data`, `uv-cache`, `npm-cache`, and Caddy TLS volumes.
- `infra/Caddyfile` — Automatic HTTPS + `mcpx-oauth` `forward_auth` guard + OAuth 2.1 public route forwarding + streaming reverse proxy (`flush_interval -1`).
- `infra/oauth/server.py` — Pure-stdlib Python OAuth 2.1 Authorization Server (RFC 9728, RFC 8414, RFC 7591 DCR, static Client ID/Secret, PKCE `S256`, refresh-token rotation, RFC 7009 revocation) and Caddy `forward_auth` verifier.
- `infra/servers/<name>.json` — Per-server `tbxark/mcp-proxy` definitions (`context7.json`, `uv-time.json`, `gemini-sound.json`, `mcpx-logs.json`).
- `infra/.env.example` — Non-secret environment variable template.
- `servers/gemini-sound/` — Self-contained Python `uv` MCP server for Gemini text-to-speech (`gemini_generate_sound` returning WAV `AudioContent`, default model `gemini-2.5-flash-preview-tts`, and `gemini_sound_list_voices`).
- `servers/mcpx-logs/` — Self-contained Python `uv` MCP server exposing `list_log_sources` and `get_logs(source, lines, search)` over `/var/log/mcpx`.
- `scripts/provision-gcp.sh` — Idempotently enables Compute Engine API, reserves static IP, sets firewall rules, and creates the VM.
- `scripts/mcp-servers.sh` — Server catalog and deployment entrypoint (`list`, `build-config`, `start`/`deploy`); defines `ENABLED_SERVERS`, composes `infra/servers/<name>.json`, and invokes `scripts/deploy-vm.sh` with `MCPS_ENV_FILE`.
- `scripts/deploy-vm.sh` — Verifies DNS, configures 2 GB `/swapfile` + Docker, uploads configs, `servers/`, and `.env`, validates `config.json`, starts Docker Compose, verifies `uv`/`uvx`, checks HTTPS auth (`401` vs `200`), and runs `scripts/e2e-test.py`.
- `scripts/inspect.sh` — Client-side observability tool: `--list` prints tools/descriptions across all enabled servers, and default mode launches Anthropic's interactive MCP Inspector web UI with temporary `chmod 600` config.
- `scripts/e2e-test.py` — Live E2E integration test & latency profiler against `https://<MCP_SUBDOMAIN>` (auth checks, `/_healthz` & `/_readyz`, real MCP `initialize`/`tools/list`/`tools/call` on `uv-time`, `context7`, `mcpx-logs`, and `gemini-sound`, plus p50/p95 latency budgets).

## Security & Secret Hygiene Rules

1. **Never commit secrets to git.** No Bearer tokens, API keys (`GEMINI_API_KEY`, `GEMINI_TOKEN`, etc.), `.env` / `*.env` files, or GCP Service Account JSON keys (`mcpx-agent-key.json`, `gcp-sa*.json`, etc.) may ever be committed or pushed.
2. **Private config & credentials live in Google Drive folder `mcpx/` (`1E0Ml9EUgTNMEZDWNqxmMXoX_VM5A6mL0`).** The folder stores three files:
   - `mcpx.agent.env` — GCP/agent infrastructure settings (`GCP_PROJECT_ID`, `DOMAIN`, `SERVICE_ACCOUNT_MAIN`).
   - `mcpx.mcps.env` — All MCP gateway & server secrets (`MCP_BEARER_TOKEN`, `GEMINI_TOKEN`, etc.).
   - `mcpx-agent-key.json` — GCP service account JSON key.
   Always download them into an isolated `chmod 700` temporary directory with `chmod 600` file permissions (never inside the git repo), and delete the temporary directory (`rm -rf`) immediately after provisioning/deployment completes.
3. **Never expose gateway or admin ports publicly.** Only Caddy binds host ports `80` and `443`. `mcp-proxy` port `9000` (including its unauthenticated `/_healthz` and `/_readyz` endpoints) MUST stay internal to the Docker bridge network behind Caddy's Bearer token guard.
4. **Defense in depth:** Both `Caddyfile` and `mcp-proxy` (`options.authTokens`) enforce `MCP_BEARER_TOKEN`.

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

### 5. Adding New MCP Servers
- **Per-server config:** create `infra/servers/<name>.json` with the `tbxark/mcp-proxy` server definition (`url` + `transportType` for remote servers, or `command: "uv"` / `"uvx"` + `args` for stdio servers).
- **Repo-hosted `uv` servers:** place scripts in `servers/<name>/server.py` using PEP 723 inline script metadata and run via `uv run --script /servers/<name>/server.py` so `/servers` stays mounted `:ro` and dependencies cache in `uv-cache`.
- **Enable:** add `<name>` to `ENABLED_SERVERS` in `scripts/mcp-servers.sh`.
- **Secrets:** reference secrets via `${ENV_VAR}` in `infra/servers/<name>.json` (`tbxark/mcp-proxy` expands env vars by default) and store real values only in `mcpx.mcps.env` inside Drive folder `mcpx/` and `~/mcp-server/.env` (`chmod 600`) on the VM.

### 6. Testing & Profiling Policy (E2E / Integration Only — No Unit Tests)
This repository intentionally uses **only E2E and integration tests** (no unit tests or mocked tests — see `.agents/skills/testing/SKILL.md`).

- **Static config & script checks:**
  - `python3 -c "import yaml; yaml.safe_load(open('infra/docker-compose.yml'))"`
  - `jq empty .agents/environment.json infra/servers/*.json`
  - `python3 -m py_compile servers/*/*.py scripts/e2e-test.py`
  - `bash -n .agents/setup.sh scripts/provision-gcp.sh scripts/mcp-servers.sh scripts/deploy-vm.sh scripts/inspect.sh`
- **Live E2E integration tests & latency profiling:**
  ```bash
  python3 scripts/e2e-test.py --iterations 5
  ```
  *(Auto-detects enabled servers from `scripts/mcp-servers.sh list`; supports `--servers`, `--skip-servers`, and `--call-gemini-sound`.)*
  Verifies:
  1. Unauthenticated and invalid-token requests return `401` on `/_healthz` and `/<probe-server>/mcp`.
  2. Authorized `/_healthz` and `/_readyz` return `200` with zero unhealthy servers.
  3. Full MCP Streamable HTTP handshake (`initialize` → `tools/list` → `tools/call`) on `uv-time`, `context7`, `mcpx-logs` (`list_log_sources` + `get_logs` for `mcp-proxy` and `mcpx-logs`), and `gemini-sound` (`gemini_sound_list_voices`, plus WAV `AudioContent` check with `--call-gemini-sound`).
  4. Multi-iteration latency profile (`min` / `p50` / `avg` / `p95` / `max` in ms) with configurable p95 latency budgets.
