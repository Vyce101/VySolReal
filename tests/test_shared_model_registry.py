"""Tests for the shared model catalog and profile-specific Qdrant storage."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from backend.embeddings import catalog as embedding_catalog
from backend.embeddings.catalog import create_embedding_profile, get_supported_embedding_model
from backend.embeddings.errors import EmbeddingConfigurationError
from backend.embeddings.models import EmbeddingProfile, EmbeddingWorkItem, WorldMetadata
from backend.embeddings.providers import create_embedding_provider
from backend.embeddings.qdrant_store import QdrantChunkStore, collection_name_for_profile
from backend.models.registry import default_catalog_root, load_model_registry


class SharedModelRegistryTests(unittest.TestCase):
    def test_backend_embedding_catalog_reads_shared_gemini_model_file(self) -> None:
        model_path = default_catalog_root() / "providers" / "google-ai-studio" / "models" / "gemini-embedding-2-preview.json"
        shared_payload = json.loads(model_path.read_text(encoding="utf-8"))

        supported_model = get_supported_embedding_model("google/gemini-embedding-2-preview")

        self.assertEqual(supported_model.model_id, shared_payload["id"])
        self.assertEqual(supported_model.call_name, shared_payload["callName"])
        self.assertEqual(supported_model.max_input_tokens, shared_payload["limits"]["maxInputTokens"])
        self.assertEqual(supported_model.max_dimensions, shared_payload["limits"]["maxEmbeddingDimensions"])

    def test_added_embedding_model_file_can_create_backend_profile(self) -> None:
        temp_dir = Path(tempfile.mkdtemp())
        try:
            catalog_root = temp_dir / "catalog"
            shutil.copytree(default_catalog_root(), catalog_root)
            new_model_path = catalog_root / "providers" / "google-ai-studio" / "models" / "test-embedding.json"
            new_model_path.write_text(
                json.dumps(
                    {
                        "id": "google/test-embedding",
                        "displayName": "Test Embedding",
                        "callName": "test-embedding",
                        "description": "Test-only embedding model.",
                        "surfaces": ["embedding"],
                        "limits": {
                            "maxInputTokens": 1024,
                            "maxEmbeddingDimensions": 256,
                        },
                        "settings": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            manifest_path = catalog_root / "providers.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["providers"][0]["modelFiles"].append(
                "providers/google-ai-studio/models/test-embedding.json"
            )
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            registry = load_model_registry(catalog_root)

            original_registry_loader = embedding_catalog.load_default_model_registry
            embedding_catalog.load_default_model_registry = lambda: registry
            try:
                profile = embedding_catalog.create_embedding_profile(model_id="google/test-embedding")
            finally:
                embedding_catalog.load_default_model_registry = original_registry_loader

            self.assertEqual(profile.provider_id, "google")
            self.assertEqual(profile.model_id, "google/test-embedding")
            self.assertEqual(profile.dimensions, 256)
            self.assertEqual(profile.max_input_tokens, 1024)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_chat_only_model_is_rejected_for_embedding_profile(self) -> None:
        with self.assertRaises(EmbeddingConfigurationError) as raised:
            create_embedding_profile(model_id="google/gemini-3-flash-preview")

        self.assertEqual(raised.exception.code, "UNSUPPORTED_EMBEDDING_MODEL")

    def test_missing_runtime_adapter_is_rejected_cleanly(self) -> None:
        with self.assertRaises(EmbeddingConfigurationError) as raised:
            create_embedding_provider("not-yet-wired")

        self.assertEqual(raised.exception.code, "UNSUPPORTED_EMBEDDING_PROVIDER")


class QdrantProfileCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.vector_root = self.temp_dir / "vector_store"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_different_embedding_dimensions_use_different_collections(self) -> None:
        profile_a = self._profile(model_id="test/model-a", dimensions=3)
        profile_b = self._profile(model_id="test/model-b", dimensions=5)
        store = QdrantChunkStore(store_root=self.vector_root)
        try:
            store.ensure_collection(profile_a)
            store.upsert_chunk_embedding(
                world=self._world(profile=profile_a),
                ingestion_run_id="run-profile-a",
                work_item=self._work_item(point_id=str(uuid4())),
                vector=[1.0, 2.0, 3.0],
                profile=profile_a,
            )

            store.ensure_collection(profile_b)
            store.upsert_chunk_embedding(
                world=self._world(profile=profile_b),
                ingestion_run_id="run-profile-b",
                work_item=self._work_item(point_id=str(uuid4())),
                vector=[1.0, 2.0, 3.0, 4.0, 5.0],
                profile=profile_b,
            )
        finally:
            store.close()

        self.assertNotEqual(collection_name_for_profile(profile_a), collection_name_for_profile(profile_b))

    def test_same_embedding_profile_collection_keeps_world_payloads_separate(self) -> None:
        profile = self._profile(model_id="test/shared-model", dimensions=3)
        first_point_id = str(uuid4())
        second_point_id = str(uuid4())
        first_world = self._world(profile=profile)
        second_world = self._world(profile=profile)
        store = QdrantChunkStore(store_root=self.vector_root)
        try:
            store.ensure_collection(profile)
            store.upsert_chunk_embedding(
                world=first_world,
                ingestion_run_id="run-first-world",
                work_item=self._work_item(point_id=first_point_id),
                vector=[1.0, 1.0, 1.0],
                profile=profile,
            )
            store.upsert_chunk_embedding(
                world=second_world,
                ingestion_run_id="run-second-world",
                work_item=self._work_item(point_id=second_point_id),
                vector=[2.0, 2.0, 2.0],
                profile=profile,
            )
            records = store.retrieve_existing_points([first_point_id, second_point_id])
        finally:
            store.close()

        self.assertEqual(records[first_point_id].payload["world_uuid"], first_world.world_uuid)
        self.assertEqual(records[second_point_id].payload["world_uuid"], second_world.world_uuid)
        self.assertEqual(records[first_point_id].payload["ingestion_run_id"], "run-first-world")
        self.assertEqual(records[second_point_id].payload["ingestion_run_id"], "run-second-world")
        self.assertEqual(records[first_point_id].payload["embedding_profile_key"], records[second_point_id].payload["embedding_profile_key"])
        self.assertEqual(records[first_point_id].payload["dimensions"], 3)

    def _profile(self, *, model_id: str, dimensions: int) -> EmbeddingProfile:
        return EmbeddingProfile(
            provider_id="test",
            model_id=model_id,
            dimensions=dimensions,
            task_type="RETRIEVAL_DOCUMENT",
            profile_version=1,
            extra_settings={"max_input_tokens": 1024},
        )

    def _world(self, *, profile: EmbeddingProfile) -> WorldMetadata:
        world_uuid = str(uuid4())
        return WorldMetadata(
            world_id=world_uuid,
            world_uuid=world_uuid,
            world_name="Test World",
            embedding_profile=profile,
        )

    def _work_item(self, *, point_id: str) -> EmbeddingWorkItem:
        return EmbeddingWorkItem(
            book_number=1,
            chunk_number=1,
            point_id=point_id,
            chunk_text="alpha",
            text_hash="hash",
            source_filename="source.txt",
            chunk_path=Path("chunk_0001.json"),
            chunk_position="1/1",
        )


if __name__ == "__main__":
    unittest.main()
