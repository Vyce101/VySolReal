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
from .models import EmbeddingManifest, EmbeddingProfile, WorldMetadata, WorldSplitterConfig


def world_metadata_file_path(world_dir: Path) -> Path:
    """Return the world metadata file path."""
    return world_dir / "world.json"


def load_world_metadata(world_dir: Path) -> WorldMetadata | None:
    """Load world metadata when it already exists."""
    metadata_path = world_metadata_file_path(world_dir)
    if not metadata_path.exists():
        return None
    return WorldMetadata.from_dict(json.loads(metadata_path.read_text(encoding="utf-8")))


def ensure_world_metadata(
    *,
    world_dir: Path,
    world_name: str,
    embedding_profile: EmbeddingProfile | None,
    splitter_config: WorldSplitterConfig | None = None,
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
        # BLOCK 3: Backfill the world-level splitter lock for older worlds the first time a caller provides it
        # WHY: The lock has to become authoritative for existing worlds without breaking older metadata files that predate this field
        if metadata.splitter_config is None and splitter_config is not None:
            metadata.splitter_config = splitter_config
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
        # BLOCK 4: Reject splitter changes for existing worlds once the world-level lock has been established
        # WHY: Chunk size, overlap, and lookback define the whole world's derived storage shape, so changing them requires a full re-ingest instead of a normal append
        if metadata.splitter_config is not None and splitter_config is not None and metadata.splitter_config != splitter_config:
            raise EmbeddingConfigurationError(
                code="WORLD_SPLITTER_CONFIG_LOCKED",
                message="The world already has locked chunking settings that do not match this request.",
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
        splitter_config=splitter_config,
    )
    save_world_metadata(metadata_path, metadata)
    return metadata


def save_world_metadata(metadata_path: Path, metadata: WorldMetadata) -> None:
    """Persist world metadata atomically."""
    atomic_write_json(metadata_path, metadata.to_dict())


def begin_world_ingestion_run(*, world_dir: Path, metadata: WorldMetadata) -> str:
    """Return the active ingestion run id for this world, creating one when needed."""
    # BLOCK 1: Reuse unfinished ingestion run identity or create a new durable run boundary
    # VARS: metadata_path = world.json path that stores the active run id between app restarts
    # WHY: Books added while prior embedding or extraction work is still incomplete must join the same run instead of silently creating unrelated graph extraction manifests
    metadata_path = world_metadata_file_path(world_dir)
    if metadata.active_ingestion_run_id and metadata.active_ingestion_run_status == "active":
        return metadata.active_ingestion_run_id
    # BLOCK 2: Flip a paused run back to active without minting a new run id
    # WHY: Resume and same-run book append both need the unfinished run boundary to stay stable until all current work is complete
    if metadata.active_ingestion_run_id and metadata.active_ingestion_run_status == "paused":
        metadata.active_ingestion_run_status = "active"
        save_world_metadata(metadata_path, metadata)
        return metadata.active_ingestion_run_id
    metadata.active_ingestion_run_id = f"run-{uuid4()}"
    metadata.active_ingestion_run_status = "active"
    save_world_metadata(metadata_path, metadata)
    return metadata.active_ingestion_run_id


def finish_world_ingestion_run(*, world_dir: Path, metadata: WorldMetadata, completed: bool) -> None:
    """Mark the active ingestion run completed only when all current work finished."""
    # BLOCK 1: Complete the active run only after every current book stage has finished
    # WHY: Partial embedding, pending graph extraction, or hard failures need the same run id available for the next resume attempt
    if not metadata.active_ingestion_run_id:
        return
    metadata.active_ingestion_run_status = "completed" if completed else "paused"
    save_world_metadata(world_metadata_file_path(world_dir), metadata)


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
