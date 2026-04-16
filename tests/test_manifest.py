"""Tests for :class:`organizer.manifest.Manifest`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from organizer.manifest import Manifest


def _entry(original: str, category: str = "produce", **extra: object) -> dict[str, object]:
    """Build a minimal manifest entry for tests."""
    base: dict[str, object] = {"original": original, "category": category}
    base.update(extra)
    return base


def test_new_manifest_is_empty(tmp_path: Path) -> None:
    """A manifest at a fresh path starts with no entries."""
    manifest = Manifest(tmp_path / "manifest.json")
    assert len(manifest) == 0
    assert manifest.entries == []
    assert manifest.processed_names() == set()
    assert manifest.stats() == {}


def test_add_persists_to_disk(tmp_path: Path) -> None:
    """``add`` should append to memory and immediately rewrite the JSON file."""
    path = tmp_path / "manifest.json"
    manifest = Manifest(path)

    manifest.add(_entry("apple.jpg", "produce", confidence=0.9))

    assert len(manifest) == 1
    assert path.exists(), "Manifest should be saved after add()"

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(on_disk, list)
    assert on_disk[0]["original"] == "apple.jpg"
    assert on_disk[0]["category"] == "produce"


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    """A second Manifest pointed at the same file should see the same data."""
    path = tmp_path / "manifest.json"
    first = Manifest(path)
    first.add(_entry("milk.jpg", "dairy"))
    first.add(_entry("bread.jpg", "bakery"))

    second = Manifest(path)
    assert len(second) == 2
    assert {e["original"] for e in second.entries} == {"milk.jpg", "bread.jpg"}


def test_resume_skips_already_processed(tmp_path: Path) -> None:
    """``processed_names`` should expose already-handled source files."""
    path = tmp_path / "manifest.json"
    manifest = Manifest(path)
    manifest.add(_entry("a.jpg"))
    manifest.add(_entry("b.png", category="meat"))

    incoming = ["a.jpg", "b.png", "c.webp"]
    pending = [name for name in incoming if name not in manifest.processed_names()]
    assert pending == ["c.webp"]


def test_stats_counts_categories(tmp_path: Path) -> None:
    """``stats`` should produce a histogram across category strings."""
    manifest = Manifest(tmp_path / "manifest.json")
    manifest.add(_entry("a.jpg", "produce"))
    manifest.add(_entry("b.jpg", "produce"))
    manifest.add(_entry("c.jpg", "meat"))
    manifest.add(_entry("d.jpg", "dairy"))

    assert manifest.stats() == {"produce": 2, "meat": 1, "dairy": 1}


def test_load_handles_corrupt_file(tmp_path: Path) -> None:
    """A malformed manifest file should be treated as empty, not raise."""
    path = tmp_path / "manifest.json"
    path.write_text("{not valid json", encoding="utf-8")

    manifest = Manifest(path)
    assert manifest.entries == []
    # Should still be writable after recovery.
    manifest.add(_entry("recovered.jpg"))
    assert len(manifest) == 1


def test_add_rejects_non_dict(tmp_path: Path) -> None:
    """``add`` must reject anything that isn't a dict."""
    manifest = Manifest(tmp_path / "manifest.json")
    with pytest.raises(TypeError):
        manifest.add("oops")  # type: ignore[arg-type]


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    """``save`` should create missing parent directories."""
    nested = tmp_path / "deep" / "nested" / "manifest.json"
    manifest = Manifest(nested)
    manifest.add(_entry("x.jpg"))
    assert nested.exists()
    assert nested.parent.is_dir()
