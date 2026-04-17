"""MCP server for the image organizer.

Exposes the organizer to any MCP-aware client (Claude Desktop, Claude
Code, Cursor, Cline, Continue, etc.) over stdio.

Tools
-----
- ``organize_folder``: kick off an async organize job
- ``get_status``: snapshot current/last job — progress, stats, log tail
- ``stop_job``: cancel a running job between images
- ``analyze_image``: classify a single image synchronously
- ``list_categories``: return the active category list

Resources
---------
- ``imgorg://categories``: same as the ``list_categories`` tool, exposed
  as an MCP resource so clients that prefer resources can subscribe.
- ``imgorg://manifest/{output_dir}``: read a previous run's manifest.

Run with:  ``python -m organizer.mcp_server``
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import config
from .manifest import Manifest
from .vision import VisionError, analyze_image as vision_analyze
from .webui.jobs import manager as job_manager

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="image-organizer",
    instructions=(
        "Tools for organizing folders of images with the NVIDIA NIM vision "
        "API. Call `organize_folder` to start a job, then poll "
        "`get_status` until status == 'done'. Use `analyze_image` for "
        "one-off classification without moving files."
    ),
)


# ----------------------------------------------------------------------- tools


@mcp.tool()
def organize_folder(
    source: str,
    output: str,
    mode: str = "copy",
    dry_run: bool = False,
    threshold: float = 0.7,
    instructions: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Start an organize job in the background.

    The job runs asynchronously; poll ``get_status`` for progress.

    Args:
        source: Absolute path to the folder of images to organize.
        output: Absolute path where ``<output>/<category>/`` folders are
            created. The folder is created if missing.
        mode: ``"copy"`` (safe, default) or ``"move"`` (relocates files).
        dry_run: When true, prints what would happen without touching files
            and does NOT update the manifest.
        threshold: Confidence below which an image goes to ``_review/``.
            Range 0.0–1.0. Default 0.7.
        instructions: Optional free-form text appended to the AI prompt to
            steer labelling (e.g. "These are cosmetics, prefer brand names").
        model: Optional override of the vision model name. Defaults to
            ``meta/llama-3.2-90b-vision-instruct``.

    Returns:
        ``{"started": True, "state": <snapshot>}`` on success, or
        ``{"started": False, "error": "..."}`` if a job is already running
        or inputs are invalid.
    """
    src = Path(source)
    if not src.exists() or not src.is_dir():
        return {"started": False, "error": f"Source folder not found: {source}"}
    if mode not in ("copy", "move"):
        return {"started": False, "error": f"mode must be 'copy' or 'move', got {mode!r}"}
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return {"started": False, "error": f"threshold must be a number, got {threshold!r}"}

    settings = {
        "source": str(src),
        "output": str(Path(output)),
        "mode": mode,
        "dry_run": bool(dry_run),
        "threshold": threshold,
        "model": (model or "").strip() or None,
        "instructions": (instructions or "").strip() or None,
    }
    try:
        job_manager.start(settings)
    except RuntimeError as exc:
        return {"started": False, "error": str(exc), "state": job_manager.snapshot()}

    return {"started": True, "state": job_manager.snapshot()}


@mcp.tool()
def get_status(log_lines: int = 20) -> dict[str, Any]:
    """Return the current/most-recent job snapshot plus a log tail.

    Args:
        log_lines: How many of the most recent log lines to include
            (default 20, capped at 200).

    Returns:
        ``{status, running, processed, total, percent, last_message,
        stats, error, settings, recent_log}``.
    """
    snap = job_manager.snapshot()
    total = snap.get("total", 0) or 0
    processed = snap.get("processed", 0) or 0
    snap["percent"] = round(processed / total * 100, 1) if total else 0.0

    capped = max(1, min(int(log_lines or 20), 200))
    snap["recent_log"] = _tail_log(capped)
    return snap


@mcp.tool()
def stop_job() -> dict[str, Any]:
    """Request the running job stop after the current image.

    Returns:
        ``{"stopped": True, "state": <snapshot>}`` (idempotent — safe to
        call when no job is running).
    """
    job_manager.stop()
    return {"stopped": True, "state": job_manager.snapshot()}


@mcp.tool()
def analyze_image(
    path: str,
    instructions: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Classify one image synchronously, without moving any files.

    Args:
        path: Absolute path to a single image file (.jpg/.jpeg/.png/.webp/.gif).
        instructions: Optional free-form steering text.
        model: Optional model override.

    Returns:
        ``{category, item_name, description, confidence}`` on success or
        ``{"error": "..."}`` on failure.
    """
    img = Path(path)
    if not img.exists() or not img.is_file():
        return {"error": f"Image file not found: {path}"}
    if img.suffix.lower() not in config.VALID_EXTS:
        return {"error": f"Unsupported extension {img.suffix!r}. Allowed: {sorted(config.VALID_EXTS)}"}
    try:
        result = vision_analyze(
            img,
            model=(model or "").strip() or None,
            instructions=(instructions or "").strip() or None,
        )
    except VisionError as exc:
        return {"error": str(exc)}
    return result


@mcp.tool()
def list_categories() -> dict[str, Any]:
    """Return the active category list (from categories.txt or built-in defaults)."""
    return {
        "categories": config.load_categories(),
        "default_model": config.DEFAULT_MODEL,
        "api_key_configured": bool(config.NVIDIA_API_KEY),
    }


@mcp.tool()
def read_manifest(output_dir: str, limit: int = 50) -> dict[str, Any]:
    """Read entries from a previous run's manifest.json.

    Args:
        output_dir: The output folder containing ``manifest.json``.
        limit: Maximum number of entries to return (default 50, capped 500).

    Returns:
        ``{count, total_in_manifest, entries: [...]}`` or ``{"error": "..."}``.
    """
    out = Path(output_dir)
    manifest_path = out / "manifest.json"
    if not manifest_path.exists():
        return {"error": f"No manifest at {manifest_path}"}
    try:
        m = Manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Could not read manifest: {exc}"}

    cap = max(1, min(int(limit or 50), 500))
    entries = m.entries[-cap:] if hasattr(m, "entries") else []
    return {
        "count": len(entries),
        "total_in_manifest": len(m),
        "stats": m.stats(),
        "entries": entries,
    }


# ------------------------------------------------------------------ resources


@mcp.resource("imgorg://categories")
def categories_resource() -> str:
    """Active category list, one per line."""
    return "\n".join(config.load_categories())


@mcp.resource("imgorg://status")
def status_resource() -> str:
    """Current job snapshot as JSON. Refreshes on every read."""
    snap = job_manager.snapshot()
    snap["timestamp"] = time.time()
    return json.dumps(snap, indent=2)


# --------------------------------------------------------------------- helpers


def _tail_log(n: int) -> list[str]:
    """Return the last ``n`` log lines from the job manager's history."""
    # The JobManager keeps a bounded ``_history`` of recent events; we
    # filter to log entries only and grab the most recent text lines.
    history = list(getattr(job_manager, "_history", []))
    log_texts: list[str] = []
    for event in history:
        if event.get("type") == "log":
            text = event.get("text", "")
            if text:
                log_texts.append(text)
    return log_texts[-n:]


# ------------------------------------------------------------------------ main


def main() -> int:
    """Run the MCP server over stdio."""
    # MCP uses stdio for protocol; logs MUST go to stderr only or the
    # client will choke on the framing. basicConfig defaults to stderr.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    if not config.NVIDIA_API_KEY:
        logger.warning(
            "NVIDIA_API_KEY is not set — organize tools will fail until you "
            "configure a .env file or set the env var."
        )
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
