"""High-level orchestration for sorting an image directory.

The :class:`ImageOrganizer` walks a source directory, calls
:func:`organizer.vision.analyze_image` for each unseen file, and then
moves or copies the image into ``{output}/{category}/`` with a sanitized
filename. Progress is logged through Python's ``logging`` module while
per-image status is printed to ``stdout`` so users see real-time output.
"""

from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import config
from .manifest import Manifest
from .vision import VisionError, analyze_image

logger = logging.getLogger(__name__)

# Folder name used for low-confidence results that need human review.
_REVIEW_DIR = "_review"

# Maximum API call attempts per image before giving up.
_MAX_RETRIES = 3


class ImageOrganizer:
    """Walk a directory of images and sort them into category folders."""

    def __init__(
        self,
        source_dir: Path,
        output_dir: Path,
        mode: str = "copy",
        dry_run: bool = False,
        confidence_threshold: float = 0.7,
        *,
        model: str | None = None,
        manifest_path: Path | None = None,
        categories: list[str] | None = None,
        instructions: str | None = None,
    ) -> None:
        """Configure the organizer.

        Args:
            source_dir: Directory to scan for images.
            output_dir: Destination root for categorized images.
            mode: Either ``"move"`` or ``"copy"``.
            dry_run: When ``True`` no files are touched; planned actions are
                printed instead.
            confidence_threshold: Images returned with confidence below this
                value go to ``output_dir/_review/`` for manual review.
            model: Optional model name override (defaults to
                :data:`config.DEFAULT_MODEL`).
            manifest_path: Optional explicit manifest location. Defaults to
                ``output_dir/manifest.json``.
            categories: Optional override for the allowed category list.
            instructions: Optional free-form text appended to the prompt so
                the user can steer how the model labels each image.
        """
        if mode not in ("move", "copy"):
            raise ValueError(f"mode must be 'move' or 'copy', got {mode!r}")

        self.source_dir = Path(source_dir)
        self.output_dir = Path(output_dir)
        self.mode = mode
        self.dry_run = dry_run
        self.confidence_threshold = float(confidence_threshold)
        self.model = model or config.DEFAULT_MODEL
        self.categories = categories if categories is not None else config.load_categories()
        self.instructions = instructions or None

        manifest_default = self.output_dir / "manifest.json"
        self.manifest = Manifest(manifest_path if manifest_path is not None else manifest_default)

    # --------------------------------------------------------------- helpers

    def _discover_images(self) -> list[Path]:
        """Return a sorted list of image files in :attr:`source_dir`."""
        if not self.source_dir.exists():
            raise FileNotFoundError(f"Source directory does not exist: {self.source_dir}")
        if not self.source_dir.is_dir():
            raise NotADirectoryError(f"Source path is not a directory: {self.source_dir}")
        candidates: list[Path] = []
        for child in sorted(self.source_dir.iterdir()):
            if child.is_file() and child.suffix.lower() in config.VALID_EXTS:
                candidates.append(child)
        return candidates

    def _next_sequence(self) -> int:
        """Return the next 1-indexed sequence number for naming files."""
        return len(self.manifest) + 1

    def _target_path(self, category: str, item_name: str, seq: int, suffix: str) -> Path:
        """Build the destination path for a processed image."""
        ext = suffix.lower()
        new_name = f"{item_name}_{seq:04d}{ext}"
        return self.output_dir / category / new_name

    def _analyze_with_retry(self, path: Path) -> dict[str, Any]:
        """Call :func:`analyze_image` with exponential backoff retries.

        Backs off 2s, 4s, 8s on consecutive failures. After
        :data:`_MAX_RETRIES` attempts the final :class:`VisionError` is
        re-raised so the caller can decide whether to skip or abort.
        """
        last_exc: VisionError | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return analyze_image(
                    path,
                    model=self.model,
                    categories=self.categories,
                    instructions=self.instructions,
                )
            except VisionError as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    break
                backoff = 2 ** attempt  # 2, 4, 8
                logger.warning(
                    "Vision call failed for %s (attempt %d/%d): %s — retrying in %ds",
                    path.name,
                    attempt,
                    _MAX_RETRIES,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
        assert last_exc is not None  # for type checker
        raise last_exc

    def _place_file(self, source: Path, target: Path) -> None:
        """Move or copy ``source`` to ``target`` honoring :attr:`dry_run`."""
        if self.dry_run:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "move":
            shutil.move(str(source), str(target))
        else:
            shutil.copy2(str(source), str(target))

    # ------------------------------------------------------------------ run

    def run(self) -> dict[str, int]:
        """Process every unseen image in :attr:`source_dir`.

        Returns:
            The category histogram from :meth:`Manifest.stats` after the
            run completes (or after an interrupt).
        """
        images = self._discover_images()
        already_done = self.manifest.processed_names()
        pending = [p for p in images if p.name not in already_done]

        total = len(images)
        if not pending:
            logger.info("Nothing to do: all %d images already processed.", total)
            print(f"Nothing to do: all {total} images already processed.")
            return self.manifest.stats()

        logger.info(
            "Starting run: %d images total, %d pending (mode=%s, dry_run=%s)",
            total,
            len(pending),
            self.mode,
            self.dry_run,
        )

        index = len(already_done)
        try:
            for image_path in pending:
                index += 1
                self._process_one(image_path, index, total)
                # Stay under NVIDIA's free-tier rate limit.
                time.sleep(config.RATE_LIMIT_DELAY)
        except KeyboardInterrupt:
            print("\nInterrupted by user — manifest saved. Re-run to resume.", file=sys.stderr)
            logger.warning("Run interrupted by user; %d entries persisted.", len(self.manifest))
            return self.manifest.stats()

        stats = self.manifest.stats()
        logger.info("Run complete. Stats: %s", stats)
        return stats

    def _process_one(self, image_path: Path, index: int, total: int) -> None:
        """Analyze a single image and place it in the output tree."""
        try:
            result = self._analyze_with_retry(image_path)
        except VisionError as exc:
            logger.error("Giving up on %s after retries: %s", image_path.name, exc)
            print(f"[{index}/{total}] {image_path.name} → SKIPPED ({exc})")
            return

        confidence = float(result["confidence"])
        item_name = result["item_name"]
        category = result["category"]

        if confidence < self.confidence_threshold:
            placement_dir = _REVIEW_DIR
        else:
            placement_dir = category

        seq = self._next_sequence()
        target = self._target_path(placement_dir, item_name, seq, image_path.suffix)

        action = "WOULD " + self.mode.upper() if self.dry_run else self.mode.upper()
        print(
            f"[{index}/{total}] {image_path.name} → "
            f"{placement_dir}/{target.name} ({confidence:.2f}) [{action}]"
        )

        self._place_file(image_path, target)

        # Only persist to the manifest when actually moving/copying files.
        # A dry-run must not mark images as processed, otherwise a real run
        # immediately after would skip everything thinking it was done.
        if not self.dry_run:
            entry = {
                "original": image_path.name,
                "category": category,
                "placed_in": placement_dir,
                "item_name": item_name,
                "description": result["description"],
                "confidence": confidence,
                "new_path": str(target),
                "mode": self.mode,
            }
            self.manifest.add(entry)
