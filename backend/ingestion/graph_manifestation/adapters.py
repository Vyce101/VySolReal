"""Runtime adapters that connect graph manifestation to embeddings, Qdrant, and Neo4j."""

from __future__ import annotations

import json
import os
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from backend.embeddings.models import EmbeddingFailure, EmbeddingSuccess, EmbeddingWorkItem, WorldMetadata
from backend.embeddings.providers import create_embedding_provider
from backend.embeddings.qdrant_store import QdrantNodeStore
from backend.embeddings.storage import default_vector_store_root
from backend.logger import get_logger
from backend.provider_keys import ProviderKeyScheduler, ProviderRateLimitFailure, default_provider_keys_root
from backend.provider_keys.models import ProviderCredential

from .errors import GraphStoreUnavailable, NodeEmbeddingManifestationError
from .models import (
    ManifestationFailure,
    NodeEmbeddingBatchResult,
    NodeEmbeddingWorkItem,
    NodeVectorWrite,
)
from .neo4j_adapter import Neo4jGraphWriter

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class Neo4jConnectionConfig:
    """Local Neo4j connection settings for the graph writer."""

    uri: str
    username: str
    password: str


class ScheduledNodeEmbedder:
    """Embed extracted graph nodes through the shared provider-key scheduler."""

    def __init__(
        self,
        *,
        world: WorldMetadata,
        provider_keys_root: Path | None,
        concurrency: int,
    ) -> None:
        # BLOCK 1: Resolve the locked world embedding profile and shared key scheduler used for node embeddings
        # WHY: Node vectors must use the exact same embedding contract as chunk vectors, and key use must share the global provider gate with extraction and chunk embedding
        self._world = world
        self._provider_keys_root = provider_keys_root if provider_keys_root is not None else default_provider_keys_root()
        self._concurrency = max(1, concurrency)
        self._provider = create_embedding_provider(world.embedding_profile.provider_id)
        self._scheduler = ProviderKeyScheduler.for_model(
            provider_id=world.embedding_profile.provider_id,
            model_id=world.embedding_profile.model_id,
            provider_keys_root=self._provider_keys_root,
        )

    def embed_nodes(self, work_items: list[NodeEmbeddingWorkItem]) -> NodeEmbeddingBatchResult:
        """Embed graph nodes and return vectors keyed by node id."""
        # BLOCK 1: Short-circuit missing embedding credentials as per-node failures
        # WHY: Manifestation should stay resumable and visible instead of throwing away completed extraction output when setup is incomplete
        if not self._scheduler.credentials:
            logger.warning(
                "Node embedding batch cannot start because no credentials are configured for world_uuid=%s provider=%s model=%s requested_nodes=%s.",
                self._world.world_uuid,
                self._world.embedding_profile.provider_id,
                self._world.embedding_profile.model_id,
                len(work_items),
            )
            return NodeEmbeddingBatchResult(
                failures={
                    item.node_id: ManifestationFailure(
                        code="NODE_EMBEDDING_PROVIDER_KEYS_MISSING",
                        message="No provider credentials are configured for the locked embedding model.",
                    )
                    for item in work_items
                }
            )

        # BLOCK 2: Dispatch node embedding calls through the same reservation and failover flow used by chunk embeddings
        # VARS: pending_queue = node work not yet accepted by a provider, futures = in-flight provider calls keyed by future
        # WHY: Node embedding has a different manifest, but provider keys and quota buckets are shared globally across all AI workflows
        vectors: dict[str, list[float]] = {}
        failures: dict[str, ManifestationFailure] = {}
        pending_queue = work_items[:]
        futures: dict[Future[EmbeddingSuccess | EmbeddingFailure], tuple[NodeEmbeddingWorkItem, ProviderCredential]] = {}
        waiting_for_credential_logged = False
        with ThreadPoolExecutor(max_workers=self._concurrency) as executor:
            while pending_queue or futures:
                while len(futures) < self._concurrency and pending_queue:
                    node_item = pending_queue[0]
                    embedding_item = _embedding_work_item_from_node(node_item)
                    credential = self._scheduler.select_credential(
                        token_estimate=_estimate_tokens(node_item.embedding_text),
                    )
                    if credential is None:
                        break
                    pending_queue.pop(0)
                    waiting_for_credential_logged = False
                    future = executor.submit(
                        self._provider.embed_text,
                        credential=credential,
                        profile=self._world.embedding_profile,
                        work_item=embedding_item,
                    )
                    futures[future] = (node_item, credential)

                if not futures:
                    if self._scheduler.has_future_credential_availability():
                        if not waiting_for_credential_logged:
                            logger.info(
                                "Node embedding batch is waiting for an eligible credential for world_uuid=%s provider=%s model=%s pending_nodes=%s configured_credentials=%s.",
                                self._world.world_uuid,
                                self._world.embedding_profile.provider_id,
                                self._world.embedding_profile.model_id,
                                len(pending_queue),
                                len(self._scheduler.credentials),
                            )
                            waiting_for_credential_logged = True
                        self._scheduler.wait_for_next_available_credential()
                        continue
                    logger.warning(
                        "Node embedding batch paused because no eligible credential is currently usable for world_uuid=%s provider=%s model=%s pending_nodes=%s configured_credentials=%s.",
                        self._world.world_uuid,
                        self._world.embedding_profile.provider_id,
                        self._world.embedding_profile.model_id,
                        len(pending_queue),
                        len(self._scheduler.credentials),
                    )
                    for node_item in pending_queue:
                        failures[node_item.node_id] = ManifestationFailure(
                            code="NODE_EMBEDDING_PROVIDER_KEYS_UNAVAILABLE",
                            message="No eligible provider credential is currently available for node embedding.",
                        )
                    pending_queue.clear()
                    break

                done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    node_item, credential = futures.pop(future)
                    outcome = future.result()
                    if isinstance(outcome, EmbeddingSuccess):
                        self._scheduler.record_success(
                            scope_key=outcome.quota_scope,
                            token_estimate=_estimate_tokens(node_item.embedding_text),
                        )
                        vectors[node_item.node_id] = outcome.vector
                        continue
                    self._handle_failure(
                        node_item=node_item,
                        credential=credential,
                        failure=outcome,
                        pending_queue=pending_queue,
                        failures=failures,
                    )
        self._scheduler.save_runtime_states()
        return NodeEmbeddingBatchResult(vectors=vectors, failures=failures)

    def _handle_failure(
        self,
        *,
        node_item: NodeEmbeddingWorkItem,
        credential: ProviderCredential,
        failure: EmbeddingFailure,
        pending_queue: list[NodeEmbeddingWorkItem],
        failures: dict[str, ManifestationFailure],
    ) -> None:
        # BLOCK 1: Convert provider failures into node-level retry state without writing provider response bodies
        # WHY: The manifestation manifest owns node retries, while the shared scheduler owns key cooldowns and reservation cleanup
        if failure.rate_limit_type is not None:
            self._scheduler.apply_rate_limit_failure(
                credential=credential,
                failure=ProviderRateLimitFailure(
                    rate_limit_type=failure.rate_limit_type,
                    message=failure.message,
                    retry_after_seconds=failure.retry_after_seconds,
                    limit_scope=failure.rate_limit_scope,
                ),
            )
            pending_queue.append(node_item)
            return
        self._scheduler.release_reservation(
            scope_key=failure.quota_scope,
            token_estimate=_estimate_tokens(node_item.embedding_text),
        )
        failures[node_item.node_id] = ManifestationFailure(
            code=failure.code,
            message=failure.message,
            retryable=failure.retryable,
        )


class QdrantGraphNodeVectorStore:
    """Graph manifestation adapter for Qdrant node vectors."""

    def __init__(
        self,
        *,
        world: WorldMetadata,
        vector_store_root: Path | None,
    ) -> None:
        # BLOCK 1: Open the node-vector collection for the world's locked embedding profile
        # WHY: Node and chunk vectors share a model profile but must stay in separate Qdrant collections to avoid payload-schema mixing
        self._world = world
        self._store = QdrantNodeStore(store_root=vector_store_root if vector_store_root is not None else default_vector_store_root())
        self._store.ensure_collection(world.embedding_profile)

    def close(self) -> None:
        self._store.close()

    def upsert_node_embeddings(self, writes: list[NodeVectorWrite]) -> None:
        """Persist node vectors into Qdrant."""
        # BLOCK 1: Persist each node vector with source provenance from manifestation state
        # WHY: The vector store should be idempotent by point id and should not store raw provider responses or full chunk text
        try:
            for write in writes:
                self._store.upsert_node_embedding(
                    world=self._world,
                    point_id=write.point_id,
                    vector=write.vector,
                    profile=self._world.embedding_profile,
                    ingestion_run_id=write.ingestion_run_id,
                    source_filename=write.source_filename,
                    book_number=write.book_number,
                    chunk_number=write.chunk_number,
                    chunk_position=write.chunk_position,
                    chunk_file=write.chunk_file,
                    chunk_text_hash=write.chunk_text_hash,
                    node_id=write.node_id,
                    display_name=write.display_name,
                    text_hash=write.text_hash,
                )
        except Exception as exc:
            raise NodeEmbeddingManifestationError(
                code="NODE_VECTOR_STORE_WRITE_FAILED",
                message="The node vector store could not save graph node embeddings.",
                details={"reason": str(exc), "node_count": len(writes)},
            ) from exc

    def delete_node_points(self, point_ids: list[str]) -> None:
        """Delete explicit stale node vector points."""
        self._store.delete_node_points(point_ids)

    def delete_chunk_node_vectors(
        self,
        *,
        world_uuid: str,
        ingestion_run_id: str,
        book_number: int,
        chunk_number: int,
    ) -> None:
        """Delete all node vectors produced by one source chunk."""
        self._store.delete_node_vectors_for_chunk(
            world_uuid=world_uuid,
            ingestion_run_id=ingestion_run_id,
            book_number=book_number,
            chunk_number=chunk_number,
        )


class UnavailableGraphWriter:
    """Graph writer used when local Neo4j is not configured or reachable."""

    def __init__(self, *, code: str, message: str) -> None:
        self._code = code
        self._message = message

    def upsert_nodes(self, nodes) -> None:
        raise GraphStoreUnavailable(code=self._code, message=self._message, details={"operation": "node_write"})

    def upsert_edges(self, edges) -> None:
        raise GraphStoreUnavailable(code=self._code, message=self._message, details={"operation": "edge_write"})

    def delete_chunk(self, *, world_uuid: str, ingestion_run_id: str, book_number: int, chunk_number: int) -> None:
        raise GraphStoreUnavailable(code=self._code, message=self._message, details={"operation": "chunk_delete"})


def create_default_graph_writer(*, world_dir: Path):
    """Create a Neo4j graph writer or a pending-state writer when config is missing."""
    # BLOCK 1: Load local connection config first, then apply explicit environment overrides
    # WHY: run.bat generates ignored local credentials, while advanced users can point the backend at a different local Neo4j without changing tracked files
    config = load_neo4j_connection_config(world_dir=world_dir)
    if config is None:
        logger.warning(
            "Neo4j graph writer config is missing, so manifestation will stay pending until local credentials are provided."
        )
        return UnavailableGraphWriter(
            code="NEO4J_NOT_CONFIGURED",
            message="Neo4j is not configured, so graph manifestation was left pending.",
        )
    try:
        return Neo4jGraphWriter(uri=config.uri, username=config.username, password=config.password)
    except GraphStoreUnavailable as error:
        logger.warning(
            "Neo4j graph writer setup is unavailable during adapter creation: code=%s.",
            error.code,
        )
        return UnavailableGraphWriter(code=error.code, message=error.message)


def load_neo4j_connection_config(*, world_dir: Path) -> Neo4jConnectionConfig | None:
    """Load local Neo4j connection settings from ignored runtime state and environment overrides."""
    # BLOCK 1: Start with the generated local connection file under user/neo4j
    # WHY: The generated password must live outside tracked files, and Windows-created JSON may include a UTF-8 BOM that should not block local startup
    repo_root = _find_repo_root(world_dir)
    connection_path = repo_root / "user" / "neo4j" / "connection.json"
    payload: dict[str, object] = {}
    if connection_path.exists():
        payload = json.loads(connection_path.read_text(encoding="utf-8-sig"))

    # BLOCK 2: Apply explicit process environment overrides after local config
    # WHY: External/local Neo4j setups should be selectable without editing the generated connection file or committing credentials
    uri = os.environ.get("NEO4J_URI", str(payload.get("uri", ""))).strip()
    username = os.environ.get("NEO4J_USERNAME", str(payload.get("username", ""))).strip()
    password = os.environ.get("NEO4J_PASSWORD", str(payload.get("password", ""))).strip()
    missing_fields = [
        field_name
        for field_name, value in {"uri": uri, "username": username, "password": password}.items()
        if not value
    ]
    if missing_fields:
        logger.warning(
            "Neo4j graph writer config is incomplete: local_config_present=%s missing_fields=%s.",
            bool(payload),
            ",".join(missing_fields),
        )
        return None
    return Neo4jConnectionConfig(uri=uri, username=username, password=password)


def _embedding_work_item_from_node(node_item: NodeEmbeddingWorkItem) -> EmbeddingWorkItem:
    return EmbeddingWorkItem(
        book_number=node_item.book_number,
        chunk_number=node_item.chunk_number,
        point_id=node_item.point_id,
        chunk_text=node_item.embedding_text,
        text_hash=node_item.text_hash,
        source_filename=node_item.source_filename,
        chunk_path=Path(node_item.chunk_file),
        chunk_position=node_item.chunk_position,
    )


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _find_repo_root(path: Path) -> Path:
    # BLOCK 1: Walk upward until the app root marker is found
    # WHY: Worlds normally live under user/worlds, but tests and future storage roots can vary, so hardcoded parent counts would point at the wrong connection file
    for candidate in [path, *path.parents]:
        if (candidate / "run.bat").exists():
            return candidate
    return Path.cwd()
