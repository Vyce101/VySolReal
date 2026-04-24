"""Filesystem storage helpers for embedding manifests and world metadata."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from backend.ingestion.txt_splitting.storage import atomic_write_json
from backend.provider_keys.storage import (
    load_provider_runtime_states,
    provider_runtime_state_file_path,
    save_provider_runtime_states,
)

from .catalog import lock_profile_to_model_maxima
from .errors import EmbeddingConfigurationError
from .models import EmbeddingManifest, WorldMetadata, EmbeddingProfile


def world_metadata_file_path(world_dir: Path) -> Path:
    """Return the world metadata file path."""
    return world_dir / "world.json"


def ensure_world_metadata(
    *,
    world_dir: Path,
    world_name: str,
    embedding_profile: EmbeddingProfile | None,
) -> WorldMetadata:
    """Load or create world metadata with a locked embedding profile."""
    # BLOCK 1: Reuse existing world metadata when it exists, otherwise create new stable world identity metadata for the world directory
    # WHY: Worlds need one permanent UUID and one locked embedding contract so renames and future re-embed actions do not break vector identity or resume safety
    metadata_path = world_metadata_file_path(world_dir)
    if metadata_path.exists():
        metadata = WorldMetadata.from_dict(json.loads(metadata_path.read_text(encoding="utf-8")))
        # BLOCK 2: Upgrade stored world profiles to the backend-owned maxima so older worlds keep matching the current locked model contract
        # WHY: Earlier worlds may be missing newly locked profile fields, and normalizing them on load prevents false lock mismatches without requiring a manual migration step
        normalized_profile = lock_profile_to_model_maxima(metadata.embedding_profile)
        if normalized_profile != metadata.embedding_profile:
            metadata.embedding_profile = normalized_profile
            save_world_metadata(metadata_path, metadata)
        if embedding_profile is not None and metadata.embedding_profile != embedding_profile:
            raise EmbeddingConfigurationError(
                code="WORLD_EMBEDDING_PROFILE_LOCKED",
                message="The world already has a locked embedding profile that does not match this request.",
                details={
                    "world_uuid": metadata.world_uuid,
                    "world_name": metadata.world_name,
                },
            )
        return metadata
    if embedding_profile is None:
        raise EmbeddingConfigurationError(
            code="EMBEDDING_PROFILE_REQUIRED",
            message="A new world must be created with an explicit embedding profile.",
            details={"world_name": world_name},
        )

    metadata = WorldMetadata(
        world_id=world_name,
        world_uuid=str(uuid4()),
        world_name=world_name,
        embedding_profile=lock_profile_to_model_maxima(embedding_profile),
    )
    save_world_metadata(metadata_path, metadata)
    return metadata


def save_world_metadata(metadata_path: Path, metadata: WorldMetadata) -> None:
    """Persist world metadata atomically."""
    atomic_write_json(metadata_path, metadata.to_dict())


def embedding_manifest_file_path(book_dir: Path) -> Path:
    """Return the per-book embedding manifest path."""
    return book_dir / "embeddings.json"


def load_embedding_manifest(manifest_path: Path) -> EmbeddingManifest | None:
    """Load an embedding manifest if it exists."""
    if not manifest_path.exists():
        return None
    return EmbeddingManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))


def save_embedding_manifest(manifest_path: Path, manifest: EmbeddingManifest) -> None:
    """Persist the embedding manifest atomically."""
    atomic_write_json(manifest_path, manifest.to_dict())


def default_vector_store_root() -> Path:
    """Resolve the shared vector store root."""
    return Path(__file__).resolve().parents[2] / "user" / "vector_store"


def utc_now() -> datetime:
    """Return the current machine clock in UTC."""
    return datetime.now(timezone.utc)


def chunk_text_hash(text: str) -> str:
    """Return the stable hash for embedded chunk text."""
    # BLOCK 1: Hash only the embedded chunk text so stale vector detection stays tied to the exact content sent to the embedding provider
    # WHY: Overlap text is intentionally excluded from embeddings, so including it in the hash would trigger unnecessary vector rewrites for content that never reached the provider
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
