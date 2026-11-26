#!/usr/bin/env python3
"""Live end-to-end integration tests and latency profiler for the MCPX HTTPS gateway.

Fits into MCPX as the automated verification step run after `scripts/deploy-vm.sh` or
`scripts/mcp-servers.sh deploy`, and standalone by operators to validate the live gateway.
Exercises real HTTPS/TLS auth, `/_healthz` & `/_readyz`, every enabled MCP server
(`uv-time`, `context7`, `mcpx-logs`, `gemini-sound`), and multi-iteration p95 latency budgets.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import math
import os
import ssl
import statistics
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]
    body: str
    elapsed_ms: float


@dataclass
class ProfileSummary:
    name: str
    samples_ms: list[float] = field(default_factory=list)

    @property
    def min_ms(self) -> float:
        return min(self.samples_ms)

    @property
    def avg_ms(self) -> float:
        return statistics.fmean(self.samples_ms)

    @property
    def p50_ms(self) -> float:
        return percentile(self.samples_ms, 50)

    @property
    def p95_ms(self) -> float:
        return percentile(self.samples_ms, 95)

    @property
    def max_ms(self) -> float:
        return max(self.samples_ms)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return ordered[int(k)]
    return ordered[f] * (c - k) + ordered[c] * (k - f)


_CONN_POOL: dict[tuple[str, str, int], http.client.HTTPConnection] = {}


def _get_connection(scheme: str, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    key = (scheme, host, port)
    conn = _CONN_POOL.get(key)
    if conn is not None:
        conn.timeout = timeout
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        return conn
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    elif scheme == "http":
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    else:
        raise ValueError(f"Unsupported URL scheme: {scheme}")
    _CONN_POOL[key] = conn
    return conn


def _drop_connection(scheme: str, host: str, port: int) -> None:
    conn = _CONN_POOL.pop((scheme, host, port), None)
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    payload: Optional[dict[str, Any]] = None,
    timeout: float = 30.0,
) -> HttpResult:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    req_headers = dict(headers or {})
    data: Optional[bytes] = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req_headers.setdefault("Connection", "keep-alive")

    for attempt in range(2):
        conn = _get_connection(scheme, host, port, timeout)
        start = time.perf_counter()
        try:
            conn.request(method, path, body=data, headers=req_headers)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            if resp.will_close or "close" in resp_headers.get("connection", "").lower():
                _drop_connection(scheme, host, port)
            return HttpResult(resp.status, resp_headers, body, elapsed_ms)
        except (
            http.client.RemoteDisconnected,
            http.client.BadStatusLine,
            http.client.CannotSendRequest,
            http.client.ResponseNotReady,
            BrokenPipeError,
            ConnectionResetError,
            ssl.SSLEOFError,
        ):
            _drop_connection(scheme, host, port)
            if attempt == 1:
                raise
        except Exception:
            _drop_connection(scheme, host, port)
            raise
    raise RuntimeError("Unreachable")


def parse_mcp_response(res: HttpResult) -> dict[str, Any]:
    """Parse either direct JSON or SSE-framed `data: {...}` Streamable HTTP responses."""
    text = res.body.strip()
    if not text:
        return {}
    if text.startswith("{"):
        return json.loads(text)

    # Streamable HTTP may return text/event-stream with one or more `data:` lines.
    last_json: Optional[dict[str, Any]] = None
    for line in text.splitlines():
        if line.startswith("data:"):
            candidate = line[len("data:") :].strip()
            if not candidate or candidate == "[DONE]":
                continue
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    last_json = parsed
            except json.JSONDecodeError:
                continue
    if last_json is not None:
        return last_json
    raise AssertionError(f"Could not parse MCP response (HTTP {res.status}): {text[:400]}")


class McpStreamableClient:
    """Minimal Streamable HTTP MCP client for E2E protocol verification."""

    def __init__(self, endpoint_url: str, bearer_token: str, timeout: float = 30.0) -> None:
        self.endpoint_url = endpoint_url
        self.bearer_token = bearer_token
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._req_id = 0

    def _headers(self) -> dict[str, str]:
        hdrs = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            hdrs["Mcp-Session-Id"] = self.session_id
        return hdrs

    def rpc(self, method: str, params: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], float]:
        self._req_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        res = http_request(
            self.endpoint_url,
            method="POST",
            headers=self._headers(),
            payload=payload,
            timeout=self.timeout,
        )
        if res.status != 200:
            raise AssertionError(
                f"MCP RPC {method} on {self.endpoint_url} failed with HTTP {res.status}: {res.body[:400]}"
            )
        if "mcp-session-id" in res.headers:
            self.session_id = res.headers["mcp-session-id"]

        parsed = parse_mcp_response(res)
        if "error" in parsed:
            raise AssertionError(f"MCP RPC {method} returned JSON-RPC error: {parsed['error']}")
        return parsed, res.elapsed_ms

    def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> float:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        res = http_request(
            self.endpoint_url,
            method="POST",
            headers=self._headers(),
            payload=payload,
            timeout=self.timeout,
        )
        if res.status not in (200, 202, 204):
            raise AssertionError(
                f"MCP notification {method} failed with HTTP {res.status}: {res.body[:300]}"
            )
        return res.elapsed_ms

    def initialize(self) -> float:
        _, ms = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mcpx-e2e-test", "version": "1.0.0"},
            },
        )
        self.notify("notifications/initialized")
        return ms

    def list_tools(self) -> tuple[list[dict[str, Any]], float]:
        resp, ms = self.rpc("tools/list", {})
        tools = resp.get("result", {}).get("tools", [])
        return tools, ms

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], float]:
        resp, ms = self.rpc("tools/call", {"name": name, "arguments": arguments})
        return resp.get("result", {}), ms


DEFAULT_SERVERS = ["context7", "uv-time", "gemini-sound", "mcpx-logs"]


def discover_enabled_servers() -> list[str]:
    """Query `scripts/mcp-servers.sh list` for enabled servers, falling back to defaults."""
    helper = Path(__file__).resolve().parent / "mcp-servers.sh"
    if helper.is_file():
        try:
            out = subprocess.check_output(
                ["bash", str(helper), "list"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            servers = [line.strip() for line in out.splitlines() if line.strip()]
            if servers:
                return servers
        except (OSError, subprocess.SubprocessError):
            pass
    return list(DEFAULT_SERVERS)


def run_security_and_health_checks(base_url: str, token: str, probe_server: str) -> None:
    print("==> Security & Auth integration tests")

    r_no_auth = http_request(f"{base_url}/_healthz")
    assert r_no_auth.status == 401, f"Expected 401 for unauthenticated /_healthz, got {r_no_auth.status}"
    print(f"    PASS  GET /_healthz (no token)          -> HTTP {r_no_auth.status} ({r_no_auth.elapsed_ms:.1f} ms)")

    r_bad_auth = http_request(f"{base_url}/_healthz", headers={"Authorization": "Bearer invalid-token"})
    assert r_bad_auth.status == 401, f"Expected 401 for bad token on /_healthz, got {r_bad_auth.status}"
    print(f"    PASS  GET /_healthz (wrong token)       -> HTTP {r_bad_auth.status} ({r_bad_auth.elapsed_ms:.1f} ms)")

    r_mcp_no_auth = http_request(
        f"{base_url}/{probe_server}/mcp",
        method="POST",
        payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert r_mcp_no_auth.status == 401, (
        f"Expected 401 for unauthenticated /{probe_server}/mcp, got {r_mcp_no_auth.status}"
    )
    print(
        f"    PASS  POST /{probe_server}/mcp (no token) -> HTTP {r_mcp_no_auth.status} ({r_mcp_no_auth.elapsed_ms:.1f} ms)"
    )

    auth_headers = {"Authorization": f"Bearer {token}"}
    r_health = http_request(f"{base_url}/_healthz", headers=auth_headers)
    assert r_health.status == 200, f"Expected 200 for /_healthz with token, got {r_health.status}: {r_health.body}"
    health_json = json.loads(r_health.body)
    assert health_json.get("status") == "ok", f"Unexpected /_healthz payload: {health_json}"
    print(
        f"    PASS  GET /_healthz (authorized)        -> HTTP 200 ({r_health.elapsed_ms:.1f} ms) "
        f"servers={health_json.get('serverCount')}"
    )

    r_ready = http_request(f"{base_url}/_readyz", headers=auth_headers)
    assert r_ready.status == 200, f"Expected 200 for /_readyz with token, got {r_ready.status}: {r_ready.body}"
    ready_json = json.loads(r_ready.body)
    assert not ready_json.get("unhealthy"), f"Unhealthy servers reported: {ready_json}"
    print(
        f"    PASS  GET /_readyz (authorized)         -> HTTP 200 ({r_ready.elapsed_ms:.1f} ms) "
        f"status={ready_json.get('status')}"
    )


def run_uv_time_e2e(base_url: str, token: str) -> McpStreamableClient:
    print("\n==> E2E test: uv-time (verifies uv/uvx stdio MCP end-to-end)")
    client = McpStreamableClient(f"{base_url}/uv-time/mcp", token)
    init_ms = client.initialize()
    tools, list_ms = client.list_tools()
    tool_names = [t.get("name") for t in tools]
    assert "get_current_time" in tool_names, f"Expected get_current_time in uv-time tools, got {tool_names}"

    result, call_ms = client.call_tool("get_current_time", {"timezone": "UTC"})
    assert not result.get("isError"), f"uv-time tool returned error: {result}"
    content = result.get("content", [])
    assert content and any("UTC" in str(c.get("text", "")) or "datetime" in str(c.get("text", "")) for c in content), (
        f"Unexpected uv-time response: {result}"
    )
    print(
        f"    PASS  uv-time initialize={init_ms:.1f}ms tools/list={list_ms:.1f}ms "
        f"get_current_time={call_ms:.1f}ms tools={tool_names}"
    )
    return client


def run_context7_e2e(base_url: str, token: str) -> McpStreamableClient:
    print("\n==> E2E test: context7 (remote Streamable HTTP MCP)")
    client = McpStreamableClient(f"{base_url}/context7/mcp", token)
    init_ms = client.initialize()
    tools, list_ms = client.list_tools()
    tool_names = [t.get("name") for t in tools]
    assert "resolve-library-id" in tool_names, f"Expected resolve-library-id in context7 tools, got {tool_names}"

    # Inspect schema to provide required params cleanly across Context7 versions.
    resolve_tool = next(t for t in tools if t.get("name") == "resolve-library-id")
    props = resolve_tool.get("inputSchema", {}).get("properties", {})
    args: dict[str, Any] = {"libraryName": "fastapi"}
    if "query" in props:
        args["query"] = "FastAPI routing and dependencies"

    result, call_ms = client.call_tool("resolve-library-id", args)
    assert not result.get("isError"), f"context7 resolve-library-id returned error: {result}"
    text_blob = " ".join(str(c.get("text", "")) for c in result.get("content", []))
    assert len(text_blob.strip()) > 20, f"Expected non-empty Context7 docs resolution, got: {result}"
    print(
        f"    PASS  context7 initialize={init_ms:.1f}ms tools/list={list_ms:.1f}ms "
        f"resolve-library-id={call_ms:.1f}ms tools={tool_names}"
    )
    return client


def run_mcpx_logs_e2e(base_url: str, token: str) -> McpStreamableClient:
    print("\n==> E2E test: mcpx-logs (gateway and server log inspection MCP)")
    client = McpStreamableClient(f"{base_url}/mcpx-logs/mcp", token)
    init_ms = client.initialize()
    tools, list_ms = client.list_tools()
    tool_names = [t.get("name") for t in tools]
    assert "list_log_sources" in tool_names, f"Expected list_log_sources in mcpx-logs tools, got {tool_names}"
    assert "get_logs" in tool_names, f"Expected get_logs in mcpx-logs tools, got {tool_names}"

    sources_res, sources_ms = client.call_tool("list_log_sources", {})
    assert not sources_res.get("isError"), f"mcpx-logs list_log_sources returned error: {sources_res}"
    sources_text = " ".join(str(c.get("text", "")) for c in sources_res.get("content", [])).strip()
    assert sources_text, f"Expected non-empty log sources output, got: {sources_res}"

    proxy_res, proxy_ms = client.call_tool("get_logs", {"source": "mcp-proxy", "lines": 20})
    assert not proxy_res.get("isError"), f"mcpx-logs get_logs(mcp-proxy) returned error: {proxy_res}"
    assert proxy_res.get("content"), f"Expected content from get_logs(mcp-proxy), got: {proxy_res}"

    self_res, self_ms = client.call_tool("get_logs", {"source": "mcpx-logs", "lines": 20})
    assert not self_res.get("isError"), f"mcpx-logs get_logs(mcpx-logs) returned error: {self_res}"
    assert self_res.get("content"), f"Expected content from get_logs(mcpx-logs), got: {self_res}"

    print(
        f"    PASS  mcpx-logs initialize={init_ms:.1f}ms tools/list={list_ms:.1f}ms "
        f"list_log_sources={sources_ms:.1f}ms get_logs(mcp-proxy)={proxy_ms:.1f}ms "
        f"get_logs(mcpx-logs)={self_ms:.1f}ms tools={tool_names}"
    )
    return client


def run_gemini_sound_e2e(base_url: str, token: str, call_generate: bool) -> McpStreamableClient:
    print("\n==> E2E test: gemini-sound (local uv Gemini TTS/audio MCP)")
    client = McpStreamableClient(f"{base_url}/gemini-sound/mcp", token, timeout=90.0)
    init_ms = client.initialize()
    tools, list_ms = client.list_tools()
    tool_names = [t.get("name") for t in tools]
    assert "gemini_generate_sound" in tool_names, (
        f"Expected gemini_generate_sound in gemini-sound tools, got {tool_names}"
    )
    assert "gemini_sound_list_voices" in tool_names, (
        f"Expected gemini_sound_list_voices in gemini-sound tools, got {tool_names}"
    )

    voices_res, voices_ms = client.call_tool("gemini_sound_list_voices", {})
    assert not voices_res.get("isError"), f"gemini_sound_list_voices returned error: {voices_res}"
    voices_text = " ".join(str(c.get("text", "")) for c in voices_res.get("content", [])).strip()
    assert voices_text, f"Expected non-empty voice list from gemini_sound_list_voices, got: {voices_res}"

    if call_generate:
        gen_tool = next(t for t in tools if t.get("name") == "gemini_generate_sound")
        props = gen_tool.get("inputSchema", {}).get("properties", {})
        if "text" in props:
            gen_args: dict[str, Any] = {"text": "MCPX audio check."}
        elif "prompt" in props:
            gen_args = {"prompt": "MCPX audio check."}
        else:
            required = gen_tool.get("inputSchema", {}).get("required", [])
            first_key = required[0] if required else next(iter(props), "text")
            gen_args = {first_key: "MCPX audio check."}

        gen_res, gen_ms = client.call_tool("gemini_generate_sound", gen_args)
        assert not gen_res.get("isError"), f"gemini_generate_sound returned error: {gen_res}"
        audio_items = [c for c in gen_res.get("content", []) if c.get("type") == "audio"]
        assert audio_items, f"Expected AudioContent item from gemini_generate_sound, got: {gen_res}"

        audio = audio_items[0]
        mime = str(audio.get("mimeType", ""))
        assert mime.startswith("audio/wav"), f"Expected audio/wav mimeType, got: {mime!r}"
        raw_b64 = audio.get("data", "")
        assert isinstance(raw_b64, str) and raw_b64, "AudioContent data is empty"
        wav_bytes = base64.b64decode(raw_b64)
        assert len(wav_bytes) >= 44 and wav_bytes[:4] == b"RIFF" and wav_bytes[8:12] == b"WAVE", (
            f"Decoded audio is not a valid WAV file (len={len(wav_bytes)}, header={wav_bytes[:12]!r})"
        )
        print(
            f"    PASS  gemini-sound initialize={init_ms:.1f}ms tools/list={list_ms:.1f}ms "
            f"list_voices={voices_ms:.1f}ms gemini_generate_sound={gen_ms:.1f}ms "
            f"wav_bytes={len(wav_bytes)} mime={mime}"
        )
    else:
        print(
            f"    PASS  gemini-sound initialize={init_ms:.1f}ms tools/list={list_ms:.1f}ms "
            f"list_voices={voices_ms:.1f}ms tools={tool_names}"
        )
    return client


def run_generic_server_e2e(base_url: str, token: str, server_name: str) -> McpStreamableClient:
    print(f"\n==> E2E test: {server_name} (generic Streamable HTTP MCP)")
    client = McpStreamableClient(f"{base_url}/{server_name}/mcp", token)
    init_ms = client.initialize()
    tools, list_ms = client.list_tools()
    tool_names = [t.get("name") for t in tools]
    assert tool_names, f"Expected at least one tool from {server_name}, got empty list"
    print(f"    PASS  {server_name} initialize={init_ms:.1f}ms tools/list={list_ms:.1f}ms tools={tool_names}")
    return client


def run_profiling(
    base_url: str,
    token: str,
    uv_client: Optional[McpStreamableClient],
    c7_client: Optional[McpStreamableClient],
    logs_client: Optional[McpStreamableClient],
    iterations: int,
    max_p95_health_ms: float,
    max_p95_uv_ms: float,
) -> list[ProfileSummary]:
    print(f"\n==> Latency profiling ({iterations} warm iterations each)")
    auth_headers = {"Authorization": f"Bearer {token}"}

    health_prof = ProfileSummary("GET /_healthz (HTTPS + auth)")
    summaries: list[ProfileSummary] = [health_prof]

    uv_prof = ProfileSummary("POST /uv-time/mcp tools/call (uvx stdio)") if uv_client else None
    c7_prof = ProfileSummary("POST /context7/mcp tools/list (remote proxy)") if c7_client else None
    logs_prof = ProfileSummary("POST /mcpx-logs/mcp tools/call (list_log_sources)") if logs_client else None
    for prof in (uv_prof, c7_prof, logs_prof):
        if prof is not None:
            summaries.append(prof)

    for _ in range(iterations):
        r = http_request(f"{base_url}/_healthz", headers=auth_headers)
        assert r.status == 200
        health_prof.samples_ms.append(r.elapsed_ms)

        if uv_client and uv_prof:
            _, uv_ms = uv_client.call_tool("get_current_time", {"timezone": "UTC"})
            uv_prof.samples_ms.append(uv_ms)

        if c7_client and c7_prof:
            _, c7_ms = c7_client.list_tools()
            c7_prof.samples_ms.append(c7_ms)

        if logs_client and logs_prof:
            _, logs_ms = logs_client.call_tool("list_log_sources", {})
            logs_prof.samples_ms.append(logs_ms)

    print(f"\n{'Operation':<52} {'Min':>8} {'p50':>8} {'Avg':>8} {'p95':>8} {'Max':>8}")
    print("-" * 96)
    for s in summaries:
        print(
            f"{s.name:<52} {s.min_ms:>7.1f}ms {s.p50_ms:>7.1f}ms "
            f"{s.avg_ms:>7.1f}ms {s.p95_ms:>7.1f}ms {s.max_ms:>7.1f}ms"
        )

    assert health_prof.p95_ms <= max_p95_health_ms, (
        f"Healthcheck p95 latency {health_prof.p95_ms:.1f}ms exceeded budget {max_p95_health_ms:.1f}ms"
    )
    if uv_prof is not None:
        assert uv_prof.p95_ms <= max_p95_uv_ms, (
            f"uv-time tool call p95 latency {uv_prof.p95_ms:.1f}ms exceeded budget {max_p95_uv_ms:.1f}ms"
        )
    budget_note = f"health p95 {health_prof.p95_ms:.1f}ms <= {max_p95_health_ms:.0f}ms"
    if uv_prof is not None:
        budget_note += f", uv-time p95 {uv_prof.p95_ms:.1f}ms <= {max_p95_uv_ms:.0f}ms"
    print(f"\n    PASS  Latency budgets satisfied ({budget_note})")
    return summaries


def _parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E & profiling suite for live MCPX gateway")
    default_host = (os.environ.get("MCP_SUBDOMAIN") or "").strip() or (os.environ.get("DOMAIN") or "").strip()
    if default_host and not default_host.lower().startswith(("http://", "https://")):
        default_base_url = f"https://{default_host}"
    else:
        default_base_url = default_host
    parser.add_argument(
        "--base-url",
        default=default_base_url,
        help="Gateway base URL (default: https://$MCP_SUBDOMAIN or https://$DOMAIN)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("MCP_BEARER_TOKEN", ""),
        help="Gateway Bearer token (default: $MCP_BEARER_TOKEN)",
    )
    parser.add_argument(
        "--servers",
        default="",
        help="Comma-separated servers to test (default: auto-detected via scripts/mcp-servers.sh list)",
    )
    parser.add_argument(
        "--skip-servers",
        default="",
        help="Comma-separated servers to skip (e.g. gemini-sound,context7)",
    )
    parser.add_argument(
        "--call-gemini-sound",
        action="store_true",
        help="Invoke gemini_generate_sound and verify valid WAV AudioContent (uses Gemini API)",
    )
    parser.add_argument("--iterations", type=int, default=5, help="Warm profiling iterations per operation")
    parser.add_argument("--max-p95-health-ms", type=float, default=1500.0, help="p95 budget for /_healthz (ms)")
    parser.add_argument("--max-p95-uv-ms", type=float, default=2000.0, help="p95 budget for uv-time tools/call (ms)")
    parser.add_argument("--json-out", default="", help="Optional path to write JSON profiling report")
    args = parser.parse_args()

    if args.iterations < 1:
        parser.error("--iterations must be >= 1")

    base_url = args.base_url.strip().rstrip("/")
    if not base_url or not args.token:
        parser.error("Provide --base-url (or MCP_SUBDOMAIN / DOMAIN) and --token (or MCP_BEARER_TOKEN).")

    servers = _parse_csv(args.servers) if args.servers else discover_enabled_servers()
    skip = set(_parse_csv(args.skip_servers))
    active_servers = [s for s in servers if s not in skip]
    if not active_servers:
        parser.error("No servers selected to test after applying --skip-servers.")

    probe_server = "context7" if "context7" in active_servers else active_servers[0]
    run_security_and_health_checks(base_url, args.token, probe_server=probe_server)

    uv_client: Optional[McpStreamableClient] = None
    c7_client: Optional[McpStreamableClient] = None
    logs_client: Optional[McpStreamableClient] = None

    for server in active_servers:
        if server == "uv-time":
            uv_client = run_uv_time_e2e(base_url, args.token)
        elif server == "context7":
            c7_client = run_context7_e2e(base_url, args.token)
        elif server == "mcpx-logs":
            logs_client = run_mcpx_logs_e2e(base_url, args.token)
        elif server == "gemini-sound":
            run_gemini_sound_e2e(base_url, args.token, call_generate=args.call_gemini_sound)
        else:
            run_generic_server_e2e(base_url, args.token, server)

    summaries = run_profiling(
        base_url,
        args.token,
        uv_client=uv_client,
        c7_client=c7_client,
        logs_client=logs_client,
        iterations=args.iterations,
        max_p95_health_ms=args.max_p95_health_ms,
        max_p95_uv_ms=args.max_p95_uv_ms,
    )

    if args.json_out:
        report = {
            "baseUrl": base_url,
            "servers": active_servers,
            "iterations": args.iterations,
            "profiles": [
                {
                    "name": s.name,
                    "minMs": round(s.min_ms, 2),
                    "p50Ms": round(s.p50_ms, 2),
                    "avgMs": round(s.avg_ms, 2),
                    "p95Ms": round(s.p95_ms, 2),
                    "maxMs": round(s.max_ms, 2),
                    "samplesMs": [round(x, 2) for x in s.samples_ms],
                }
                for s in summaries
            ],
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"    Wrote JSON profile report to {args.json_out}")

    print("\nALL E2E & PROFILING CHECKS PASSED!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
