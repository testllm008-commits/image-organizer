"""FastAPI app that serves the modern desktop UI."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config
from .jobs import manager

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    app = FastAPI(title="Image Organizer", docs_url=None, redoc_url=None)
    app.mount(
        "/static",
        StaticFiles(directory=str(_BASE_DIR / "static")),
        name="static",
    )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(
            "index.html",
            {
                "request": request,
                "categories": config.load_categories(),
                "default_model": config.DEFAULT_MODEL,
                "api_key_present": bool(config.NVIDIA_API_KEY),
            },
        )

    @app.get("/api/defaults")
    async def defaults() -> dict[str, Any]:
        return {
            "categories": config.load_categories(),
            "default_model": config.DEFAULT_MODEL,
            "api_key_present": bool(config.NVIDIA_API_KEY),
            "rate_limit_delay": config.RATE_LIMIT_DELAY,
        }

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return manager.snapshot()

    @app.post("/api/pick-folder")
    async def pick_folder(payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "Pick a folder")
        initial = payload.get("initial") or ""
        # Native dialog runs on the main process; do it off the asyncio loop.
        path = await asyncio.to_thread(_pick_folder_dialog, title, initial)
        return {"path": path}

    @app.post("/api/start")
    async def start(payload: dict[str, Any]) -> dict[str, Any]:
        source = (payload.get("source") or "").strip()
        output = (payload.get("output") or "").strip()
        if not source:
            raise HTTPException(status_code=400, detail="Source folder is required.")
        if not output:
            raise HTTPException(status_code=400, detail="Output folder is required.")
        source_path = Path(source)
        if not source_path.exists() or not source_path.is_dir():
            raise HTTPException(status_code=400, detail=f"Source folder not found: {source}")

        settings = {
            "source": source,
            "output": output,
            "mode": payload.get("mode", "copy"),
            "dry_run": bool(payload.get("dry_run", False)),
            "threshold": float(payload.get("threshold", 0.7)),
            "model": (payload.get("model") or "").strip() or None,
            "instructions": (payload.get("instructions") or "").strip() or None,
        }
        try:
            manager.start(settings)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "state": manager.snapshot()}

    @app.post("/api/stop")
    async def stop() -> dict[str, Any]:
        manager.stop()
        return {"ok": True, "state": manager.snapshot()}

    @app.post("/api/open-folder")
    async def open_folder(payload: dict[str, Any]) -> dict[str, Any]:
        path = (payload.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="Path required.")
        target = Path(path)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_open_in_file_browser, target)
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        q = manager.subscribe()

        async def stream() -> Any:
            try:
                # Initial heartbeat so the browser knows the stream is live.
                yield ": ping\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.to_thread(q.get, True, 1.0)
                    except queue.Empty:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                manager.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


def _pick_folder_dialog(title: str, initial: str) -> str:
    """Show a native folder picker. Returns "" if cancelled.

    We use Tkinter purely as the OS dialog provider — it never opens a
    visible Tk window. This keeps the main UI 100% web-based while still
    giving the user a real native folder browser.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:  # pragma: no cover - Tk ships with CPython on Windows
        return ""

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askdirectory(
            title=title,
            initialdir=initial or None,
            mustexist=False,
        )
    finally:
        root.destroy()
    return chosen or ""


def _open_in_file_browser(path: Path) -> None:
    """Open ``path`` in the OS file browser. Best-effort, never raises."""
    import os
    import subprocess

    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not open folder %s: %s", path, exc)


# Module-level app for `uvicorn organizer.webui.server:app`.
app = create_app()
