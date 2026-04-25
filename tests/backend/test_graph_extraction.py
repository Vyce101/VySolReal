"""Graph extraction parser and service tests."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.graph_extraction.models import (
    ExtractionPassRecord,
    ExtractionProviderFailure,
    ExtractionProviderSuccess,
    GraphExtractionConfig,
    GraphExtractionRunCancellation,
)
from backend.graph_extraction.parser import merge_pass_records, parse_extraction_response
from backend.graph_extraction.service import extract_book_chunks


class _QueuedExtractionProvider:
    responses: list[str] = []
    lock = threading.Lock()

    def extract(self, *, credential, config, prompt, log_context):
        # BLOCK 1: Return the next queued response without contacting a real model provider
        # WHY: Extraction tests need deterministic parser and resume behavior without writing real API keys or depending on live provider responses
        with type(self).lock:
            response_text = type(self).responses.pop(0)
        return ExtractionProviderSuccess(
            response_text=response_text,
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
        )


class _RetryableFailureProvider:
    call_count = 0

    def extract(self, *, credential, config, prompt, log_context):
        # BLOCK 1: Return the same non-rate-limit provider failure each time so retry accounting can be asserted precisely
        # WHY: The extraction service should spend the chunk retry budget on ordinary provider crashes without needing any live API outage to reproduce that path
        type(self).call_count += 1
        return ExtractionProviderFailure(
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            code="EXTRACTION_PROVIDER_FAILED",
            message="The fake provider crashed before returning a usable response.",
            retryable=True,
        )


class _SlowFailureProvider:
    call_count = 0

    def extract(self, *, credential, config, prompt, log_context):
        # BLOCK 1: Sleep long enough for the test cancellation handle to flip before returning a failure
        # WHY: Cancellation coverage only matters when a response arrives after the user paused the run, so the fake provider has to create that exact timing edge case
        type(self).call_count += 1
        time.sleep(0.2)
        return ExtractionProviderFailure(
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            code="EXTRACTION_PROVIDER_FAILED",
            message="The fake provider returned after cancellation.",
            retryable=True,
        )


class GraphExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.book_dir = self.temp_dir / "world" / "books" / "book_01"
        self.chunks_dir = self.book_dir / "chunks"
        self.keys_root = self.temp_dir / "user" / "keys"
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self._write_google_key()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_parser_requires_completion_marker(self) -> None:
        with self.assertRaises(Exception):
            parse_extraction_response('{"nodes": [], "edges": []}')

    def test_parser_accepts_valid_json_with_completion_marker(self) -> None:
        nodes, edges = parse_extraction_response(
            """{"nodes": [], "edges": []}
---COMPLETE---"""
        )

        self.assertEqual(nodes, [])
        self.assertEqual(edges, [])

    def test_parser_accepts_fenced_json_with_completion_marker(self) -> None:
        nodes, edges = parse_extraction_response(
            """```json
{"nodes": [{"display_name": "Rudeus", "description": "A mage."}], "edges": []}
```
---COMPLETE---"""
        )

        self.assertEqual(nodes[0]["display_name"], "Rudeus")
        self.assertEqual(edges, [])

    def test_parser_rejects_marker_with_invalid_schema(self) -> None:
        with self.assertRaises(Exception):
            parse_extraction_response(
                """{"nodes": {}, "edges": []}
---COMPLETE---"""
            )

    def test_merge_exact_duplicate_nodes_and_drop_bad_strength(self) -> None:
        record = ExtractionPassRecord(
            pass_type="initial",
            pass_number=0,
            nodes=[
                {"display_name": "Rudeus", "description": "A mage."},
                {"display_name": "Rudeus", "description": "A student."},
            ],
            edges=[
                {
                    "source_display_name": "Rudeus",
                    "target_display_name": "Rudeus",
                    "description": "Self-reflection.",
                    "strength": 11,
                }
            ],
            provider_id="google",
            model_id="google/gemma-4-31b-it",
            prompt_preset_id="default",
            prompt_preset_version=1,
        )

        nodes, edges = merge_pass_records([record])

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].description, "A mage.\n\nA student.")
        self.assertEqual(edges, [])

    def test_merge_does_not_fuzzy_merge_and_retains_duplicate_edges(self) -> None:
        record = ExtractionPassRecord(
            pass_type="initial",
            pass_number=0,
            nodes=[
                {"display_name": "Rudeus", "description": "A boy."},
                {"display_name": "Rudeus Greyrat", "description": "A named boy."},
                {"display_name": "Sylphie", "description": "A friend."},
            ],
            edges=[
                {
                    "source_display_name": "Rudeus",
                    "target_display_name": "Sylphie",
                    "description": "Rudeus meets Sylphie.",
                    "strength": 7,
                },
                {
                    "source_display_name": "Rudeus",
                    "target_display_name": "Sylphie",
                    "description": "Rudeus befriends Sylphie.",
                    "strength": 7,
                },
                {
                    "source_display_name": "Rudeus",
                    "target_display_name": "Missing",
                    "description": "This endpoint does not exist.",
                    "strength": 7,
                },
            ],
            provider_id="google",
            model_id="google/gemma-4-31b-it",
            prompt_preset_id="default",
            prompt_preset_version=1,
        )

        nodes, edges = merge_pass_records([record])

        self.assertEqual([node.display_name for node in nodes], ["Rudeus", "Rudeus Greyrat", "Sylphie"])
        self.assertEqual(len(edges), 2)

    def test_merge_uses_stable_candidate_ids_when_chunk_identity_is_provided(self) -> None:
        record = ExtractionPassRecord(
            pass_type="initial",
            pass_number=0,
            nodes=[
                {"display_name": "Rudeus", "description": "A boy."},
                {"display_name": "Sylphie", "description": "A friend."},
            ],
            edges=[
                {
                    "source_display_name": "Rudeus",
                    "target_display_name": "Sylphie",
                    "description": "They meet.",
                    "strength": 7,
                },
                {
                    "source_display_name": "Rudeus",
                    "target_display_name": "Sylphie",
                    "description": "They meet.",
                    "strength": 7,
                },
            ],
            provider_id="google",
            model_id="google/gemma-4-31b-it",
            prompt_preset_id="default",
            prompt_preset_version=1,
        )

        first_nodes, first_edges = merge_pass_records(
            [record],
            world_uuid="c603f3be-9b82-4d37-9a46-c9b634d38757",
            ingestion_run_id="run-1",
            book_number=1,
            chunk_number=1,
        )
        second_nodes, second_edges = merge_pass_records(
            [record],
            world_uuid="c603f3be-9b82-4d37-9a46-c9b634d38757",
            ingestion_run_id="run-1",
            book_number=1,
            chunk_number=1,
        )

        self.assertEqual([node.node_id for node in first_nodes], [node.node_id for node in second_nodes])
        self.assertEqual([edge.edge_id for edge in first_edges], [edge.edge_id for edge in second_edges])
        self.assertNotEqual(first_edges[0].edge_id, first_edges[1].edge_id)

    def test_service_keeps_initial_edge_when_glean_adds_missing_node(self) -> None:
        chunk_path = self._write_chunk("Rudeus met Sylphie in the village.")
        _QueuedExtractionProvider.responses = [
            """{"nodes": [{"display_name": "Rudeus", "description": "A boy in the village."}], "edges": [{"source_display_name": "Rudeus", "target_display_name": "Sylphie", "description": "Rudeus met Sylphie in the village.", "strength": 7}]}
---COMPLETE---""",
            """{"nodes": [{"display_name": "Sylphie", "description": "A person Rudeus met in the village."}], "edges": []}
---COMPLETE---""",
        ]

        with self._provider(_QueuedExtractionProvider):
            result, warnings = extract_book_chunks(
                world_id="Test World",
                world_uuid="c603f3be-9b82-4d37-9a46-c9b634d38757",
                ingestion_run_id="run-1",
                book_dir=self.book_dir,
                book_number=1,
                source_filename="book.txt",
                chunk_paths=[str(chunk_path)],
                config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=1,
                    extraction_concurrency=5,
                ),
                provider_keys_root=self.keys_root,
            )

        manifest = json.loads((self.book_dir / "graph_extraction.json").read_text(encoding="utf-8"))
        chunk_state = manifest["chunk_states"][0]

        self.assertEqual(warnings, [])
        self.assertEqual(result.status, "completed")
        self.assertEqual(chunk_state["status"], "extracted")
        self.assertEqual(len(chunk_state["nodes"]), 2)
        self.assertEqual(len(chunk_state["edges"]), 1)
        self.assertNotEqual(chunk_state["edges"][0]["source_node_id"], chunk_state["edges"][0]["target_node_id"])

    def test_resume_retries_failed_glean_without_rerunning_initial_pass(self) -> None:
        chunk_path = self._write_chunk("Rudeus met Sylphie in the village.")
        _QueuedExtractionProvider.responses = [
            """{"nodes": [{"display_name": "Rudeus", "description": "A boy in the village."}], "edges": [{"source_display_name": "Rudeus", "target_display_name": "Sylphie", "description": "Rudeus met Sylphie in the village.", "strength": 7}]}
---COMPLETE---""",
            '{"nodes": [], "edges": []}',
            '{"nodes": [], "edges": []}',
            '{"nodes": [], "edges": []}',
        ]

        with self._provider(_QueuedExtractionProvider):
            first_result, _ = extract_book_chunks(
                world_id="Test World",
                world_uuid="c603f3be-9b82-4d37-9a46-c9b634d38757",
                ingestion_run_id="run-1",
                book_dir=self.book_dir,
                book_number=1,
                source_filename="book.txt",
                chunk_paths=[str(chunk_path)],
                config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=1,
                    extraction_concurrency=1,
                ),
                provider_keys_root=self.keys_root,
            )

        first_manifest = json.loads((self.book_dir / "graph_extraction.json").read_text(encoding="utf-8"))
        first_chunk_state = first_manifest["chunk_states"][0]
        self.assertEqual(first_result.status, "partial")
        self.assertIsNotNone(first_chunk_state["initial_pass"])
        self.assertEqual(first_chunk_state["glean_retry_count"], 3)
        self.assertEqual(first_chunk_state["status"], "partial")

        _QueuedExtractionProvider.responses = [
            """{"nodes": [{"display_name": "Sylphie", "description": "A person Rudeus met in the village."}], "edges": []}
---COMPLETE---""",
        ]

        with self._provider(_QueuedExtractionProvider):
            resumed_result, _ = extract_book_chunks(
                world_id="Test World",
                world_uuid="c603f3be-9b82-4d37-9a46-c9b634d38757",
                ingestion_run_id="run-1",
                book_dir=self.book_dir,
                book_number=1,
                source_filename="book.txt",
                chunk_paths=[str(chunk_path)],
                config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=1,
                    extraction_concurrency=1,
                ),
                provider_keys_root=self.keys_root,
            )

        resumed_manifest = json.loads((self.book_dir / "graph_extraction.json").read_text(encoding="utf-8"))
        resumed_chunk_state = resumed_manifest["chunk_states"][0]

        self.assertEqual(resumed_result.status, "completed")
        self.assertEqual(resumed_chunk_state["status"], "extracted")
        self.assertEqual(resumed_chunk_state["initial_pass"]["nodes"][0]["display_name"], "Rudeus")
        self.assertEqual(len(resumed_chunk_state["glean_passes"]), 1)
        self.assertEqual(resumed_chunk_state["glean_retry_count"], 0)
        self.assertEqual(len(resumed_chunk_state["nodes"]), 2)
        self.assertEqual(len(resumed_chunk_state["edges"]), 1)

    def test_malformed_initial_output_spends_full_retry_budget(self) -> None:
        chunk_path = self._write_chunk("Rudeus met Sylphie in the village.")
        _QueuedExtractionProvider.responses = [
            '{"nodes": [], "edges": []}',
            '{"nodes": [], "edges": []}',
            '{"nodes": [], "edges": []}',
        ]

        with self._provider(_QueuedExtractionProvider):
            result, _ = extract_book_chunks(
                world_id="Test World",
                world_uuid="c603f3be-9b82-4d37-9a46-c9b634d38757",
                ingestion_run_id="run-1",
                book_dir=self.book_dir,
                book_number=1,
                source_filename="book.txt",
                chunk_paths=[str(chunk_path)],
                config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=0,
                    extraction_concurrency=1,
                ),
                provider_keys_root=self.keys_root,
            )

        manifest = json.loads((self.book_dir / "graph_extraction.json").read_text(encoding="utf-8"))
        chunk_state = manifest["chunk_states"][0]

        self.assertEqual(result.status, "failed")
        self.assertEqual(chunk_state["status"], "failed")
        self.assertEqual(chunk_state["retry_count"], 3)
        self.assertEqual(chunk_state["glean_retry_count"], 0)
        self.assertEqual(chunk_state["last_error_code"], "EXTRACTION_RESPONSE_INCOMPLETE")

    def test_provider_failure_spends_full_retry_budget(self) -> None:
        chunk_path = self._write_chunk("Rudeus met Sylphie in the village.")
        _RetryableFailureProvider.call_count = 0

        with self._provider(_RetryableFailureProvider):
            result, _ = extract_book_chunks(
                world_id="Test World",
                world_uuid="c603f3be-9b82-4d37-9a46-c9b634d38757",
                ingestion_run_id="run-1",
                book_dir=self.book_dir,
                book_number=1,
                source_filename="book.txt",
                chunk_paths=[str(chunk_path)],
                config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=0,
                    extraction_concurrency=1,
                ),
                provider_keys_root=self.keys_root,
            )

        manifest = json.loads((self.book_dir / "graph_extraction.json").read_text(encoding="utf-8"))
        chunk_state = manifest["chunk_states"][0]

        self.assertEqual(result.status, "failed")
        self.assertEqual(_RetryableFailureProvider.call_count, 3)
        self.assertEqual(chunk_state["status"], "failed")
        self.assertEqual(chunk_state["retry_count"], 3)
        self.assertEqual(chunk_state["last_error_code"], "EXTRACTION_PROVIDER_FAILED")

    def test_late_failure_after_cancellation_does_not_spend_retry_budget(self) -> None:
        chunk_path = self._write_chunk("Rudeus met Sylphie in the village.")
        cancellation = GraphExtractionRunCancellation()
        cancel_thread = threading.Thread(target=self._cancel_after_delay, args=(cancellation,), daemon=True)
        _SlowFailureProvider.call_count = 0

        cancel_thread.start()
        try:
            with self._provider(_SlowFailureProvider):
                result, _ = extract_book_chunks(
                    world_id="Test World",
                    world_uuid="c603f3be-9b82-4d37-9a46-c9b634d38757",
                    ingestion_run_id="run-1",
                    book_dir=self.book_dir,
                    book_number=1,
                    source_filename="book.txt",
                    chunk_paths=[str(chunk_path)],
                    config=GraphExtractionConfig(
                        provider_id="google",
                        model_id="google/gemma-4-31b-it",
                        gleaning_count=0,
                        extraction_concurrency=1,
                    ),
                    provider_keys_root=self.keys_root,
                    cancellation=cancellation,
                )
        finally:
            cancel_thread.join(timeout=1)

        manifest = json.loads((self.book_dir / "graph_extraction.json").read_text(encoding="utf-8"))
        chunk_state = manifest["chunk_states"][0]

        self.assertEqual(result.status, "partial")
        self.assertEqual(_SlowFailureProvider.call_count, 1)
        self.assertEqual(chunk_state["status"], "pending")
        self.assertEqual(chunk_state["retry_count"], 0)
        self.assertEqual(chunk_state["glean_retry_count"], 0)
        self.assertIsNone(chunk_state.get("last_error_code"))

    def test_missing_run_identity_marks_chunk_failed(self) -> None:
        chunk_path = self._write_chunk("Rudeus met Sylphie in the village.")

        result, _ = extract_book_chunks(
            world_id="Test World",
            world_uuid="",
            ingestion_run_id="",
            book_dir=self.book_dir,
            book_number=1,
            source_filename="book.txt",
            chunk_paths=[str(chunk_path)],
            config=GraphExtractionConfig(
                provider_id="google",
                model_id="google/gemma-4-31b-it",
            ),
            provider_keys_root=self.keys_root,
        )

        manifest = json.loads((self.book_dir / "graph_extraction.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "failed")
        self.assertEqual(manifest["chunk_states"][0]["status"], "failed")
        self.assertEqual(manifest["chunk_states"][0]["last_error_code"], "GRAPH_EXTRACTION_RUN_IDENTITY_MISSING")

    def test_corrupt_manifest_rebuilds_and_redoes_extraction(self) -> None:
        chunk_path = self._write_chunk("Rudeus met Sylphie in the village.")
        (self.book_dir / "graph_extraction.json").write_text("{not-json", encoding="utf-8")
        _QueuedExtractionProvider.responses = [
            """{"nodes": [{"display_name": "Rudeus", "description": "A boy."}], "edges": []}
---COMPLETE---""",
            """{"nodes": [], "edges": []}
---COMPLETE---""",
        ]

        with self._provider(_QueuedExtractionProvider):
            result, _ = extract_book_chunks(
                world_id="Test World",
                world_uuid="c603f3be-9b82-4d37-9a46-c9b634d38757",
                ingestion_run_id="run-1",
                book_dir=self.book_dir,
                book_number=1,
                source_filename="book.txt",
                chunk_paths=[str(chunk_path)],
                config=GraphExtractionConfig(
                    provider_id="google",
                    model_id="google/gemma-4-31b-it",
                    gleaning_count=1,
                ),
                provider_keys_root=self.keys_root,
            )

        manifest = json.loads((self.book_dir / "graph_extraction.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "completed")
        self.assertEqual(manifest["warnings"][0]["code"], "GRAPH_EXTRACTION_MANIFEST_CORRUPT")
        self.assertEqual(manifest["chunk_states"][0]["status"], "extracted")

    def _write_google_key(self) -> None:
        # BLOCK 1: Write a fake local key that is allowed to serve the extraction model under test
        # WHY: The service performs local key eligibility checks, but tests must never use a real API key
        provider_dir = self.keys_root / "google-ai-studio"
        provider_dir.mkdir(parents=True, exist_ok=True)
        provider_dir.joinpath("primary.json").write_text(
            json.dumps(
                {
                    "name": "Primary",
                    "api_key": "fake-api-key",
                    "allowed_models": ["google/gemma-4-31b-it"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _write_chunk(self, chunk_text: str) -> Path:
        # BLOCK 1: Write a minimal persisted chunk fixture matching the ingestion chunk contract
        # WHY: The extraction service should read the same chunk JSON shape produced by real TXT/PDF/EPUB ingestion
        chunk_path = self.chunks_dir / "book_01_chunk_0001.json"
        chunk_path.write_text(
            json.dumps(
                {
                    "world_id": "Test World",
                    "world_uuid": "c603f3be-9b82-4d37-9a46-c9b634d38757",
                    "source_filename": "book.txt",
                    "book_number": 1,
                    "chunk_number": 1,
                    "chunk_position": "1/1",
                    "overlap_text": "",
                    "chunk_text": chunk_text,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return chunk_path

    def _provider(self, provider_class):
        # BLOCK 1: Patch the service-level provider factory for the duration of one test
        # WHY: The service imports the factory directly, so tests need to replace that symbol where the service resolves it
        from contextlib import contextmanager
        from backend.graph_extraction import service as service_module

        @contextmanager
        def manager():
            original_factory = service_module.create_graph_extraction_provider
            service_module.create_graph_extraction_provider = lambda provider_id: provider_class()
            try:
                yield
            finally:
                service_module.create_graph_extraction_provider = original_factory

        return manager()

    def _cancel_after_delay(self, cancellation: GraphExtractionRunCancellation) -> None:
        time.sleep(0.05)
        cancellation.cancel()


if __name__ == "__main__":
    unittest.main()
