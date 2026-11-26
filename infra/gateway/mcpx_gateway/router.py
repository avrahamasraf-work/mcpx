"""MCP server builders for per-server, unified, and group-scoped gateway endpoints.

Fits into MCPX as the protocol routing layer between `app.py`'s Streamable HTTP
session managers and `upstream.py`'s `UpstreamRegistry`. Registers low-level MCP
request handlers directly so upstream `CallToolResult` payloads (including audio,
images, structured content, and `isError`) pass through without loss.
"""

from __future__ import annotations

from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import McpError

from mcpx_gateway.config import GroupConfig
from mcpx_gateway.upstream import UpstreamClient, UpstreamRegistry

_NAMESPACE_SEP = "__"


def _namespace(server_name: str, item_name: str) -> str:
    return f"{server_name}{_NAMESPACE_SEP}{item_name}"


def _split_namespace(namespaced: str) -> tuple[str, str]:
    if _NAMESPACE_SEP not in namespaced:
        raise McpError(
            types.ErrorData(
                code=types.INVALID_PARAMS,
                message=f"Expected namespaced identifier '<server>{_NAMESPACE_SEP}<name>', got '{namespaced}'",
            )
        )
    server_name, item_name = namespaced.split(_NAMESPACE_SEP, 1)
    if not server_name or not item_name:
        raise McpError(
            types.ErrorData(
                code=types.INVALID_PARAMS,
                message=f"Invalid namespaced identifier '{namespaced}'",
            )
        )
    return server_name, item_name


def _find_resource_owner(clients: list[UpstreamClient], uri: str) -> UpstreamClient | None:
    for client in clients:
        if any(str(resource.uri) == uri for resource in client.resources):
            return client
    return None


def build_server_mcp(server_name: str, registry: UpstreamRegistry, version: str) -> Server:
    """Build a direct MCP server proxying one upstream with unprefixed tool names."""
    server = Server(name=server_name, version=version)

    async def _list_tools(_req: types.ListToolsRequest) -> types.ServerResult:
        client = registry.get(server_name)
        tools = list(client.tools) if client and client.healthy else []
        return types.ServerResult(types.ListToolsResult(tools=tools))

    async def _call_tool(req: types.CallToolRequest) -> types.ServerResult:
        client = registry.get(server_name)
        if client is None:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Unknown server '{server_name}'",
                )
            )
        result = await client.call_tool(req.params.name, req.params.arguments)
        return types.ServerResult(result)

    async def _list_resources(_req: types.ListResourcesRequest) -> types.ServerResult:
        client = registry.get(server_name)
        resources = list(client.resources) if client and client.healthy else []
        return types.ServerResult(types.ListResourcesResult(resources=resources))

    async def _read_resource(req: types.ReadResourceRequest) -> types.ServerResult:
        client = registry.get(server_name)
        if client is None:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Unknown server '{server_name}'",
                )
            )
        result = await client.read_resource(req.params.uri)
        return types.ServerResult(result)

    async def _list_prompts(_req: types.ListPromptsRequest) -> types.ServerResult:
        client = registry.get(server_name)
        prompts = list(client.prompts) if client and client.healthy else []
        return types.ServerResult(types.ListPromptsResult(prompts=prompts))

    async def _get_prompt(req: types.GetPromptRequest) -> types.ServerResult:
        client = registry.get(server_name)
        if client is None:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Unknown server '{server_name}'",
                )
            )
        result = await client.get_prompt(req.params.name, req.params.arguments)
        return types.ServerResult(result)

    server.request_handlers[types.ListToolsRequest] = _list_tools
    server.request_handlers[types.CallToolRequest] = _call_tool
    server.request_handlers[types.ListResourcesRequest] = _list_resources
    server.request_handlers[types.ReadResourceRequest] = _read_resource
    server.request_handlers[types.ListPromptsRequest] = _list_prompts
    server.request_handlers[types.GetPromptRequest] = _get_prompt
    return server


def build_unified_mcp(gateway_name: str, registry: UpstreamRegistry, version: str) -> Server:
    """Build the unified `/mcp` server aggregating all healthy upstreams with `<server>__` prefixes."""
    server = Server(name=gateway_name, version=version)

    async def _list_tools(_req: types.ListToolsRequest) -> types.ServerResult:
        tools: list[types.Tool] = []
        for client in registry.healthy_clients():
            for tool in client.tools:
                tools.append(tool.model_copy(update={"name": _namespace(client.config.name, tool.name)}))
        return types.ServerResult(types.ListToolsResult(tools=tools))

    async def _call_tool(req: types.CallToolRequest) -> types.ServerResult:
        server_name, tool_name = _split_namespace(req.params.name)
        client = registry.get(server_name)
        if client is None:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Unknown server '{server_name}' in tool '{req.params.name}'",
                )
            )
        result = await client.call_tool(tool_name, req.params.arguments)
        return types.ServerResult(result)

    async def _list_resources(_req: types.ListResourcesRequest) -> types.ServerResult:
        resources: list[types.Resource] = []
        for client in registry.healthy_clients():
            resources.extend(client.resources)
        return types.ServerResult(types.ListResourcesResult(resources=resources))

    async def _read_resource(req: types.ReadResourceRequest) -> types.ServerResult:
        uri_str = str(req.params.uri)
        owner = _find_resource_owner(registry.healthy_clients(), uri_str)
        if owner is None:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Resource '{uri_str}' not found on any healthy server",
                )
            )
        result = await owner.read_resource(req.params.uri)
        return types.ServerResult(result)

    async def _list_prompts(_req: types.ListPromptsRequest) -> types.ServerResult:
        prompts: list[types.Prompt] = []
        for client in registry.healthy_clients():
            for prompt in client.prompts:
                prompts.append(
                    prompt.model_copy(update={"name": _namespace(client.config.name, prompt.name)})
                )
        return types.ServerResult(types.ListPromptsResult(prompts=prompts))

    async def _get_prompt(req: types.GetPromptRequest) -> types.ServerResult:
        server_name, prompt_name = _split_namespace(req.params.name)
        client = registry.get(server_name)
        if client is None:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Unknown server '{server_name}' in prompt '{req.params.name}'",
                )
            )
        result = await client.get_prompt(prompt_name, req.params.arguments)
        return types.ServerResult(result)

    server.request_handlers[types.ListToolsRequest] = _list_tools
    server.request_handlers[types.CallToolRequest] = _call_tool
    server.request_handlers[types.ListResourcesRequest] = _list_resources
    server.request_handlers[types.ReadResourceRequest] = _read_resource
    server.request_handlers[types.ListPromptsRequest] = _list_prompts
    server.request_handlers[types.GetPromptRequest] = _get_prompt
    return server


def build_group_mcp(group: GroupConfig, registry: UpstreamRegistry, version: str) -> Server:
    """Build a group-scoped `/groups/<group>/mcp` server enforcing per-server tool allow-lists."""
    server = Server(
        name=f"mcpx-group-{group.name}",
        version=version,
        instructions=group.description or None,
    )

    def _wildcard_clients() -> list[UpstreamClient]:
        clients: list[UpstreamClient] = []
        for server_name, spec in group.servers.items():
            if spec != "*":
                continue
            client = registry.get(server_name)
            if client and client.healthy:
                clients.append(client)
        return clients

    async def _list_tools(_req: types.ListToolsRequest) -> types.ServerResult:
        tools: list[types.Tool] = []
        for server_name, spec in group.servers.items():
            client = registry.get(server_name)
            if not client or not client.healthy:
                continue
            allowed = None if spec == "*" else set(spec)
            for tool in client.tools:
                if allowed is None or tool.name in allowed:
                    tools.append(tool.model_copy(update={"name": _namespace(server_name, tool.name)}))
        return types.ServerResult(types.ListToolsResult(tools=tools))

    async def _call_tool(req: types.CallToolRequest) -> types.ServerResult:
        server_name, tool_name = _split_namespace(req.params.name)
        spec = group.servers.get(server_name)
        if spec is None or (spec != "*" and tool_name not in spec):
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Tool '{req.params.name}' is not allowed in group '{group.name}'",
                )
            )
        client = registry.get(server_name)
        if client is None:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Unknown server '{server_name}'",
                )
            )
        result = await client.call_tool(tool_name, req.params.arguments)
        return types.ServerResult(result)

    async def _list_resources(_req: types.ListResourcesRequest) -> types.ServerResult:
        resources: list[types.Resource] = []
        for client in _wildcard_clients():
            resources.extend(client.resources)
        return types.ServerResult(types.ListResourcesResult(resources=resources))

    async def _read_resource(req: types.ReadResourceRequest) -> types.ServerResult:
        uri_str = str(req.params.uri)
        owner = _find_resource_owner(_wildcard_clients(), uri_str)
        if owner is None:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Resource '{uri_str}' is not accessible in group '{group.name}'",
                )
            )
        result = await owner.read_resource(req.params.uri)
        return types.ServerResult(result)

    async def _list_prompts(_req: types.ListPromptsRequest) -> types.ServerResult:
        prompts: list[types.Prompt] = []
        for client in _wildcard_clients():
            for prompt in client.prompts:
                prompts.append(
                    prompt.model_copy(update={"name": _namespace(client.config.name, prompt.name)})
                )
        return types.ServerResult(types.ListPromptsResult(prompts=prompts))

    async def _get_prompt(req: types.GetPromptRequest) -> types.ServerResult:
        server_name, prompt_name = _split_namespace(req.params.name)
        if group.servers.get(server_name) != "*":
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Prompt '{req.params.name}' is not allowed in group '{group.name}'",
                )
            )
        client = registry.get(server_name)
        if client is None:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"Unknown server '{server_name}'",
                )
            )
        result = await client.get_prompt(prompt_name, req.params.arguments)
        return types.ServerResult(result)

    server.request_handlers[types.ListToolsRequest] = _list_tools
    server.request_handlers[types.CallToolRequest] = _call_tool
    server.request_handlers[types.ListResourcesRequest] = _list_resources
    server.request_handlers[types.ReadResourceRequest] = _read_resource
    server.request_handlers[types.ListPromptsRequest] = _list_prompts
    server.request_handlers[types.GetPromptRequest] = _get_prompt
    return server
