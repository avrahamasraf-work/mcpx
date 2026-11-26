#!/usr/bin/env python3
"""MCPX OAuth 2.1 authorization server and Caddy forward_auth verifier.

Fits into MCPX as an internal Docker Compose sidecar behind Caddy (`mcpx-oauth:9100`).
Implements RFC 9728 Protected Resource Metadata, RFC 8414 Authorization Server Metadata,
RFC 7591 Dynamic Client Registration, OAuth 2.1 Authorization Code + PKCE (S256) with
refresh-token rotation, and `/oauth/verify` for dual static-Bearer / OAuth JWT auth.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from typing import Any, Optional

SERVICE_NAME = "mcpx-oauth"
DEFAULT_PORT = 9100
DEFAULT_DB_PATH = "/data/oauth.db"
DEFAULT_LOG_DIR = "/var/log/mcpx"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
DEFAULT_SCOPE = "mcp:tools"
CSRF_TTL_SECONDS = 600
MAX_REQUEST_BODY_BYTES = 64 * 1024


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pkce_s256(code_verifier: str) -> str:
    return _b64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())


def _configure_logging(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    log_file = log_dir / f"{SERVICE_NAME}.log"
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("File logging disabled (%s): %s", log_file, exc)

    return logger


def _is_valid_redirect_uri(uri: str, allowed_hosts: set[str] | None = None) -> bool:
    parsed = urllib.parse.urlsplit(uri)
    if parsed.fragment:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if parsed.scheme == "https":
        if allowed_hosts and host not in allowed_hosts:
            return False
        return True
    if parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return True
    return False


def _normalize_resource_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def _append_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value:
            existing.append((key, value))
    new_query = urllib.parse.urlencode(existing)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, ""))


class OAuthConfig:
    """Runtime configuration loaded from environment variables."""

    def __init__(self) -> None:
        raw_subdomain = (os.environ.get("MCP_SUBDOMAIN") or os.environ.get("DOMAIN") or "").strip()
        if not raw_subdomain:
            raise RuntimeError("MCP_SUBDOMAIN (or DOMAIN) must be set")
        if raw_subdomain.startswith(("http://", "https://")):
            self.issuer = raw_subdomain.rstrip("/")
        else:
            self.issuer = f"https://{raw_subdomain.rstrip('/')}"

        self.bearer_token = (os.environ.get("MCP_BEARER_TOKEN") or "").strip()
        if not self.bearer_token:
            raise RuntimeError("MCP_BEARER_TOKEN must be set")

        self.consent_password = (os.environ.get("MCP_OAUTH_PASSWORD") or "").strip() or self.bearer_token
        self.static_client_id = (os.environ.get("MCP_OAUTH_CLIENT_ID") or "").strip()
        self.static_client_secret = (os.environ.get("MCP_OAUTH_CLIENT_SECRET") or "").strip()

        raw_hosts = (os.environ.get("MCP_OAUTH_ALLOWED_REDIRECT_HOSTS") or "").strip()
        self.allowed_redirect_hosts: set[str] | None = (
            {h.strip().lower() for h in raw_hosts.split(",") if h.strip()} if raw_hosts else None
        )

        explicit_key = (os.environ.get("MCP_OAUTH_SIGNING_KEY") or "").strip()
        key_seed = explicit_key if explicit_key else f"mcpx-oauth-jwt-v1:{self.bearer_token}"
        self.signing_key = hashlib.sha256(key_seed.encode("utf-8")).digest()

        self.db_path = Path((os.environ.get("MCP_OAUTH_DB_PATH") or "").strip() or DEFAULT_DB_PATH)
        self.log_dir = Path((os.environ.get("MCPX_LOG_DIR") or "").strip() or DEFAULT_LOG_DIR)
        self.port = int((os.environ.get("MCP_OAUTH_PORT") or "").strip() or DEFAULT_PORT)
        self.access_token_ttl = int((os.environ.get("MCP_OAUTH_ACCESS_TOKEN_TTL") or "").strip() or 3600)
        self.refresh_token_ttl = int((os.environ.get("MCP_OAUTH_REFRESH_TOKEN_TTL") or "").strip() or 2592000)
        self.code_ttl = int((os.environ.get("MCP_OAUTH_CODE_TTL") or "").strip() or 120)


class OAuthStore:
    """Thread-safe SQLite store for OAuth clients, authorization codes, and refresh tokens."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    client_secret_hash TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    auth_method TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                """
            )

    def register_client(
        self,
        client_id: str,
        client_secret: str,
        client_name: str,
        redirect_uris: list[str],
        auth_method: str,
    ) -> None:
        secret_hash = _sha256_hex(client_secret) if client_secret else ""
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO oauth_clients (
                    client_id, client_secret_hash, client_name, redirect_uris, auth_method, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    secret_hash,
                    client_name,
                    json.dumps(redirect_uris),
                    auth_method,
                    int(time.time()),
                ),
            )

    def get_client(self, client_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM oauth_clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "client_id": row["client_id"],
            "client_secret_hash": row["client_secret_hash"],
            "client_name": row["client_name"],
            "redirect_uris": json.loads(row["redirect_uris"]),
            "auth_method": row["auth_method"],
            "static": False,
        }

    def save_code(
        self,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scope: str,
        resource: str,
        expires_at: int,
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
            self._conn.execute(
                """
                INSERT INTO oauth_codes (
                    code_hash, client_id, redirect_uri, code_challenge, scope, resource, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (_sha256_hex(code), client_id, redirect_uri, code_challenge, scope, resource, expires_at),
            )

    def consume_code(self, code: str) -> Optional[dict[str, Any]]:
        code_hash = _sha256_hex(code)
        now = int(time.time())
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM oauth_codes WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM oauth_codes WHERE code_hash = ?", (code_hash,))
        if row["expires_at"] < now:
            return None
        return dict(row)

    def save_refresh_token(
        self,
        token: str,
        client_id: str,
        scope: str,
        resource: str,
        expires_at: int,
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM oauth_refresh_tokens WHERE expires_at < ?", (now,))
            self._conn.execute(
                """
                INSERT INTO oauth_refresh_tokens (token_hash, client_id, scope, resource, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (_sha256_hex(token), client_id, scope, resource, expires_at),
            )

    def consume_refresh_token(self, token: str) -> Optional[dict[str, Any]]:
        token_hash = _sha256_hex(token)
        now = int(time.time())
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM oauth_refresh_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM oauth_refresh_tokens WHERE token_hash = ?", (token_hash,))
        if row["expires_at"] < now:
            return None
        return dict(row)

    def revoke_refresh_token(self, token: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM oauth_refresh_tokens WHERE token_hash = ?",
                (_sha256_hex(token),),
            )


class OAuthService:
    """Core OAuth 2.1 protocol logic: JWT signing, client validation, and token issuance."""

    def __init__(self, config: OAuthConfig, store: OAuthStore, logger: logging.Logger) -> None:
        self.config = config
        self.store = store
        self.logger = logger

    def sign_jwt(self, payload: dict[str, Any]) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        sig = hmac.new(self.config.signing_key, signing_input, hashlib.sha256).digest()
        return f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"

    def verify_jwt(self, token: str) -> Optional[dict[str, Any]]:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        try:
            header = json.loads(_b64url_decode(header_b64))
            if header.get("alg") != "HS256":
                return None
            expected_sig = _b64url_encode(
                hmac.new(
                    self.config.signing_key,
                    f"{header_b64}.{payload_b64}".encode("ascii"),
                    hashlib.sha256,
                ).digest()
            )
            if not hmac.compare_digest(sig_b64, expected_sig):
                return None
            payload = json.loads(_b64url_decode(payload_b64))
        except (ValueError, KeyError, TypeError):
            return None

        now = int(time.time())
        if payload.get("iss") != self.config.issuer:
            return None
        exp = payload.get("exp")
        if not isinstance(exp, int) or exp < now:
            return None
        return payload

    def make_csrf_token(self, client_id: str, redirect_uri: str, code_challenge: str) -> str:
        payload = {
            "iss": self.config.issuer,
            "use": "csrf",
            "cid": client_id,
            "uri": redirect_uri,
            "cc": code_challenge,
            "exp": int(time.time()) + CSRF_TTL_SECONDS,
            "nonce": secrets.token_hex(8),
        }
        return self.sign_jwt(payload)

    def verify_csrf_token(self, token: str, client_id: str, redirect_uri: str, code_challenge: str) -> bool:
        payload = self.verify_jwt(token)
        if not payload or payload.get("use") != "csrf":
            return False
        return (
            hmac.compare_digest(str(payload.get("cid", "")), client_id)
            and hmac.compare_digest(str(payload.get("uri", "")), redirect_uri)
            and hmac.compare_digest(str(payload.get("cc", "")), code_challenge)
        )

    def resolve_client(self, client_id: str) -> Optional[dict[str, Any]]:
        if self.config.static_client_id and hmac.compare_digest(client_id, self.config.static_client_id):
            return {
                "client_id": self.config.static_client_id,
                "client_secret_hash": _sha256_hex(self.config.static_client_secret)
                if self.config.static_client_secret
                else "",
                "client_name": f"Pre-registered client ({self.config.static_client_id})",
                "redirect_uris": [],
                "auth_method": "client_secret_post" if self.config.static_client_secret else "none",
                "static": True,
            }
        return self.store.get_client(client_id)

    def is_redirect_allowed(self, client: dict[str, Any], redirect_uri: str) -> bool:
        if client.get("static"):
            return _is_valid_redirect_uri(redirect_uri, self.config.allowed_redirect_hosts)
        return redirect_uri in client.get("redirect_uris", [])

    def verify_client_secret(self, client: dict[str, Any], provided_secret: str) -> bool:
        expected_hash = client.get("client_secret_hash") or ""
        if not expected_hash:
            return True
        if not provided_secret:
            return False
        return hmac.compare_digest(_sha256_hex(provided_secret), expected_hash)

    def issue_token_pair(self, client_id: str, scope: str, resource: str) -> dict[str, Any]:
        now = int(time.time())
        effective_resource = _normalize_resource_url(resource) or self.config.issuer
        effective_scope = scope.strip() or DEFAULT_SCOPE
        access_payload = {
            "iss": self.config.issuer,
            "sub": "mcpx-owner",
            "aud": effective_resource,
            "client_id": client_id,
            "scope": effective_scope,
            "use": "access",
            "iat": now,
            "exp": now + self.config.access_token_ttl,
            "jti": secrets.token_hex(12),
        }
        access_token = self.sign_jwt(access_payload)
        refresh_token = f"mcpx_rt_{secrets.token_urlsafe(32)}"
        self.store.save_refresh_token(
            token=refresh_token,
            client_id=client_id,
            scope=effective_scope,
            resource=effective_resource,
            expires_at=now + self.config.refresh_token_ttl,
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self.config.access_token_ttl,
            "refresh_token": refresh_token,
            "scope": effective_scope,
        }

    def verify_request_token(self, token: str, forwarded_uri: str) -> tuple[bool, str]:
        """Validate either the static MCP_BEARER_TOKEN or an OAuth 2.1 access JWT."""
        if hmac.compare_digest(token, self.config.bearer_token):
            return True, "static-bearer"

        payload = self.verify_jwt(token)
        if not payload or payload.get("use") != "access":
            return False, "invalid_token"

        aud = _normalize_resource_url(str(payload.get("aud", "")))
        request_path = urllib.parse.urlsplit(forwarded_uri or "/").path.rstrip("/")
        request_resource = _normalize_resource_url(f"{self.config.issuer}{request_path}")

        if aud == _normalize_resource_url(self.config.issuer) or aud == request_resource:
            return True, f"oauth:{payload.get('client_id', 'unknown')}"
        return False, "invalid_audience"

    def protected_resource_metadata(self, suffix_path: str) -> dict[str, Any]:
        clean_suffix = suffix_path.strip("/")
        resource_url = f"{self.config.issuer}/{clean_suffix}" if clean_suffix else self.config.issuer
        return {
            "resource": resource_url,
            "authorization_servers": [self.config.issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [DEFAULT_SCOPE],
        }

    def authorization_server_metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.config.issuer,
            "authorization_endpoint": f"{self.config.issuer}/oauth/authorize",
            "token_endpoint": f"{self.config.issuer}/oauth/token",
            "registration_endpoint": f"{self.config.issuer}/oauth/register",
            "revocation_endpoint": f"{self.config.issuer}/oauth/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": [
                "none",
                "client_secret_post",
                "client_secret_basic",
            ],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [DEFAULT_SCOPE],
        }


def _render_consent_page(
    *,
    client_name: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    resource: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    csrf_token: str,
    error_message: str = "",
) -> str:
    err_html = (
        f'<div class="err">{html.escape(error_message)}</div>' if error_message else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Authorize MCPX Access</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f1117; color: #e6edf3;
         margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 16px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; max-width: 440px;
           width: 100%; padding: 28px; box-sizing: border-box; box-shadow: 0 12px 32px rgba(0,0,0,.45); }}
  h1 {{ margin: 0 0 8px; font-size: 20px; }}
  p {{ margin: 0 0 18px; color: #8b949e; font-size: 14px; line-height: 1.5; }}
  .meta {{ background: #0d1117; border: 1px solid #21262d; border-radius: 8px; padding: 12px;
           margin-bottom: 18px; font-size: 13px; word-break: break-all; }}
  .meta div {{ margin-bottom: 6px; }}
  .meta div:last-child {{ margin-bottom: 0; }}
  .label {{ color: #8b949e; font-weight: 600; }}
  label {{ display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }}
  input[type="password"] {{ width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px;
                            border: 1px solid #30363d; background: #0d1117; color: #e6edf3;
                            font-size: 14px; margin-bottom: 16px; }}
  .err {{ background: #3b1219; border: 1px solid #f85149; color: #ffa198; padding: 10px 12px;
          border-radius: 8px; font-size: 13px; margin-bottom: 16px; }}
  .actions {{ display: flex; gap: 10px; }}
  button {{ flex: 1; padding: 10px 14px; border-radius: 8px; border: 0; font-size: 14px;
            font-weight: 600; cursor: pointer; }}
  .approve {{ background: #238636; color: #fff; }}
  .approve:hover {{ background: #2ea043; }}
  .deny {{ background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Authorize MCPX Gateway</h1>
    <p>An application is requesting OAuth 2.1 access to your MCPX tools.</p>
    {err_html}
    <div class="meta">
      <div><span class="label">Application:</span> {html.escape(client_name)}</div>
      <div><span class="label">Client ID:</span> {html.escape(client_id)}</div>
      <div><span class="label">Redirect URI:</span> {html.escape(redirect_uri)}</div>
      <div><span class="label">Resource:</span> {html.escape(resource)}</div>
      <div><span class="label">Scope:</span> {html.escape(scope)}</div>
    </div>
    <form method="POST" action="/oauth/authorize">
      <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
      <input type="hidden" name="client_id" value="{html.escape(client_id)}" />
      <input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}" />
      <input type="hidden" name="state" value="{html.escape(state)}" />
      <input type="hidden" name="scope" value="{html.escape(scope)}" />
      <input type="hidden" name="resource" value="{html.escape(resource)}" />
      <input type="hidden" name="code_challenge" value="{html.escape(code_challenge)}" />
      <input type="hidden" name="code_challenge_method" value="{html.escape(code_challenge_method)}" />
      <label for="password">Gateway Token / Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required autofocus />
      <div class="actions">
        <button type="submit" name="action" value="deny" class="deny" formnovalidate>Deny</button>
        <button type="submit" name="action" value="approve" class="approve">Authorize</button>
      </div>
    </form>
  </div>
</body>
</html>
"""


def _make_handler(service: OAuthService) -> type[BaseHTTPRequestHandler]:
    class OAuthHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            service.logger.debug("http %s", format % args)

        def _send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization, Content-Type, Mcp-Session-Id, Mcp-Protocol-Version",
            )

        def _send_json(
            self,
            status: int,
            body: dict[str, Any],
            extra_headers: Optional[dict[str, str]] = None,
        ) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(encoded)

        def _send_html(self, status: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_empty(self, status: int, extra_headers: Optional[dict[str, str]] = None) -> None:
            self.send_response(status)
            self._send_cors_headers()
            self.send_header("Content-Length", "0")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or "0")
            if length > MAX_REQUEST_BODY_BYTES:
                raise ValueError("Request body too large")
            return self.rfile.read(length) if length > 0 else b""

        def _extract_basic_client_auth(self) -> tuple[str, str]:
            auth_header = (self.headers.get("Authorization") or "").strip()
            if not auth_header.lower().startswith("basic "):
                return "", ""
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
                cid, csec = decoded.split(":", 1)
                return urllib.parse.unquote_plus(cid), urllib.parse.unquote_plus(csec)
            except (ValueError, UnicodeDecodeError):
                return "", ""

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send_empty(HTTPStatus.NO_CONTENT)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path

            if path == "/_healthz":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": SERVICE_NAME,
                        "time": datetime.now(timezone.utc).isoformat(),
                    },
                )
                return

            if path == "/oauth/verify":
                self._handle_verify()
                return

            if path == "/.well-known/oauth-protected-resource":
                self._send_json(HTTPStatus.OK, service.protected_resource_metadata(""))
                return

            if path.startswith("/.well-known/oauth-protected-resource/"):
                suffix = path[len("/.well-known/oauth-protected-resource/") :]
                self._send_json(HTTPStatus.OK, service.protected_resource_metadata(suffix))
                return

            if path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
                self._send_json(HTTPStatus.OK, service.authorization_server_metadata())
                return

            if path == "/oauth/authorize":
                self._handle_authorize_get(parsed.query)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path

            if path == "/oauth/verify":
                self._handle_verify()
                return
            if path == "/oauth/register":
                self._handle_register()
                return
            if path == "/oauth/authorize":
                self._handle_authorize_post()
                return
            if path == "/oauth/token":
                self._handle_token()
                return
            if path == "/oauth/revoke":
                self._handle_revoke()
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_DELETE(self) -> None:  # noqa: N802
            if urllib.parse.urlsplit(self.path).path == "/oauth/verify":
                self._handle_verify()
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def _handle_verify(self) -> None:
            forwarded_uri = self.headers.get("X-Forwarded-Uri") or "/"
            forwarded_path = urllib.parse.urlsplit(forwarded_uri).path
            auth_header = (self.headers.get("Authorization") or "").strip()

            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()
                ok, principal = service.verify_request_token(token, forwarded_uri)
                if ok:
                    self._send_empty(
                        HTTPStatus.OK,
                        extra_headers={
                            "X-Mcpx-Auth": f"Bearer {service.config.bearer_token}",
                            "X-Mcpx-Principal": principal,
                        },
                    )
                    return

            if forwarded_path and forwarded_path not in ("/", "/_healthz", "/_readyz"):
                metadata_url = f"{service.config.issuer}/.well-known/oauth-protected-resource{forwarded_path}"
            else:
                metadata_url = f"{service.config.issuer}/.well-known/oauth-protected-resource"

            www_auth = f'Bearer resource_metadata="{metadata_url}", scope="{DEFAULT_SCOPE}"'
            service.logger.info("Rejected unauthorized request uri=%s", forwarded_path)
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "unauthorized", "error_description": "Valid Bearer or OAuth 2.1 token required"},
                extra_headers={"WWW-Authenticate": www_auth},
            )

        def _handle_register(self) -> None:
            try:
                body = json.loads(self._read_body().decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_client_metadata"})
                return

            redirect_uris = body.get("redirect_uris")
            if not isinstance(redirect_uris, list) or not redirect_uris:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_redirect_uri", "error_description": "redirect_uris must be a non-empty list"},
                )
                return

            cleaned_uris: list[str] = []
            for uri in redirect_uris:
                if not isinstance(uri, str) or not _is_valid_redirect_uri(uri):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_redirect_uri", "error_description": f"Invalid redirect_uri: {uri!r}"},
                    )
                    return
                cleaned_uris.append(uri)

            auth_method = str(body.get("token_endpoint_auth_method") or "none")
            if auth_method not in ("none", "client_secret_post", "client_secret_basic"):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_client_metadata", "error_description": "Unsupported auth method"},
                )
                return

            client_name = str(body.get("client_name") or "MCP Client")[:120]
            client_id = f"mcpx_cli_{secrets.token_hex(12)}"
            client_secret = f"mcpx_sec_{secrets.token_urlsafe(24)}" if auth_method != "none" else ""
            service.store.register_client(
                client_id=client_id,
                client_secret=client_secret,
                client_name=client_name,
                redirect_uris=cleaned_uris,
                auth_method=auth_method,
            )
            service.logger.info("Registered DCR client client_id=%s name=%s", client_id, client_name)

            resp: dict[str, Any] = {
                "client_id": client_id,
                "client_id_issued_at": int(time.time()),
                "client_name": client_name,
                "redirect_uris": cleaned_uris,
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": auth_method,
            }
            if client_secret:
                resp["client_secret"] = client_secret
                resp["client_secret_expires_at"] = 0
            self._send_json(HTTPStatus.CREATED, resp)

        def _validate_authorize_params(
            self, params: dict[str, str]
        ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
            response_type = params.get("response_type", "")
            client_id = params.get("client_id", "")
            redirect_uri = params.get("redirect_uri", "")
            code_challenge = params.get("code_challenge", "")
            code_challenge_method = params.get("code_challenge_method", "")

            if response_type != "code":
                return None, "Unsupported response_type (expected 'code')."
            client = service.resolve_client(client_id)
            if client is None:
                return None, "Unknown client_id."
            if not redirect_uri or not service.is_redirect_allowed(client, redirect_uri):
                return None, "Invalid or unregistered redirect_uri."
            if not code_challenge or code_challenge_method != "S256":
                return None, "PKCE with code_challenge_method=S256 is required."
            return client, None

        def _handle_authorize_get(self, raw_query: str) -> None:
            params = {k: v[0] for k, v in urllib.parse.parse_qs(raw_query, keep_blank_values=True).items()}
            client, err = self._validate_authorize_params(params)
            if err or client is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "error_description": err})
                return

            scope = params.get("scope") or DEFAULT_SCOPE
            resource = _normalize_resource_url(params.get("resource") or "") or service.config.issuer
            csrf_token = service.make_csrf_token(
                params["client_id"],
                params["redirect_uri"],
                params["code_challenge"],
            )
            page = _render_consent_page(
                client_name=client["client_name"],
                client_id=params["client_id"],
                redirect_uri=params["redirect_uri"],
                scope=scope,
                resource=resource,
                state=params.get("state", ""),
                code_challenge=params["code_challenge"],
                code_challenge_method=params["code_challenge_method"],
                csrf_token=csrf_token,
            )
            self._send_html(HTTPStatus.OK, page)

        def _handle_authorize_post(self) -> None:
            try:
                raw = self._read_body().decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                return

            params = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
            params.setdefault("response_type", "code")
            client, err = self._validate_authorize_params(params)
            if err or client is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "error_description": err})
                return

            csrf_ok = service.verify_csrf_token(
                params.get("csrf_token", ""),
                params["client_id"],
                params["redirect_uri"],
                params["code_challenge"],
            )
            if not csrf_ok:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_request", "error_description": "Expired or invalid consent session"},
                )
                return

            redirect_uri = params["redirect_uri"]
            state = params.get("state", "")
            scope = params.get("scope") or DEFAULT_SCOPE
            resource = _normalize_resource_url(params.get("resource") or "") or service.config.issuer

            if params.get("action") != "approve":
                target = _append_query_params(redirect_uri, {"error": "access_denied", "state": state})
                self._send_empty(HTTPStatus.FOUND, extra_headers={"Location": target})
                return

            password = params.get("password", "")
            if not hmac.compare_digest(password, service.config.consent_password):
                service.logger.warning("Failed consent authentication client_id=%s", params["client_id"])
                csrf_token = service.make_csrf_token(
                    params["client_id"],
                    redirect_uri,
                    params["code_challenge"],
                )
                page = _render_consent_page(
                    client_name=client["client_name"],
                    client_id=params["client_id"],
                    redirect_uri=redirect_uri,
                    scope=scope,
                    resource=resource,
                    state=state,
                    code_challenge=params["code_challenge"],
                    code_challenge_method=params["code_challenge_method"],
                    csrf_token=csrf_token,
                    error_message="Invalid gateway token or password.",
                )
                self._send_html(HTTPStatus.UNAUTHORIZED, page)
                return

            code = f"mcpx_code_{secrets.token_urlsafe(32)}"
            service.store.save_code(
                code=code,
                client_id=params["client_id"],
                redirect_uri=redirect_uri,
                code_challenge=params["code_challenge"],
                scope=scope,
                resource=resource,
                expires_at=int(time.time()) + service.config.code_ttl,
            )
            service.logger.info("Issued authorization code client_id=%s resource=%s", params["client_id"], resource)
            target = _append_query_params(redirect_uri, {"code": code, "state": state})
            self._send_empty(HTTPStatus.FOUND, extra_headers={"Location": target})

        def _parse_token_params(self) -> Optional[dict[str, str]]:
            try:
                raw = self._read_body().decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "application/json" in ctype:
                try:
                    data = json.loads(raw or "{}")
                    return {str(k): str(v) for k, v in data.items() if v is not None}
                except ValueError:
                    return None
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

        def _handle_token(self) -> None:
            params = self._parse_token_params()
            if params is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                return

            basic_id, basic_secret = self._extract_basic_client_auth()
            client_id = basic_id or params.get("client_id", "")
            client_secret = basic_secret or params.get("client_secret", "")
            grant_type = params.get("grant_type", "")

            client = service.resolve_client(client_id)
            if client is None or not service.verify_client_secret(client, client_secret):
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_client"})
                return

            if grant_type == "authorization_code":
                code = params.get("code", "")
                redirect_uri = params.get("redirect_uri", "")
                code_verifier = params.get("code_verifier", "")
                record = service.store.consume_code(code)
                if record is None:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"})
                    return
                if record["client_id"] != client_id or record["redirect_uri"] != redirect_uri:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"})
                    return
                if not code_verifier or not hmac.compare_digest(_pkce_s256(code_verifier), record["code_challenge"]):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"})
                    return

                requested_resource = _normalize_resource_url(params.get("resource") or "")
                if requested_resource and requested_resource != record["resource"]:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_target"})
                    return

                tokens = service.issue_token_pair(client_id, record["scope"], record["resource"])
                service.logger.info("Exchanged auth code for tokens client_id=%s", client_id)
                self._send_json(HTTPStatus.OK, tokens)
                return

            if grant_type == "refresh_token":
                refresh_token = params.get("refresh_token", "")
                record = service.store.consume_refresh_token(refresh_token)
                if record is None or record["client_id"] != client_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"})
                    return
                tokens = service.issue_token_pair(client_id, record["scope"], record["resource"])
                service.logger.info("Rotated refresh token client_id=%s", client_id)
                self._send_json(HTTPStatus.OK, tokens)
                return

            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unsupported_grant_type"})

        def _handle_revoke(self) -> None:
            params = self._parse_token_params() or {}
            token = params.get("token", "")
            if token:
                service.store.revoke_refresh_token(token)
                service.logger.info("Processed token revocation request")
            self._send_empty(HTTPStatus.OK)

    return OAuthHandler


def main() -> int:
    config = OAuthConfig()
    logger = _configure_logging(config.log_dir)
    store = OAuthStore(config.db_path)
    service = OAuthService(config, store, logger)

    server = ThreadingHTTPServer(("0.0.0.0", config.port), _make_handler(service))
    logger.info(
        "Starting %s on :%d (issuer=%s, static_client=%s)",
        SERVICE_NAME,
        config.port,
        config.issuer,
        bool(config.static_client_id),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down %s", SERVICE_NAME)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
