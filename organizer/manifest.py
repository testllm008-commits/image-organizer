"""Persistent manifest for tracking processed images.

The manifest is a JSON file containing a list of entry dicts. It supports
resume-after-crash by exposing the set of already-processed source
filenames and provides a quick category histogram via :meth:`Manifest.stats`.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


class Manifest:
    """JSON-backed log of files the organizer has handled.

    Each entry is a free-form dict but is expected to include at least an
    ``original`` key (the source filename) and a ``category`` key so
    :meth:`processed_names` and :meth:`stats` work.
    """

    def __init__(self, path: Path) -> None:
        """Create or load a manifest at ``path``.

        Args:
            path: Filesystem location of the manifest JSON. Parent
                directories are created on save if necessary.
        """
        self.path: Path = Path(path)
        self.entries: list[dict[str, Any]] = []
        self.load()

    # ------------------------------------------------------------------ I/O

    def load(self) -> None:
        """Replace in-memory entries with the contents of :attr:`path`.

        A missing or empty file is treated as an empty manifest. A file
        that contains malformed JSON also resets to empty so the caller
        can recover instead of crashing mid-run.
        """
        if not self.path.exists():
            self.entries = []
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            self.entries = []
            return
        if not raw.strip():
            self.entries = []
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.entries = []
            return
        if isinstance(data, list):
            self.entries = [e for e in data if isinstance(e, dict)]
        else:
            self.entries = []

    def save(self) -> None:
        """Write the in-memory entries to :attr:`path` as pretty JSON."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # -------------------------------------------------------------- mutate

    def add(self, entry: dict[str, Any]) -> None:
        """Append ``entry`` and persist immediately.

        Args:
            entry: Mapping describing the processed image. Should contain
                ``original`` and ``category`` for downstream helpers to work.
        """
        if not isinstance(entry, dict):
            raise TypeError("Manifest entries must be dicts")
        self.entries.append(dict(entry))
        self.save()

    # ---------------------------------------------------------------- read

    def processed_names(self) -> set[str]:
        """Return the set of source filenames already in the manifest."""
        names: set[str] = set()
        for entry in self.entries:
            original = entry.get("original")
            if isinstance(original, str) and original:
                names.add(original)
        return names

    def stats(self) -> dict[str, int]:
        """Return a ``{category: count}`` histogram of processed entries."""
        counter: Counter[str] = Counter()
        for entry in self.entries:
            category = entry.get("category")
            if isinstance(category, str) and category:
                counter[category] += 1
        return dict(counter)

    def __len__(self) -> int:
        """Number of entries currently stored in the manifest."""
        return len(self.entries)
