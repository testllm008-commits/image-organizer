"""Background job runner that drives :class:`ImageOrganizer` from the web UI.

The web layer talks to a single :class:`JobManager` instance. The manager
owns at most one running job, captures its log lines + per-image progress
into a thread-safe queue, and exposes a snapshot the SSE endpoint can
stream back to the browser.
"""

from __future__ import annotations

import io
import logging
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..organizer import ImageOrganizer

logger = logging.getLogger(__name__)

# Matches the per-image status line printed by ImageOrganizer._process_one,
# e.g. "[12/101] photo.jpg → meat/beef_steak_0012.jpg (0.92) [COPY]"
_PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]\s")


@dataclass
class JobState:
    """Snapshot of the current/most-recent job."""

    status: str = "idle"  # idle | running | done | error | cancelled
    started_at: float | None = None
    finished_at: float | None = None
    processed: int = 0
    total: int = 0
    last_message: str = ""
    error: str | None = None
    stats: dict[str, int] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


class _QueueWriter(io.TextIOBase):
    """File-like wrapper that pushes whole lines onto a queue."""

    def __init__(self, sink: queue.Queue[dict[str, Any]], stream: str) -> None:
        super().__init__()
        self._sink = sink
        self._stream = stream
        self._buffer = ""

    def writable(self) -> bool:  # noqa: D401 - io interface
        return True

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                self._sink.put({"type": "log", "stream": self._stream, "text": line})
        return len(data)

    def flush(self) -> None:
        if self._buffer.strip():
            self._sink.put(
                {"type": "log", "stream": self._stream, "text": self._buffer.strip()}
            )
        self._buffer = ""


class _QueueLogHandler(logging.Handler):
    """Forward Python ``logging`` records to the same queue as stdout."""

    def __init__(self, sink: queue.Queue[dict[str, Any]]) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.put(
                {
                    "type": "log",
                    "stream": "log",
                    "level": record.levelname.lower(),
                    "text": self.format(record),
                }
            )
        except Exception:  # pragma: no cover - logging must never crash
            self.handleError(record)


class JobManager:
    """Owns the worker thread, the message queue and the public state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        # Subscribers receive every event; queue is unbounded so a slow
        # browser cannot drop log lines.
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._history: list[dict[str, Any]] = []
        self._history_limit = 500
        self.state = JobState()

    # ------------------------------------------------------------------ pubsub

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        """Register a new SSE listener and replay recent history into it."""
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            for event in self._history:
                q.put(event)
            q.put({"type": "state", "state": self._state_dict()})
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_limit:
                # Drop the oldest entries while keeping the most recent state.
                overflow = len(self._history) - self._history_limit
                self._history = self._history[overflow:]
            for q in list(self._subscribers):
                q.put(event)

    # ---------------------------------------------------------------- snapshot

    def _state_dict(self) -> dict[str, Any]:
        return {
            "status": self.state.status,
            "started_at": self.state.started_at,
            "finished_at": self.state.finished_at,
            "processed": self.state.processed,
            "total": self.state.total,
            "last_message": self.state.last_message,
            "error": self.state.error,
            "stats": dict(self.state.stats),
            "settings": dict(self.state.settings),
            "running": self.is_running(),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._state_dict()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------- start

    def start(self, settings: dict[str, Any]) -> None:
        """Kick off a background organize job. Raises if one is already running."""
        with self._lock:
            if self.is_running():
                raise RuntimeError("A job is already running.")
            self._cancel.clear()
            self.state = JobState(
                status="running",
                started_at=time.time(),
                settings=dict(settings),
            )
        self._broadcast({"type": "state", "state": self._state_dict()})

        thread = threading.Thread(
            target=self._run,
            name="organizer-job",
            args=(settings,),
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Request the running job stop after the current image."""
        if self.is_running():
            self._cancel.set()
            self._broadcast(
                {"type": "log", "stream": "log", "level": "warning",
                 "text": "Stop requested — finishing current image..."}
            )

    # --------------------------------------------------------------- internals

    def _run(self, settings: dict[str, Any]) -> None:
        sink: queue.Queue[dict[str, Any]] = queue.Queue()
        writer_out = _QueueWriter(sink, "stdout")
        writer_err = _QueueWriter(sink, "stderr")
        log_handler = _QueueLogHandler(sink)
        log_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s — %(message)s"))

        original_stdout = sys.stdout
        original_stderr = sys.stderr
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)
        previous_level = root_logger.level
        root_logger.setLevel(logging.INFO)

        sys.stdout = writer_out
        sys.stderr = writer_err

        cancel = self._cancel

        # Drain the per-job sink onto the broadcast bus on a helper thread so
        # log lines flow even while the organizer is busy inside an API call.
        drain_stop = threading.Event()

        def drain_loop() -> None:
            while not drain_stop.is_set():
                try:
                    event = sink.get(timeout=0.1)
                except queue.Empty:
                    continue
                self._handle_event(event)

        drain_thread = threading.Thread(target=drain_loop, name="organizer-drain", daemon=True)
        drain_thread.start()

        try:
            organizer = ImageOrganizer(
                source_dir=Path(settings["source"]),
                output_dir=Path(settings["output"]),
                mode=settings.get("mode", "copy"),
                dry_run=bool(settings.get("dry_run", False)),
                confidence_threshold=float(settings.get("threshold", 0.7)),
                model=settings.get("model") or None,
                instructions=settings.get("instructions") or None,
            )

            # Patch _process_one so the user can interrupt between images
            # without us having to fork the organizer module.
            original_process = organizer._process_one

            def cancellable(image_path: Path, index: int, total: int) -> None:
                if cancel.is_set():
                    raise KeyboardInterrupt()
                original_process(image_path, index, total)

            organizer._process_one = cancellable  # type: ignore[assignment]

            try:
                stats = organizer.run()
            except KeyboardInterrupt:
                stats = organizer.manifest.stats()
                with self._lock:
                    self.state.status = "cancelled"
                    self.state.finished_at = time.time()
                    self.state.stats = dict(stats)
                self._broadcast({"type": "state", "state": self._state_dict()})
                self._broadcast(
                    {"type": "log", "stream": "log", "level": "warning",
                     "text": "Run cancelled by user."}
                )
                return

            with self._lock:
                self.state.status = "done"
                self.state.finished_at = time.time()
                self.state.stats = dict(stats)
            self._broadcast({"type": "state", "state": self._state_dict()})
            self._broadcast(
                {"type": "log", "stream": "log", "level": "info",
                 "text": f"Run complete. Stats: {stats}"}
            )
        except Exception as exc:  # noqa: BLE001 - we want to surface anything
            logger.exception("Organize job failed")
            with self._lock:
                self.state.status = "error"
                self.state.finished_at = time.time()
                self.state.error = str(exc)
            self._broadcast({"type": "state", "state": self._state_dict()})
            self._broadcast(
                {"type": "log", "stream": "log", "level": "error",
                 "text": f"Job failed: {exc}"}
            )
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            root_logger.removeHandler(log_handler)
            root_logger.setLevel(previous_level)
            writer_out.flush()
            writer_err.flush()
            drain_stop.set()
            drain_thread.join(timeout=1.0)
            # Final flush so anything lingering still reaches the browser.
            while not sink.empty():
                try:
                    self._handle_event(sink.get_nowait())
                except queue.Empty:
                    break

    def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "log":
            text = event.get("text", "")
            match = _PROGRESS_RE.match(text)
            if match:
                processed = int(match.group(1))
                total = int(match.group(2))
                with self._lock:
                    self.state.processed = processed
                    self.state.total = total
                    self.state.last_message = text
                self._broadcast(
                    {"type": "progress", "processed": processed, "total": total}
                )
            else:
                with self._lock:
                    self.state.last_message = text
        self._broadcast(event)


# Module-level singleton so every request handler shares state.
manager = JobManager()
