"""Filesystem helpers for graph extraction manifests and world config."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.ingestion.text_sources.storage import atomic_write_json
from backend.logger import get_logger

from .errors import GraphExtractionError
from .models import GraphExtractionConfig, GraphExtractionManifest

DEFAULT_GRAPH_EXTRACTION_PROVIDER_ID = "google"
DEFAULT_GRAPH_EXTRACTION_MODEL_ID = "google/gemma-4-31b-it"

logger = get_logger(__name__)


def graph_config_file_path(world_dir: Path) -> Path:
    """Return the world-level graph extraction config path."""
    return world_dir / "graph_config.json"


def extraction_manifest_file_path(book_dir: Path) -> Path:
    """Return the per-book graph extraction manifest path."""
    return book_dir / "graph_extraction.json"


def load_graph_config(world_dir: Path) -> GraphExtractionConfig | None:
    """Load the world-level graph config if it exists."""
    # BLOCK 1: Treat missing graph config as an unconfigured world instead of a corrupt one
    # WHY: Graph extraction is a new feature, so older worlds may not have config until the first extraction-capable run writes it
    config_path = graph_config_file_path(world_dir)
    if not config_path.exists():
        return None
    return GraphExtractionConfig.from_dict(json.loads(config_path.read_text(encoding="utf-8")))


def default_graph_config(*, extraction_concurrency: int = 5) -> GraphExtractionConfig:
    """Return backend-owned default graph extraction settings."""
    # BLOCK 1: Build the default graph extraction config used for worlds without saved graph settings
    # WHY: Sweep 1 owns backend defaults in code until a later prompt-preset/UI system exists, while manifests snapshot the exact values used per run
    return GraphExtractionConfig(
        provider_id=DEFAULT_GRAPH_EXTRACTION_PROVIDER_ID,
        model_id=DEFAULT_GRAPH_EXTRACTION_MODEL_ID,
        gleaning_count=1,
        extraction_concurrency=extraction_concurrency,
        prompt_preset_id="default",
        prompt_preset_version=1,
        parser_version=1,
    )


def load_or_create_graph_config(*, world_dir: Path, extraction_concurrency: int = 5) -> GraphExtractionConfig:
    """Load a world's graph config, creating the backend defaults when absent."""
    # BLOCK 1: Ensure every extraction-capable world has one default graph config file
    # WHY: The world-level config is editable between paused/future calls, but missing config should not block older worlds from starting Sweep 1 extraction
    config = load_graph_config(world_dir)
    if config is not None:
        return config
    config = default_graph_config(extraction_concurrency=extraction_concurrency)
    save_graph_config(world_dir, config)
    return config


def save_graph_config(world_dir: Path, config: GraphExtractionConfig) -> None:
    """Persist the world-level graph config atomically."""
    # BLOCK 1: Save graph extraction defaults in the world folder without touching chunk or embedding manifests
    # WHY: Per-world user preferences need a stable home, while per-run snapshots still live in extraction manifests for reproducible resume
    atomic_write_json(graph_config_file_path(world_dir), config.to_dict())


def load_extraction_manifest(manifest_path: Path) -> GraphExtractionManifest | None:
    """Load a graph extraction manifest if it exists."""
    # BLOCK 1: Treat a missing manifest as a normal first-run case before attempting any JSON parsing
    # WHY: Graph extraction resume state is created lazily, so the storage boundary should only flag files that exist but cannot be trusted
    if not manifest_path.exists():
        return None
    try:
        return GraphExtractionManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "Graph extraction manifest load failed because the file is corrupt: manifest_name=%s error_type=%s",
            manifest_path.name,
            type(exc).__name__,
        )
        raise GraphExtractionError(
            code="GRAPH_EXTRACTION_MANIFEST_CORRUPT",
            message="The graph extraction manifest could not be trusted and must be rebuilt.",
            details={"manifest_path": str(manifest_path)},
        ) from exc


def save_extraction_manifest(manifest_path: Path, manifest: GraphExtractionManifest) -> None:
    """Persist a graph extraction manifest atomically."""
    atomic_write_json(manifest_path, manifest.to_dict())


def chunk_text_hash(text: str) -> str:
    """Return the stable hash for extracted chunk text."""
    # BLOCK 1: Hash only the chunk body sent as the extraction target
    # WHY: Overlap is reference-only for pronoun/title resolution, so including it would make resume treat unchanged chunk bodies as stale when only context text changed
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
