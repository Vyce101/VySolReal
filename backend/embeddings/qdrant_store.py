"""Qdrant-backed vector storage helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from backend.logger import get_logger
from qdrant_client import QdrantClient, models

from .errors import VectorStoreError
from .models import EmbeddingProfile, EmbeddingWorkItem, WorldMetadata

logger = get_logger(__name__)


class QdrantChunkStore:
    """Shared Qdrant chunk vector store."""

    def __init__(self, *, store_root: Path, collection_name: str | None = None) -> None:
        self._store_root = store_root
        self._collection_name = collection_name
        self._client = self._create_client()

    def close(self) -> None:
        logger.info("Closing Qdrant chunk store for collection=%s.", self._collection_name)
        self._client.close()

    def ensure_collection(self, profile: EmbeddingProfile) -> None:
        # BLOCK 1: Select or create the Qdrant collection that matches this world's locked embedding profile
        # WHY: Qdrant vectors in one collection must share one schema, so each embedding profile needs its own collection instead of the old global chunks bucket
        self._collection_name = collection_name_for_profile(profile)
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
                message="The profile-specific vector collection does not match the requested embedding profile.",
                details={
                    "collection_name": self._collection_name,
                    "expected_dimensions": profile.dimensions,
                },
            )

    def retrieve_existing_points(self, point_ids: list[str]) -> dict[str, models.Record]:
        """Fetch existing Qdrant points by id."""
        if not point_ids:
            return {}
        collection_name = self._active_collection_name()
        try:
            logger.info(
                "Retrieving %s existing Qdrant points from collection=%s.",
                len(point_ids),
                collection_name,
            )
            records = self._client.retrieve(
                collection_name=collection_name,
                ids=point_ids,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            logger.error(
                "Qdrant retrieve failed for collection=%s point_count=%s reason=%s.",
                collection_name,
                len(point_ids),
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_READ_FAILED",
                message="The local Qdrant store could not read existing embeddings.",
                details={"reason": str(exc)},
            ) from exc
        return {str(record.id): record for record in records}

    def query_similar_chunks(
        self,
        *,
        query_vector: list[float],
        world_uuid: str,
        limit: int,
        score_threshold: float,
    ) -> list[models.ScoredPoint]:
        """Search this profile collection for one world's nearest chunk vectors."""
        if limit <= 0:
            return []
        collection_name = self._active_collection_name()
        try:
            logger.info(
                "Querying Qdrant chunk vectors for collection=%s world_uuid=%s limit=%s score_threshold=%s.",
                collection_name,
                world_uuid,
                limit,
                score_threshold,
            )
            response = self._client.query_points(
                collection_name=collection_name,
                query=query_vector,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="world_uuid",
                            match=models.MatchValue(value=world_uuid),
                        )
                    ]
                ),
                limit=limit,
                with_payload=True,
                with_vectors=False,
                score_threshold=score_threshold,
            )
        except Exception as exc:
            logger.error(
                "Qdrant similarity query failed for collection=%s world_uuid=%s limit=%s reason=%s.",
                collection_name,
                world_uuid,
                limit,
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_QUERY_FAILED",
                message="The local Qdrant store could not query chunk embeddings.",
                details={"world_uuid": world_uuid, "reason": str(exc)},
            ) from exc
        return list(response.points)

    def upsert_chunk_embedding(
        self,
        *,
        world: WorldMetadata,
        ingestion_run_id: str,
        work_item: EmbeddingWorkItem,
        vector: list[float],
        profile: EmbeddingProfile,
    ) -> None:
        """Store one chunk embedding in Qdrant."""
        # BLOCK 1: Upsert one stable chunk point with retrieval metadata and the active ingestion run after the provider has already produced a vector
        # WHY: Stable point ids let resume overwrite stale vectors safely, while saving the run boundary in payload provenance lets later stages detect when an older run's chunk vector must be redone
        collection_name = self._active_collection_name()
        profile_key = embedding_profile_key(profile)
        payload = {
            "world_id": world.world_id,
            "world_uuid": world.world_uuid,
            "ingestion_run_id": ingestion_run_id,
            "book_number": work_item.book_number,
            "chunk_number": work_item.chunk_number,
            "source_filename": work_item.source_filename,
            "chunk_position": work_item.chunk_position,
            "chunk_file": str(Path("books") / f"book_{work_item.book_number:02d}" / "chunks" / work_item.chunk_path.name),
            "provider_id": profile.provider_id,
            "model_id": profile.model_id,
            "task_type": profile.task_type,
            "dimensions": profile.dimensions,
            "embedding_model_id": profile.model_id,
            "embedding_profile_version": profile.profile_version,
            "embedding_profile_key": profile_key,
            "text_hash": work_item.text_hash,
        }
        try:
            logger.info(
                "Upserting Qdrant point for collection=%s world_uuid=%s book=%s chunk=%s point_id=%s.",
                collection_name,
                world.world_uuid,
                work_item.book_number,
                work_item.chunk_number,
                work_item.point_id,
            )
            self._client.upsert(
                collection_name=collection_name,
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
                collection_name,
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
        collection_name = self._active_collection_name()
        try:
            logger.info(
                "Deleting %s stale Qdrant points from collection=%s.",
                len(point_ids),
                collection_name,
            )
            self._client.delete(
                collection_name=collection_name,
                points_selector=point_ids,
                wait=True,
            )
        except Exception as exc:
            logger.error(
                "Qdrant delete failed for collection=%s point_count=%s reason=%s.",
                collection_name,
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

    def _active_collection_name(self) -> str:
        # BLOCK 1: Return the selected profile collection or fail before touching Qdrant
        # WHY: The old default chunks collection is intentionally no longer implicit, because writes must target the collection that matches the world's locked embedding profile
        if self._collection_name is None:
            raise VectorStoreError(
                code="VECTOR_COLLECTION_NOT_SELECTED",
                message="The vector collection must be selected from the embedding profile before Qdrant access.",
                details={},
            )
        return self._collection_name


class QdrantNodeStore:
    """Shared Qdrant node vector store."""

    def __init__(self, *, store_root: Path, collection_name: str | None = None) -> None:
        self._store_root = store_root
        self._collection_name = collection_name
        self._client = self._create_client()

    def close(self) -> None:
        logger.info("Closing Qdrant node store for collection=%s.", self._collection_name)
        self._client.close()

    def ensure_collection(self, profile: EmbeddingProfile) -> None:
        # BLOCK 1: Select or create the Qdrant collection that stores node vectors for this embedding profile
        # WHY: Node vectors and chunk vectors have different payload contracts, so nodes need their own collection namespace even when they share the same vector dimensions
        self._collection_name = collection_name_for_node_profile(profile)
        try:
            logger.info(
                "Ensuring Qdrant node collection=%s for provider=%s model=%s dimensions=%s.",
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
                logger.info("Created Qdrant node collection=%s.", self._collection_name)
                return
            info = self._client.get_collection(self._collection_name)
            vectors_config = info.config.params.vectors
        except Exception as exc:
            logger.error(
                "Qdrant node collection check failed for collection=%s store_root=%s reason=%s.",
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
                message="The node vector collection does not match the requested embedding profile.",
                details={
                    "collection_name": self._collection_name,
                    "expected_dimensions": profile.dimensions,
                },
            )

    def retrieve_node_points(self, point_ids: list[str]) -> dict[str, models.Record]:
        """Fetch existing Qdrant node points by id."""
        if not point_ids:
            return {}
        collection_name = self._active_collection_name()
        try:
            logger.info(
                "Retrieving %s existing Qdrant node points from collection=%s.",
                len(point_ids),
                collection_name,
            )
            records = self._client.retrieve(
                collection_name=collection_name,
                ids=point_ids,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            logger.error(
                "Qdrant node retrieve failed for collection=%s point_count=%s reason=%s.",
                collection_name,
                len(point_ids),
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_READ_FAILED",
                message="The local Qdrant store could not read existing node embeddings.",
                details={"reason": str(exc)},
            ) from exc
        return {str(record.id): record for record in records}

    def upsert_node_embedding(
        self,
        *,
        world: WorldMetadata,
        point_id: str,
        vector: list[float],
        profile: EmbeddingProfile,
        ingestion_run_id: str,
        source_filename: str,
        book_number: int,
        chunk_number: int,
        chunk_position: str,
        chunk_file: str,
        chunk_text_hash: str,
        node_id: str,
        display_name: str,
        text_hash: str,
    ) -> None:
        """Store one node embedding in Qdrant."""
        # BLOCK 1: Upsert one graph node vector with enough source metadata to trace it back to the chunk that produced it
        # WHY: Node retrieval needs the graph identity and source chunk references, but storing node descriptions or chunk text would duplicate user content inside the vector payload
        collection_name = self._active_collection_name()
        profile_key = embedding_profile_key(profile)
        payload = {
            "world_uuid": world.world_uuid,
            "ingestion_run_id": ingestion_run_id,
            "source_filename": source_filename,
            "book_number": book_number,
            "chunk_number": chunk_number,
            "chunk_position": chunk_position,
            "chunk_file": chunk_file,
            "chunk_text_hash": chunk_text_hash,
            "node_id": node_id,
            "display_name": display_name,
            "provider_id": profile.provider_id,
            "model_id": profile.model_id,
            "task_type": profile.task_type,
            "dimensions": profile.dimensions,
            "embedding_model_id": profile.model_id,
            "embedding_profile_version": profile.profile_version,
            "embedding_profile_key": profile_key,
            "text_hash": text_hash,
        }
        try:
            logger.info(
                "Upserting Qdrant node point for collection=%s world_uuid=%s node_id=%s point_id=%s.",
                collection_name,
                world.world_uuid,
                node_id,
                point_id,
            )
            self._client.upsert(
                collection_name=collection_name,
                points=[
                    models.PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=payload,
                    )
                ],
                wait=True,
            )
        except Exception as exc:
            logger.error(
                "Qdrant node upsert failed for collection=%s point_id=%s node_id=%s reason=%s.",
                collection_name,
                point_id,
                node_id,
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_WRITE_FAILED",
                message="The local Qdrant store could not save the node embedding vector.",
                details={"point_id": point_id, "reason": str(exc)},
            ) from exc

    def delete_node_points(self, point_ids: list[str]) -> None:
        """Delete explicit node point ids."""
        if not point_ids:
            return
        collection_name = self._active_collection_name()
        try:
            logger.info(
                "Deleting %s Qdrant node points from collection=%s.",
                len(point_ids),
                collection_name,
            )
            self._client.delete(
                collection_name=collection_name,
                points_selector=point_ids,
                wait=True,
            )
        except Exception as exc:
            logger.error(
                "Qdrant node delete failed for collection=%s point_count=%s reason=%s.",
                collection_name,
                len(point_ids),
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_DELETE_FAILED",
                message="The local Qdrant store could not delete node vectors.",
                details={"reason": str(exc)},
            ) from exc

    def delete_node_vectors_for_chunk(
        self,
        *,
        world_uuid: str,
        ingestion_run_id: str,
        book_number: int,
        chunk_number: int,
    ) -> None:
        """Delete node vectors for one extraction chunk boundary."""
        # BLOCK 1: Delete node vectors by the exact source chunk boundary.
        # WHY: When a chunk is redone, removed candidates may no longer have point ids in the fresh manifest, so cleanup has to use provenance metadata instead of explicit ids only.
        collection_name = self._active_collection_name()
        try:
            logger.info(
                "Deleting Qdrant node vectors for collection=%s world_uuid=%s run=%s book=%s chunk=%s.",
                collection_name,
                world_uuid,
                ingestion_run_id,
                book_number,
                chunk_number,
            )
            self._client.delete(
                collection_name=collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(key="world_uuid", match=models.MatchValue(value=world_uuid)),
                            models.FieldCondition(key="ingestion_run_id", match=models.MatchValue(value=ingestion_run_id)),
                            models.FieldCondition(key="book_number", match=models.MatchValue(value=book_number)),
                            models.FieldCondition(key="chunk_number", match=models.MatchValue(value=chunk_number)),
                        ]
                    )
                ),
                wait=True,
            )
        except Exception as exc:
            logger.error(
                "Qdrant node chunk delete failed for collection=%s world_uuid=%s book=%s chunk=%s reason=%s.",
                collection_name,
                world_uuid,
                book_number,
                chunk_number,
                str(exc),
            )
            raise VectorStoreError(
                code="VECTOR_STORE_DELETE_FAILED",
                message="The local Qdrant store could not delete stale node vectors for a redone chunk.",
                details={"reason": str(exc)},
            ) from exc

    def _create_client(self) -> QdrantClient:
        # BLOCK 1: Open Qdrant in local on-disk mode under the shared app vector-store folder
        # WHY: Node vectors must live beside chunk vectors in the same local database while remaining isolated by collection name
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

    def _active_collection_name(self) -> str:
        # BLOCK 1: Return the selected node profile collection or fail before touching Qdrant
        # WHY: Node writes must target the nodes-prefixed collection for the active embedding profile, otherwise they could mix with chunk-vector payloads
        if self._collection_name is None:
            raise VectorStoreError(
                code="VECTOR_COLLECTION_NOT_SELECTED",
                message="The node vector collection must be selected from the embedding profile before Qdrant access.",
                details={},
            )
        return self._collection_name


def embedding_profile_key(profile: EmbeddingProfile) -> str:
    """Return a stable key for the vector shape used by one embedding profile."""
    # BLOCK 1: Hash the profile fields that affect vector compatibility into one stable key
    # WHY: Collection names need to be deterministic across runs while avoiding raw provider/model punctuation that may be awkward in storage names
    payload = {
        "provider_id": profile.provider_id,
        "model_id": profile.model_id,
        "dimensions": profile.dimensions,
        "task_type": profile.task_type,
        "profile_version": profile.profile_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def collection_name_for_profile(profile: EmbeddingProfile) -> str:
    """Return the Qdrant collection name for one embedding profile."""
    # BLOCK 1: Build a readable profile-specific collection name with a short collision-resistant suffix
    # WHY: One Qdrant collection has one vector size, so the collection name must encode the locked vector contract instead of using the old global chunks bucket
    base_name = (
        f"chunks_{profile.provider_id}_{profile.model_id}_{profile.dimensions}_"
        f"{profile.task_type}_v{profile.profile_version}"
    ).lower()
    safe_name = re.sub(r"[^a-z0-9_]+", "_", base_name).strip("_")
    return f"{safe_name[:64]}_{embedding_profile_key(profile)}"


def collection_name_for_node_profile(profile: EmbeddingProfile) -> str:
    """Return the Qdrant node collection name for one embedding profile."""
    # BLOCK 1: Build a readable node-profile collection name with the same compatibility hash as chunk collections
    # WHY: The shared hash keeps node and chunk collections aligned to the same vector contract while the nodes prefix prevents payload-schema mixing
    base_name = (
        f"nodes_{profile.provider_id}_{profile.model_id}_{profile.dimensions}_"
        f"{profile.task_type}_v{profile.profile_version}"
    ).lower()
    safe_name = re.sub(r"[^a-z0-9_]+", "_", base_name).strip("_")
    return f"{safe_name[:64]}_{embedding_profile_key(profile)}"
