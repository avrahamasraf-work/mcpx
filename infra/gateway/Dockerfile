# Container image for the MCPX custom Python MCP gateway (mcpx-gateway).
# Fits into MCPX as the internal gateway service behind Caddy in infra/docker-compose.yml.
# Bundles Python 3.13, uv/uvx, and Node.js 22 (node/npm/npx) so all stdio and
# remote Streamable HTTP MCP servers run inside one self-contained image.

FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /usr/local/bin/
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /app
COPY pyproject.toml ./
COPY mcpx_gateway ./mcpx_gateway
RUN uv pip install --system --no-cache .

ENTRYPOINT ["python3", "-m", "mcpx_gateway"]
CMD ["--config", "/config/config.json"]
