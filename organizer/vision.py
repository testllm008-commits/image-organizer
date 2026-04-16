"""NVIDIA NIM vision client for the image organizer.

Wraps the OpenAI-compatible client pointed at NVIDIA's endpoint and
returns a normalized dictionary with ``category``, ``item_name``,
``description`` and ``confidence`` fields.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from openai import OpenAI, OpenAIError

from . import config

logger = logging.getLogger(__name__)

# Maximum number of words allowed in an item_name. Anything longer is truncated.
_MAX_NAME_WORDS = 4

# Mime-type lookup keyed on file extension. Used to build the data URL we
# hand to the vision model.
_MIME_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_PROMPT = (
    "You are an expert image classifier. Look at the image and respond with "
    "STRICT JSON only — no prose, no markdown fences. The JSON object must "
    "contain exactly these keys:\n"
    '  "category": one of {categories}\n'
    '  "item_name": a short snake_case identifier, max 4 words (e.g. "chicken_breast")\n'
    '  "description": a one-sentence description of what is shown\n'
    '  "confidence": a float between 0.0 and 1.0 reflecting your certainty\n'
    "If you are unsure, prefer the category \"other\" with a low confidence."
    "{instructions}"
)

_INSTRUCTIONS_BLOCK = (
    "\n\nAdditional user instructions (apply these when categorizing):\n{text}"
)


class VisionError(RuntimeError):
    """Raised when the vision API call fails or returns invalid JSON."""


def _build_client(api_key: str | None = None) -> OpenAI:
    """Return an OpenAI client configured for NVIDIA's NIM endpoint."""
    key = api_key if api_key is not None else config.NVIDIA_API_KEY
    if not key:
        raise VisionError(
            "NVIDIA_API_KEY is not set. Add it to your environment or .env file."
        )
    return OpenAI(base_url=config.BASE_URL, api_key=key)


def _encode_image(path: Path) -> str:
    """Return a base64 data URL for ``path``.

    Args:
        path: Path to the image file.

    Returns:
        A ``data:`` URL string suitable for the OpenAI vision message format.
    """
    suffix = path.suffix.lower()
    mime = _MIME_TYPES.get(suffix, "image/jpeg")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _strip_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences from ``text``."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence (``` or ```json) and the closing fence.
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _sanitize_item_name(raw: Any) -> str:
    """Coerce ``raw`` into a snake_case identifier of at most 4 words."""
    if not isinstance(raw, str) or not raw.strip():
        return "unknown_item"
    # Replace any non-alphanumeric run with a single underscore.
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", raw.strip().lower()).strip("_")
    if not cleaned:
        return "unknown_item"
    words = [w for w in cleaned.split("_") if w]
    if len(words) > _MAX_NAME_WORDS:
        words = words[:_MAX_NAME_WORDS]
    return "_".join(words)


def _validate_category(raw: Any, allowed: list[str]) -> str:
    """Return ``raw`` if it is a known category, otherwise ``"other"``."""
    if isinstance(raw, str):
        candidate = raw.strip().lower()
        if candidate in allowed:
            return candidate
    return "other" if "other" in allowed else allowed[-1]


def _validate_confidence(raw: Any) -> float:
    """Coerce ``raw`` into a float clamped to ``[0.0, 1.0]``."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def analyze_image(
    path: Path,
    *,
    client: OpenAI | None = None,
    model: str | None = None,
    categories: list[str] | None = None,
    instructions: str | None = None,
) -> dict[str, Any]:
    """Classify a single image with the NVIDIA vision model.

    Args:
        path: Path to the image to analyze.
        client: Optional pre-built OpenAI client (useful for tests). When
            omitted a new client is built from :mod:`config`.
        model: Optional model override. Defaults to :data:`config.DEFAULT_MODEL`.
        categories: Optional list of allowed categories. Defaults to the
            value returned by :func:`config.load_categories`.
        instructions: Optional free-form text appended to the system prompt
            so users can steer how the model labels images (e.g. "These are
            cosmetics — prefer brand_product names").

    Returns:
        Dict with keys ``category``, ``item_name``, ``description`` and
        ``confidence``.

    Raises:
        VisionError: If the API call fails or the response is not valid JSON.
    """
    allowed = categories if categories is not None else config.load_categories()
    use_model = model or config.DEFAULT_MODEL
    api_client = client if client is not None else _build_client()

    image_url = _encode_image(path)
    instructions_text = ""
    if instructions and instructions.strip():
        instructions_text = _INSTRUCTIONS_BLOCK.format(text=instructions.strip())
    prompt_text = _PROMPT.format(categories=allowed, instructions=instructions_text)

    try:
        response = api_client.chat.completions.create(
            model=use_model,
            temperature=0.1,
            max_tokens=250,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        )
    except OpenAIError as exc:
        raise VisionError(f"NVIDIA API request failed: {exc}") from exc

    try:
        content = response.choices[0].message.content or ""
    except (AttributeError, IndexError) as exc:
        raise VisionError("Malformed response from vision API") from exc

    payload = _strip_fences(content)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.debug("Raw model output was: %s", content)
        raise VisionError(f"Vision response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise VisionError("Vision response JSON was not an object")

    return {
        "category": _validate_category(parsed.get("category"), allowed),
        "item_name": _sanitize_item_name(parsed.get("item_name")),
        "description": str(parsed.get("description", "")).strip(),
        "confidence": _validate_confidence(parsed.get("confidence")),
    }
