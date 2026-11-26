"""Module entrypoint for running `python3 -m mcpx_gateway`.

Fits into MCPX as the default container entrypoint defined in
infra/gateway/Dockerfile, delegating CLI parsing and server startup
to `mcpx_gateway.main.main`.
"""

from mcpx_gateway.main import main

if __name__ == "__main__":
    main()
