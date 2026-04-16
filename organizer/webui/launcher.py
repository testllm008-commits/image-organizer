"""Boot the web UI: pick a free port, start uvicorn, open the browser."""

from __future__ import annotations

import io
import logging
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from .server import app

logger = logging.getLogger(__name__)


def _ensure_streams() -> None:
    """Provide harmless stdio when launched via ``pythonw`` (no console).

    Without this, ``print`` and uvicorn's logger raise ``AttributeError``
    because ``sys.stdout`` / ``sys.stderr`` are ``None`` under ``pythonw``.
    """
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()


def _free_port(preferred: int = 8765) -> int:
    """Return an open localhost port, preferring ``preferred`` if available."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free port available for the UI server.")


def _open_browser_when_ready(url: str, port: int) -> None:
    """Poll the server port and open the browser as soon as it accepts."""
    deadline = time.time() + 10.0
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            try:
                sock.connect(("127.0.0.1", port))
                webbrowser.open(url)
                return
            except OSError:
                time.sleep(0.15)
    logger.warning("Server did not become ready within 10s; open %s manually.", url)


def main() -> int:
    """Run the desktop web UI. Returns a process exit code."""
    _ensure_streams()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    port = _free_port()
    url = f"http://127.0.0.1:{port}/"

    threading.Thread(
        target=_open_browser_when_ready,
        args=(url, port),
        name="open-browser",
        daemon=True,
    ).start()

    print(f"Image Organizer UI running at {url}")
    print("Press Ctrl+C in this window to quit.")

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
