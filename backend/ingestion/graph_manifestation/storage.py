"""Filesystem helpers for graph manifestation manifests."""

from __future__ import annotations

import json
from pathlib import Path

from backend.ingestion.text_sources.storage import atomic_write_json

from .errors import GraphManifestationConfigurationError
from .models import GraphManifestationManifest


def manifestation_manifest_file_path(book_dir: Path) -> Path:
    """Return the per-book graph manifestation manifest path."""
    return book_dir / "graph_manifestation.json"


def load_manifestation_manifest(manifest_path: Path) -> GraphManifestationManifest | None:
    """Load a graph manifestation manifest if it exists."""
    if not manifest_path.exists():
        return None
    try:
        return GraphManifestationManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise GraphManifestationConfigurationError(
            code="GRAPH_MANIFESTATION_MANIFEST_CORRUPT",
            message="The graph manifestation manifest could not be trusted and must be rebuilt.",
            details={"manifest_path": str(manifest_path)},
        ) from exc


def save_manifestation_manifest(manifest_path: Path, manifest: GraphManifestationManifest) -> None:
    """Persist a graph manifestation manifest atomically."""
    atomic_write_json(manifest_path, manifest.to_dict())
