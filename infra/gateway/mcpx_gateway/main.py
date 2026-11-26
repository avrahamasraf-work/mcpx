"""CLI entrypoint and logging setup for mcpx-gateway.

Fits into MCPX as the process launcher invoked by `python3 -m mcpx_gateway`
inside the gateway container and by deployment config validation (`--check-config`).
Configures stdout + rotating file logging under `MCPX_LOG_DIR` and runs Uvicorn.
"""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

import uvicorn

from mcpx_gateway.app import create_app
from mcpx_gateway.config import ConfigError, load_config

DEFAULT_LOG_DIR = "/var/log/mcpx"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def configure_logging(enable_file_log: bool = True) -> None:
    """Configure root logging to stdout and optionally `<MCPX_LOG_DIR>/mcpx-gateway.log`."""
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handlers: list[logging.Handler] = []

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    if enable_file_log:
        log_dir = Path(os.environ.get("MCPX_LOG_DIR", DEFAULT_LOG_DIR))
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "servers").mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "mcpx-gateway.log",
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except OSError as exc:
            print(
                f"mcpx-gateway: warning: could not open file log in '{log_dir}': {exc}",
                file=sys.stderr,
            )

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcpx-gateway",
        description="MCPX remote MCP gateway server.",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the gateway JSON configuration file.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and exit without starting the server.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"mcpx-gateway: invalid config: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.check_config:
        server_names = ", ".join(config.servers.keys())
        group_names = ", ".join(config.groups.keys()) or "(none)"
        print(
            f"mcpx-gateway config OK: "
            f"{len(config.servers)} servers [{server_names}], "
            f"{len(config.groups)} groups [{group_names}]"
        )
        return

    configure_logging(enable_file_log=True)
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.gateway.host,
        port=config.gateway.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
