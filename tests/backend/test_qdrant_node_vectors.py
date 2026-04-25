"""Focused tests for Qdrant node vector storage."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from backend.embeddings.models import EmbeddingProfile, WorldMetadata
from backend.embeddings.qdrant_store import (
    QdrantNodeStore,
    collection_name_for_node_profile,
    collection_name_for_profile,
    embedding_profile_key,
)


class QdrantNodeVectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.vector_root = self.temp_dir / "vector_store"
        self.profile = self._profile()
        self.world = self._world(profile=self.profile)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_node_collection_uses_nodes_prefix_and_profile_hash(self) -> None:
        node_collection = collection_name_for_node_profile(self.profile)
        chunk_collection = collection_name_for_profile(self.profile)

        self.assertTrue(node_collection.startswith("nodes_"))
        self.assertTrue(node_collection.endswith(embedding_profile_key(self.profile)))
        self.assertNotEqual(node_collection, chunk_collection)

    def test_upserts_retrieves_and_deletes_node_payload_without_source_text(self) -> None:
        point_id = str(uuid4())
        store = QdrantNodeStore(store_root=self.vector_root)

        try:
            store.ensure_collection(self.profile)
            store.upsert_node_embedding(
                world=self.world,
                point_id=point_id,
                vector=[0.1, 0.2, 0.3],
                profile=self.profile,
                ingestion_run_id="run-123",
                source_filename="source.txt",
                book_number=2,
                chunk_number=7,
                chunk_position="7/9",
                chunk_file="books/book_02/chunks/chunk_0007.json",
                chunk_text_hash="chunk-hash",
                node_id="node-abc",
                display_name="Rudeus Greyrat",
                text_hash="node-text-hash",
            )
            records = store.retrieve_node_points([point_id])
            store.delete_node_points([point_id])
            deleted_records = store.retrieve_node_points([point_id])
        finally:
            store.close()

        payload = records[point_id].payload
        self.assertEqual(payload["world_uuid"], self.world.world_uuid)
        self.assertEqual(payload["ingestion_run_id"], "run-123")
        self.assertEqual(payload["source_filename"], "source.txt")
        self.assertEqual(payload["book_number"], 2)
        self.assertEqual(payload["chunk_number"], 7)
        self.assertEqual(payload["chunk_position"], "7/9")
        self.assertEqual(payload["chunk_file"], "books/book_02/chunks/chunk_0007.json")
        self.assertEqual(payload["chunk_text_hash"], "chunk-hash")
        self.assertEqual(payload["node_id"], "node-abc")
        self.assertEqual(payload["display_name"], "Rudeus Greyrat")
        self.assertEqual(payload["provider_id"], self.profile.provider_id)
        self.assertEqual(payload["model_id"], self.profile.model_id)
        self.assertEqual(payload["task_type"], self.profile.task_type)
        self.assertEqual(payload["dimensions"], self.profile.dimensions)
        self.assertEqual(payload["embedding_model_id"], self.profile.model_id)
        self.assertEqual(payload["embedding_profile_version"], self.profile.profile_version)
        self.assertEqual(payload["embedding_profile_key"], embedding_profile_key(self.profile))
        self.assertEqual(payload["text_hash"], "node-text-hash")
        self.assertNotIn("description", payload)
        self.assertNotIn("node_description", payload)
        self.assertNotIn("chunk_text", payload)
        self.assertEqual(deleted_records, {})

    def _profile(self) -> EmbeddingProfile:
        # BLOCK 1: Build a tiny deterministic embedding profile for local Qdrant tests
        # WHY: A small dimension keeps the test fast while still proving the collection schema and payload contract are profile-driven
        return EmbeddingProfile(
            provider_id="test",
            model_id="test/node-model",
            dimensions=3,
            task_type="RETRIEVAL_DOCUMENT",
            profile_version=1,
            extra_settings={"max_input_tokens": 1024},
        )

    def _world(self, *, profile: EmbeddingProfile) -> WorldMetadata:
        world_uuid = str(uuid4())
        return WorldMetadata(
            world_id=world_uuid,
            world_uuid=world_uuid,
            world_name="Node Test World",
            embedding_profile=profile,
            active_ingestion_run_id="run-123",
            active_ingestion_run_status="running",
        )


if __name__ == "__main__":
    unittest.main()
