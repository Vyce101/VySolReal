"""Graph manifestation orchestration for completed extraction manifests."""

from __future__ import annotations

from pathlib import Path

from backend.ingestion.graph_extraction.models import GraphExtractionManifest
from backend.ingestion.graph_extraction.storage import load_extraction_manifest
from backend.ingestion.text_sources.models import OperationEvent
from backend.logger import get_logger

from .errors import (
    GraphManifestationConfigurationError,
    GraphManifestationError,
    GraphStoreUnavailable,
    GraphStoreWriteError,
    NodeEmbeddingManifestationError,
)
from .models import (
    EDGE_FAILED,
    EDGE_FAILED_DEPENDENCY,
    EDGE_PENDING,
    EDGE_WAITING_DEPENDENCY,
    EDGE_WRITTEN,
    NEO4J_NODE_FAILED,
    NEO4J_NODE_PENDING,
    NEO4J_NODE_WRITTEN,
    NODE_EMBEDDING_EMBEDDED,
    NODE_EMBEDDING_FAILED,
    NODE_EMBEDDING_PENDING,
    GraphManifestationBookResult,
    GraphManifestationManifest,
    GraphNodeEmbedder,
    GraphNodeVectorStore,
    GraphWriter,
    NodeVectorWrite,
)
from .storage import (
    load_manifestation_manifest,
    manifestation_manifest_file_path,
    save_manifestation_manifest,
)

_MAX_RETRIES = 3

logger = get_logger(__name__)


def manifest_extracted_graph(
    *,
    extraction_manifest_path: Path,
    node_embedder: GraphNodeEmbedder,
    vector_store: GraphNodeVectorStore,
    graph_writer: GraphWriter,
) -> tuple[GraphManifestationBookResult, list[OperationEvent]]:
    """Manifest completed extracted graph candidates into node vectors and Neo4j."""
    # BLOCK 1: Load the completed extraction manifest before creating manifestation state
    # WHY: Manifestation must never guess graph candidates from chunks directly because extraction completion is the trust boundary for raw nodes and edges
    extraction_manifest = load_extraction_manifest(extraction_manifest_path)
    if extraction_manifest is None:
        raise GraphManifestationConfigurationError(
            code="GRAPH_EXTRACTION_MANIFEST_MISSING",
            message="Graph manifestation requires an existing graph extraction manifest.",
            details={"manifest_path": str(extraction_manifest_path)},
        )
    return manifest_extraction_manifest(
        extraction_manifest=extraction_manifest,
        book_dir=extraction_manifest_path.parent,
        node_embedder=node_embedder,
        vector_store=vector_store,
        graph_writer=graph_writer,
    )


def manifest_extraction_manifest(
    *,
    extraction_manifest: GraphExtractionManifest,
    book_dir: Path,
    node_embedder: GraphNodeEmbedder,
    vector_store: GraphNodeVectorStore,
    graph_writer: GraphWriter,
) -> tuple[GraphManifestationBookResult, list[OperationEvent]]:
    """Manifest an already loaded completed extraction manifest."""
    # BLOCK 1: Reject incomplete extraction manifests before any vector or graph writes start
    # WHY: Pending extraction chunks may still change final candidates, so manifestation must wait until extraction reports a completed book
    if extraction_manifest.status != "completed":
        raise GraphManifestationConfigurationError(
            code="GRAPH_EXTRACTION_NOT_COMPLETED",
            message="Graph manifestation can only run after graph extraction completed for the book.",
            details={
                "book_number": extraction_manifest.book_number,
                "source_filename": extraction_manifest.source_filename,
                "status": extraction_manifest.status,
            },
        )

    # BLOCK 2: Load or reconcile the per-book manifestation manifest against the completed extraction manifest
    # VARS: manifest_path = stable per-book graph_manifestation.json path, manifest = mutable resume state for vectors and graph writes
    # WHY: Node vectors and Neo4j writes can fail independently, so manifestation needs its own progress file separate from extraction output
    manifest_path = manifestation_manifest_file_path(book_dir)
    manifest, stale_point_ids, stale_chunk_numbers = _load_or_create_manifest(
        manifest_path=manifest_path,
        extraction_manifest=extraction_manifest,
    )
    warnings: list[OperationEvent] = []
    logger.info(
        "Graph manifestation started for world_uuid=%s run=%s book=%s total_nodes=%s total_edges=%s summary=%s stale_chunks=%s stale_points=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        manifest.total_nodes,
        manifest.total_edges,
        _manifest_summary_text(manifest),
        len(stale_chunk_numbers),
        len(stale_point_ids),
    )
    _delete_stale_chunk_outputs(
        manifest=manifest,
        stale_point_ids=stale_point_ids,
        stale_chunk_numbers=stale_chunk_numbers,
        vector_store=vector_store,
        graph_writer=graph_writer,
        warnings=warnings,
    )
    save_manifestation_manifest(manifest_path, manifest)

    # BLOCK 3: Run node embeddings, Neo4j node writes, then relationship writes in dependency order
    # WHY: A node is manifested only after both its vector and Neo4j record exist, and an edge is safe only after both endpoint nodes are manifested
    _manifest_node_embeddings(
        manifest=manifest,
        manifest_path=manifest_path,
        node_embedder=node_embedder,
        vector_store=vector_store,
    )
    if not _manifest_neo4j_nodes(
        manifest=manifest,
        manifest_path=manifest_path,
        graph_writer=graph_writer,
        warnings=warnings,
    ):
        result = _result_from_manifest(manifest, manifest_path)
        logger.info(
            "Graph manifestation finished for world_uuid=%s run=%s book=%s manifest_name=%s summary=%s warnings=%s.",
            manifest.world_uuid,
            manifest.ingestion_run_id,
            manifest.book_number,
            manifest_path.name,
            _manifest_result_summary_text(result),
            len(warnings),
        )
        return result, warnings
    _manifest_neo4j_edges(
        manifest=manifest,
        manifest_path=manifest_path,
        graph_writer=graph_writer,
        warnings=warnings,
    )
    result = _result_from_manifest(manifest, manifest_path)
    logger.info(
        "Graph manifestation finished for world_uuid=%s run=%s book=%s manifest_name=%s summary=%s warnings=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        manifest_path.name,
        _manifest_result_summary_text(result),
        len(warnings),
    )
    return result, warnings


def _load_or_create_manifest(
    *,
    manifest_path: Path,
    extraction_manifest: GraphExtractionManifest,
) -> tuple[GraphManifestationManifest, list[str], list[int]]:
    # BLOCK 1: Reuse manifestation state only when it belongs to the same extraction run and candidate set
    # WHY: A different ingestion run or changed extraction output invalidates pending vector and graph write state for this book
    fresh_manifest = GraphManifestationManifest.create_from_extraction(extraction_manifest)
    try:
        existing_manifest = load_manifestation_manifest(manifest_path)
    except GraphManifestationConfigurationError as error:
        if error.code != "GRAPH_MANIFESTATION_MANIFEST_CORRUPT":
            raise
        fresh_manifest.append_warning(
            _warning_payload(
                code="GRAPH_MANIFESTATION_MANIFEST_CORRUPT",
                message="The graph manifestation manifest was corrupt, so manifestation for this book was reset.",
                book_number=extraction_manifest.book_number,
                source_filename=extraction_manifest.source_filename,
            )
        )
        return fresh_manifest, [], _chunk_numbers_for_manifest(fresh_manifest)
    if existing_manifest is None:
        return fresh_manifest, [], []
    if (
        existing_manifest.world_uuid != fresh_manifest.world_uuid
        or existing_manifest.ingestion_run_id != fresh_manifest.ingestion_run_id
        or existing_manifest.book_number != fresh_manifest.book_number
    ):
        return fresh_manifest, [], _chunk_numbers_for_manifest(fresh_manifest)
    return _reconcile_manifest(existing_manifest=existing_manifest, fresh_manifest=fresh_manifest)


def _reconcile_manifest(
    *,
    existing_manifest: GraphManifestationManifest,
    fresh_manifest: GraphManifestationManifest,
) -> tuple[GraphManifestationManifest, list[str], list[int]]:
    # BLOCK 1: Preserve trusted state for unchanged node and edge ids while rebuilding stale candidates
    # VARS: existing_nodes = saved node states keyed by node id, existing_edges = saved edge states keyed by edge id, stale_chunk_number_set = source chunks whose raw candidates no longer match the saved manifestation chunk fingerprint
    # WHY: Stale cleanup deletes storage by whole chunk, so any surviving candidate inside a changed chunk must be reset instead of keeping a completed flag with no backing data
    existing_nodes = {state.node_id: state for state in existing_manifest.node_states}
    existing_edges = {state.edge_id: state for state in existing_manifest.edge_states}
    stale_chunk_numbers = _stale_chunk_numbers_for_reconciliation(
        existing_manifest=existing_manifest,
        fresh_manifest=fresh_manifest,
    )
    stale_chunk_number_set = set(stale_chunk_numbers)
    stale_point_ids = [
        state.point_id
        for state in existing_manifest.node_states
        if state.chunk_number in stale_chunk_number_set
    ]
    reconciled_nodes = []
    for fresh_node in fresh_manifest.node_states:
        if fresh_node.chunk_number in stale_chunk_number_set:
            reconciled_nodes.append(fresh_node)
            continue
        saved_node = existing_nodes.get(fresh_node.node_id)
        if saved_node is None or saved_node.text_hash != fresh_node.text_hash or saved_node.point_id != fresh_node.point_id:
            reconciled_nodes.append(fresh_node)
            continue
        saved_node.world_uuid = fresh_node.world_uuid
        saved_node.ingestion_run_id = fresh_node.ingestion_run_id
        saved_node.source_filename = fresh_node.source_filename
        saved_node.book_number = fresh_node.book_number
        saved_node.chunk_number = fresh_node.chunk_number
        saved_node.chunk_position = fresh_node.chunk_position
        saved_node.chunk_file = fresh_node.chunk_file
        saved_node.chunk_text_hash = fresh_node.chunk_text_hash
        saved_node.display_name = fresh_node.display_name
        saved_node.description = fresh_node.description
        reconciled_nodes.append(saved_node)

    reconciled_edges = []
    for fresh_edge in fresh_manifest.edge_states:
        if fresh_edge.chunk_number in stale_chunk_number_set:
            reconciled_edges.append(fresh_edge)
            continue
        saved_edge = existing_edges.get(fresh_edge.edge_id)
        if saved_edge is None:
            reconciled_edges.append(fresh_edge)
            continue
        saved_edge.world_uuid = fresh_edge.world_uuid
        saved_edge.ingestion_run_id = fresh_edge.ingestion_run_id
        saved_edge.source_filename = fresh_edge.source_filename
        saved_edge.book_number = fresh_edge.book_number
        saved_edge.chunk_number = fresh_edge.chunk_number
        saved_edge.chunk_position = fresh_edge.chunk_position
        saved_edge.chunk_file = fresh_edge.chunk_file
        saved_edge.chunk_text_hash = fresh_edge.chunk_text_hash
        saved_edge.source_display_name = fresh_edge.source_display_name
        saved_edge.target_display_name = fresh_edge.target_display_name
        saved_edge.description = fresh_edge.description
        saved_edge.strength = fresh_edge.strength
        reconciled_edges.append(saved_edge)

    existing_manifest.total_nodes = fresh_manifest.total_nodes
    existing_manifest.total_edges = fresh_manifest.total_edges
    existing_manifest.node_states = reconciled_nodes
    existing_manifest.edge_states = reconciled_edges
    return existing_manifest, stale_point_ids, stale_chunk_numbers


def _stale_chunk_numbers_for_reconciliation(
    *,
    existing_manifest: GraphManifestationManifest,
    fresh_manifest: GraphManifestationManifest,
) -> list[int]:
    # BLOCK 1: Compare chunk-level candidate fingerprints instead of individual saved statuses
    # VARS: existing_signatures = raw node and edge fingerprints grouped by chunk from the saved manifestation manifest, fresh_signatures = the same grouping rebuilt from the completed extraction manifest
    # WHY: Manifestation cleanup deletes vectors and graph rows by chunk, so reconciliation must reset every candidate in any chunk whose extracted payload changed
    existing_signatures = _chunk_candidate_signatures(existing_manifest)
    fresh_signatures = _chunk_candidate_signatures(fresh_manifest)
    return sorted(
        chunk_number
        for chunk_number in set(existing_signatures) | set(fresh_signatures)
        if existing_signatures.get(chunk_number) != fresh_signatures.get(chunk_number)
    )


def _chunk_candidate_signatures(
    manifest: GraphManifestationManifest,
) -> dict[int, tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...]]]:
    # BLOCK 1: Build one deterministic fingerprint per source chunk from its raw node and edge payloads
    # VARS: node_signatures_by_chunk = node fingerprints grouped by source chunk, edge_signatures_by_chunk = edge fingerprints grouped by source chunk
    # WHY: Order alone should never force a reset, but any chunk-level candidate or provenance change must trigger fresh writes after cleanup
    node_signatures_by_chunk: dict[int, list[tuple[str, ...]]] = {}
    edge_signatures_by_chunk: dict[int, list[tuple[str, ...]]] = {}
    for state in manifest.node_states:
        node_signatures_by_chunk.setdefault(state.chunk_number, []).append(_node_chunk_candidate_signature(state))
    for state in manifest.edge_states:
        edge_signatures_by_chunk.setdefault(state.chunk_number, []).append(_edge_chunk_candidate_signature(state))
    return {
        chunk_number: (
            tuple(sorted(node_signatures_by_chunk.get(chunk_number, []))),
            tuple(sorted(edge_signatures_by_chunk.get(chunk_number, []))),
        )
        for chunk_number in set(node_signatures_by_chunk) | set(edge_signatures_by_chunk)
    }


def _node_chunk_candidate_signature(state) -> tuple[str, ...]:
    # BLOCK 1: Capture the saved node fields that must match before one chunk can safely keep its completed writes
    # WHY: If any of these raw node details changed, the chunk cleanup path will delete the old backing data and the node must be rebuilt from pending state
    return (
        state.node_id,
        state.point_id,
        state.text_hash,
        state.chunk_position,
        state.chunk_file,
        state.chunk_text_hash,
        state.display_name,
        state.description,
    )


def _edge_chunk_candidate_signature(state) -> tuple[str, ...]:
    # BLOCK 1: Capture the saved edge fields that define one chunk's Neo4j relationship payload
    # WHY: Relationship ids alone are not enough because description, strength, or chunk provenance changes still require the deleted chunk data to be written again
    return (
        state.edge_id,
        state.source_node_id,
        state.target_node_id,
        state.chunk_position,
        state.chunk_file,
        state.chunk_text_hash,
        state.source_display_name,
        state.target_display_name,
        state.description,
        str(state.strength),
    )


def _delete_stale_chunk_outputs(
    *,
    manifest: GraphManifestationManifest,
    stale_point_ids: list[str],
    stale_chunk_numbers: list[int],
    vector_store: GraphNodeVectorStore,
    graph_writer: GraphWriter,
    warnings: list[OperationEvent],
) -> None:
    # BLOCK 1: Remove stale node vectors and graph records for chunks whose extracted candidates changed
    # WHY: Chunk redo must not leave old raw graph records behind when the fresh extraction manifest no longer contains those candidates
    if not stale_point_ids and not stale_chunk_numbers:
        return
    logger.info(
        "Cleaning stale graph manifestation outputs for world_uuid=%s run=%s book=%s stale_chunks=%s stale_points=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        len(stale_chunk_numbers),
        len(stale_point_ids),
    )
    if stale_point_ids:
        vector_store.delete_node_points(stale_point_ids)
    neo4j_cleanup_deferred = 0
    for chunk_number in stale_chunk_numbers:
        vector_store.delete_chunk_node_vectors(
            world_uuid=manifest.world_uuid,
            ingestion_run_id=manifest.ingestion_run_id,
            book_number=manifest.book_number,
            chunk_number=chunk_number,
        )
        try:
            graph_writer.delete_chunk(
                world_uuid=manifest.world_uuid,
                ingestion_run_id=manifest.ingestion_run_id,
                book_number=manifest.book_number,
                chunk_number=chunk_number,
            )
        except GraphStoreUnavailable as error:
            neo4j_cleanup_deferred += 1
            _append_warning_from_error(manifest=manifest, warnings=warnings, error=error)
    logger.info(
        "Finished stale graph manifestation cleanup for world_uuid=%s run=%s book=%s stale_chunks=%s stale_points=%s neo4j_deferred=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        len(stale_chunk_numbers),
        len(stale_point_ids),
        neo4j_cleanup_deferred,
    )


def _chunk_numbers_for_manifest(manifest: GraphManifestationManifest) -> list[int]:
    # BLOCK 1: Return the source chunks represented by one manifestation manifest
    # WHY: Resetting corrupt or incompatible manifestation state needs chunk-level cleanup without touching unrelated books or runs
    return sorted(
        {
            state.chunk_number
            for state in [*manifest.node_states, *manifest.edge_states]
        }
    )


def _manifest_node_embeddings(
    *,
    manifest: GraphManifestationManifest,
    manifest_path: Path,
    node_embedder: GraphNodeEmbedder,
    vector_store: GraphNodeVectorStore,
) -> None:
    # BLOCK 1: Give previously failed node embeddings a fresh retry budget on a new manifestation pass
    # WHY: Provider setup or quota issues can be fixed between runs, so a terminal node failure should not permanently block dependent edges
    for state in manifest.node_states:
        if state.node_embedding_status == NODE_EMBEDDING_FAILED:
            state.node_embedding_status = NODE_EMBEDDING_PENDING
            state.node_embedding_retry_count = 0

    # BLOCK 2: Embed only nodes that do not already have a confirmed vector point
    # WHY: Re-running manifestation should skip trusted vector writes and focus only on pending or retryable node states
    pending_nodes = [
        state
        for state in manifest.node_states
        if state.node_embedding_status != NODE_EMBEDDING_EMBEDDED
        and state.node_embedding_status != NODE_EMBEDDING_FAILED
    ]
    if not pending_nodes:
        return

    # BLOCK 3: Log the node-embedding batch boundary before handing work to the adapter layer
    # WHY: Manifestation needs one summary per provider-facing batch so resume behavior can be diagnosed without printing node text or provider payloads
    logger.info(
        "Node embedding batch started for world_uuid=%s run=%s book=%s batch_size=%s embedding_states=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        len(pending_nodes),
        _node_embedding_status_summary(manifest),
    )
    work_items = [state.to_embedding_work_item() for state in pending_nodes]
    batch_result = node_embedder.embed_nodes(work_items)
    missing_outcome_count = max(0, len(pending_nodes) - len(batch_result.vectors) - len(batch_result.failures))
    logger.info(
        "Node embedding batch returned for world_uuid=%s run=%s book=%s requested=%s vectors=%s failures=%s missing_outcomes=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        len(pending_nodes),
        len(batch_result.vectors),
        len(batch_result.failures),
        missing_outcome_count,
    )
    vector_writes = []
    for state in pending_nodes:
        failure = batch_result.failures.get(state.node_id)
        if failure is not None:
            _mark_node_embedding_failed(state=state, code=failure.code, message=failure.message)
            continue
        vector = batch_result.vectors.get(state.node_id)
        if vector is None:
            _mark_node_embedding_failed(
                state=state,
                code="NODE_EMBEDDING_MISSING",
                message="The node embedder did not return a vector or failure for this node.",
            )
            continue
        vector_writes.append(
            NodeVectorWrite(
                node_id=state.node_id,
                point_id=state.point_id,
                vector=vector,
                text_hash=state.text_hash,
                world_uuid=state.world_uuid,
                ingestion_run_id=state.ingestion_run_id,
                source_filename=state.source_filename,
                book_number=state.book_number,
                chunk_number=state.chunk_number,
                chunk_position=state.chunk_position,
                chunk_file=state.chunk_file,
                chunk_text_hash=state.chunk_text_hash,
                display_name=state.display_name,
                description=state.description,
            )
        )

    # BLOCK 4: Mark node embeddings complete only after the vector store accepts the batch
    # WHY: The manifestation manifest must never claim a node vector exists before the storage layer confirms the write
    if vector_writes:
        try:
            vector_store.upsert_node_embeddings(vector_writes)
        except NodeEmbeddingManifestationError:
            raise
        except GraphManifestationError:
            raise
        except Exception as exc:
            logger.error(
                "Node embedding vector write failed for world_uuid=%s run=%s book=%s node_count=%s.",
                manifest.world_uuid,
                manifest.ingestion_run_id,
                manifest.book_number,
                len(vector_writes),
            )
            raise NodeEmbeddingManifestationError(
                code="NODE_VECTOR_STORE_WRITE_FAILED",
                message="The node vector store could not save graph node embeddings.",
                details={"reason": str(exc), "node_count": len(vector_writes)},
            ) from exc
        written_node_ids = {write.node_id for write in vector_writes}
        for state in pending_nodes:
            if state.node_id in written_node_ids:
                state.node_embedding_status = NODE_EMBEDDING_EMBEDDED
                state.node_embedding_retry_count = 0
                state.node_embedding_last_error_code = None
                state.node_embedding_last_error_message = None
                if state.neo4j_node_status == NEO4J_NODE_FAILED:
                    state.neo4j_node_status = NEO4J_NODE_PENDING
    logger.info(
        "Node embedding batch finished for world_uuid=%s run=%s book=%s wrote_vectors=%s embedding_states=%s neo4j_node_states=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        len(vector_writes),
        _node_embedding_status_summary(manifest),
        _neo4j_node_status_summary(manifest),
    )
    save_manifestation_manifest(manifest_path, manifest)


def _manifest_neo4j_nodes(
    *,
    manifest: GraphManifestationManifest,
    manifest_path: Path,
    graph_writer: GraphWriter,
    warnings: list[OperationEvent],
) -> bool:
    # BLOCK 1: Give previously failed Neo4j node writes a fresh retry budget on a new manifestation pass
    # WHY: A local Neo4j outage or transient write problem should not permanently trap node vectors that are already safely stored
    for state in manifest.node_states:
        if state.neo4j_node_status == NEO4J_NODE_FAILED:
            state.neo4j_node_status = NEO4J_NODE_PENDING
            state.neo4j_node_retry_count = 0

    # BLOCK 2: Write Neo4j nodes only after their vector points are confirmed
    # WHY: A graph node is not manifested unless both storage systems can point to the same node state
    pending_nodes = [
        state
        for state in manifest.node_states
        if state.node_embedding_status == NODE_EMBEDDING_EMBEDDED
        and state.neo4j_node_status != NEO4J_NODE_WRITTEN
        and state.neo4j_node_status != NEO4J_NODE_FAILED
    ]
    if not pending_nodes:
        return True

    # BLOCK 3: Log the Neo4j node batch boundary before the adapter write starts
    # WHY: Node manifestation can pause or fail independently from embeddings, so the graph-store handoff needs its own resumable progress signal
    logger.info(
        "Neo4j node batch started for world_uuid=%s run=%s book=%s batch_size=%s node_states=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        len(pending_nodes),
        _neo4j_node_status_summary(manifest),
    )
    try:
        graph_writer.upsert_nodes([state.to_graph_write() for state in pending_nodes])
    except GraphStoreUnavailable as error:
        logger.warning(
            "Neo4j node batch left pending for world_uuid=%s run=%s book=%s node_count=%s code=%s.",
            manifest.world_uuid,
            manifest.ingestion_run_id,
            manifest.book_number,
            len(pending_nodes),
            error.code,
        )
        _append_warning_from_error(manifest=manifest, warnings=warnings, error=error)
        save_manifestation_manifest(manifest_path, manifest)
        return False
    except GraphStoreWriteError as error:
        for state in pending_nodes:
            _mark_neo4j_node_failed(state=state, code=error.code, message=error.message)
        logger.warning(
            "Neo4j node batch failed for world_uuid=%s run=%s book=%s node_count=%s retry_pending=%s terminal=%s code=%s.",
            manifest.world_uuid,
            manifest.ingestion_run_id,
            manifest.book_number,
            len(pending_nodes),
            sum(1 for state in pending_nodes if state.neo4j_node_status == NEO4J_NODE_PENDING),
            sum(1 for state in pending_nodes if state.neo4j_node_status == NEO4J_NODE_FAILED),
            error.code,
        )
        save_manifestation_manifest(manifest_path, manifest)
        return True
    for state in pending_nodes:
        state.neo4j_node_status = NEO4J_NODE_WRITTEN
        state.neo4j_node_retry_count = 0
        state.neo4j_node_last_error_code = None
        state.neo4j_node_last_error_message = None
    logger.info(
        "Neo4j node batch finished for world_uuid=%s run=%s book=%s wrote_nodes=%s node_states=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        len(pending_nodes),
        _neo4j_node_status_summary(manifest),
    )
    save_manifestation_manifest(manifest_path, manifest)
    return True


def _manifest_neo4j_edges(
    *,
    manifest: GraphManifestationManifest,
    manifest_path: Path,
    graph_writer: GraphWriter,
    warnings: list[OperationEvent],
) -> None:
    # BLOCK 1: Write only edges whose endpoint nodes are fully manifested
    # VARS: manifested_node_ids = extracted node ids with both vector and Neo4j node writes confirmed
    # WHY: Relationship writes without both endpoints would either fail or create misleading partial graph structure
    manifested_node_ids = {
        state.node_id
        for state in manifest.node_states
        if state.status == "manifested"
    }
    failed_node_ids = {
        state.node_id
        for state in manifest.node_states
        if state.status == "failed"
    }
    waiting_dependency_updates = 0
    failed_dependency_updates = 0
    dependency_released_updates = 0
    for state in manifest.edge_states:
        previous_status = state.status
        if previous_status == EDGE_WRITTEN:
            continue
        if state.source_node_id in failed_node_ids or state.target_node_id in failed_node_ids:
            state.status = EDGE_FAILED_DEPENDENCY
            state.last_error_code = "EDGE_ENDPOINT_FAILED"
            state.last_error_message = "One or both endpoint nodes failed to manifest."
            if previous_status != EDGE_FAILED_DEPENDENCY:
                failed_dependency_updates += 1
            continue
        if state.source_node_id not in manifested_node_ids or state.target_node_id not in manifested_node_ids:
            state.status = EDGE_WAITING_DEPENDENCY
            state.last_error_code = None
            state.last_error_message = None
            if previous_status != EDGE_WAITING_DEPENDENCY:
                waiting_dependency_updates += 1
            continue
        if previous_status in {EDGE_WAITING_DEPENDENCY, EDGE_FAILED_DEPENDENCY, EDGE_FAILED}:
            if previous_status == EDGE_FAILED:
                state.retry_count = 0
            state.status = EDGE_PENDING
            state.last_error_code = None
            state.last_error_message = None
            dependency_released_updates += 1
    if waiting_dependency_updates or failed_dependency_updates or dependency_released_updates:
        logger.info(
            "Edge dependency states updated for world_uuid=%s run=%s book=%s waiting=%s failed_dependency=%s released=%s manifested_nodes=%s failed_nodes=%s.",
            manifest.world_uuid,
            manifest.ingestion_run_id,
            manifest.book_number,
            waiting_dependency_updates,
            failed_dependency_updates,
            dependency_released_updates,
            len(manifested_node_ids),
            len(failed_node_ids),
        )
    pending_edges = [
        state
        for state in manifest.edge_states
        if state.status == EDGE_PENDING
        and state.source_node_id in manifested_node_ids
        and state.target_node_id in manifested_node_ids
    ]
    if not pending_edges:
        save_manifestation_manifest(manifest_path, manifest)
        return

    # BLOCK 2: Log the Neo4j edge batch boundary only for dependency-ready edges
    # WHY: Edge writes are the last manifestation stage, so operators need to see how many relationships actually became eligible this pass
    logger.info(
        "Neo4j edge batch started for world_uuid=%s run=%s book=%s batch_size=%s edge_states=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        len(pending_edges),
        _edge_status_summary(manifest),
    )
    try:
        graph_writer.upsert_edges([state.to_graph_write() for state in pending_edges])
    except GraphStoreUnavailable as error:
        logger.warning(
            "Neo4j edge batch left pending for world_uuid=%s run=%s book=%s edge_count=%s code=%s.",
            manifest.world_uuid,
            manifest.ingestion_run_id,
            manifest.book_number,
            len(pending_edges),
            error.code,
        )
        _append_warning_from_error(manifest=manifest, warnings=warnings, error=error)
        save_manifestation_manifest(manifest_path, manifest)
        return
    except GraphStoreWriteError as error:
        for state in pending_edges:
            _mark_neo4j_edge_failed(state=state, code=error.code, message=error.message)
        retryable_edges = sum(1 for state in pending_edges if state.status == EDGE_PENDING)
        terminal_edges = sum(1 for state in pending_edges if state.status == EDGE_FAILED)
        if retryable_edges:
            logger.warning(
                "Neo4j edge batch failed and will retry for world_uuid=%s run=%s book=%s retryable_edges=%s terminal_edges=%s code=%s.",
                manifest.world_uuid,
                manifest.ingestion_run_id,
                manifest.book_number,
                retryable_edges,
                terminal_edges,
                error.code,
            )
        if terminal_edges:
            logger.error(
                "Neo4j edge batch reached terminal failure for world_uuid=%s run=%s book=%s terminal_edges=%s code=%s.",
                manifest.world_uuid,
                manifest.ingestion_run_id,
                manifest.book_number,
                terminal_edges,
                error.code,
            )
        save_manifestation_manifest(manifest_path, manifest)
        return
    for state in pending_edges:
        state.status = EDGE_WRITTEN
        state.retry_count = 0
        state.last_error_code = None
        state.last_error_message = None
    logger.info(
        "Neo4j edge batch finished for world_uuid=%s run=%s book=%s wrote_edges=%s edge_states=%s.",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        len(pending_edges),
        _edge_status_summary(manifest),
    )
    save_manifestation_manifest(manifest_path, manifest)


def _mark_node_embedding_failed(*, state, code: str, message: str) -> None:
    # BLOCK 1: Store a failed node embedding attempt without touching Neo4j state
    # WHY: Neo4j node writes must wait for a vector point, so embedding failures should block only that node and its dependent edges
    state.node_embedding_retry_count += 1
    state.node_embedding_last_error_code = code
    state.node_embedding_last_error_message = message
    if state.node_embedding_retry_count >= _MAX_RETRIES:
        state.node_embedding_status = NODE_EMBEDDING_FAILED
    else:
        state.node_embedding_status = NODE_EMBEDDING_PENDING


def _mark_neo4j_node_failed(*, state, code: str, message: str) -> None:
    # BLOCK 1: Store a non-availability Neo4j node failure after vector embedding has already succeeded
    # WHY: The vector can remain trusted while the graph-store side gets retried or inspected separately
    state.neo4j_node_retry_count += 1
    state.neo4j_node_last_error_code = code
    state.neo4j_node_last_error_message = message
    if state.neo4j_node_retry_count >= _MAX_RETRIES:
        state.neo4j_node_status = NEO4J_NODE_FAILED
    else:
        state.neo4j_node_status = NEO4J_NODE_PENDING


def _mark_neo4j_edge_failed(*, state, code: str, message: str) -> None:
    # BLOCK 1: Keep retryable edge batch failures pending until the retry budget for this dependency-ready edge is exhausted
    # WHY: A failed edge batch should not permanently block dependency recovery or the next manifestation pass when the endpoint nodes are already manifested
    state.retry_count += 1
    state.last_error_code = code
    state.last_error_message = message
    if state.retry_count >= _MAX_RETRIES:
        state.status = EDGE_FAILED
    else:
        state.status = EDGE_PENDING


def _append_warning_from_error(
    *,
    manifest: GraphManifestationManifest,
    warnings: list[OperationEvent],
    error: GraphStoreUnavailable,
) -> None:
    # BLOCK 1: Convert Neo4j unavailability into a warning instead of throwing
    # WHY: The user can start Neo4j later and rerun manifestation, so pending state is safer than marking graph candidates failed
    warning = OperationEvent(
        code=error.code,
        message=error.message,
        severity="warning",
        book_number=manifest.book_number,
        source_filename=manifest.source_filename,
    )
    warnings.append(warning)
    manifest.append_warning(warning.to_dict())
    logger.warning(
        "Graph manifestation paused because Neo4j is unavailable: world_uuid=%s book=%s code=%s.",
        manifest.world_uuid,
        manifest.book_number,
        error.code,
    )


def _warning_payload(*, code: str, message: str, book_number: int, source_filename: str) -> dict[str, object]:
    return OperationEvent(
        code=code,
        message=message,
        severity="warning",
        book_number=book_number,
        source_filename=source_filename,
    ).to_dict()


def _manifest_summary_text(manifest: GraphManifestationManifest) -> str:
    # BLOCK 1: Convert the current manifestation state into one compact summary string
    # WHY: Start and finish logs should stay consistent across early returns without repeating formatting logic in each call site
    return (
        f"status={manifest.status} "
        f"manifested_nodes={manifest.manifested_nodes} failed_nodes={manifest.failed_nodes} pending_nodes={manifest.pending_nodes} "
        f"manifested_edges={manifest.manifested_edges} failed_edges={manifest.failed_edges} pending_edges={manifest.pending_edges}"
    )


def _manifest_result_summary_text(result: GraphManifestationBookResult) -> str:
    # BLOCK 1: Format the returned book result for finish logs without recalculating counters from the manifest
    # WHY: The finish boundary should log the exact result object the caller receives, especially on early exits after Neo4j pauses
    return (
        f"status={result.status} "
        f"manifested_nodes={result.manifested_nodes} failed_nodes={result.failed_nodes} pending_nodes={result.pending_nodes} "
        f"manifested_edges={result.manifested_edges} failed_edges={result.failed_edges} pending_edges={result.pending_edges}"
    )


def _node_embedding_status_summary(manifest: GraphManifestationManifest) -> str:
    # BLOCK 1: Summarize node embedding states without exposing raw node text
    # WHY: Provider-facing batch logs need high-level counts, while per-node logging would add noise and leak more content than necessary
    return (
        f"pending={sum(1 for state in manifest.node_states if state.node_embedding_status == NODE_EMBEDDING_PENDING)} "
        f"embedded={sum(1 for state in manifest.node_states if state.node_embedding_status == NODE_EMBEDDING_EMBEDDED)} "
        f"failed={sum(1 for state in manifest.node_states if state.node_embedding_status == NODE_EMBEDDING_FAILED)}"
    )


def _neo4j_node_status_summary(manifest: GraphManifestationManifest) -> str:
    # BLOCK 1: Summarize Neo4j node states at the graph-writer boundary
    # WHY: Node batch logs need to show what remains pending versus terminal without listing every node id
    return (
        f"pending={sum(1 for state in manifest.node_states if state.neo4j_node_status == NEO4J_NODE_PENDING)} "
        f"written={sum(1 for state in manifest.node_states if state.neo4j_node_status == NEO4J_NODE_WRITTEN)} "
        f"failed={sum(1 for state in manifest.node_states if state.neo4j_node_status == NEO4J_NODE_FAILED)}"
    )


def _edge_status_summary(manifest: GraphManifestationManifest) -> str:
    # BLOCK 1: Summarize edge dependency and write states in one log-safe string
    # WHY: Edge orchestration now has dependency waiting, dependency failure, retry, and success states that are easier to scan as counts
    return (
        f"pending={sum(1 for state in manifest.edge_states if state.status == EDGE_PENDING)} "
        f"waiting_dependency={sum(1 for state in manifest.edge_states if state.status == EDGE_WAITING_DEPENDENCY)} "
        f"failed_dependency={sum(1 for state in manifest.edge_states if state.status == EDGE_FAILED_DEPENDENCY)} "
        f"written={sum(1 for state in manifest.edge_states if state.status == EDGE_WRITTEN)} "
        f"failed={sum(1 for state in manifest.edge_states if state.status == EDGE_FAILED)}"
    )


def _result_from_manifest(manifest: GraphManifestationManifest, manifest_path: Path) -> GraphManifestationBookResult:
    return GraphManifestationBookResult(
        status=manifest.status,
        manifested_nodes=manifest.manifested_nodes,
        failed_nodes=manifest.failed_nodes,
        pending_nodes=manifest.pending_nodes,
        manifested_edges=manifest.manifested_edges,
        failed_edges=manifest.failed_edges,
        pending_edges=manifest.pending_edges,
        manifest_path=str(manifest_path),
    )
