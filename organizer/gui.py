"""Tkinter desktop UI for the image organizer.

Wraps :class:`organizer.organizer.ImageOrganizer` with a folder picker, an
"instructions" text box that is passed straight to the vision prompt, and
a live log/progress view. The actual organize run happens in a worker
thread so the UI stays responsive; messages flow back through a thread-
safe queue.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import traceback
from io import StringIO
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any

from . import config
from .organizer import ImageOrganizer


# How often (ms) the UI polls the message queue from the worker thread.
_POLL_INTERVAL_MS = 100


class _QueueWriter:
    """File-like object that pushes each ``write`` call onto a queue."""

    def __init__(self, q: "queue.Queue[tuple[str, Any]]", tag: str) -> None:
        self._queue = q
        self._tag = tag
        self._buffer = StringIO()

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buffer.write(text)
        # Flush on newline so progress lines arrive as they happen.
        if "\n" in text:
            self._queue.put((self._tag, self._buffer.getvalue()))
            self._buffer = StringIO()
        return len(text)

    def flush(self) -> None:
        leftover = self._buffer.getvalue()
        if leftover:
            self._queue.put((self._tag, leftover))
            self._buffer = StringIO()


class _QueueLogHandler(logging.Handler):
    """Logging handler that forwards records into the GUI queue."""

    def __init__(self, q: "queue.Queue[tuple[str, Any]]") -> None:
        super().__init__()
        self._queue = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # pragma: no cover - defensive
            msg = record.getMessage()
        self._queue.put(("log", msg + "\n"))


class OrganizerApp:
    """Main application window for the image organizer."""

    def __init__(self, root: tk.Tk) -> None:
        """Build the widget tree and wire up event handlers."""
        self.root = root
        self.root.title("Image Organizer")
        self.root.geometry("900x780")
        self.root.minsize(720, 620)

        self._queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._cancel_event = threading.Event()

        self._source_var = tk.StringVar(value="")
        self._output_var = tk.StringVar(value="")
        self._mode_var = tk.StringVar(value="copy")
        self._dry_run_var = tk.BooleanVar(value=False)
        self._threshold_var = tk.DoubleVar(value=0.7)
        self._status_var = tk.StringVar(value="Idle. Choose folders and click Start.")

        self._build_layout()
        self._poll_queue()

    # ---------------------------------------------------------------- layout

    def _build_layout(self) -> None:
        """Construct all widgets."""
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        # ---- Header / instructions block ------------------------------
        header = ttk.Label(
            outer,
            text="Image Organizer",
            font=("Segoe UI", 16, "bold"),
        )
        header.pack(anchor="w")

        howto = ttk.Label(
            outer,
            text=(
                "1. Pick the folder of images you want sorted.\n"
                "2. Pick where the sorted folders should go.\n"
                "3. (Optional) Write extra instructions for the AI — e.g. "
                '"these are receipts, sort by store" or "ignore packaging colors".\n'
                "4. Click Start. Use Dry Run first if you want a preview."
            ),
            justify=tk.LEFT,
            wraplength=860,
        )
        howto.pack(anchor="w", pady=(4, 12))

        # ---- Folder pickers -------------------------------------------
        folders = ttk.LabelFrame(outer, text="Folders", padding=10)
        folders.pack(fill=tk.X, pady=(0, 10))
        folders.columnconfigure(1, weight=1)

        ttk.Label(folders, text="Source folder:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(folders, textvariable=self._source_var).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        ttk.Button(folders, text="Browse…", command=self._pick_source).grid(
            row=0, column=2
        )

        ttk.Label(folders, text="Output folder:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(folders, textvariable=self._output_var).grid(
            row=1, column=1, sticky="ew", padx=8
        )
        ttk.Button(folders, text="Browse…", command=self._pick_output).grid(
            row=1, column=2
        )

        # ---- Options ---------------------------------------------------
        options = ttk.LabelFrame(outer, text="Options", padding=10)
        options.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(options, text="Mode:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Radiobutton(
            options, text="Copy (safer)", variable=self._mode_var, value="copy"
        ).grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(
            options, text="Move", variable=self._mode_var, value="move"
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Checkbutton(
            options,
            text="Dry run (preview only — no files touched)",
            variable=self._dry_run_var,
        ).grid(row=0, column=3, sticky="w", padx=(20, 0))

        ttk.Label(options, text="Confidence threshold:").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        threshold_scale = ttk.Scale(
            options,
            from_=0.0,
            to=1.0,
            orient=tk.HORIZONTAL,
            variable=self._threshold_var,
            command=self._on_threshold_change,
            length=240,
        )
        threshold_scale.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        self._threshold_label = ttk.Label(options, text="0.70")
        self._threshold_label.grid(row=1, column=3, sticky="w", padx=(8, 0), pady=(8, 0))

        # ---- Custom instructions text box ------------------------------
        instr_frame = ttk.LabelFrame(
            outer,
            text="Instructions for the AI (optional — describe what these images are or how to label them)",
            padding=10,
        )
        instr_frame.pack(fill=tk.X, pady=(0, 10))
        self._instructions = scrolledtext.ScrolledText(
            instr_frame, height=4, wrap=tk.WORD, font=("Segoe UI", 10)
        )
        self._instructions.pack(fill=tk.BOTH, expand=True)
        self._instructions.insert(
            "1.0",
            "e.g. These are pantry/grocery items. Use snake_case product names. "
            "Treat all fragrances as 'hygiene'.",
        )

        # ---- Action buttons --------------------------------------------
        actions = ttk.Frame(outer)
        actions.pack(fill=tk.X, pady=(0, 10))

        self._start_btn = ttk.Button(
            actions, text="Start", command=self._on_start, width=14
        )
        self._start_btn.pack(side=tk.LEFT)

        self._stop_btn = ttk.Button(
            actions, text="Stop", command=self._on_stop, width=10, state=tk.DISABLED
        )
        self._stop_btn.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Button(
            actions, text="Open output folder", command=self._open_output, width=18
        ).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Button(actions, text="Clear log", command=self._clear_log, width=10).pack(
            side=tk.RIGHT
        )

        # ---- Progress bar ----------------------------------------------
        self._progress = ttk.Progressbar(outer, mode="determinate", maximum=1.0)
        self._progress.pack(fill=tk.X, pady=(0, 6))

        # ---- Live log --------------------------------------------------
        ttk.Label(outer, text="Activity").pack(anchor="w")
        self._log = scrolledtext.ScrolledText(
            outer, height=14, wrap=tk.WORD, font=("Consolas", 9), state=tk.DISABLED
        )
        self._log.pack(fill=tk.BOTH, expand=True, pady=(2, 8))

        # ---- Status bar ------------------------------------------------
        ttk.Separator(outer, orient=tk.HORIZONTAL).pack(fill=tk.X)
        ttk.Label(outer, textvariable=self._status_var, anchor="w").pack(
            fill=tk.X, pady=(6, 0)
        )

    # ----------------------------------------------------------- callbacks

    def _pick_source(self) -> None:
        path = filedialog.askdirectory(title="Choose the folder of images to organize")
        if path:
            self._source_var.set(path)
            # Suggest a default output sibling folder if the user hasn't set one.
            if not self._output_var.get().strip():
                suggested = Path(path).with_name(Path(path).name + "_organized")
                self._output_var.set(str(suggested))

    def _pick_output(self) -> None:
        path = filedialog.askdirectory(title="Choose where to save sorted folders")
        if path:
            self._output_var.set(path)

    def _open_output(self) -> None:
        out = self._output_var.get().strip()
        if not out:
            messagebox.showinfo("No folder", "Pick an output folder first.")
            return
        target = Path(out)
        if not target.exists():
            messagebox.showinfo("Not yet", f"{target} doesn't exist yet.")
            return
        # Best-effort cross-platform open via ``os.startfile`` on Windows.
        try:
            import os

            os.startfile(str(target))  # type: ignore[attr-defined]
        except (AttributeError, OSError) as exc:
            messagebox.showerror("Cannot open", f"Could not open folder:\n{exc}")

    def _on_threshold_change(self, _value: str) -> None:
        self._threshold_label.config(text=f"{self._threshold_var.get():.2f}")

    def _clear_log(self) -> None:
        self._log.config(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.config(state=tk.DISABLED)

    # ---------------------------------------------------------------- run

    def _on_start(self) -> None:
        if self._worker and self._worker.is_alive():
            return

        source = self._source_var.get().strip()
        output = self._output_var.get().strip()
        if not source or not Path(source).is_dir():
            messagebox.showerror("Source missing", "Pick a valid source folder first.")
            return
        if not output:
            messagebox.showerror("Output missing", "Pick an output folder first.")
            return
        if not config.NVIDIA_API_KEY:
            messagebox.showerror(
                "API key missing",
                "NVIDIA_API_KEY is not set.\n\n"
                "Create a .env file in the project folder containing:\n\n"
                "    NVIDIA_API_KEY=nvapi-…",
            )
            return

        instructions_text = self._instructions.get("1.0", tk.END).strip()
        # Treat the placeholder text as empty so it doesn't pollute the prompt.
        if instructions_text.startswith("e.g. "):
            instructions_text = ""

        self._cancel_event.clear()
        self._set_running(True)
        self._progress["value"] = 0.0
        self._status_var.set("Starting…")
        self._append_log("=" * 60 + "\n")
        self._append_log(
            f"Source: {source}\nOutput: {output}\n"
            f"Mode: {self._mode_var.get()} | "
            f"Dry run: {self._dry_run_var.get()} | "
            f"Threshold: {self._threshold_var.get():.2f}\n"
        )
        if instructions_text:
            self._append_log(f"Instructions: {instructions_text}\n")
        self._append_log("=" * 60 + "\n")

        self._worker = threading.Thread(
            target=self._run_organizer,
            args=(
                Path(source),
                Path(output),
                self._mode_var.get(),
                self._dry_run_var.get(),
                float(self._threshold_var.get()),
                instructions_text or None,
            ),
            daemon=True,
        )
        self._worker.start()

    def _on_stop(self) -> None:
        if not (self._worker and self._worker.is_alive()):
            return
        # The organizer doesn't currently support cooperative cancellation, but
        # we set a flag and warn the user; it will stop on the next pass.
        self._cancel_event.set()
        self._status_var.set("Stop requested — finishing current image…")
        self._append_log("\n[stop requested — will exit after current image]\n")

    def _run_organizer(
        self,
        source: Path,
        output: Path,
        mode: str,
        dry_run: bool,
        threshold: float,
        instructions: str | None,
    ) -> None:
        """Worker thread — instantiate ImageOrganizer and run it."""
        # Reroute stdout/print and logging into the queue so the GUI sees it.
        import sys

        original_stdout = sys.stdout
        original_stderr = sys.stderr
        sys.stdout = _QueueWriter(self._queue, "out")  # type: ignore[assignment]
        sys.stderr = _QueueWriter(self._queue, "err")  # type: ignore[assignment]

        log_handler = _QueueLogHandler(self._queue)
        log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        root_logger.addHandler(log_handler)
        root_logger.setLevel(logging.INFO)

        try:
            organizer = ImageOrganizer(
                source_dir=source,
                output_dir=output,
                mode=mode,
                dry_run=dry_run,
                confidence_threshold=threshold,
                instructions=instructions,
            )
            # Wrap run() so we can post a "done" event with the stats.
            stats = self._run_with_progress(organizer)
            self._queue.put(("done", stats))
        except Exception as exc:  # noqa: BLE001 — surface any failure to UI
            self._queue.put(("error", f"{exc}\n\n{traceback.format_exc()}"))
        finally:
            root_logger.removeHandler(log_handler)
            root_logger.setLevel(previous_level)
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    def _run_with_progress(self, organizer: ImageOrganizer) -> dict[str, int]:
        """Patch ``_process_one`` so we can update the progress bar each step."""
        original = organizer._process_one  # type: ignore[attr-defined]

        def wrapped(path: Path, index: int, total: int) -> None:
            if self._cancel_event.is_set():
                raise KeyboardInterrupt("user requested stop")
            self._queue.put(("progress", (index, total)))
            original(path, index, total)

        organizer._process_one = wrapped  # type: ignore[attr-defined]
        return organizer.run()

    # ------------------------------------------------------------- queue

    def _poll_queue(self) -> None:
        """Drain messages from the worker thread and update the UI."""
        try:
            while True:
                tag, payload = self._queue.get_nowait()
                if tag in ("out", "err", "log"):
                    self._append_log(payload)
                elif tag == "progress":
                    index, total = payload
                    if total > 0:
                        self._progress["value"] = index / total
                    self._status_var.set(f"Processing image {index} of {total}…")
                elif tag == "done":
                    self._on_done(payload)
                elif tag == "error":
                    self._on_error(payload)
        except queue.Empty:
            pass
        self.root.after(_POLL_INTERVAL_MS, self._poll_queue)

    def _on_done(self, stats: dict[str, int]) -> None:
        self._set_running(False)
        self._progress["value"] = 1.0
        total_done = sum(stats.values()) if stats else 0
        if stats:
            summary = ", ".join(f"{k}: {v}" for k, v in sorted(stats.items()))
            self._status_var.set(f"Done — {total_done} images sorted ({summary}).")
            self._append_log(f"\n✓ Finished. {summary}\n")
        else:
            self._status_var.set("Done — nothing to do.")
            self._append_log("\n✓ Finished. No new images to process.\n")

    def _on_error(self, message: str) -> None:
        self._set_running(False)
        self._status_var.set("Failed — see log for details.")
        self._append_log(f"\n✗ ERROR: {message}\n")
        messagebox.showerror("Run failed", message.splitlines()[0])

    # -------------------------------------------------------------- helpers

    def _append_log(self, text: str) -> None:
        self._log.config(state=tk.NORMAL)
        self._log.insert(tk.END, text)
        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

    def _set_running(self, running: bool) -> None:
        self._start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self._stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)


def main() -> int:
    """Launch the GUI. Returns ``0`` when the window closes normally."""
    root = tk.Tk()
    try:
        # Slight DPI tweak on Windows so text is crisper on hi-res displays.
        from ctypes import windll  # type: ignore[attr-defined]

        windll.shcore.SetProcessDpiAwareness(1)
    except (ImportError, AttributeError, OSError):
        pass
    OrganizerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
