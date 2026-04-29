"""Neo4j graph writer adapter for graph manifestation."""

from __future__ import annotations

from typing import Any

from backend.logger import get_logger

from .errors import GraphStoreUnavailable, GraphStoreWriteError
from .models import GraphEdgeWrite, GraphNodeWrite

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, TransientError
except ImportError:
    GraphDatabase = None
    ServiceUnavailable = None
    TransientError = None

logger = get_logger(__name__)


class Neo4jGraphWriter:
    """Neo4j-backed graph writer using the official neo4j driver when installed."""

    def __init__(self, *, uri: str, username: str, password: str, database: str | None = None) -> None:
        # BLOCK 1: Open the official Neo4j driver only when the optional package is installed
        # WHY: Graph manifestation tests and local non-Neo4j setups must be able to import this module without adding a hard dependency
        if GraphDatabase is None:
            raise GraphStoreUnavailable(
                code="NEO4J_DRIVER_UNAVAILABLE",
                message="The official neo4j driver is not installed, so graph writes were left pending.",
                details={},
            )
        self._driver = GraphDatabase.driver(uri, auth=(username, password))
        self._database = database
        try:
            with self._driver.session(database=self._database) as session:
                session.execute_write(_ensure_schema_tx)
        except Exception as exc:
            _raise_graph_store_error(exc, operation="schema_setup", item_count=0)

    def close(self) -> None:
        self._driver.close()

    def upsert_nodes(self, nodes: list[GraphNodeWrite]) -> None:
        """Persist graph nodes in Neo4j."""
        if not nodes:
            return
        payloads = [node.to_dict() for node in nodes]
        try:
            logger.info("Writing %s graph node(s) to Neo4j.", len(payloads))
            with self._driver.session(database=self._database) as session:
                session.execute_write(_write_nodes_tx, payloads)
        except Exception as exc:
            _raise_graph_store_error(exc, operation="node_write", item_count=len(payloads))

    def upsert_edges(self, edges: list[GraphEdgeWrite]) -> None:
        """Persist graph relationships in Neo4j."""
        if not edges:
            return
        payloads = [edge.to_dict() for edge in edges]
        try:
            logger.info("Writing %s graph edge(s) to Neo4j.", len(payloads))
            with self._driver.session(database=self._database) as session:
                session.execute_write(_write_edges_tx, payloads)
        except Exception as exc:
            _raise_graph_store_error(exc, operation="edge_write", item_count=len(payloads))

    def delete_chunk(
        self,
        *,
        world_uuid: str,
        ingestion_run_id: str,
        book_number: int,
        chunk_number: int,
    ) -> None:
        """Delete graph records produced by one source chunk."""
        try:
            logger.info(
                "Deleting Neo4j graph records for world_uuid=%s run=%s book=%s chunk=%s.",
                world_uuid,
                ingestion_run_id,
                book_number,
                chunk_number,
            )
            with self._driver.session(database=self._database) as session:
                session.execute_write(
                    _delete_chunk_tx,
                    world_uuid,
                    ingestion_run_id,
                    book_number,
                    chunk_number,
                )
        except Exception as exc:
            _raise_graph_store_error(exc, operation="chunk_delete", item_count=0)


def _write_nodes_tx(tx: Any, nodes: list[dict[str, object]]) -> None:
    # BLOCK 1: Upsert nodes by stable candidate id and update source metadata in one batch
    # WHY: Repeated manifestation runs must be idempotent and refresh metadata without creating duplicate graph nodes
    tx.run(
        """
        UNWIND $nodes AS node
        MERGE (n:ExtractedNode {node_id: node.node_id})
        SET n.point_id = node.point_id,
            n.text_hash = node.text_hash,
            n.world_uuid = node.world_uuid,
            n.ingestion_run_id = node.ingestion_run_id,
            n.source_filename = node.source_filename,
            n.book_number = node.book_number,
            n.chunk_number = node.chunk_number,
            n.chunk_position = node.chunk_position,
            n.chunk_file = node.chunk_file,
            n.chunk_text_hash = node.chunk_text_hash,
            n.display_name = node.display_name,
            n.description = node.description
        """,
        nodes=nodes,
    )


def _write_edges_tx(tx: Any, edges: list[dict[str, object]]) -> None:
    # BLOCK 1: Upsert relationships only between nodes that already exist in Neo4j
    # WHY: The service writes edges after endpoint nodes manifest, and these matches keep Neo4j from creating incomplete endpoint placeholders
    tx.run(
        """
        UNWIND $edges AS edge
        MATCH (source:ExtractedNode {node_id: edge.source_node_id, world_uuid: edge.world_uuid})
        MATCH (target:ExtractedNode {node_id: edge.target_node_id, world_uuid: edge.world_uuid})
        MERGE (source)-[r:EXTRACTED_RELATION {edge_id: edge.edge_id}]->(target)
        SET r.world_uuid = edge.world_uuid,
            r.ingestion_run_id = edge.ingestion_run_id,
            r.source_filename = edge.source_filename,
            r.book_number = edge.book_number,
            r.chunk_number = edge.chunk_number,
            r.chunk_position = edge.chunk_position,
            r.chunk_file = edge.chunk_file,
            r.chunk_text_hash = edge.chunk_text_hash,
            r.source_display_name = edge.source_display_name,
            r.target_display_name = edge.target_display_name,
            r.description = edge.description,
            r.strength = edge.strength
        """,
        edges=edges,
    )


def _ensure_schema_tx(tx: Any) -> None:
    # BLOCK 1: Create idempotent Neo4j schema support for extracted graph writes
    # WHY: Stable node ids are the merge key for resume, and an edge-id index keeps relationship overwrite checks efficient without relying on Enterprise-only relationship constraints
    tx.run(
        "CREATE CONSTRAINT extracted_node_node_id IF NOT EXISTS FOR (n:ExtractedNode) REQUIRE n.node_id IS UNIQUE"
    )
    tx.run(
        "CREATE INDEX extracted_relation_edge_id IF NOT EXISTS FOR ()-[r:EXTRACTED_RELATION]-() ON (r.edge_id)"
    )


def _delete_chunk_tx(tx: Any, world_uuid: str, ingestion_run_id: str, book_number: int, chunk_number: int) -> None:
    # BLOCK 1: Delete only graph records produced by one exact source chunk.
    # WHY: Chunk redo must replace stale raw graph data without touching other books, runs, worlds, or chunks.
    tx.run(
        """
        MATCH (n:ExtractedNode)
        WHERE n.world_uuid = $world_uuid
          AND n.ingestion_run_id = $ingestion_run_id
          AND n.book_number = $book_number
          AND n.chunk_number = $chunk_number
        DETACH DELETE n
        """,
        world_uuid=world_uuid,
        ingestion_run_id=ingestion_run_id,
        book_number=book_number,
        chunk_number=chunk_number,
    )


def _raise_graph_store_error(exc: Exception, *, operation: str, item_count: int) -> None:
    # BLOCK 1: Classify Neo4j connection-style failures as resumable unavailability
    # WHY: A missing or sleeping Neo4j service should leave manifestation pending instead of turning extracted graph data into hard failures
    unavailable_types = tuple(
        error_type
        for error_type in (ServiceUnavailable, TransientError, OSError, ConnectionError)
        if error_type is not None
    )
    if isinstance(exc, unavailable_types):
        raise GraphStoreUnavailable(
            code="NEO4J_UNAVAILABLE",
            message="Neo4j is unavailable, so graph manifestation was left pending.",
            details={"operation": operation, "item_count": item_count, "reason": str(exc)},
        ) from exc
    raise GraphStoreWriteError(
        code="NEO4J_WRITE_FAILED",
        message="Neo4j rejected a graph manifestation write.",
        details={"operation": operation, "item_count": item_count, "reason": str(exc)},
    ) from exc
