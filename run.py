"""Thin wrapper that invokes the image-organizer CLI."""

from __future__ import annotations

from organizer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
