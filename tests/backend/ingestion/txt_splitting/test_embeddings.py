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
from backend.embeddings.models import EmbeddingRunCancellation, EmbeddingSuccess
from backend.embeddings.qdrant_store import QdrantChunkStore
from backend.ingestion.txt_splitting.service import ingest_sources, ingest_sources_into_existing_world
from backend.ingestion.txt_splitting.storage import book_directory


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
            vector=[1.0, 2.0, 3.0],
            billable_character_count=len(work_item.chunk_text),
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

    def _write_google_key(self) -> None:
        provider_dir = self.keys_root / "google-ai-studio"
        provider_dir.mkdir(parents=True, exist_ok=True)
        provider_dir.joinpath("primary.json").write_text(
            json.dumps(
                {
                    "name": "Primary Google Project",
                    "api_key": "fake-api-key",
                    "project_id": "project-one",
                    "allowed_models": ["google/gemini-embedding-2-preview"],
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
        from backend.ingestion.txt_splitting.models import SplitterConfig

        return SplitterConfig(chunk_size=12, max_lookback=5, overlap_size=2)

    def _embedding_profile(self):
        # BLOCK 1: Build the same explicit embedding profile each test run so the tests mirror the real product rule that worlds must choose a model up front
        # WHY: The backend should never silently pick an embedder for new worlds, but the tests still need one compact helper to avoid repeating the same explicit model choice everywhere
        return create_embedding_profile(model_id="google/gemini-embedding-2-preview")

    @contextmanager
    def _provider(self, provider_class):
        from backend.embeddings import service as embedding_service_module

        original_provider_factory = embedding_service_module.create_embedding_provider
        provider_class.call_count = 0
        embedding_service_module.create_embedding_provider = lambda provider_id: provider_class()
        try:
            yield
        finally:
            embedding_service_module.create_embedding_provider = original_provider_factory

    def _cancel_after_delay(self, cancellation: EmbeddingRunCancellation) -> None:
        time.sleep(0.05)
        cancellation.cancel()

    def _point_id(self, world_uuid: str, book_number: int, chunk_number: int) -> str:
        return str(uuid5(UUID(world_uuid), f"book:{book_number}:chunk:{chunk_number}"))


if __name__ == "__main__":
    unittest.main()
