"""Chunk similarity retrieval tests."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid5

from backend.embeddings.catalog import create_embedding_profile
from backend.embeddings.models import EmbeddingManifest, QueryEmbeddingSuccess, WorldMetadata
from backend.embeddings.qdrant_store import QdrantChunkStore
from backend.embeddings.storage import chunk_text_hash, ensure_world_metadata, save_embedding_manifest
from backend.ingestion.text_sources.models import ChunkRecord
from backend.ingestion.text_sources.storage import atomic_write_json, book_directory, chunk_file_path
from backend.retrieval.chunks.service import retrieve_similar_chunks


class _QueryProvider:
    call_count = 0
    query_vector: list[float] = []

    def embed_query(self, *, credential, profile, query):
        # BLOCK 1: Return a deterministic query vector without reaching a real provider
        # WHY: Retrieval tests need stable offline query embeddings so they can validate Qdrant filtering and result shaping without API keys
        type(self).call_count += 1
        return QueryEmbeddingSuccess(
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            vector=list(type(self).query_vector),
            billable_character_count=len(query),
        )

    def embed_text(self, *, credential, profile, work_item):
        raise AssertionError("Chunk retrieval tests should not embed document chunks.")


class _CapturingStore:
    created_count = 0
    closed_count = 0
    last_limit = None
    last_score_threshold = None
    last_world_uuid = None

    def __init__(self, *, store_root):
        type(self).created_count += 1

    def ensure_collection(self, profile):
        return None

    def query_similar_chunks(self, *, query_vector, world_uuid, limit, score_threshold):
        # BLOCK 1: Capture the Qdrant query settings that retrieval passes through
        # WHY: The similarity minimum must be applied inside the vector query rather than by filtering an already-limited result set afterward
        type(self).last_limit = limit
        type(self).last_score_threshold = score_threshold
        type(self).last_world_uuid = world_uuid
        return []

    def close(self):
        type(self).closed_count += 1


class ChunkRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.worlds_root = self.temp_dir / "worlds"
        self.keys_root = self.temp_dir / "keys"
        self.vector_root = self.temp_dir / "vector_store"
        self.profile = create_embedding_profile(model_id="google/gemini-embedding-2-preview")
        self._write_google_key()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_retrieves_only_requested_world_and_builds_chunk_text_context(self) -> None:
        first_world = self._create_world_with_book(
            world_name="First",
            chunks=[
                ("Alpha text", "Alpha overlap", [1.0, 0.0]),
                ("Beta text", "Beta overlap", [0.8, 0.6]),
                ("Gamma text", "Gamma overlap", [0.0, 1.0]),
            ],
        )
        self._create_world_with_book(
            world_name="Second",
            chunks=[
                ("Other world text", "Other overlap", [1.0, 0.0]),
            ],
        )

        with self._query_provider([1.0, 0.0]):
            result = retrieve_similar_chunks(
                world_dir=first_world,
                query="alpha",
                top_k=10,
                similarity_minimum=0.15,
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.requested_top_k, 10)
        self.assertEqual(result.top_k, 3)
        self.assertEqual([chunk.chunk_text for chunk in result.results], ["Alpha text", "Beta text"])
        self.assertEqual(result.model_context.chunks, ["Alpha text", "Beta text"])
        self.assertEqual(result.model_context.text, "Alpha text\n\nBeta text")
        self.assertNotIn("Alpha overlap", result.model_context.text)
        self.assertTrue(all(chunk.world_uuid == result.world_uuid for chunk in result.results))

    def test_score_threshold_is_passed_to_qdrant_query(self) -> None:
        world_dir = self._create_world_with_book(
            world_name="Threshold",
            chunks=[("Alpha text", "", [1.0, 0.0])],
        )

        with self._query_provider([1.0, 0.0]), self._qdrant_store(_CapturingStore):
            result = retrieve_similar_chunks(
                world_dir=world_dir,
                query="alpha",
                top_k=9,
                similarity_minimum=0.42,
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertTrue(result.success)
        self.assertEqual(_CapturingStore.last_limit, 1)
        self.assertEqual(_CapturingStore.last_score_threshold, 0.42)
        self.assertEqual(_CapturingStore.last_world_uuid, result.world_uuid)

    def test_partial_embeddings_search_only_embedded_chunks(self) -> None:
        world_dir = self._create_world_with_book(
            world_name="Partial",
            chunks=[
                ("Embedded text", "", [1.0, 0.0]),
                ("Pending text", "", [0.0, 1.0]),
            ],
            embedded_chunk_numbers={1},
        )

        with self._query_provider([0.0, 1.0]), self._qdrant_store(_CapturingStore):
            captured = retrieve_similar_chunks(
                world_dir=world_dir,
                query="pending",
                top_k=10,
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        with self._query_provider([1.0, 0.0]):
            result = retrieve_similar_chunks(
                world_dir=world_dir,
                query="embedded",
                top_k=10,
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertTrue(captured.success)
        self.assertEqual(_CapturingStore.last_limit, 1)
        self.assertTrue(result.success)
        self.assertEqual(result.top_k, 1)
        self.assertEqual([chunk.chunk_text for chunk in result.results], ["Embedded text"])

    def test_top_k_zero_validates_world_without_provider_or_qdrant(self) -> None:
        world_dir = self._create_world_with_book(
            world_name="No Chunks Requested",
            chunks=[("Alpha text", "", [1.0, 0.0])],
        )

        with self._query_provider([1.0, 0.0]), self._qdrant_store(_CapturingStore):
            result = retrieve_similar_chunks(
                world_dir=world_dir,
                query="",
                top_k=0,
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.results, [])
        self.assertEqual(result.model_context.text, "")
        self.assertEqual(_QueryProvider.call_count, 0)
        self.assertEqual(_CapturingStore.created_count, 0)

    def test_world_with_no_embedded_chunks_returns_warning_without_provider_call(self) -> None:
        world_dir = self.worlds_root / "Empty"
        world_dir.mkdir(parents=True, exist_ok=True)
        ensure_world_metadata(
            world_dir=world_dir,
            world_name="Empty",
            embedding_profile=self.profile,
        )

        with self._query_provider([1.0, 0.0]):
            result = retrieve_similar_chunks(
                world_dir=world_dir,
                query="alpha",
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.results, [])
        self.assertEqual(result.warnings[0].code, "WORLD_HAS_NO_EMBEDDED_CHUNKS")
        self.assertEqual(_QueryProvider.call_count, 0)

    def test_missing_chunk_file_is_skipped_and_manifest_marked_pending(self) -> None:
        world_dir = self._create_world_with_book(
            world_name="Missing Chunk",
            chunks=[("Alpha text", "", [1.0, 0.0])],
        )
        chunk_file_path(book_directory(world_dir, 1), 1, 1).unlink()

        with self._query_provider([1.0, 0.0]):
            result = retrieve_similar_chunks(
                world_dir=world_dir,
                query="alpha",
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.results, [])
        self.assertEqual(result.warnings[0].code, "RETRIEVAL_CHUNK_FILE_MISSING")
        manifest = self._load_embedding_manifest(world_dir, 1)
        self.assertEqual(manifest["chunk_states"][0]["status"], "pending")
        self.assertEqual(manifest["chunk_states"][0]["last_error_code"], "RETRIEVAL_CHUNK_FILE_MISSING")

    def test_hash_mismatch_is_skipped_deleted_and_manifest_marked_pending(self) -> None:
        world_dir = self._create_world_with_book(
            world_name="Stale Chunk",
            chunks=[("Alpha text", "", [1.0, 0.0])],
        )
        metadata = self._load_world_metadata(world_dir)
        point_id = self._point_id(metadata.world_uuid, 1, 1)
        self._write_chunk(
            world_dir=world_dir,
            world=metadata,
            book_number=1,
            chunk_number=1,
            total_chunks=1,
            chunk_text="Changed text",
            overlap_text="",
        )

        with self._query_provider([1.0, 0.0]):
            result = retrieve_similar_chunks(
                world_dir=world_dir,
                query="alpha",
                provider_keys_root=self.keys_root,
                vector_store_root=self.vector_root,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.results, [])
        self.assertEqual(result.warnings[0].code, "RETRIEVAL_CHUNK_VECTOR_STALE")
        manifest = self._load_embedding_manifest(world_dir, 1)
        self.assertEqual(manifest["chunk_states"][0]["status"], "pending")
        self.assertEqual(manifest["chunk_states"][0]["text_hash"], chunk_text_hash("Changed text"))

        store = QdrantChunkStore(store_root=self.vector_root)
        try:
            store.ensure_collection(self.profile)
            self.assertEqual(store.retrieve_existing_points([point_id]), {})
        finally:
            store.close()

    def test_invalid_settings_and_empty_query_return_structured_errors(self) -> None:
        world_dir = self._create_world_with_book(
            world_name="Validation",
            chunks=[("Alpha text", "", [1.0, 0.0])],
        )

        invalid = retrieve_similar_chunks(
            world_dir=world_dir,
            query="alpha",
            top_k=-1,
            similarity_minimum=1.5,
            provider_keys_root=self.keys_root,
            vector_store_root=self.vector_root,
        )
        empty = retrieve_similar_chunks(
            world_dir=world_dir,
            query="   ",
            provider_keys_root=self.keys_root,
            vector_store_root=self.vector_root,
        )

        self.assertFalse(invalid.success)
        self.assertEqual([error.code for error in invalid.errors], ["INVALID_TOP_K", "INVALID_SIMILARITY_MINIMUM"])
        self.assertFalse(empty.success)
        self.assertEqual(empty.errors[0].code, "EMPTY_RETRIEVAL_QUERY")

    def _create_world_with_book(
        self,
        *,
        world_name: str,
        chunks: list[tuple[str, str, list[float]]],
        embedded_chunk_numbers: set[int] | None = None,
    ) -> Path:
        # BLOCK 1: Build a complete world, chunk files, embedding manifest, and Qdrant points for retrieval tests
        # WHY: Tests should exercise the same file-backed chunk source of truth and local Qdrant storage used by production retrieval
        embedded_chunks = embedded_chunk_numbers if embedded_chunk_numbers is not None else set(range(1, len(chunks) + 1))
        world_dir = self.worlds_root / world_name
        world_dir.mkdir(parents=True, exist_ok=True)
        world = ensure_world_metadata(
            world_dir=world_dir,
            world_name=world_name,
            embedding_profile=self.profile,
        )
        point_ids: list[str] = []
        store = QdrantChunkStore(store_root=self.vector_root)
        try:
            store.ensure_collection(self.profile)
            for chunk_number, (chunk_text, overlap_text, vector_prefix) in enumerate(chunks, start=1):
                chunk_path = self._write_chunk(
                    world_dir=world_dir,
                    world=world,
                    book_number=1,
                    chunk_number=chunk_number,
                    total_chunks=len(chunks),
                    chunk_text=chunk_text,
                    overlap_text=overlap_text,
                )
                point_id = self._point_id(world.world_uuid, 1, chunk_number)
                point_ids.append(point_id)
                if chunk_number not in embedded_chunks:
                    continue
                from backend.embeddings.models import EmbeddingWorkItem

                store.upsert_chunk_embedding(
                    world=world,
                    ingestion_run_id="run-1",
                    work_item=EmbeddingWorkItem(
                        book_number=1,
                        chunk_number=chunk_number,
                        point_id=point_id,
                        chunk_text=chunk_text,
                        text_hash=chunk_text_hash(chunk_text),
                        source_filename="source.txt",
                        chunk_path=chunk_path,
                        chunk_position=f"{chunk_number}/{len(chunks)}",
                    ),
                    vector=self._vector(vector_prefix),
                    profile=self.profile,
                )
        finally:
            store.close()

        manifest = EmbeddingManifest.create(
            world_id=world.world_id,
            world_uuid=world.world_uuid,
            ingestion_run_id="run-1",
            source_filename="source.txt",
            book_number=1,
            total_chunks=len(chunks),
            profile=self.profile,
            point_ids=point_ids,
        )
        for state, (chunk_text, _, _) in zip(manifest.chunk_states, chunks, strict=True):
            if state.chunk_number not in embedded_chunks:
                continue
            state.status = "embedded"
            state.text_hash = chunk_text_hash(chunk_text)
        save_embedding_manifest(book_directory(world_dir, 1) / "embeddings.json", manifest)
        return world_dir

    def _write_chunk(
        self,
        *,
        world_dir: Path,
        world: WorldMetadata,
        book_number: int,
        chunk_number: int,
        total_chunks: int,
        chunk_text: str,
        overlap_text: str,
    ) -> Path:
        chunk_path = chunk_file_path(book_directory(world_dir, book_number), book_number, chunk_number)
        atomic_write_json(
            chunk_path,
            ChunkRecord(
                world_id=world.world_id,
                world_uuid=world.world_uuid,
                source_filename="source.txt",
                book_number=book_number,
                chunk_number=chunk_number,
                chunk_position=f"{chunk_number}/{total_chunks}",
                overlap_text=overlap_text,
                chunk_text=chunk_text,
            ).to_dict(),
        )
        return chunk_path

    def _write_google_key(self) -> None:
        provider_dir = self.keys_root / "google-ai-studio"
        provider_dir.mkdir(parents=True, exist_ok=True)
        provider_dir.joinpath("primary.json").write_text(
            json.dumps(
                {
                    "name": "Primary",
                    "api_key": "fake-api-key",
                    "project_id": "project-one",
                    "allowed_models": ["google/gemini-embedding-2-preview"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _vector(self, prefix: list[float]) -> list[float]:
        return prefix + [0.0] * (self.profile.dimensions - len(prefix))

    def _load_world_metadata(self, world_dir: Path) -> WorldMetadata:
        return WorldMetadata.from_dict(json.loads((world_dir / "world.json").read_text(encoding="utf-8")))

    def _load_embedding_manifest(self, world_dir: Path, book_number: int) -> dict[str, object]:
        return json.loads((book_directory(world_dir, book_number) / "embeddings.json").read_text(encoding="utf-8"))

    def _point_id(self, world_uuid: str, book_number: int, chunk_number: int) -> str:
        return str(uuid5(UUID(world_uuid), f"book:{book_number}:chunk:{chunk_number}"))

    @contextmanager
    def _query_provider(self, query_vector: list[float]):
        from backend.retrieval.chunks import service as retrieval_service_module

        original_provider_factory = retrieval_service_module.create_embedding_provider
        _QueryProvider.call_count = 0
        _QueryProvider.query_vector = self._vector(query_vector)
        retrieval_service_module.create_embedding_provider = lambda provider_id: _QueryProvider()
        try:
            yield
        finally:
            retrieval_service_module.create_embedding_provider = original_provider_factory

    @contextmanager
    def _qdrant_store(self, store_class):
        from backend.retrieval.chunks import service as retrieval_service_module

        original_store = retrieval_service_module.QdrantChunkStore
        store_class.created_count = 0
        store_class.closed_count = 0
        store_class.last_limit = None
        store_class.last_score_threshold = None
        store_class.last_world_uuid = None
        retrieval_service_module.QdrantChunkStore = store_class
        try:
            yield
        finally:
            retrieval_service_module.QdrantChunkStore = original_store


if __name__ == "__main__":
    unittest.main()
