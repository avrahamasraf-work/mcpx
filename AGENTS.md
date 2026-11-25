# AGENTS.md — MCPX Remote MCP Gateway on GCP

This repository manages a remote, HTTPS-protected Model Context Protocol (MCP) gateway hosted on Google Cloud Platform (GCP).

## Architecture Overview

- **Cloud Provider & Project:** GCP (`GCP_PROJECT_ID` stored in private `mcpx.agent.env` inside Google Drive folder `mcpx/`; currently `mcpx-515813`).
- **Compute Tier:** GCP Always Free `e2-micro` VM (`us-east1` / `us-east1-b`, Ubuntu 24.04 LTS, 30 GB `pd-standard` boot disk, 2 GB `/swapfile`).
- **Networking & TLS:**
  - Regional static external IP attached to the VM.
  - Firewall allows inbound TCP `80`, TCP/UDP `443` (`http-server`, `https-server` tags), and SSH (`22`).
  - Domain: `mcpx.duckdns.org` (DuckDNS `A` record pointing to the static IP).
  - Reverse Proxy: `caddy:alpine` provides automatic Let's Encrypt HTTPS and blocks any request missing `Authorization: Bearer <MCP_BEARER_TOKEN>` with HTTP `401`.
- **MCP Gateway:** `ghcr.io/tbxark/mcp-proxy:latest` running on internal Docker network port `9000` (never exposed directly to the public internet). The upstream image includes `uv`, `uvx`, Python 3.13, `node`, and `npx` out of the box.
- **Transport:** `streamable-http` — each configured server `<name>` is exposed at `https://<MCP_SUBDOMAIN>/<name>/mcp`.

## Repository Layout

- `.agents/environment.json`, `.agents/setup.sh` — Cloud agent session bootstrap (installs `gcloud`, `dig`, `nslookup`, `jq`, `openssl`, `pyyaml`).
- `infra/docker-compose.yml` — Runs `mcp-proxy` (internal only) + `caddy` (ports `80`/`443`) with persistent `uv-cache`, `npm-cache`, and Caddy TLS volumes.
- `infra/Caddyfile` — Automatic HTTPS + strict Bearer token guard + streaming reverse proxy (`flush_interval -1`).
- `infra/config.json` — Default `tbxark/mcp-proxy` configuration (`context7` remote Streamable HTTP + `uv-time` via `uvx` to verify `uv` works).
- `infra/config.with-gemini.json` — Extended config enabling the local `uv`-runnable Gemini MCP server once `GEMINI_API_KEY` (or `GEMINI_TOKEN`) is configured.
- `infra/.env.example` — Non-secret environment variable template.
- `servers/gemini-mcp/` — Self-contained Python `uv` MCP server (`mcp>=2.0.0` `MCPServer` + `google-genai`) runnable via `uv run --script /servers/gemini-mcp/server.py`.
- `scripts/provision-gcp.sh` — Idempotently enables Compute Engine API, reserves static IP, sets firewall rules, and creates the VM.
- `scripts/deploy-vm.sh` — Verifies DNS, configures 2 GB `/swapfile` + Docker, uploads configs, validates `config.json`, starts Docker Compose, verifies `uv`/`uvx`, and checks HTTPS auth (`401` vs `200`).
- `scripts/e2e-test.py` — Live E2E integration test & latency profiler against `https://<MCP_SUBDOMAIN>` (auth checks, real MCP `initialize`/`tools/list`/`tools/call` on `uv-time`, `context7`, and optional `gemini`, plus p50/p95 latency budgets).

## Security & Secret Hygiene Rules

1. **Never commit secrets to git.** No Bearer tokens, API keys (`GEMINI_API_KEY`, `GEMINI_TOKEN`, etc.), `.env` / `*.env` files, or GCP Service Account JSON keys (`mcpx-agent-key.json`, `gcp-sa*.json`, etc.) may ever be committed or pushed.
2. **Private config & credentials live in Google Drive folder `mcpx/` (`1E0Ml9EUgTNMEZDWNqxmMXoX_VM5A6mL0`).** The folder stores three files:
   - `mcpx.agent.env` — GCP/agent infrastructure settings (`GCP_PROJECT_ID`, `DOMAIN`, `SERVICE_ACCOUNT_MAIN`).
   - `mcpx.mcps.env` — MCP gateway & server secrets (`MCP_BEARER_TOKEN`, `GEMINI_TOKEN`).
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
*(Note: scripts accept either `MCP_SUBDOMAIN` or `DOMAIN` from `mcpx.agent.env`, falling back to `DOMAIN` when `MCP_SUBDOMAIN` is unset, and accept either `GEMINI_API_KEY` or `GEMINI_TOKEN` from `mcpx.mcps.env`, falling back to `GEMINI_TOKEN` when `GEMINI_API_KEY` is unset.)*

### 3. Provision GCP Infrastructure (Steps 1–3)
```bash
bash scripts/provision-gcp.sh
```
Pause after this step if DNS is not yet pointed: update the `A` record for `${MCP_SUBDOMAIN:-${DOMAIN:-mcpx.duckdns.org}}` to the printed Static External IP and confirm with `dig +short A "${MCP_SUBDOMAIN:-${DOMAIN:-mcpx.duckdns.org}}" @1.1.1.1`.

### 4. Deploy & Verify Gateway on VM (Steps 5–6)
```bash
bash scripts/deploy-vm.sh
```
To deploy with the local `uv` Gemini MCP server enabled (requires `GEMINI_API_KEY` or `GEMINI_TOKEN` in `mcpx.mcps.env`) and invoke a live Gemini call during E2E verification:
```bash
CONFIG_FILE=infra/config.with-gemini.json E2E_EXTRA_ARGS="--call-gemini" bash scripts/deploy-vm.sh
```
*(Note: `deploy-vm.sh` automatically passes `--include-gemini` to `scripts/e2e-test.py` when `CONFIG_FILE` defines `.mcpServers.gemini`, and forwards any additional flags in `E2E_EXTRA_ARGS`.)*

Always delete temporary secret files immediately after finishing:
```bash
rm -rf "$WORK_TMP"
```

### 5. Adding New MCP Servers
- **Remote Streamable HTTP / SSE servers** (lowest RAM usage on `e2-micro`): add an entry with `"url"` and `"transportType": "streamable-http"` (or `"sse"`) to `infra/config.json`.
- **`uvx` / `uv` stdio servers:** add an entry with `"command": "uvx"` (or `"uv"`) and `"args"`. Prefer PEP 723 inline script metadata + `uv run --script /servers/<name>/server.py` for repo-hosted servers so `/servers` stays mounted `:ro` and dependencies cache in the persistent `uv-cache` volume.
- **Secrets:** reference secrets via `${ENV_VAR}` in `config.json` (`tbxark/mcp-proxy` expands env vars by default), store real values only in `mcpx.mcps.env` (or `mcpx.agent.env` for GCP/infra settings) inside Drive folder `mcpx/` and `~/mcp-server/.env` (`chmod 600`) on the VM.

### 6. Testing & Profiling Policy (E2E / Integration Only — No Unit Tests)
This repository intentionally uses **only E2E and integration tests** (no unit tests or mocked tests).

- **Static config & script checks:**
  - `python3 -c "import yaml; yaml.safe_load(open('infra/docker-compose.yml'))"`
  - `jq empty .agents/environment.json infra/config.json infra/config.with-gemini.json`
  - `python3 -m py_compile servers/gemini-mcp/server.py scripts/e2e-test.py`
  - `bash -n .agents/setup.sh scripts/provision-gcp.sh scripts/deploy-vm.sh`
- **Live E2E integration tests & latency profiling:**
  ```bash
  python3 scripts/e2e-test.py --iterations 5
  ```
  *(Uses `MCP_SUBDOMAIN` — or `DOMAIN` fallback — and `MCP_BEARER_TOKEN` from the environment; add `--include-gemini` or `--call-gemini` once Gemini MCP is enabled.)*
  Verifies:
  1. Unauthenticated and invalid-token requests return `401` on `/_healthz` and `/context7/mcp`.
  2. Authorized `/_healthz` and `/_readyz` return `200` with zero unhealthy servers.
  3. Full MCP Streamable HTTP handshake (`initialize` → `tools/list` → `tools/call`) on both `uv-time` (proving `uv`/`uvx` works end-to-end) and `context7`.
  4. Multi-iteration latency profile (`min` / `p50` / `avg` / `p95` / `max` in ms) with configurable p95 latency budgets.
