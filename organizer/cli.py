"""Command-line interface for the image organizer."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config
from .organizer import ImageOrganizer

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser used by :func:`main`."""
    parser = argparse.ArgumentParser(
        prog="image-organizer",
        description=(
            "Categorize, rename and organize a folder of images using "
            "NVIDIA NIM's free vision API."
        ),
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("./unsorted_images"),
        help="Source directory containing images to organize (default: ./unsorted_images).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./organized_images"),
        help="Destination directory for sorted images (default: ./organized_images).",
    )
    parser.add_argument(
        "--mode",
        choices=("move", "copy"),
        default="copy",
        help="Whether to move or copy files into the output folder (default: copy).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the run without modifying any files.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.7,
        help="Confidence threshold below which images go to _review/ (default: 0.7).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=f"Override the vision model name (default: {config.DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the manifest and start fresh (asks for confirmation).",
    )
    return parser


def _maybe_reset_manifest(output_dir: Path) -> None:
    """Optionally delete the manifest after asking the user."""
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        print("--reset requested but no manifest was found; nothing to delete.")
        return
    answer = input(f"Delete manifest at {manifest_path}? [y/N]: ").strip().lower()
    if answer in ("y", "yes"):
        manifest_path.unlink()
        print("Manifest deleted.")
    else:
        print("Reset aborted.")


def _ensure_utf8_streams() -> None:
    """Reconfigure stdout/stderr to UTF-8 so unicode progress chars print on Windows.

    The progress output contains ``→``, which crashes on a default Windows
    console (cp1252). Newer Pythons expose ``reconfigure`` on the standard
    streams; we fall back silently if it isn't available.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                # Stream was redirected to something we can't reconfigure;
                # safe to ignore — non-UTF-8 chars will be replaced.
                pass


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list (mainly for tests). When ``None``,
            :data:`sys.argv` is used.

    Returns:
        Process exit code: ``0`` on success, ``1`` on a fatal error.
    """
    _ensure_utf8_streams()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.reset:
        _maybe_reset_manifest(args.output)

    try:
        organizer = ImageOrganizer(
            source_dir=args.source,
            output_dir=args.output,
            mode=args.mode,
            dry_run=args.dry_run,
            confidence_threshold=args.threshold,
            model=args.model,
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        stats = organizer.run()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if stats:
        print("\nCategory totals:")
        for category, count in sorted(stats.items()):
            print(f"  {category}: {count}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
