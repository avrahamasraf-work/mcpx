# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=2.0.0",
#   "google-genai>=1.0.0",
# ]
# ///
"""Minimal stdio MCP server that calls the Google Gemini API via google-genai."""

from __future__ import annotations

import os
from typing import Optional

from google import genai
from google.genai import types
from mcp.server.mcpserver import MCPServer

DEFAULT_MODEL = (os.environ.get("GEMINI_DEFAULT_MODEL") or "").strip() or "gemini-3.8-flash"

mcp = MCPServer("gemini-mcp")


def _get_client() -> genai.Client:
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip() or (os.environ.get("GEMINI_TOKEN") or "").strip()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY (or GEMINI_TOKEN) is not set. Add it to your gateway .env before calling Gemini tools."
        )
    use_vertexai = os.environ.get("GEMINI_USE_VERTEXAI", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if use_vertexai:
        return genai.Client(vertexai=True, api_key=api_key)
    return genai.Client(api_key=api_key)


@mcp.tool()
def gemini_generate(
    prompt: str,
    model: Optional[str] = None,
    system_instruction: Optional[str] = None,
    temperature: Optional[float] = None,
) -> str:
    """Generate text using Google Gemini.

    Args:
        prompt: The user prompt to send to Gemini.
        model: Optional Gemini model name (defaults to GEMINI_DEFAULT_MODEL or gemini-3.8-flash).
        system_instruction: Optional system instruction to steer the response.
        temperature: Optional sampling temperature (0.0 to 2.0).
    """
    client = _get_client()
    chosen_model = (model or "").strip() or DEFAULT_MODEL

    config_kwargs: dict[str, object] = {}
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    if temperature is not None:
        config_kwargs["temperature"] = temperature

    config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
    response = client.models.generate_content(
        model=chosen_model,
        contents=prompt,
        config=config,
    )
    return response.text or ""


@mcp.tool()
def gemini_list_models(limit: int = 20) -> list[str]:
    """List available Gemini models accessible with the configured API key."""
    client = _get_client()
    models: list[str] = []
    for m in client.models.list():
        name = getattr(m, "name", None)
        if name:
            models.append(name)
        if len(models) >= max(1, min(limit, 100)):
            break
    return models


if __name__ == "__main__":
    mcp.run(transport="stdio")
