"""Upstream MCP client lifecycle, health monitoring, and registry for mcpx-gateway.

Fits into MCPX by managing persistent connections to every configured stdio and
Streamable HTTP MCP server, caching their tools/resources/prompts, running periodic
pings with automatic reconnects, and reporting health/readiness to `app.py`.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import logging
import os
import sys
import time
from typing import Any

import httpx
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from mcpx_gateway.config import GatewayConfig, ServerConfig

logger = logging.getLogger("mcpx-gateway.upstream")


class UpstreamClient:
    """Manages one downstream MCP server with background reconnect and ping loop."""

    def __init__(
        self,
        config: ServerConfig,
        ping_interval: float,
        reconnect_interval: float,
        stdio_start_lock: asyncio.Semaphore | None = None,
    ) -> None:
        self.config = config
        self._ping_interval = ping_interval
        self._reconnect_interval = reconnect_interval
        self._stdio_start_lock = stdio_start_lock

        self.healthy: bool = False
        self.last_error: str | None = "starting"
        self.tools: list[types.Tool] = []
        self.resources: list[types.Resource] = []
        self.prompts: list[types.Prompt] = []

        self._session: ClientSession | None = None
        self._stop_event = asyncio.Event()
        self._reconnect_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background connection and supervision task."""
        if self._task is None:
            self._task = asyncio.create_task(
                self._run_loop(),
                name=f"mcpx-upstream-{self.config.name}",
            )

    async def stop(self) -> None:
        """Signal shutdown and wait for the background task to close cleanly."""
        self._stop_event.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._session = None
            self.healthy = False

    async def _wait_for_signal(self, timeout: float, include_reconnect: bool) -> str:
        """Wait for stop/reconnect events or timeout; returns 'stop', 'reconnect', or 'timeout'."""
        waiters: dict[asyncio.Task[bool], str] = {
            asyncio.create_task(self._stop_event.wait()): "stop",
        }
        if include_reconnect:
            waiters[asyncio.create_task(self._reconnect_event.wait())] = "reconnect"

        done, pending = await asyncio.wait(
            waiters.keys(),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if not done:
            return "timeout"
        for task in done:
            if waiters[task] == "stop":
                return "stop"
        return "reconnect"

    async def _discover_capabilities(
        self,
        session: ClientSession,
        init_result: types.InitializeResult,
    ) -> None:
        """Fetch tools, resources, and prompts advertised by the upstream server."""
        tools: list[types.Tool] = []
        resources: list[types.Resource] = []
        prompts: list[types.Prompt] = []

        try:
            tools_result = await asyncio.wait_for(session.list_tools(), timeout=self.config.timeout)
            tools = list(tools_result.tools)
        except McpError as exc:
            logger.debug("Server '%s' list_tools unsupported or failed: %s", self.config.name, exc)

        if init_result.capabilities.resources is not None:
            try:
                resources_result = await asyncio.wait_for(
                    session.list_resources(),
                    timeout=self.config.timeout,
                )
                resources = list(resources_result.resources)
            except McpError as exc:
                logger.debug(
                    "Server '%s' list_resources unsupported or failed: %s",
                    self.config.name,
                    exc,
                )

        if init_result.capabilities.prompts is not None:
            try:
                prompts_result = await asyncio.wait_for(
                    session.list_prompts(),
                    timeout=self.config.timeout,
                )
                prompts = list(prompts_result.prompts)
            except McpError as exc:
                logger.debug(
                    "Server '%s' list_prompts unsupported or failed: %s",
                    self.config.name,
                    exc,
                )

        self.tools = tools
        self.resources = resources
        self.prompts = prompts

    async def _connect_and_supervise(self) -> None:
        """Open transport, initialize session, cache capabilities, and run ping loop."""
        async with AsyncExitStack() as stack:
            if self.config.transport == "stdio":
                assert self.config.command is not None
                params = StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    env={**os.environ, **self.config.env},
                )
                # Serialize stdio cold starts so concurrent uv/npx installs do not
                # saturate low-CPU/RAM VMs or contend on package cache locks.
                if self._stdio_start_lock is not None:
                    await self._stdio_start_lock.acquire()
                try:
                    read_stream, write_stream = await stack.enter_async_context(
                        stdio_client(params, errlog=sys.stderr)
                    )
                    session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                    init_result = await asyncio.wait_for(session.initialize(), timeout=self.config.timeout)
                    await self._discover_capabilities(session, init_result)
                finally:
                    if self._stdio_start_lock is not None:
                        self._stdio_start_lock.release()
            else:
                assert self.config.url is not None
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=self.config.headers or None,
                        timeout=httpx.Timeout(
                            self.config.timeout,
                            read=max(self.config.timeout, 300.0),
                        ),
                        follow_redirects=True,
                    )
                )
                read_stream, write_stream, _ = await stack.enter_async_context(
                    streamable_http_client(self.config.url, http_client=http_client)
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                init_result = await asyncio.wait_for(session.initialize(), timeout=self.config.timeout)
                await self._discover_capabilities(session, init_result)

            self._session = session
            self._reconnect_event.clear()
            self.healthy = True
            self.last_error = None
            logger.info(
                "Upstream '%s' (%s) ready: %d tools, %d resources, %d prompts",
                self.config.name,
                self.config.transport,
                len(self.tools),
                len(self.resources),
                len(self.prompts),
            )

            ping_timeout = min(self.config.timeout, 15.0)
            consecutive_ping_failures = 0
            while not self._stop_event.is_set():
                signal = await self._wait_for_signal(self._ping_interval, include_reconnect=True)
                if signal == "stop":
                    return
                if signal == "reconnect":
                    raise RuntimeError("reconnect requested after transport failure")
                try:
                    await asyncio.wait_for(session.send_ping(), timeout=ping_timeout)
                    consecutive_ping_failures = 0
                except Exception as exc:
                    consecutive_ping_failures += 1
                    if consecutive_ping_failures >= 2:
                        raise
                    logger.warning(
                        "Upstream '%s' ping failed (%d/2): %s",
                        self.config.name,
                        consecutive_ping_failures,
                        exc,
                    )

    async def _run_loop(self) -> None:
        """Continuously maintain the upstream session until shutdown."""
        while not self._stop_event.is_set():
            try:
                await self._connect_and_supervise()
                if self._stop_event.is_set():
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.healthy = False
                self._session = None
                self.last_error = str(exc) or exc.__class__.__name__
                logger.warning(
                    "Upstream '%s' disconnected (%s); reconnecting in %.1fs",
                    self.config.name,
                    self.last_error,
                    self._reconnect_interval,
                )

            if self._stop_event.is_set():
                break
            signal = await self._wait_for_signal(self._reconnect_interval, include_reconnect=False)
            if signal == "stop":
                break

        self.healthy = False
        self._session = None

    def _require_session(self) -> ClientSession:
        session = self._session
        if session is None or not self.healthy:
            raise McpError(
                types.ErrorData(
                    code=types.INTERNAL_ERROR,
                    message=f"Upstream server '{self.config.name}' is unavailable",
                )
            )
        return session

    def _handle_transport_error(self, exc: Exception) -> McpError:
        self.healthy = False
        self.last_error = str(exc) or exc.__class__.__name__
        self._reconnect_event.set()
        logger.warning("Upstream '%s' request failed: %s", self.config.name, self.last_error)
        return McpError(
            types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"Upstream server '{self.config.name}' failed: {self.last_error}",
            )
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        """Invoke a tool on the upstream server within its configured timeout."""
        session = self._require_session()
        try:
            return await asyncio.wait_for(
                session.call_tool(name, arguments=arguments),
                timeout=self.config.timeout,
            )
        except McpError:
            raise
        except asyncio.TimeoutError as exc:
            raise McpError(
                types.ErrorData(
                    code=types.INTERNAL_ERROR,
                    message=f"Upstream server '{self.config.name}' timed out after {self.config.timeout:.0f}s",
                )
            ) from exc
        except Exception as exc:
            raise self._handle_transport_error(exc) from exc

    async def read_resource(self, uri: AnyUrl) -> types.ReadResourceResult:
        """Read a resource from the upstream server within its configured timeout."""
        session = self._require_session()
        try:
            return await asyncio.wait_for(
                session.read_resource(uri),
                timeout=self.config.timeout,
            )
        except McpError:
            raise
        except asyncio.TimeoutError as exc:
            raise McpError(
                types.ErrorData(
                    code=types.INTERNAL_ERROR,
                    message=f"Upstream server '{self.config.name}' timed out after {self.config.timeout:.0f}s",
                )
            ) from exc
        except Exception as exc:
            raise self._handle_transport_error(exc) from exc

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> types.GetPromptResult:
        """Fetch a prompt from the upstream server within its configured timeout."""
        session = self._require_session()
        try:
            return await asyncio.wait_for(
                session.get_prompt(name, arguments=arguments),
                timeout=self.config.timeout,
            )
        except McpError:
            raise
        except asyncio.TimeoutError as exc:
            raise McpError(
                types.ErrorData(
                    code=types.INTERNAL_ERROR,
                    message=f"Upstream server '{self.config.name}' timed out after {self.config.timeout:.0f}s",
                )
            ) from exc
        except Exception as exc:
            raise self._handle_transport_error(exc) from exc


class UpstreamRegistry:
    """Starts, supervises, and reports health across all configured upstream servers."""

    def __init__(self, config: GatewayConfig) -> None:
        self._startup_grace_period = config.gateway.startup_grace_period
        self.started_at: float = time.monotonic()
        stdio_start_lock = asyncio.Semaphore(1)
        self._clients: dict[str, UpstreamClient] = {
            name: UpstreamClient(
                server_cfg,
                ping_interval=config.gateway.ping_interval,
                reconnect_interval=config.gateway.reconnect_interval,
                stdio_start_lock=stdio_start_lock,
            )
            for name, server_cfg in config.servers.items()
        }

    async def start(self) -> None:
        """Launch all upstream connection loops concurrently."""
        self.started_at = time.monotonic()
        for client in self._clients.values():
            client.start()

    async def stop(self) -> None:
        """Stop all upstream connection loops concurrently."""
        await asyncio.gather(
            *(client.stop() for client in self._clients.values()),
            return_exceptions=True,
        )

    def get(self, name: str) -> UpstreamClient | None:
        return self._clients.get(name)

    def healthy_clients(self) -> list[UpstreamClient]:
        return [client for client in self._clients.values() if client.healthy]

    def _server_statuses(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "healthy": client.healthy,
                "transport": client.config.transport,
                "tools": len(client.tools),
                "error": client.last_error,
            }
            for name, client in self._clients.items()
        }

    def health_snapshot(self) -> dict[str, Any]:
        """Return liveness payload for `/_healthz`."""
        return {
            "status": "ok",
            "serverCount": len(self._clients),
            "servers": self._server_statuses(),
        }

    def readiness_status(self) -> tuple[int, dict[str, Any]]:
        """Return HTTP status code and readiness payload for `/_readyz`."""
        unhealthy = [name for name, client in self._clients.items() if not client.healthy]
        healthy_count = len(self._clients) - len(unhealthy)
        elapsed = time.monotonic() - self.started_at

        if not unhealthy:
            http_status, state = 200, "ready"
        elif healthy_count > 0 and elapsed >= self._startup_grace_period:
            http_status, state = 200, "degraded"
        elif elapsed < self._startup_grace_period:
            http_status, state = 503, "initializing"
        else:
            http_status, state = 503, "unavailable"

        return http_status, {
            "status": state,
            "servers": self._server_statuses(),
            "unhealthy": unhealthy,
        }
