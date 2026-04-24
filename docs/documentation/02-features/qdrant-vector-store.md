# Qdrant Vector Store

Qdrant Vector Store is the shared local vector database that keeps each embedded chunk as a searchable vector point with retrieval metadata.

## Why VySol Uses A Separate Vector Database

This layer exists because chunk files alone are not enough for similarity retrieval. Retrieval needs vectors, point ids, and filterable metadata that can be searched efficiently at scale without rereading every chunk file on disk.

It also exists so vector persistence can be validated independently from chunk persistence. A chunk can already exist in world storage while its embedding is still pending, stale, or missing. Qdrant gives VySol a dedicated place to confirm that vector-side truth.

## How Chunk Embeddings Are Stored In Qdrant

VySol uses one shared local Qdrant store for the app rather than one separate store per world. Inside that store, chunk vectors are split into profile-specific collections. Each collection name is derived from the locked embedding provider, model, dimensions, task type, and profile version. The world UUID is still stored as payload metadata so worlds that share the same embedding profile can stay isolated inside the same collection.

Each chunk embedding is stored under a stable point id derived from the world UUID, book number, and chunk number. The text hash is stored in payload, not in the point id. That means when a chunk's text changes, VySol overwrites the same logical point instead of creating a duplicate record for the same chunk slot.

The vector payload keeps retrieval metadata such as the world UUID, source filename, chunk position, embedding profile details, and the chunk text hash. The full chunk text is not duplicated here. World Storage remains the source of truth for full text, while Qdrant keeps the vector and the metadata needed to find the right chunk again.

```json
{
  "world_uuid": "b1934f2b-7d5e-4e1f-9d55-7d7b4f454e42",
  "book_number": 1,
  "chunk_number": 4,
  "chunk_position": "4/37",
  "source_filename": "chapter-one.txt",
  "provider_id": "google",
  "model_id": "google/gemini-embedding-2-preview",
  "task_type": "RETRIEVAL_DOCUMENT",
  "dimensions": 3072,
  "embedding_profile_key": "5030236c06f1118e",
  "text_hash": "9fd72d4eb18a4f8df4a9fe9de718d2558c7ee2f4d40fe2f329f7de7f5d0dff0f"
}
```

When the embedding pipeline starts, it reads the live Qdrant points for the book's expected point ids and compares them against the embedding manifest and the current chunk hashes. If a stored point hash does not match the current chunk text, that point is deleted before overwrite. If the manifest says a chunk is embedded but the point is missing, the chunk is marked for re-embedding.

Qdrant only becomes trusted after the upsert succeeds. The embedding manifest is updated after that confirmation, never before.

## Why Qdrant Is Shaped This Way

The shared-store design keeps the vector layer simpler to operate while still allowing worlds to stay isolated by `world_uuid`. Profile-specific collections avoid Qdrant's single-vector-size-per-collection constraint without multiplying local database lifecycle problems for every world.

The stable point-id design is deliberate too. If the text hash were part of the point id, every content change would create a brand-new point and stale cleanup would become mandatory for every update. Using a stable chunk-slot id makes overwrite behavior predictable and keeps one logical chunk tied to one logical vector record.

Finally, the choice not to duplicate full chunk text in Qdrant keeps responsibilities clearer. Qdrant is the vector index and retrieval metadata layer. World Storage is the authoritative content store. That split makes recovery and inspection easier because each system has one clear job.
