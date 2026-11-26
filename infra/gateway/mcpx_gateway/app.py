"""Starlette ASGI application for mcpx-gateway HTTP, auth, and MCP routing.

Fits into MCPX as the HTTP server layer running inside `mcpx-gateway:9000` behind Caddy.
Manages lifespan startup/shutdown for `UpstreamRegistry` and all stateless
`StreamableHTTPSessionManager` endpoints (`/mcp`, `/<server>/mcp`, `/groups/<group>/mcp`)
without trailing-slash redirects, alongside `/_healthz` and `/_readyz`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
import hmac
import logging

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from mcpx_gateway.config import GatewayConfig
from mcpx_gateway.router import build_group_mcp, build_server_mcp, build_unified_mcp
from mcpx_gateway.upstream import UpstreamRegistry

logger = logging.getLogger("mcpx-gateway.app")


class GatewayApp:
    """ASGI application dispatching health checks and exact-path MCP endpoints."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._registry = UpstreamRegistry(config)
        self._managers: dict[str, StreamableHTTPSessionManager] = {}
        self._lifespan_app = Starlette(lifespan=self._lifespan)

    @asynccontextmanager
    async def _lifespan(self, _app: Starlette) -> AsyncIterator[None]:
        version = self._config.gateway.version
        await self._registry.start()

        managers: dict[str, StreamableHTTPSessionManager] = {
            "/mcp": StreamableHTTPSessionManager(
                app=build_unified_mcp(self._config.gateway.name, self._registry, version),
                stateless=True,
            ),
        }
        for server_name in self._config.servers:
            managers[f"/{server_name}/mcp"] = StreamableHTTPSessionManager(
                app=build_server_mcp(server_name, self._registry, version),
                stateless=True,
            )
        for group_name, group_cfg in self._config.groups.items():
            managers[f"/groups/{group_name}/mcp"] = StreamableHTTPSessionManager(
                app=build_group_mcp(group_cfg, self._registry, version),
                stateless=True,
            )

        async with AsyncExitStack() as stack:
            for manager in managers.values():
                await stack.enter_async_context(manager.run())
            self._managers = managers
            logger.info(
                "mcpx-gateway listening on %s:%d with %d MCP endpoints (%d servers, %d groups)",
                self._config.gateway.host,
                self._config.gateway.port,
                len(managers),
                len(self._config.servers),
                len(self._config.groups),
            )
            try:
                yield
            finally:
                self._managers = {}
                await self._registry.stop()

    def _is_authorized(self, scope: Scope) -> bool:
        tokens = self._config.gateway.auth_tokens
        if not tokens:
            return True
        auth_header = ""
        for raw_key, raw_val in scope.get("headers", ()):
            if raw_key.lower() == b"authorization":
                auth_header = raw_val.decode("latin-1").strip()
                break
        if not auth_header.startswith("Bearer "):
            return False
        presented = auth_header[7:].strip()
        if not presented:
            return False
        return any(hmac.compare_digest(presented, expected) for expected in tokens)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self._lifespan_app(scope, receive, send)
            return

        if scope_type != "http":
            return

        path: str = scope["path"]
        if path == "/_healthz":
            payload = self._registry.health_snapshot()
            payload["groupCount"] = len(self._config.groups)
            response = JSONResponse(payload, status_code=200)
            await response(scope, receive, send)
            return

        if path == "/_readyz":
            status_code, payload = self._registry.readiness_status()
            response = JSONResponse(payload, status_code=status_code)
            await response(scope, receive, send)
            return

        manager = self._managers.get(path)
        if manager is not None:
            if not self._is_authorized(scope):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
            await manager.handle_request(scope, receive, send)
            return

        response = JSONResponse({"error": "not_found"}, status_code=404)
        await response(scope, receive, send)


def create_app(config: GatewayConfig) -> GatewayApp:
    """Create the ASGI application for a validated gateway configuration."""
    return GatewayApp(config)
