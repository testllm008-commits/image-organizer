"""Configuration constants and helpers for the image organizer.

Loads the NVIDIA API key from the environment (or a ``.env`` file) and
exposes the constants used across the package. Categories may be
overridden by placing a ``categories.txt`` file (one category per line)
next to the working directory or by passing an explicit path to
:func:`load_categories`.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load variables from a local .env file if one exists. This is a no-op when
# the file is missing, so it is safe to call unconditionally at import time.
load_dotenv()

#: NVIDIA API key, read from the environment. May be ``None`` until the user
#: configures it; callers that need it should validate before use.
NVIDIA_API_KEY: str | None = os.getenv("NVIDIA_API_KEY")

#: NVIDIA NIM OpenAI-compatible endpoint.
BASE_URL: str = "https://integrate.api.nvidia.com/v1"

#: Default vision model. Can be overridden via the CLI ``--model`` flag.
DEFAULT_MODEL: str = "meta/llama-3.2-90b-vision-instruct"

#: Seconds to wait between API calls so we stay under the 40 req/min free tier.
RATE_LIMIT_DELAY: float = 1.6

#: File extensions the organizer will consider as images.
VALID_EXTS: set[str] = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

#: Default category list used when no ``categories.txt`` is provided.
CATEGORIES: list[str] = [
    "meat",
    "dairy",
    "groceries",
    "produce",
    "hygiene",
    "beverages",
    "snacks",
    "frozen",
    "bakery",
    "household",
    "other",
]


def load_categories(path: Path | None = None) -> list[str]:
    """Return the active category list.

    If ``path`` (or ``./categories.txt`` when ``path`` is ``None``) exists,
    each non-empty, non-comment line is read as a category name. Otherwise
    the built-in :data:`CATEGORIES` list is returned.

    Args:
        path: Optional path to a categories file. Lines starting with ``#``
            are treated as comments and ignored.

    Returns:
        The list of category names to use for classification.
    """
    candidate = path if path is not None else Path("categories.txt")
    if not candidate.exists():
        return list(CATEGORIES)

    categories: list[str] = []
    for raw_line in candidate.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        categories.append(line.lower())

    # Always keep "other" available as a fallback bucket.
    if "other" not in categories:
        categories.append("other")
    return categories
