"""Graph manifestation service tests with injected adapters."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.ingestion.graph_extraction.models import (
    GraphExtractionChunkState,
    GraphExtractionConfig,
    GraphExtractionManifest,
    RawExtractedEdge,
    RawExtractedNode,
)
from backend.ingestion.graph_extraction.storage import save_extraction_manifest
from backend.ingestion.graph_manifestation.errors import GraphStoreUnavailable, GraphStoreWriteError
from backend.ingestion.graph_manifestation.models import ManifestationFailure, NodeEmbeddingBatchResult, node_embedding_text
from backend.ingestion.graph_manifestation.service import manifest_extracted_graph


class _FakeNodeEmbedder:
    def __init__(self, *, failed_node_ids: set[str] | None = None) -> None:
        self.failed_node_ids = failed_node_ids if failed_node_ids is not None else set()
        self.calls = []

    def embed_nodes(self, work_items):
        # BLOCK 1: Return deterministic vectors for successful nodes and structured failures for selected nodes
        # WHY: Manifestation tests need to prove service behavior without live model calls or real credentials
        self.calls.append(list(work_items))
        vectors = {}
        failures = {}
        for item in work_items:
            if item.node_id in self.failed_node_ids:
                failures[item.node_id] = ManifestationFailure(
                    code="FAKE_NODE_EMBEDDING_FAILED",
                    message="The fake embedder was told to fail this node.",
                )
                continue
            vectors[item.node_id] = [float(len(item.display_name)), float(len(item.description))]
        return NodeEmbeddingBatchResult(vectors=vectors, failures=failures)


class _FakeNodeVectorStore:
    def __init__(self) -> None:
        self.writes = []
        self.deleted_point_ids = []
        self.deleted_chunk_requests = []

    def upsert_node_embeddings(self, writes):
        # BLOCK 1: Record node vector writes without touching Qdrant
        # WHY: The service should only mark node embeddings complete after this fake persistence boundary accepts the batch
        self.writes.extend(writes)

    def delete_node_points(self, point_ids):
        # BLOCK 1: Record explicit stale node point deletions without touching Qdrant
        # WHY: Reconciliation tests need to prove chunk cleanup asked the vector store to remove stale data before rewriting survivors
        self.deleted_point_ids.extend(point_ids)

    def delete_chunk_node_vectors(self, *, world_uuid, ingestion_run_id, book_number, chunk_number):
        # BLOCK 1: Record chunk-scoped vector deletions without touching Qdrant
        # WHY: Manifestation cleanup happens by source chunk, so tests need to see which chunk numbers were reset
        self.deleted_chunk_requests.append(
            {
                "world_uuid": world_uuid,
                "ingestion_run_id": ingestion_run_id,
                "book_number": book_number,
                "chunk_number": chunk_number,
            }
        )


class _FakeGraphWriter:
    def __init__(
        self,
        *,
        unavailable_on_nodes: bool = False,
        unavailable_on_edges: bool = False,
        edge_write_failures: int = 0,
    ) -> None:
        self.unavailable_on_nodes = unavailable_on_nodes
        self.unavailable_on_edges = unavailable_on_edges
        self.edge_write_failures = edge_write_failures
        self.nodes = []
        self.edges = []
        self.deleted_chunks = []

    def upsert_nodes(self, nodes):
        # BLOCK 1: Either record Neo4j node writes or simulate Neo4j being offline
        # WHY: The service must treat Neo4j unavailability as a warning and keep pending state resumable
        if self.unavailable_on_nodes:
            raise GraphStoreUnavailable(
                code="NEO4J_UNAVAILABLE",
                message="Neo4j is unavailable in this fake writer.",
                details={},
            )
        self.nodes.extend(nodes)

    def upsert_edges(self, edges):
        # BLOCK 1: Either record Neo4j edge writes or simulate Neo4j being offline
        # WHY: Relationship manifestation has its own availability boundary after endpoint nodes are confirmed
        if self.unavailable_on_edges:
            raise GraphStoreUnavailable(
                code="NEO4J_UNAVAILABLE",
                message="Neo4j is unavailable in this fake writer.",
                details={},
            )
        # BLOCK 2: Fail one requested edge batch when the test needs to prove retry behavior
        # WHY: Edge manifestation now needs coverage for resumable write errors that happen after both endpoint nodes were already written
        if self.edge_write_failures > 0:
            self.edge_write_failures -= 1
            raise GraphStoreWriteError(
                code="NEO4J_EDGE_BATCH_FAILED",
                message="The fake writer was told to fail this edge batch.",
                details={},
            )
        self.edges.extend(edges)

    def delete_chunk(self, *, world_uuid, ingestion_run_id, book_number, chunk_number):
        # BLOCK 1: Record chunk-scoped Neo4j deletions without touching a real graph store
        # WHY: Reconciliation tests need proof that stale chunk cleanup reached the graph boundary before survivors were rewritten
        self.deleted_chunks.append(
            {
                "world_uuid": world_uuid,
                "ingestion_run_id": ingestion_run_id,
                "book_number": book_number,
                "chunk_number": chunk_number,
            }
        )


class GraphManifestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.book_dir = self.temp_dir / "world" / "books" / "book_01"
        self.book_dir.mkdir(parents=True, exist_ok=True)
        self.extraction_manifest_path = self.book_dir / "graph_extraction.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_manifestation_writes_node_vectors_nodes_and_edges(self) -> None:
        self._write_extraction_manifest()
        vector_store = _FakeNodeVectorStore()
        graph_writer = _FakeGraphWriter()

        result, warnings = manifest_extracted_graph(
            extraction_manifest_path=self.extraction_manifest_path,
            node_embedder=_FakeNodeEmbedder(),
            vector_store=vector_store,
            graph_writer=graph_writer,
        )

        manifest = self._read_manifestation_manifest()

        self.assertEqual(warnings, [])
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(vector_store.writes), 2)
        self.assertEqual(len(graph_writer.nodes), 2)
        self.assertEqual(len(graph_writer.edges), 1)
        self.assertEqual(manifest["summary"]["status"], "completed")
        self.assertEqual({state["status"] for state in manifest["node_states"]}, {"manifested"})
        self.assertEqual(manifest["edge_states"][0]["status"], "written")

    def test_node_embedding_text_uses_exact_name_blank_line_description_format(self) -> None:
        self.assertEqual(
            node_embedding_text(display_name="Rudeus", description="A mage in the village."),
            "Rudeus\n\nA mage in the village.",
        )

    def test_neo4j_unavailable_returns_warning_and_leaves_graph_pending(self) -> None:
        self._write_extraction_manifest()
        vector_store = _FakeNodeVectorStore()

        result, warnings = manifest_extracted_graph(
            extraction_manifest_path=self.extraction_manifest_path,
            node_embedder=_FakeNodeEmbedder(),
            vector_store=vector_store,
            graph_writer=_FakeGraphWriter(unavailable_on_nodes=True),
        )

        manifest = self._read_manifestation_manifest()

        self.assertEqual(result.status, "partial")
        self.assertEqual(warnings[0].code, "NEO4J_UNAVAILABLE")
        self.assertEqual(len(vector_store.writes), 2)
        self.assertEqual({state["node_embedding_status"] for state in manifest["node_states"]}, {"embedded"})
        self.assertEqual({state["neo4j_node_status"] for state in manifest["node_states"]}, {"pending"})
        self.assertEqual(manifest["edge_states"][0]["status"], "pending")
        self.assertEqual(manifest["warnings"][0]["code"], "NEO4J_UNAVAILABLE")

    def test_edge_waits_when_one_endpoint_node_is_not_manifested(self) -> None:
        self._write_extraction_manifest()
        vector_store = _FakeNodeVectorStore()
        graph_writer = _FakeGraphWriter()

        result, warnings = manifest_extracted_graph(
            extraction_manifest_path=self.extraction_manifest_path,
            node_embedder=_FakeNodeEmbedder(failed_node_ids={"node-sylphie"}),
            vector_store=vector_store,
            graph_writer=graph_writer,
        )

        manifest = self._read_manifestation_manifest()
        node_statuses = {state["node_id"]: state for state in manifest["node_states"]}

        self.assertEqual(warnings, [])
        self.assertEqual(result.status, "partial")
        self.assertEqual(len(vector_store.writes), 1)
        self.assertEqual(len(graph_writer.nodes), 1)
        self.assertEqual(graph_writer.nodes[0].node_id, "node-rudeus")
        self.assertEqual(graph_writer.edges, [])
        self.assertEqual(node_statuses["node-sylphie"]["node_embedding_status"], "pending")
        self.assertEqual(node_statuses["node-sylphie"]["node_embedding_retry_count"], 1)
        self.assertEqual(manifest["edge_states"][0]["status"], "waiting_dependency")

    def test_stale_chunk_replacement_rewrites_surviving_candidates_after_cleanup(self) -> None:
        self._write_extraction_manifest()

        first_result, first_warnings = manifest_extracted_graph(
            extraction_manifest_path=self.extraction_manifest_path,
            node_embedder=_FakeNodeEmbedder(),
            vector_store=_FakeNodeVectorStore(),
            graph_writer=_FakeGraphWriter(),
        )

        self._write_extraction_manifest(
            nodes=[
                RawExtractedNode(
                    node_id="node-rudeus",
                    display_name="Rudeus",
                    description="A mage in the village.",
                )
            ],
            edges=[],
        )
        rebuild_embedder = _FakeNodeEmbedder()
        rebuild_vector_store = _FakeNodeVectorStore()
        rebuild_graph_writer = _FakeGraphWriter()

        second_result, second_warnings = manifest_extracted_graph(
            extraction_manifest_path=self.extraction_manifest_path,
            node_embedder=rebuild_embedder,
            vector_store=rebuild_vector_store,
            graph_writer=rebuild_graph_writer,
        )

        manifest = self._read_manifestation_manifest()

        self.assertEqual(first_result.status, "completed")
        self.assertEqual(first_warnings, [])
        self.assertEqual(second_result.status, "completed")
        self.assertEqual(second_warnings, [])
        self.assertEqual(len(rebuild_embedder.calls), 1)
        self.assertEqual([item.node_id for item in rebuild_embedder.calls[0]], ["node-rudeus"])
        self.assertEqual(
            [entry["chunk_number"] for entry in rebuild_vector_store.deleted_chunk_requests],
            [1],
        )
        self.assertEqual(
            [entry["chunk_number"] for entry in rebuild_graph_writer.deleted_chunks],
            [1],
        )
        self.assertEqual([write.node_id for write in rebuild_vector_store.writes], ["node-rudeus"])
        self.assertEqual([node.node_id for node in rebuild_graph_writer.nodes], ["node-rudeus"])
        self.assertEqual(rebuild_graph_writer.edges, [])
        self.assertEqual(manifest["summary"]["status"], "completed")
        self.assertEqual(manifest["summary"]["manifested_nodes"], 1)
        self.assertEqual(manifest["summary"]["manifested_edges"], 0)

    def test_edge_batch_write_failure_stays_retryable_and_recovers_next_run(self) -> None:
        self._write_extraction_manifest()
        first_graph_writer = _FakeGraphWriter(edge_write_failures=1)

        first_result, first_warnings = manifest_extracted_graph(
            extraction_manifest_path=self.extraction_manifest_path,
            node_embedder=_FakeNodeEmbedder(),
            vector_store=_FakeNodeVectorStore(),
            graph_writer=first_graph_writer,
        )

        first_manifest = self._read_manifestation_manifest()

        second_vector_store = _FakeNodeVectorStore()
        second_graph_writer = _FakeGraphWriter()
        second_result, second_warnings = manifest_extracted_graph(
            extraction_manifest_path=self.extraction_manifest_path,
            node_embedder=_FakeNodeEmbedder(),
            vector_store=second_vector_store,
            graph_writer=second_graph_writer,
        )

        second_manifest = self._read_manifestation_manifest()

        self.assertEqual(first_result.status, "partial")
        self.assertEqual(first_warnings, [])
        self.assertEqual(first_manifest["edge_states"][0]["status"], "pending")
        self.assertEqual(first_manifest["edge_states"][0]["retry_count"], 1)
        self.assertEqual(first_manifest["edge_states"][0]["last_error_code"], "NEO4J_EDGE_BATCH_FAILED")
        self.assertEqual([node.node_id for node in first_graph_writer.nodes], ["node-rudeus", "node-sylphie"])
        self.assertEqual(first_graph_writer.edges, [])
        self.assertEqual(second_result.status, "completed")
        self.assertEqual(second_warnings, [])
        self.assertEqual(second_vector_store.writes, [])
        self.assertEqual(second_graph_writer.nodes, [])
        self.assertEqual([edge.edge_id for edge in second_graph_writer.edges], ["edge-rudeus-sylphie"])
        self.assertEqual(second_manifest["edge_states"][0]["status"], "written")
        self.assertEqual(second_manifest["edge_states"][0]["retry_count"], 0)
        self.assertNotIn("last_error_code", second_manifest["edge_states"][0])

    def _write_extraction_manifest(
        self,
        *,
        nodes: list[RawExtractedNode] | None = None,
        edges: list[RawExtractedEdge] | None = None,
    ) -> None:
        # BLOCK 1: Write the smallest completed graph extraction manifest needed by manifestation
        # WHY: The manifestation service should consume completed extraction output without depending on provider/parser tests
        # BLOCK 2: Fall back to the default two-node one-edge fixture unless a test needs a specific extraction shape
        # WHY: Most manifestation tests share the same baseline graph, and optional overrides keep the stale-chunk test focused on only the candidate change it cares about
        if nodes is None:
            nodes = [
                RawExtractedNode(
                    node_id="node-rudeus",
                    display_name="Rudeus",
                    description="A mage in the village.",
                ),
                RawExtractedNode(
                    node_id="node-sylphie",
                    display_name="Sylphie",
                    description="A friend Rudeus met.",
                ),
            ]
        if edges is None:
            edges = [
                RawExtractedEdge(
                    edge_id="edge-rudeus-sylphie",
                    source_node_id="node-rudeus",
                    target_node_id="node-sylphie",
                    source_display_name="Rudeus",
                    target_display_name="Sylphie",
                    description="Rudeus met Sylphie in the village.",
                    strength=7,
                )
            ]
        manifest = GraphExtractionManifest(
            world_id="Test World",
            world_uuid="c603f3be-9b82-4d37-9a46-c9b634d38757",
            ingestion_run_id="run-1",
            source_filename="book.txt",
            book_number=1,
            total_chunks=1,
            config=GraphExtractionConfig(
                provider_id="google",
                model_id="google/gemma-4-31b-it",
            ),
            chunk_states=[
                GraphExtractionChunkState(
                    chunk_number=1,
                    chunk_file="chunks/book_01_chunk_0001.json",
                    status="extracted",
                    text_hash="chunk-hash",
                    nodes=nodes,
                    edges=edges,
                )
            ],
        )
        save_extraction_manifest(self.extraction_manifest_path, manifest)

    def _read_manifestation_manifest(self) -> dict[str, object]:
        return json.loads((self.book_dir / "graph_manifestation.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
