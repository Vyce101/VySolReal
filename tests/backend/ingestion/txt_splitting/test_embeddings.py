"""Embedding integration tests for TXT splitting ingestion."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid5

from backend.embeddings import create_embedding_profile
from backend.embeddings.catalog import get_supported_embedding_model
from backend.embeddings.models import EmbeddingFailure, EmbeddingProfile, EmbeddingRunCancellation, EmbeddingSuccess
from backend.embeddings.qdrant_store import QdrantChunkStore
from backend.ingestion.graph_extraction.models import ExtractionProviderSuccess, GraphExtractionConfig
from backend.ingestion.graph_manifestation.errors import GraphStoreUnavailable
from backend.ingestion.text_sources.service import ingest_sources, ingest_sources_into_existing_world, reingest_world_from_stored_sources
from backend.ingestion.text_sources.storage import book_directory


class _SuccessfulProvider:
    call_count = 0

    def embed_text(self, *, credential, profile, work_item):
        # BLOCK 1: Return a deterministic vector for each chunk without reaching the real provider so embedding persistence can be tested locally
        # WHY: The ingestion tests need stable, offline provider behavior or they would become flaky and require real API keys just to verify manifest and Qdrant logic
        type(self).call_count += 1
        vector_value = float(work_item.chunk_number)
        return EmbeddingSuccess(
            work_item=work_item,
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            vector=[vector_value] * profile.dimensions,
            billable_character_count=len(work_item.chunk_text),
        )


class _SlowSuccessfulProvider:
    call_count = 0

    def embed_text(self, *, credential, profile, work_item):
        # BLOCK 1: Sleep long enough for the test cancellation handle to flip before the fake provider returns a success payload
        # WHY: Cancellation behavior only matters when a response arrives after the run was canceled, so the provider has to simulate that exact timing edge case
        type(self).call_count += 1
        time.sleep(0.2)
        return EmbeddingSuccess(
            work_item=work_item,
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            vector=[1.0] * profile.dimensions,
            billable_character_count=len(work_item.chunk_text),
        )


class _RateLimitThenSuccessfulProvider:
    call_count = 0

    def embed_text(self, *, credential, profile, work_item):
        # BLOCK 1: Simulate one provider quota failure before returning a normal embedding
        # WHY: Rate-limit failover should exercise the scheduler path without depending on real provider quotas during tests
        type(self).call_count += 1
        if type(self).call_count == 1:
            return EmbeddingFailure(
                work_item=work_item,
                credential_name=credential.display_name,
                quota_scope=credential.quota_scope,
                code="EMBEDDING_PROVIDER_RATE_LIMITED",
                message="REQUESTS_PER_MINUTE exhausted",
                retryable=True,
                rate_limit_type="rpm",
                rate_limit_scope="model",
                retry_after_seconds=60,
                billable_token_estimate=1,
            )
        return EmbeddingSuccess(
            work_item=work_item,
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            vector=[1.0] * profile.dimensions,
            billable_character_count=len(work_item.chunk_text),
        )


class _QueuedGraphProvider:
    responses: list[str] = []
    lock = threading.Lock()

    def extract(self, *, credential, config, prompt, log_context):
        # BLOCK 1: Return deterministic graph extraction text for ingestion integration tests
        # WHY: Sweep 1 wiring needs to prove ingestion calls graph extraction without using live model credentials or storing raw provider responses
        with type(self).lock:
            response_text = type(self).responses.pop(0)
        return ExtractionProviderSuccess(
            response_text=response_text,
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
        )


class _SuccessfulGraphWriter:
    nodes = []
    edges = []

    def upsert_nodes(self, nodes):
        # BLOCK 1: Record graph nodes without connecting to a live Neo4j process
        # WHY: Ingestion tests need to prove the manifestation stage completes without depending on local database startup
        type(self).nodes.extend(nodes)

    def upsert_edges(self, edges):
        # BLOCK 1: Record graph relationships without connecting to a live Neo4j process
        # WHY: Relationship manifestation is tested through the backend contract while keeping this integration test offline
        type(self).edges.extend(edges)


class _UnavailableGraphWriter:
    def upsert_nodes(self, nodes):
        # BLOCK 1: Simulate local Neo4j being unavailable during node writes
        # WHY: Ingestion should keep the run resumable instead of failing the whole book when graph persistence is offline
        raise GraphStoreUnavailable(
            code="NEO4J_UNAVAILABLE",
            message="Neo4j is unavailable in this fake writer.",
            details={},
        )

    def upsert_edges(self, edges):
        # BLOCK 1: Simulate local Neo4j being unavailable during relationship writes
        # WHY: The fake should fail both graph write boundaries consistently if the service reaches either one
        raise GraphStoreUnavailable(
            code="NEO4J_UNAVAILABLE",
            message="Neo4j is unavailable in this fake writer.",
            details={},
        )


class EmbeddingIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.worlds_root = self.temp_dir / "user" / "worlds"
        self.keys_root = self.temp_dir / "user" / "keys"
        self.vector_root = self.temp_dir / "user" / "vector_store"
        self.sources_dir = self.temp_dir / "fixtures"
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self._write_google_key()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_creates_world_metadata_and_qdrant_vectors(self) -> None:
        source_path = self._write_source("book.txt", "Alpha beta gamma delta epsilon zeta.")

        with self._provider(_SuccessfulProvider):
            result = ingest_sources(
                world_name="Embedding World",
                source_files=[source_path],
                chunk_size=12,
                max_lookback=5,
                overlap_size=2,
                worlds_root=self.worlds_root,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertTrue(result.success)
        self.assertIsNotNone(result.world_uuid)
        self.assertEqual(result.books[0].embedding.status, "completed")

        world_metadata_path = self.worlds_root / "Embedding World" / "world.json"
        world_metadata = json.loads(world_metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(world_metadata["world_uuid"], result.world_uuid)
        self.assertEqual(world_metadata["embedding_profile"]["task_type"], "RETRIEVAL_DOCUMENT")
        self.assertEqual(world_metadata["embedding_profile"]["dimensions"], 3072)
        self.assertEqual(world_metadata["embedding_profile"]["extra_settings"]["max_input_tokens"], 8192)

        embedding_manifest = json.loads(Path(result.books[0].embedding.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(embedding_manifest["ingestion_run_id"], world_metadata["active_ingestion_run_id"])
        self.assertEqual(embedding_manifest["chunk_states"][0]["status"], "embedded")

        point_id = self._point_id(result.world_uuid, 1, 1)
        store = QdrantChunkStore(store_root=self.vector_root)
        try:
            store.ensure_collection(self._embedding_profile())
            record = store.retrieve_existing_points([point_id])[point_id]
        finally:
            store.close()
        self.assertNotIn("chunk_text", record.payload)
        self.assertEqual(record.payload["world_uuid"], result.world_uuid)
        self.assertEqual(record.payload["ingestion_run_id"], embedding_manifest["ingestion_run_id"])
        self.assertIn("text_hash", record.payload)

    def test_rebuilds_missing_embedding_manifest_from_qdrant_without_reembedding(self) -> None:
        source_path = self._write_source("resume.txt", "Alpha beta gamma delta epsilon zeta.")
        world_dir = self.worlds_root / "Resume Embedding World"
        world_dir.mkdir(parents=True, exist_ok=True)
        self._run_with_provider(
            provider_class=_SuccessfulProvider,
            world_name="Resume Embedding World",
            world_dir=world_dir,
            source_path=source_path,
        )

        book_dir = book_directory(world_dir, 1)
        embedding_manifest_path = book_dir / "embeddings.json"
        embedding_manifest_path.unlink()

        with self._provider(_SuccessfulProvider):
            resumed = ingest_sources_into_existing_world(
                world_name="Resume Embedding World",
                source_files=[source_path],
                config=self._config(),
                world_dir=world_dir,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertTrue(resumed.success)
        self.assertEqual(_SuccessfulProvider.call_count, 0)
        self.assertEqual(resumed.books[0].embedding.status, "completed")

    def test_redoes_embedding_when_qdrant_point_is_missing(self) -> None:
        source_path = self._write_source("redo.txt", "Alpha beta gamma delta epsilon zeta.")
        world_dir = self.worlds_root / "Redo Embedding World"
        world_dir.mkdir(parents=True, exist_ok=True)
        first_run = self._run_with_provider(
            provider_class=_SuccessfulProvider,
            world_name="Redo Embedding World",
            world_dir=world_dir,
            source_path=source_path,
        )

        point_id = self._point_id(first_run.world_uuid, 1, 1)
        store = QdrantChunkStore(store_root=self.vector_root)
        try:
            store.ensure_collection(self._embedding_profile())
            store.delete_points([point_id])
        finally:
            store.close()

        with self._provider(_SuccessfulProvider):
            resumed = ingest_sources_into_existing_world(
                world_name="Redo Embedding World",
                source_files=[source_path],
                config=self._config(),
                world_dir=world_dir,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertTrue(resumed.success)
        self.assertGreater(_SuccessfulProvider.call_count, 0)
        self.assertEqual(resumed.books[0].embedding.status, "completed")

    def test_existing_world_append_uses_next_book_slot_and_same_run_id(self) -> None:
        first_source = self._write_source("append-one.txt", "Rudeus met Sylphie in the village.")
        second_source = self._write_source("append-two.txt", "Paul returned to the village.")
        self._write_google_key(
            allowed_models=[
                "google/gemini-embedding-2-preview",
                "google/gemma-4-31b-it",
            ],
        )
        _QueuedGraphProvider.responses = [
            """{"nodes": [{"display_name": "Rudeus", "description": "A boy in the village."}], "edges": []}
---COMPLETE---""",
            """{"nodes": [{"display_name": "Sylphie", "description": "A person Rudeus met."}], "edges": [{"source_display_name": "Rudeus", "target_display_name": "Sylphie", "description": "Rudeus met Sylphie in the village.", "strength": 7}]}
---COMPLETE---""",
        ]

        with self._provider(_SuccessfulProvider), self._graph_provider(_QueuedGraphProvider), self._graph_writer(_UnavailableGraphWriter):
            first_result = ingest_sources(
                world_name="Append Existing World",
                source_files=[first_source],
                chunk_size=100,
                max_lookback=5,
                overlap_size=2,
                worlds_root=self.worlds_root,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
                graph_extraction_config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=1,
                    extraction_concurrency=5,
                ),
            )

        world_dir = self.worlds_root / "Append Existing World"
        first_world_metadata = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
        first_run_id = first_world_metadata["active_ingestion_run_id"]
        self.assertTrue(first_result.success)
        self.assertEqual(first_world_metadata["active_ingestion_run_status"], "paused")

        _QueuedGraphProvider.responses = [
            """{"nodes": [{"display_name": "Paul", "description": "A man who returned."}], "edges": []}
---COMPLETE---""",
            """{"nodes": [{"display_name": "Village", "description": "The place Paul returned to."}], "edges": [{"source_display_name": "Paul", "target_display_name": "Village", "description": "Paul returned to the village.", "strength": 6}]}
---COMPLETE---""",
        ]

        with self._provider(_SuccessfulProvider), self._graph_provider(_QueuedGraphProvider), self._graph_writer(_UnavailableGraphWriter):
            second_result = ingest_sources_into_existing_world(
                world_name="Append Existing World",
                source_files=[second_source],
                config=self._config_with(chunk_size=100, max_lookback=5, overlap_size=2),
                world_dir=world_dir,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
                graph_extraction_config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=1,
                    extraction_concurrency=1,
                ),
            )

        updated_world_metadata = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
        saved_graph_config = json.loads((world_dir / "graph_config.json").read_text(encoding="utf-8"))
        self.assertTrue(second_result.success)
        self.assertEqual(second_result.books[0].book_number, 2)
        self.assertEqual(updated_world_metadata["active_ingestion_run_id"], first_run_id)
        self.assertEqual(updated_world_metadata["active_ingestion_run_status"], "paused")
        self.assertEqual(saved_graph_config["extraction_concurrency"], 1)
        self.assertTrue((world_dir / "source files" / "book_02" / second_source.name).exists())

    def test_existing_world_changed_splitter_requires_full_reingest(self) -> None:
        source_path = self._write_source("splitter-lock.txt", "Alpha beta gamma delta epsilon zeta.")
        world_dir = self.worlds_root / "Locked Splitter World"
        world_dir.mkdir(parents=True, exist_ok=True)
        self._run_with_provider(
            provider_class=_SuccessfulProvider,
            world_name="Locked Splitter World",
            world_dir=world_dir,
            source_path=source_path,
        )

        with self._provider(_SuccessfulProvider):
            result = ingest_sources_into_existing_world(
                world_name="Locked Splitter World",
                source_files=[source_path],
                config=self._config_with(chunk_size=24),
                world_dir=world_dir,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.errors[0].code, "WORLD_REINGEST_REQUIRED")

    def test_existing_world_changed_embedding_profile_requires_full_reingest(self) -> None:
        source_path = self._write_source("profile-lock.txt", "Alpha beta gamma delta epsilon zeta.")
        world_dir = self.worlds_root / "Locked Profile World"
        world_dir.mkdir(parents=True, exist_ok=True)
        self._run_with_provider(
            provider_class=_SuccessfulProvider,
            world_name="Locked Profile World",
            world_dir=world_dir,
            source_path=source_path,
        )

        different_profile = EmbeddingProfile(
            provider_id="google",
            model_id="google/another-embedding-model",
            dimensions=3072,
            task_type="RETRIEVAL_DOCUMENT",
            profile_version=1,
        )

        with self._provider(_SuccessfulProvider):
            result = ingest_sources_into_existing_world(
                world_name="Locked Profile World",
                source_files=[source_path],
                config=self._config(),
                world_dir=world_dir,
                embedding_profile=different_profile,
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertFalse(result.success)
        self.assertEqual(result.errors[0].code, "WORLD_REINGEST_REQUIRED")

    def test_ignores_inflight_embedding_results_after_cancellation(self) -> None:
        source_path = self._write_source("cancel.txt", "Alpha beta gamma delta epsilon zeta.")
        cancellation = EmbeddingRunCancellation()

        cancel_thread = threading.Thread(target=self._cancel_after_delay, args=(cancellation,), daemon=True)
        cancel_thread.start()
        try:
            with self._provider(_SlowSuccessfulProvider):
                result = ingest_sources(
                    world_name="Cancelled World",
                    source_files=[source_path],
                    chunk_size=12,
                    max_lookback=5,
                    overlap_size=2,
                    worlds_root=self.worlds_root,
                    embedding_profile=self._embedding_profile(),
                    provider_keys_root=self.keys_root,
                    vector_store_root=self.vector_root,
                    cancellation=cancellation,
                )
        finally:
            cancel_thread.join(timeout=1)

        self.assertTrue(result.success)
        self.assertEqual(result.books[0].embedding.status, "partial")

        manifest_payload = json.loads(Path(result.books[0].embedding.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest_payload["chunk_states"][0]["status"], "pending")

        point_id = self._point_id(result.world_uuid, 1, 1)
        store = QdrantChunkStore(store_root=self.vector_root)
        try:
            store.ensure_collection(self._embedding_profile())
            self.assertEqual(store.retrieve_existing_points([point_id]), {})
        finally:
            store.close()

    def test_embedding_rate_limit_fails_over_without_spending_chunk_retry(self) -> None:
        source_path = self._write_source("rate-limit.txt", "Alpha beta gamma delta epsilon zeta.")
        self._write_google_key(filename="secondary.json", name="Secondary Google Project", project_id="project-two")

        with self._provider(_RateLimitThenSuccessfulProvider):
            result = ingest_sources(
                world_name="Rate Limit Failover World",
                source_files=[source_path],
                chunk_size=100,
                max_lookback=5,
                overlap_size=2,
                worlds_root=self.worlds_root,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
                embedding_concurrency=1,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.books[0].embedding.status, "completed")

        manifest_payload = json.loads(Path(result.books[0].embedding.manifest_path).read_text(encoding="utf-8"))
        chunk_state = manifest_payload["chunk_states"][0]
        self.assertEqual(chunk_state["status"], "embedded")
        self.assertEqual(chunk_state["retry_count"], 0)

    def test_ingestion_runs_graph_extraction_after_embedding(self) -> None:
        source_path = self._write_source("graph.txt", "Rudeus met Sylphie in the village.")
        self._write_google_key(
            allowed_models=[
                "google/gemini-embedding-2-preview",
                "google/gemma-4-31b-it",
            ],
        )
        _QueuedGraphProvider.responses = [
            """{"nodes": [{"display_name": "Rudeus", "description": "A boy in the village."}], "edges": []}
---COMPLETE---""",
            """{"nodes": [{"display_name": "Sylphie", "description": "A person Rudeus met."}], "edges": [{"source_display_name": "Rudeus", "target_display_name": "Sylphie", "description": "Rudeus met Sylphie in the village.", "strength": 7}]}
---COMPLETE---""",
        ]

        with self._provider(_SuccessfulProvider), self._graph_provider(_QueuedGraphProvider), self._graph_writer(_SuccessfulGraphWriter):
            result = ingest_sources(
                world_name="Graph Extraction World",
                source_files=[source_path],
                chunk_size=100,
                max_lookback=5,
                overlap_size=2,
                worlds_root=self.worlds_root,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
                graph_extraction_config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=1,
                    extraction_concurrency=5,
                ),
            )

        self.assertTrue(result.success)
        self.assertEqual(result.books[0].embedding.status, "completed")
        self.assertEqual(result.books[0].graph_extraction.status, "completed")
        self.assertEqual(result.books[0].graph_manifestation.status, "completed")
        self.assertEqual(len(_SuccessfulGraphWriter.nodes), 2)
        self.assertEqual(len(_SuccessfulGraphWriter.edges), 1)

        world_metadata_path = self.worlds_root / "Graph Extraction World" / "world.json"
        world_metadata = json.loads(world_metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(world_metadata["active_ingestion_run_status"], "completed")

        graph_manifest = json.loads(
            (self.worlds_root / "Graph Extraction World" / "books" / "book_01" / "graph_extraction.json").read_text(encoding="utf-8")
        )
        self.assertEqual(graph_manifest["ingestion_run_id"], world_metadata["active_ingestion_run_id"])
        self.assertEqual(graph_manifest["chunk_states"][0]["status"], "extracted")
        self.assertEqual(len(graph_manifest["chunk_states"][0]["edges"]), 1)

    def test_missing_neo4j_leaves_manifestation_pending_and_run_active(self) -> None:
        source_path = self._write_source("graph-pending.txt", "Rudeus met Sylphie in the village.")
        self._write_google_key(
            allowed_models=[
                "google/gemini-embedding-2-preview",
                "google/gemma-4-31b-it",
            ],
        )
        _QueuedGraphProvider.responses = [
            """{"nodes": [{"display_name": "Rudeus", "description": "A boy in the village."}], "edges": []}
---COMPLETE---""",
            """{"nodes": [{"display_name": "Sylphie", "description": "A person Rudeus met."}], "edges": [{"source_display_name": "Rudeus", "target_display_name": "Sylphie", "description": "Rudeus met Sylphie in the village.", "strength": 7}]}
---COMPLETE---""",
        ]

        with self._provider(_SuccessfulProvider), self._graph_provider(_QueuedGraphProvider), self._graph_writer(_UnavailableGraphWriter):
            result = ingest_sources(
                world_name="Graph Manifestation Pending World",
                source_files=[source_path],
                chunk_size=100,
                max_lookback=5,
                overlap_size=2,
                worlds_root=self.worlds_root,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
                graph_extraction_config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=1,
                    extraction_concurrency=5,
                ),
            )

        self.assertTrue(result.success)
        self.assertEqual(result.books[0].graph_manifestation.status, "partial")
        self.assertEqual(result.warnings[0].code, "NEO4J_UNAVAILABLE")

        world_metadata = json.loads((self.worlds_root / "Graph Manifestation Pending World" / "world.json").read_text(encoding="utf-8"))
        self.assertEqual(world_metadata["active_ingestion_run_status"], "paused")

    def test_graph_config_edit_is_rejected_while_world_is_marked_active(self) -> None:
        source_path = self._write_source("graph-config-active.txt", "Alpha beta gamma delta epsilon zeta.")
        world_dir = self.worlds_root / "Graph Config Active World"
        world_dir.mkdir(parents=True, exist_ok=True)
        self._run_with_provider(
            provider_class=_SuccessfulProvider,
            world_name="Graph Config Active World",
            world_dir=world_dir,
            source_path=source_path,
        )

        world_metadata_path = world_dir / "world.json"
        world_metadata = json.loads(world_metadata_path.read_text(encoding="utf-8"))
        world_metadata["active_ingestion_run_status"] = "active"
        world_metadata_path.write_text(json.dumps(world_metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        with self._provider(_SuccessfulProvider):
            result = ingest_sources_into_existing_world(
                world_name="Graph Config Active World",
                source_files=[source_path],
                config=self._config(),
                world_dir=world_dir,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
                graph_extraction_config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=1,
                    extraction_concurrency=1,
                ),
            )

        self.assertFalse(result.success)
        self.assertEqual(result.errors[0].code, "GRAPH_CONFIG_RUN_ACTIVE")

    def test_full_world_reingest_rebuilds_all_books_from_stored_sources(self) -> None:
        first_source = self._write_source("reingest-one.txt", "Alpha beta gamma delta epsilon zeta.")
        second_source = self._write_source("reingest-two.txt", "One two three four five six seven.")

        with self._provider(_SuccessfulProvider):
            first_result = ingest_sources(
                world_name="Full Reingest World",
                source_files=[first_source, second_source],
                chunk_size=12,
                max_lookback=5,
                overlap_size=2,
                worlds_root=self.worlds_root,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        world_dir = self.worlds_root / "Full Reingest World"
        first_world_metadata = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
        first_run_id = first_world_metadata["active_ingestion_run_id"]

        with self._provider(_SuccessfulProvider):
            second_result = reingest_world_from_stored_sources(
                world_name="Full Reingest World",
                config=self._config_with(chunk_size=100, max_lookback=10, overlap_size=0),
                world_dir=world_dir,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        updated_world_metadata = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
        self.assertTrue(first_result.success)
        self.assertTrue(second_result.success)
        self.assertEqual([book.book_number for book in second_result.books], [1, 2])
        self.assertNotEqual(updated_world_metadata["active_ingestion_run_id"], first_run_id)
        self.assertEqual(updated_world_metadata["splitter_config"]["chunk_size"], 100)
        self.assertEqual((world_dir / "source files" / "book_01" / first_source.name).read_bytes(), first_source.read_bytes())
        self.assertEqual((world_dir / "source files" / "book_02" / second_source.name).read_bytes(), second_source.read_bytes())

    def test_embedding_manifest_run_mismatch_rebuilds_book_one_in_place(self) -> None:
        source_path = self._write_source("run-mismatch.txt", "Alpha beta gamma delta epsilon zeta.")
        world_dir = self.worlds_root / "Run Mismatch World"
        world_dir.mkdir(parents=True, exist_ok=True)
        first_run = self._run_with_provider(
            provider_class=_SuccessfulProvider,
            world_name="Run Mismatch World",
            world_dir=world_dir,
            source_path=source_path,
        )

        world_metadata_path = world_dir / "world.json"
        world_metadata = json.loads(world_metadata_path.read_text(encoding="utf-8"))
        old_run_id = world_metadata["active_ingestion_run_id"]
        world_metadata["active_ingestion_run_status"] = "completed"
        world_metadata_path.write_text(json.dumps(world_metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        with self._provider(_SuccessfulProvider):
            resumed = ingest_sources_into_existing_world(
                world_name="Run Mismatch World",
                source_files=[source_path],
                config=self._config(),
                world_dir=world_dir,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        embedding_manifest = json.loads(Path(resumed.books[0].embedding.manifest_path).read_text(encoding="utf-8"))
        point_id = self._point_id(first_run.world_uuid, 1, 1)
        store = QdrantChunkStore(store_root=self.vector_root)
        try:
            store.ensure_collection(self._embedding_profile())
            record = store.retrieve_existing_points([point_id])[point_id]
        finally:
            store.close()

        self.assertTrue(resumed.success)
        self.assertEqual(resumed.books[0].book_number, 1)
        self.assertGreater(_SuccessfulProvider.call_count, 0)
        self.assertNotEqual(embedding_manifest["ingestion_run_id"], old_run_id)
        self.assertEqual(record.payload["ingestion_run_id"], embedding_manifest["ingestion_run_id"])

    def test_new_world_requires_explicit_embedding_profile(self) -> None:
        source_path = self._write_source("required.txt", "Alpha beta gamma delta epsilon zeta.")

        result = ingest_sources(
            world_name="Profile Required World",
            source_files=[source_path],
            chunk_size=12,
            max_lookback=5,
            overlap_size=2,
            worlds_root=self.worlds_root,
            embedding_profile=None,
            provider_keys_root=self.keys_root,
            vector_store_root=self.vector_root,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.errors[0].code, "EMBEDDING_PROFILE_REQUIRED")

    def test_embedding_profile_uses_model_max_dimensions_and_input_tokens(self) -> None:
        supported_model = get_supported_embedding_model("google/gemini-embedding-2-preview")
        profile = self._embedding_profile()

        self.assertEqual(profile.dimensions, supported_model.max_dimensions)
        self.assertEqual(profile.max_input_tokens, supported_model.max_input_tokens)

    def test_existing_world_profile_is_normalized_to_locked_model_maxima(self) -> None:
        from backend.embeddings.storage import ensure_world_metadata

        world_dir = self.worlds_root / "Legacy World"
        world_dir.mkdir(parents=True, exist_ok=True)
        (world_dir / "world.json").write_text(
            json.dumps(
                {
                    "world_id": "Legacy World",
                    "world_uuid": "c603f3be-9b82-4d37-9a46-c9b634d38757",
                    "world_name": "Legacy World",
                    "embedding_profile": {
                        "provider_id": "google",
                        "model_id": "google/gemini-embedding-2-preview",
                        "dimensions": 3072,
                        "task_type": "RETRIEVAL_DOCUMENT",
                        "profile_version": 1,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        metadata = ensure_world_metadata(
            world_dir=world_dir,
            world_name="Legacy World",
            embedding_profile=self._embedding_profile(),
        )

        self.assertEqual(metadata.embedding_profile.dimensions, 3072)
        self.assertEqual(metadata.embedding_profile.max_input_tokens, 8192)
        stored_world = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
        self.assertEqual(stored_world["embedding_profile"]["extra_settings"]["max_input_tokens"], 8192)

    def test_missing_keys_blocks_before_chunk_ingestion_begins(self) -> None:
        source_path = self._write_source("no-keys.txt", "Alpha beta gamma delta epsilon zeta.")
        empty_keys_root = self.temp_dir / "user" / "empty-keys"

        result = ingest_sources(
            world_name="No Keys World",
            source_files=[source_path],
            chunk_size=12,
            max_lookback=5,
            overlap_size=2,
            worlds_root=self.worlds_root,
            embedding_profile=self._embedding_profile(),
            provider_keys_root=empty_keys_root,
            vector_store_root=self.vector_root,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.errors[0].code, "EMBEDDING_PROVIDER_KEYS_MISSING")
        self.assertEqual(result.books, [])
        self.assertFalse((self.worlds_root / "No Keys World").exists())

    def test_disabled_keys_block_before_chunk_ingestion_begins(self) -> None:
        source_path = self._write_source("disabled-key.txt", "Alpha beta gamma delta epsilon zeta.")
        disabled_keys_root = self.temp_dir / "user" / "disabled-keys"
        provider_dir = disabled_keys_root / "google-ai-studio"
        provider_dir.mkdir(parents=True, exist_ok=True)
        provider_dir.joinpath("primary.json").write_text(
            json.dumps(
                {
                    "name": "Disabled Google Project",
                    "api_key": "fake-api-key",
                    "project_id": "project-one",
                    "allowed_models": ["google/gemini-embedding-2-preview"],
                    "enabled": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        result = ingest_sources(
            world_name="Disabled Keys World",
            source_files=[source_path],
            chunk_size=12,
            max_lookback=5,
            overlap_size=2,
            worlds_root=self.worlds_root,
            embedding_profile=self._embedding_profile(),
            provider_keys_root=disabled_keys_root,
            vector_store_root=self.vector_root,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.errors[0].code, "EMBEDDING_PROVIDER_KEYS_MISSING")
        self.assertEqual(result.books, [])
        self.assertFalse((self.worlds_root / "Disabled Keys World").exists())

    def _run_with_provider(
        self,
        *,
        provider_class,
        world_name: str,
        world_dir: Path,
        source_path: Path,
    ):
        with self._provider(provider_class):
            return ingest_sources_into_existing_world(
                world_name=world_name,
                source_files=[source_path],
                config=self._config(),
                world_dir=world_dir,
                embedding_profile=self._embedding_profile(),
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

    def _write_google_key(
        self,
        *,
        filename: str = "primary.json",
        name: str = "Primary Google Project",
        project_id: str = "project-one",
        allowed_models: list[str] | None = None,
    ) -> None:
        provider_dir = self.keys_root / "google-ai-studio"
        provider_dir.mkdir(parents=True, exist_ok=True)
        provider_dir.joinpath(filename).write_text(
            json.dumps(
                {
                    "name": name,
                    "api_key": "fake-api-key",
                    "project_id": project_id,
                    "allowed_models": ["google/gemini-embedding-2-preview"] if allowed_models is None else allowed_models,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _write_source(self, filename: str, content: str) -> Path:
        source_path = self.sources_dir / filename
        source_path.write_text(content, encoding="utf-8")
        return source_path

    def _config(self):
        from backend.ingestion.text_sources.models import SplitterConfig

        return SplitterConfig(chunk_size=12, max_lookback=5, overlap_size=2)

    def _config_with(self, *, chunk_size: int, max_lookback: int = 5, overlap_size: int = 2):
        from backend.ingestion.text_sources.models import SplitterConfig

        return SplitterConfig(
            chunk_size=chunk_size,
            max_lookback=max_lookback,
            overlap_size=overlap_size,
        )

    def _embedding_profile(self):
        # BLOCK 1: Build the same explicit embedding profile each test run so the tests mirror the real product rule that worlds must choose a model up front
        # WHY: The backend should never silently pick an embedder for new worlds, but the tests still need one compact helper to avoid repeating the same explicit model choice everywhere
        return create_embedding_profile(model_id="google/gemini-embedding-2-preview")

    @contextmanager
    def _provider(self, provider_class):
        from backend.embeddings import service as embedding_service_module
        from backend.ingestion.graph_manifestation import adapters as manifestation_adapters_module

        original_provider_factory = embedding_service_module.create_embedding_provider
        original_node_provider_factory = manifestation_adapters_module.create_embedding_provider
        provider_class.call_count = 0
        embedding_service_module.create_embedding_provider = lambda provider_id: provider_class()
        manifestation_adapters_module.create_embedding_provider = lambda provider_id: provider_class()
        try:
            yield
        finally:
            embedding_service_module.create_embedding_provider = original_provider_factory
            manifestation_adapters_module.create_embedding_provider = original_node_provider_factory

    @contextmanager
    def _graph_provider(self, provider_class):
        from backend.ingestion.graph_extraction import service as graph_service_module

        original_provider_factory = graph_service_module.create_graph_extraction_provider
        graph_service_module.create_graph_extraction_provider = lambda provider_id: provider_class()
        try:
            yield
        finally:
            graph_service_module.create_graph_extraction_provider = original_provider_factory

    @contextmanager
    def _graph_writer(self, writer_class):
        from backend.ingestion.text_sources import service as ingestion_service_module

        original_writer_factory = ingestion_service_module.create_default_graph_writer
        writer_class.nodes = []
        writer_class.edges = []
        ingestion_service_module.create_default_graph_writer = lambda world_dir: writer_class()
        try:
            yield
        finally:
            ingestion_service_module.create_default_graph_writer = original_writer_factory

    def _cancel_after_delay(self, cancellation: EmbeddingRunCancellation) -> None:
        time.sleep(0.05)
        cancellation.cancel()

    def _point_id(self, world_uuid: str, book_number: int, chunk_number: int) -> str:
        return str(uuid5(UUID(world_uuid), f"book:{book_number}:chunk:{chunk_number}"))


if __name__ == "__main__":
    unittest.main()
