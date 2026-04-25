"""Models for graph manifestation manifests and injected runtime adapters."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Protocol
from uuid import UUID, uuid5

from backend.graph_extraction.models import GraphExtractionManifest

NODE_EMBEDDING_PENDING = "pending"
NODE_EMBEDDING_EMBEDDED = "embedded"
NODE_EMBEDDING_FAILED = "failed"
NEO4J_NODE_PENDING = "pending"
NEO4J_NODE_WRITTEN = "written"
NEO4J_NODE_FAILED = "failed"
EDGE_PENDING = "pending"
EDGE_WAITING_DEPENDENCY = "waiting_dependency"
EDGE_FAILED_DEPENDENCY = "failed_dependency"
EDGE_WRITTEN = "written"
EDGE_FAILED = "failed"


@dataclass(slots=True)
class ManifestationFailure:
    """One recoverable manifestation failure reported by an injected adapter."""

    code: str
    message: str
    retryable: bool = True


@dataclass(slots=True)
class NodeEmbeddingWorkItem:
    """One graph node that needs an embedding vector."""

    node_id: str
    point_id: str
    embedding_text: str
    text_hash: str
    world_uuid: str
    ingestion_run_id: str
    source_filename: str
    book_number: int
    chunk_number: int
    chunk_position: str
    chunk_file: str
    chunk_text_hash: str
    display_name: str
    description: str


@dataclass(slots=True)
class NodeEmbeddingBatchResult:
    """Batch node embedding outcome keyed by node id."""

    vectors: dict[str, list[float]] = field(default_factory=dict)
    failures: dict[str, ManifestationFailure] = field(default_factory=dict)


@dataclass(slots=True)
class NodeVectorWrite:
    """One node vector ready to persist in the vector store."""

    node_id: str
    point_id: str
    vector: list[float]
    text_hash: str
    world_uuid: str
    ingestion_run_id: str
    source_filename: str
    book_number: int
    chunk_number: int
    chunk_position: str
    chunk_file: str
    chunk_text_hash: str
    display_name: str
    description: str


@dataclass(slots=True)
class GraphNodeWrite:
    """One graph node ready to persist in Neo4j."""

    node_id: str
    point_id: str
    text_hash: str
    world_uuid: str
    ingestion_run_id: str
    source_filename: str
    book_number: int
    chunk_number: int
    chunk_position: str
    chunk_file: str
    chunk_text_hash: str
    display_name: str
    description: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class GraphEdgeWrite:
    """One graph relationship ready to persist in Neo4j."""

    edge_id: str
    source_node_id: str
    target_node_id: str
    world_uuid: str
    ingestion_run_id: str
    source_filename: str
    book_number: int
    chunk_number: int
    chunk_position: str
    chunk_file: str
    chunk_text_hash: str
    source_display_name: str
    target_display_name: str
    description: str
    strength: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class GraphNodeEmbedder(Protocol):
    """Runtime boundary for producing node embeddings."""

    def embed_nodes(self, work_items: list[NodeEmbeddingWorkItem]) -> NodeEmbeddingBatchResult:
        """Return vectors and per-node failures for the requested nodes."""


class GraphNodeVectorStore(Protocol):
    """Runtime boundary for storing node vectors."""

    def upsert_node_embeddings(self, writes: list[NodeVectorWrite]) -> None:
        """Persist node vectors after the embedder has produced them."""

    def delete_node_points(self, point_ids: list[str]) -> None:
        """Delete explicit stale node vector points."""

    def delete_chunk_node_vectors(
        self,
        *,
        world_uuid: str,
        ingestion_run_id: str,
        book_number: int,
        chunk_number: int,
    ) -> None:
        """Delete all node vectors produced by one source chunk."""


class GraphWriter(Protocol):
    """Runtime boundary for writing nodes and edges to Neo4j."""

    def upsert_nodes(self, nodes: list[GraphNodeWrite]) -> None:
        """Persist graph nodes."""

    def upsert_edges(self, edges: list[GraphEdgeWrite]) -> None:
        """Persist graph relationships."""

    def delete_chunk(
        self,
        *,
        world_uuid: str,
        ingestion_run_id: str,
        book_number: int,
        chunk_number: int,
    ) -> None:
        """Delete graph records produced by one source chunk."""


@dataclass(slots=True)
class GraphManifestationNodeState:
    """Manifestation progress for one extracted node."""

    node_id: str
    point_id: str
    text_hash: str
    node_embedding_status: str = NODE_EMBEDDING_PENDING
    neo4j_node_status: str = NEO4J_NODE_PENDING
    node_embedding_retry_count: int = 0
    neo4j_node_retry_count: int = 0
    node_embedding_last_error_code: str | None = None
    node_embedding_last_error_message: str | None = None
    neo4j_node_last_error_code: str | None = None
    neo4j_node_last_error_message: str | None = None
    world_uuid: str = ""
    ingestion_run_id: str = ""
    source_filename: str = ""
    book_number: int = 0
    chunk_number: int = 0
    chunk_position: str = ""
    chunk_file: str = ""
    chunk_text_hash: str = ""
    display_name: str = ""
    description: str = ""

    @property
    def status(self) -> str:
        # BLOCK 1: Collapse the two required node writes into one user-facing node status
        # WHY: A graph node is not manifested until both the vector point and Neo4j node exist, so callers need a single conservative summary
        if self.node_embedding_status == NODE_EMBEDDING_EMBEDDED and self.neo4j_node_status == NEO4J_NODE_WRITTEN:
            return "manifested"
        if self.node_embedding_status == NODE_EMBEDDING_FAILED or self.neo4j_node_status == NEO4J_NODE_FAILED:
            return "failed"
        return "pending"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = self.status
        return {key: value for key, value in payload.items() if value is not None}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "GraphManifestationNodeState":
        return cls(
            node_id=str(payload["node_id"]),
            point_id=str(payload["point_id"]),
            text_hash=str(payload["text_hash"]),
            node_embedding_status=str(payload.get("node_embedding_status", NODE_EMBEDDING_PENDING)),
            neo4j_node_status=str(payload.get("neo4j_node_status", NEO4J_NODE_PENDING)),
            node_embedding_retry_count=int(payload.get("node_embedding_retry_count", 0)),
            neo4j_node_retry_count=int(payload.get("neo4j_node_retry_count", 0)),
            node_embedding_last_error_code=str(payload["node_embedding_last_error_code"]) if payload.get("node_embedding_last_error_code") is not None else None,
            node_embedding_last_error_message=str(payload["node_embedding_last_error_message"]) if payload.get("node_embedding_last_error_message") is not None else None,
            neo4j_node_last_error_code=str(payload["neo4j_node_last_error_code"]) if payload.get("neo4j_node_last_error_code") is not None else None,
            neo4j_node_last_error_message=str(payload["neo4j_node_last_error_message"]) if payload.get("neo4j_node_last_error_message") is not None else None,
            world_uuid=str(payload.get("world_uuid", "")),
            ingestion_run_id=str(payload.get("ingestion_run_id", "")),
            source_filename=str(payload.get("source_filename", "")),
            book_number=int(payload.get("book_number", 0)),
            chunk_number=int(payload.get("chunk_number", 0)),
            chunk_position=str(payload.get("chunk_position", "")),
            chunk_file=str(payload.get("chunk_file", "")),
            chunk_text_hash=str(payload.get("chunk_text_hash", "")),
            display_name=str(payload.get("display_name", "")),
            description=str(payload.get("description", "")),
        )

    def to_embedding_work_item(self) -> NodeEmbeddingWorkItem:
        # BLOCK 1: Build the embedder payload from the saved node state instead of the extraction manifest
        # WHY: Resume should use the manifestation manifest as the immediate source of truth after reconciliation
        return NodeEmbeddingWorkItem(
            node_id=self.node_id,
            point_id=self.point_id,
            embedding_text=node_embedding_text(display_name=self.display_name, description=self.description),
            text_hash=self.text_hash,
            world_uuid=self.world_uuid,
            ingestion_run_id=self.ingestion_run_id,
            source_filename=self.source_filename,
            book_number=self.book_number,
            chunk_number=self.chunk_number,
            chunk_position=self.chunk_position,
            chunk_file=self.chunk_file,
            chunk_text_hash=self.chunk_text_hash,
            display_name=self.display_name,
            description=self.description,
        )

    def to_graph_write(self) -> GraphNodeWrite:
        # BLOCK 1: Build the Neo4j node payload only from already-saved manifestation state
        # WHY: The Neo4j write must match the vector point metadata that was marked embedded in this manifest
        return GraphNodeWrite(
            node_id=self.node_id,
            point_id=self.point_id,
            text_hash=self.text_hash,
            world_uuid=self.world_uuid,
            ingestion_run_id=self.ingestion_run_id,
            source_filename=self.source_filename,
            book_number=self.book_number,
            chunk_number=self.chunk_number,
            chunk_position=self.chunk_position,
            chunk_file=self.chunk_file,
            chunk_text_hash=self.chunk_text_hash,
            display_name=self.display_name,
            description=self.description,
        )


@dataclass(slots=True)
class GraphManifestationEdgeState:
    """Manifestation progress for one extracted edge."""

    edge_id: str
    source_node_id: str
    target_node_id: str
    status: str = EDGE_PENDING
    retry_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    world_uuid: str = ""
    ingestion_run_id: str = ""
    source_filename: str = ""
    book_number: int = 0
    chunk_number: int = 0
    chunk_position: str = ""
    chunk_file: str = ""
    chunk_text_hash: str = ""
    source_display_name: str = ""
    target_display_name: str = ""
    description: str = ""
    strength: int = 1

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "GraphManifestationEdgeState":
        return cls(
            edge_id=str(payload["edge_id"]),
            source_node_id=str(payload["source_node_id"]),
            target_node_id=str(payload["target_node_id"]),
            status=str(payload.get("status", EDGE_PENDING)),
            retry_count=int(payload.get("retry_count", 0)),
            last_error_code=str(payload["last_error_code"]) if payload.get("last_error_code") is not None else None,
            last_error_message=str(payload["last_error_message"]) if payload.get("last_error_message") is not None else None,
            world_uuid=str(payload.get("world_uuid", "")),
            ingestion_run_id=str(payload.get("ingestion_run_id", "")),
            source_filename=str(payload.get("source_filename", "")),
            book_number=int(payload.get("book_number", 0)),
            chunk_number=int(payload.get("chunk_number", 0)),
            chunk_position=str(payload.get("chunk_position", "")),
            chunk_file=str(payload.get("chunk_file", "")),
            chunk_text_hash=str(payload.get("chunk_text_hash", "")),
            source_display_name=str(payload.get("source_display_name", "")),
            target_display_name=str(payload.get("target_display_name", "")),
            description=str(payload.get("description", "")),
            strength=int(payload.get("strength", 1)),
        )

    def to_graph_write(self) -> GraphEdgeWrite:
        # BLOCK 1: Build the Neo4j relationship payload from the saved edge state
        # WHY: Relationship writes must remain resumable even when the extraction manifest is no longer held in memory
        return GraphEdgeWrite(
            edge_id=self.edge_id,
            source_node_id=self.source_node_id,
            target_node_id=self.target_node_id,
            world_uuid=self.world_uuid,
            ingestion_run_id=self.ingestion_run_id,
            source_filename=self.source_filename,
            book_number=self.book_number,
            chunk_number=self.chunk_number,
            chunk_position=self.chunk_position,
            chunk_file=self.chunk_file,
            chunk_text_hash=self.chunk_text_hash,
            source_display_name=self.source_display_name,
            target_display_name=self.target_display_name,
            description=self.description,
            strength=self.strength,
        )


@dataclass(slots=True)
class GraphManifestationManifest:
    """Per-book graph manifestation progress metadata."""

    world_id: str
    world_uuid: str
    ingestion_run_id: str
    source_filename: str
    book_number: int
    total_nodes: int
    total_edges: int
    node_states: list[GraphManifestationNodeState]
    edge_states: list[GraphManifestationEdgeState]
    warnings: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "world_id": self.world_id,
            "world_uuid": self.world_uuid,
            "ingestion_run_id": self.ingestion_run_id,
            "source_filename": self.source_filename,
            "book_number": self.book_number,
            "total_nodes": self.total_nodes,
            "total_edges": self.total_edges,
            "node_states": [state.to_dict() for state in self.node_states],
            "edge_states": [state.to_dict() for state in self.edge_states],
            "warnings": list(self.warnings),
            "summary": self.summary,
        }

    @classmethod
    def create_from_extraction(cls, extraction_manifest: GraphExtractionManifest) -> "GraphManifestationManifest":
        # BLOCK 1: Create manifestation states only from completed extraction chunks
        # WHY: Pending or failed extraction chunks do not have trustworthy final node and edge candidates yet
        node_states: list[GraphManifestationNodeState] = []
        edge_states: list[GraphManifestationEdgeState] = []
        for chunk_state in extraction_manifest.chunk_states:
            if chunk_state.status != "extracted":
                continue
            for node in chunk_state.nodes:
                text_hash = node_text_hash(display_name=node.display_name, description=node.description)
                node_states.append(
                    GraphManifestationNodeState(
                        node_id=node.node_id,
                        point_id=node_point_id(
                            world_uuid=extraction_manifest.world_uuid,
                            ingestion_run_id=extraction_manifest.ingestion_run_id,
                            book_number=extraction_manifest.book_number,
                            node_id=node.node_id,
                        ),
                        text_hash=text_hash,
                        world_uuid=extraction_manifest.world_uuid,
                        ingestion_run_id=extraction_manifest.ingestion_run_id,
                        source_filename=extraction_manifest.source_filename,
                        book_number=extraction_manifest.book_number,
                        chunk_number=chunk_state.chunk_number,
                        chunk_position=f"{chunk_state.chunk_number}/{extraction_manifest.total_chunks}",
                        chunk_file=chunk_state.chunk_file,
                        chunk_text_hash=chunk_state.text_hash or "",
                        display_name=node.display_name,
                        description=node.description,
                    )
                )
            for edge in chunk_state.edges:
                edge_states.append(
                    GraphManifestationEdgeState(
                        edge_id=edge.edge_id,
                        source_node_id=edge.source_node_id,
                        target_node_id=edge.target_node_id,
                        world_uuid=extraction_manifest.world_uuid,
                        ingestion_run_id=extraction_manifest.ingestion_run_id,
                        source_filename=extraction_manifest.source_filename,
                        book_number=extraction_manifest.book_number,
                        chunk_number=chunk_state.chunk_number,
                        chunk_position=f"{chunk_state.chunk_number}/{extraction_manifest.total_chunks}",
                        chunk_file=chunk_state.chunk_file,
                        chunk_text_hash=chunk_state.text_hash or "",
                        source_display_name=edge.source_display_name,
                        target_display_name=edge.target_display_name,
                        description=edge.description,
                        strength=edge.strength,
                    )
                )
        return cls(
            world_id=extraction_manifest.world_id,
            world_uuid=extraction_manifest.world_uuid,
            ingestion_run_id=extraction_manifest.ingestion_run_id,
            source_filename=extraction_manifest.source_filename,
            book_number=extraction_manifest.book_number,
            total_nodes=len(node_states),
            total_edges=len(edge_states),
            node_states=node_states,
            edge_states=edge_states,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "GraphManifestationManifest":
        return cls(
            world_id=str(payload["world_id"]),
            world_uuid=str(payload["world_uuid"]),
            ingestion_run_id=str(payload["ingestion_run_id"]),
            source_filename=str(payload["source_filename"]),
            book_number=int(payload["book_number"]),
            total_nodes=int(payload["total_nodes"]),
            total_edges=int(payload["total_edges"]),
            node_states=[
                GraphManifestationNodeState.from_dict(dict(state_payload))
                for state_payload in payload.get("node_states", [])
            ],
            edge_states=[
                GraphManifestationEdgeState.from_dict(dict(state_payload))
                for state_payload in payload.get("edge_states", [])
            ],
            warnings=list(payload.get("warnings", [])),
        )

    @property
    def manifested_nodes(self) -> int:
        return sum(1 for state in self.node_states if state.status == "manifested")

    @property
    def failed_nodes(self) -> int:
        return sum(1 for state in self.node_states if state.status == "failed")

    @property
    def pending_nodes(self) -> int:
        return self.total_nodes - self.manifested_nodes - self.failed_nodes

    @property
    def manifested_edges(self) -> int:
        return sum(1 for state in self.edge_states if state.status == EDGE_WRITTEN)

    @property
    def failed_edges(self) -> int:
        return sum(1 for state in self.edge_states if state.status in {EDGE_FAILED, EDGE_FAILED_DEPENDENCY})

    @property
    def pending_edges(self) -> int:
        return self.total_edges - self.manifested_edges - self.failed_edges

    @property
    def status(self) -> str:
        # BLOCK 1: Collapse node and edge progress into one book-level manifestation status
        # WHY: The caller needs a conservative result that remains partial until every required vector, node, and relationship write is confirmed
        if self.manifested_nodes == self.total_nodes and self.manifested_edges == self.total_edges:
            return "completed"
        if (self.failed_nodes + self.failed_edges) > 0 and self.pending_nodes == 0 and self.pending_edges == 0:
            return "failed"
        return "partial"

    @property
    def summary(self) -> dict[str, object]:
        return {
            "status": self.status,
            "manifested_nodes": self.manifested_nodes,
            "failed_nodes": self.failed_nodes,
            "pending_nodes": self.pending_nodes,
            "manifested_edges": self.manifested_edges,
            "failed_edges": self.failed_edges,
            "pending_edges": self.pending_edges,
        }

    def append_warning(self, warning_payload: dict[str, object]) -> None:
        # BLOCK 1: Keep warning payloads unique across repeated resumable attempts
        # WHY: Neo4j may remain unavailable across runs, and repeated identical warnings would make the manifest noisy without adding new information
        if warning_payload not in self.warnings:
            self.warnings.append(warning_payload)


@dataclass(slots=True)
class GraphManifestationBookResult:
    """Book-level manifestation result summary."""

    status: str
    manifested_nodes: int
    failed_nodes: int
    pending_nodes: int
    manifested_edges: int
    failed_edges: int
    pending_edges: int
    manifest_path: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def node_embedding_text(*, display_name: str, description: str) -> str:
    """Return the text used to embed an extracted graph node."""
    # BLOCK 1: Embed both the display name and description as one stable node text
    # WHY: The name alone is too sparse for retrieval, while description-only vectors would lose the canonical entity label
    return f"{display_name}\n\n{description}"


def node_text_hash(*, display_name: str, description: str) -> str:
    """Return the stable hash for one node embedding text."""
    return hashlib.sha256(
        node_embedding_text(display_name=display_name, description=description).encode("utf-8")
    ).hexdigest()


def node_point_id(*, world_uuid: str, ingestion_run_id: str, book_number: int, node_id: str) -> str:
    """Return a stable vector point id for one manifested graph node."""
    # BLOCK 1: Derive node vector ids from stable run and candidate identity
    # WHY: Resume needs repeated manifestation attempts to overwrite the same node vector instead of creating duplicates
    return str(uuid5(UUID(world_uuid), f"graph-node:{ingestion_run_id}:book:{book_number}:node:{node_id}"))
