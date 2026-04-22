"""Qdrant-backed vector storage helpers."""

from __future__ import annotations

from pathlib import Path

from backend.logger import get_logger
from qdrant_client import QdrantClient, models

from .errors import VectorStoreError
from .models import EmbeddingProfile, EmbeddingWorkItem, WorldMetadata

logger = get_logger(__name__)


class QdrantChunkStore:
    """Shared Qdrant chunk vector store."""

    def __init__(self, *, store_root: Path, collection_name: str = "chunks") -> None:
        self._store_root = store_root
        self._collection_name = collection_name
        self._client = self._create_client()

    def close(self) -> None:
        logger.info("Closing Qdrant chunk store for collection=%s.", self._collection_name)
        self._client.close()

    def ensure_collection(self, profile: EmbeddingProfile) -> None:
        # BLOCK 1: Create the shared chunks collection on first use and reject later profile mismatches before any upsert happens
        # WHY: Qdrant vectors in one collection must share one schema, so detecting dimensional drift before writes is the safest way to avoid silently corrupting retrieval state
        try:
            logger.info(
                "Ensuring Qdrant collection=%s for provider=%s model=%s dimensions=%s.",
                self._collection_name,
                profile.provider_id,
                profile.model_id,
                profile.dimensions,
            )
            self._store_root.mkdir(parents=True, exist_ok=True)
            if not self._client.collection_exists(self._collection_name):
                self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config=models.VectorParams(
                        size=profile.dimensions,
                        distance=models.Distance.COSINE,
                        on_disk=True,
                    ),
                    on_disk_payload=True,
                )
                logger.info("Created Qdrant collection=%s.", self._collection_name)
                return
            info = self._client.get_collection(self._collection_name)
            vectors_config = info.config.params.vectors
        except Exception as exc:
            logger.error(
                "Qdrant collection check failed for collection=%s store_root=%s reason=%s.",
                self._collection_name,
                self._store_root,
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_UNAVAILABLE",
                message="The local Qdrant store could not be opened or inspected.",
                details={"store_root": str(self._store_root), "reason": str(exc)},
            ) from exc

        if getattr(vectors_config, "size", None) != profile.dimensions or getattr(vectors_config, "distance", None) != models.Distance.COSINE:
            raise VectorStoreError(
                code="VECTOR_COLLECTION_MISMATCH",
                message="The shared chunks vector collection does not match the requested embedding profile.",
                details={
                    "collection_name": self._collection_name,
                    "expected_dimensions": profile.dimensions,
                },
            )

    def retrieve_existing_points(self, point_ids: list[str]) -> dict[str, models.Record]:
        """Fetch existing Qdrant points by id."""
        if not point_ids:
            return {}
        try:
            logger.info(
                "Retrieving %s existing Qdrant points from collection=%s.",
                len(point_ids),
                self._collection_name,
            )
            records = self._client.retrieve(
                collection_name=self._collection_name,
                ids=point_ids,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            logger.error(
                "Qdrant retrieve failed for collection=%s point_count=%s reason=%s.",
                self._collection_name,
                len(point_ids),
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_READ_FAILED",
                message="The local Qdrant store could not read existing embeddings.",
                details={"reason": str(exc)},
            ) from exc
        return {str(record.id): record for record in records}

    def upsert_chunk_embedding(
        self,
        *,
        world: WorldMetadata,
        work_item: EmbeddingWorkItem,
        vector: list[float],
        profile: EmbeddingProfile,
    ) -> None:
        """Store one chunk embedding in Qdrant."""
        # BLOCK 1: Upsert one stable chunk point with retrieval metadata after the provider has already produced a vector
        # WHY: Stable point ids let resume overwrite stale vectors safely without creating duplicates when the same chunk slot is retried
        payload = {
            "world_id": world.world_id,
            "world_uuid": world.world_uuid,
            "book_number": work_item.book_number,
            "chunk_number": work_item.chunk_number,
            "source_filename": work_item.source_filename,
            "chunk_position": work_item.chunk_position,
            "chunk_file": str(Path("books") / f"book_{work_item.book_number:02d}" / "chunks" / work_item.chunk_path.name),
            "embedding_model_id": profile.model_id,
            "embedding_profile_version": profile.profile_version,
            "text_hash": work_item.text_hash,
        }
        try:
            logger.info(
                "Upserting Qdrant point for collection=%s world_uuid=%s book=%s chunk=%s point_id=%s.",
                self._collection_name,
                world.world_uuid,
                work_item.book_number,
                work_item.chunk_number,
                work_item.point_id,
            )
            self._client.upsert(
                collection_name=self._collection_name,
                points=[
                    models.PointStruct(
                        id=work_item.point_id,
                        vector=vector,
                        payload=payload,
                    )
                ],
                wait=True,
            )
        except Exception as exc:
            logger.error(
                "Qdrant upsert failed for collection=%s point_id=%s book=%s chunk=%s reason=%s.",
                self._collection_name,
                work_item.point_id,
                work_item.book_number,
                work_item.chunk_number,
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_WRITE_FAILED",
                message="The local Qdrant store could not save the embedding vector.",
                details={"point_id": work_item.point_id, "reason": str(exc)},
            ) from exc

    def delete_points(self, point_ids: list[str]) -> None:
        """Delete explicit stale point ids."""
        if not point_ids:
            return
        try:
            logger.info(
                "Deleting %s stale Qdrant points from collection=%s.",
                len(point_ids),
                self._collection_name,
            )
            self._client.delete(
                collection_name=self._collection_name,
                points_selector=point_ids,
                wait=True,
            )
        except Exception as exc:
            logger.error(
                "Qdrant delete failed for collection=%s point_count=%s reason=%s.",
                self._collection_name,
                len(point_ids),
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_DELETE_FAILED",
                message="The local Qdrant store could not delete stale vectors.",
                details={"reason": str(exc)},
            ) from exc

    def _create_client(self) -> QdrantClient:
        # BLOCK 1: Open Qdrant in local on-disk mode under the shared app vector-store folder
        # WHY: The feature requires a local-first vector database, and Qdrant local mode keeps deployment simple while still persisting vectors across runs
        try:
            self._store_root.mkdir(parents=True, exist_ok=True)
            logger.info("Opening local Qdrant store at %s.", self._store_root / "qdrant")
            return QdrantClient(path=str(self._store_root / "qdrant"))
        except Exception as exc:
            logger.error(
                "Failed to initialize local Qdrant store at %s reason=%s.",
                self._store_root,
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_UNAVAILABLE",
                message="The local Qdrant store could not be initialized.",
                details={"store_root": str(self._store_root), "reason": str(exc)},
            ) from exc
