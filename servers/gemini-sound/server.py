# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=2.0.0",
#   "google-genai>=1.0.0",
# ]
# ///
"""MCPX stdio MCP server for generating speech/audio via the Google Gemini API.

Launched by mcp-proxy via `uv run --script /servers/gemini-sound/server.py` and
exposed behind Caddy HTTPS + Bearer auth at `/gemini-sound/mcp`. Uses
GEMINI_API_KEY (or GEMINI_TOKEN) from the gateway environment and logs to
`<MCPX_LOG_DIR>/servers/gemini-sound.log` plus stderr.
"""

from __future__ import annotations

import base64
from functools import lru_cache
import io
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import wave

from mcp import types as mcp_types
from mcp.server.mcpserver import MCPServer

SERVER_NAME = "gemini-sound"
DEFAULT_MODEL = (
    (os.environ.get("GEMINI_SOUND_MODEL") or "").strip() or "gemini-2.5-flash-preview-tts"
)
DEFAULT_VOICE = "Kore"
DEFAULT_PCM_RATE = 24000
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

PREBUILT_VOICES: tuple[str, ...] = (
    "Kore",
    "Puck",
    "Charon",
    "Fenrir",
    "Aoede",
    "Leda",
    "Orus",
    "Zephyr",
)

RATE_PATTERN = re.compile(r"rate=(\d+)", re.IGNORECASE)


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

    log_dir = Path((os.environ.get("MCPX_LOG_DIR") or "").strip() or "/var/log/mcpx")
    log_file = log_dir / "servers" / f"{SERVER_NAME}.log"
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


@lru_cache(maxsize=1)
def _build_client(api_key: str, use_vertexai: bool) -> object:
    from google import genai

    if use_vertexai:
        return genai.Client(vertexai=True, api_key=api_key)
    return genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})


def _get_client() -> object:
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip() or (
        os.environ.get("GEMINI_TOKEN") or ""
    ).strip()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY (or GEMINI_TOKEN) is not set. "
            "Add it to your gateway .env before calling gemini-sound tools."
        )
    use_vertexai = os.environ.get("GEMINI_USE_VERTEXAI", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    return _build_client(api_key, use_vertexai)


def _parse_sample_rate(mime_type: str) -> int:
    match = RATE_PATTERN.search(mime_type)
    if match:
        rate = int(match.group(1))
        if rate > 0:
            return rate
    return DEFAULT_PCM_RATE


def _wrap_pcm_as_wav(pcm_bytes: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()


def _is_raw_pcm(mime_type: str) -> bool:
    normalized = mime_type.strip().lower()
    if not normalized:
        return True
    base_mime = normalized.split(";", 1)[0].strip()
    return base_mime in ("audio/l16", "audio/pcm", "audio/raw") or "pcm" in base_mime


def _extract_audio_from_response(response: object) -> tuple[bytes, str]:
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline_data = getattr(part, "inline_data", None)
            if inline_data is None:
                continue
            raw_data = getattr(inline_data, "data", None)
            if not raw_data:
                continue
            audio_bytes = (
                base64.b64decode(raw_data) if isinstance(raw_data, str) else bytes(raw_data)
            )
            mime_type = (getattr(inline_data, "mime_type", None) or "").strip()
            return audio_bytes, mime_type
    raise RuntimeError("Gemini response did not include any inline audio data.")


@mcp.tool()
async def gemini_generate_sound(
    text: str,
    voice: str = DEFAULT_VOICE,
    model: str | None = None,
) -> mcp_types.AudioContent:
    """Generate spoken audio from text using Google Gemini and return playable WAV audio.

    Args:
        text: The text prompt or script to synthesize into audio.
        voice: Gemini prebuilt voice name (defaults to Kore).
        model: Optional Gemini TTS model name (defaults to GEMINI_SOUND_MODEL or gemini-2.5-flash-preview-tts).
    """
    from google.genai import types as genai_types

    chosen_model = (model or "").strip() or DEFAULT_MODEL
    chosen_voice = (voice or "").strip() or DEFAULT_VOICE

    logger.info(
        "Generating sound model=%s voice=%s text_len=%d",
        chosen_model,
        chosen_voice,
        len(text),
    )

    try:
        client = _get_client()
        config = genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=chosen_voice
                    )
                )
            ),
        )

        response = await client.aio.models.generate_content(
            model=chosen_model,
            contents=text,
            config=config,
        )

        raw_bytes, mime_type = _extract_audio_from_response(response)
        if _is_raw_pcm(mime_type):
            sample_rate = _parse_sample_rate(mime_type)
            wav_bytes = _wrap_pcm_as_wav(raw_bytes, sample_rate)
            output_mime = "audio/wav"
        else:
            wav_bytes = raw_bytes
            base_mime = mime_type.split(";", 1)[0].strip().lower()
            output_mime = "audio/wav" if base_mime in ("audio/wav", "audio/x-wav") else base_mime
    except Exception:
        logger.exception("Failed generating sound model=%s voice=%s", chosen_model, chosen_voice)
        raise

    logger.info(
        "Generated sound model=%s voice=%s input_mime=%s output_mime=%s bytes=%d",
        chosen_model,
        chosen_voice,
        mime_type or "unknown",
        output_mime,
        len(wav_bytes),
    )

    encoded_audio = base64.b64encode(wav_bytes).decode("ascii")
    return mcp_types.AudioContent(
        type="audio",
        data=encoded_audio,
        mimeType=output_mime,
    )


@mcp.tool()
def gemini_sound_list_voices() -> list[str]:
    """Return the list of supported Gemini prebuilt voice names for audio generation."""
    return list(PREBUILT_VOICES)


if __name__ == "__main__":
    logger.info("Starting %s MCP server (default_model=%s)", SERVER_NAME, DEFAULT_MODEL)
    mcp.run(transport="stdio")
