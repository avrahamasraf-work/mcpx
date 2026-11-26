# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=2.0.0",
# ]
# ///
"""MCPX stdio MCP server for inspecting gateway and per-server log files.

Launched by mcp-proxy via `uv run --script /servers/mcpx-logs/server.py` and
exposed behind Caddy HTTPS + Bearer auth at `/mcpx-logs/mcp`. Provides read-only
access to `<MCPX_LOG_DIR>` logs with strict source validation and secret redaction.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys

from mcp.server.mcpserver import MCPServer

SERVER_NAME = "mcpx-logs"
DEFAULT_LOG_DIR = "/var/log/mcpx"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
MIN_LINES = 1
MAX_LINES = 1000

GATEWAY_LOG_FILES: dict[str, str] = {
    "mcp-proxy": "mcp-proxy.log",
    "caddy": "caddy.log",
    "mcpx-oauth": "mcpx-oauth.log",
}

SOURCE_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
SECRET_KV_PATTERN = re.compile(
    r"(?i)\b(api_key|gemini_api_key|gemini_token|mcp_bearer_token|mcp_oauth_password|mcp_oauth_client_secret|mcp_oauth_signing_key|client_secret|refresh_token|access_token)\b"
    r"(\s*[:=]\s*[\"']?)([^\s\"',;}\]]+)"
)


def _get_log_dir() -> Path:
    return Path((os.environ.get("MCPX_LOG_DIR") or "").strip() or DEFAULT_LOG_DIR)


def _configure_logging() -> logging.Logger:
    logger = logging.getLogger(SERVER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    log_file = _get_log_dir() / "servers" / f"{SERVER_NAME}.log"
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


logger = _configure_logging()
mcp = MCPServer(SERVER_NAME)


def _file_metadata(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {
            "exists": False,
            "size_bytes": None,
            "modified_at": None,
        }
    stat = path.stat()
    modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    return {
        "exists": True,
        "size_bytes": stat.st_size,
        "modified_at": modified_at,
    }


def _validate_source(source: str) -> str:
    cleaned = source.strip()
    if not cleaned or cleaned in (".", "..") or not SOURCE_PATTERN.fullmatch(cleaned):
        raise ValueError(
            f"Invalid log source {source!r}. Must match ^[a-zA-Z0-9._-]+$ without path traversal."
        )
    return cleaned


def _redact_secrets(text: str) -> str:
    redacted = BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    redacted = SECRET_KV_PATTERN.sub(r"\1\2[REDACTED]", redacted)
    return redacted


def _read_tail_lines(
    path: Path,
    max_lines: int,
    required_substring: str | None = None,
    search: str | None = None,
) -> list[str]:
    required_lower = required_substring.lower() if required_substring else None
    search_lower = search.lower() if search else None

    tail: deque[str] = deque(maxlen=max_lines)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            line_lower = line.lower()
            if required_lower and required_lower not in line_lower:
                continue
            if search_lower and search_lower not in line_lower:
                continue
            tail.append(_redact_secrets(line))
    return list(tail)


@mcp.tool()
def list_log_sources() -> dict[str, object]:
    """List available MCPX gateway service logs and per-server logs with file metadata."""
    log_dir = _get_log_dir()
    servers_dir = log_dir / "servers"

    gateway_logs = {
        name: _file_metadata(log_dir / filename)
        for name, filename in GATEWAY_LOG_FILES.items()
    }

    server_logs: dict[str, dict[str, object]] = {}
    if servers_dir.is_dir():
        for path in sorted(servers_dir.glob("*.log")):
            if path.is_file():
                server_logs[path.stem] = _file_metadata(path)

    logger.info("Listed log sources (servers=%d)", len(server_logs))
    return {
        "log_dir": str(log_dir),
        "gateway": gateway_logs,
        "servers": server_logs,
    }


@mcp.tool()
def get_logs(source: str, lines: int = 100, search: str | None = None) -> str:
    """Read recent redacted lines from a gateway or MCP server log file.

    Args:
        source: Log source name (`mcp-proxy`, `caddy`, or an MCP server name).
        lines: Maximum number of trailing lines to return (clamped to 1..1000).
        search: Optional case-insensitive substring filter.
    """
    validated_source = _validate_source(source)
    clamped_lines = max(MIN_LINES, min(lines, MAX_LINES))
    search_filter = (search or "").strip() or None

    log_dir = _get_log_dir()
    header: str | None = None
    required_substring: str | None = None

    if validated_source in GATEWAY_LOG_FILES:
        target_path = log_dir / GATEWAY_LOG_FILES[validated_source]
    else:
        server_log_path = log_dir / "servers" / f"{validated_source}.log"
        if server_log_path.is_file():
            target_path = server_log_path
        else:
            target_path = log_dir / GATEWAY_LOG_FILES["mcp-proxy"]
            required_substring = validated_source
            header = (
                f"# No dedicated log file for '{validated_source}'; "
                f"showing lines matching '{validated_source}' from mcp-proxy.log"
            )

    if not target_path.is_file():
        logger.info("Log file not found for source=%s path=%s", validated_source, target_path)
        return f"Log file not found for source '{validated_source}': {target_path}"

    matched_lines = _read_tail_lines(
        target_path,
        max_lines=clamped_lines,
        required_substring=required_substring,
        search=search_filter,
    )

    logger.info(
        "Read logs source=%s lines_requested=%d lines_returned=%d fallback=%s",
        validated_source,
        clamped_lines,
        len(matched_lines),
        required_substring is not None,
    )

    if header:
        if matched_lines:
            return "\n".join([header, *matched_lines])
        return f"{header}\n(no matching log lines)"

    if not matched_lines:
        return f"(no matching log lines in {validated_source})"
    return "\n".join(matched_lines)


if __name__ == "__main__":
    logger.info("Starting %s MCP server (log_dir=%s)", SERVER_NAME, _get_log_dir())
    mcp.run(transport="stdio")
