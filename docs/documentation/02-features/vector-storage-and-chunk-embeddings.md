# Vector Storage And Chunk Embeddings

VySol's vector storage and chunk embedding pipeline turns persisted chunk text into locked, resumable embedding records that can be retrieved later without re-ingesting the world.

![VySol workspace preview](../../assets/social.png)

## Why Chunks Become Vectors At Ingestion Time

This layer exists because chunk files alone are not enough for similarity retrieval. Retrieval needs numeric vectors that were produced with one known embedding contract, stored somewhere durable, and kept in sync with the exact chunk text that lives inside the world.

VySol also needs that process to be safe to resume. A world can be halfway through ingestion when the app closes, a provider can reject one request while the rest succeed, or a chunk can change later because the source material or chunk settings changed. The vector layer exists so those states can be repaired without pretending that chunk storage and vector storage are the same thing.

## How A Chunk Becomes A Vector Point

The flow starts before a new world is created. The user must choose an embedding model up front, and VySol checks that at least one eligible provider key exists for that model before the world directory is created. If there is no valid key, the run stops immediately instead of creating a half-started world that can never be embedded.

When the preflight passes, the world is created with a stable UUID and a locked embedding profile. That profile stores the provider id, model id, task type, dimensions, and the model's maximum input token budget. The user chooses the model, while the backend fixes the embedding contract details so that every chunk in that world is embedded the same way.

```json
{
  "world_id": "My World",
  "world_uuid": "b1934f2b-7d5e-4e1f-9d55-7d7b4f454e42",
  "world_name": "My World",
  "embedding_profile": {
    "provider_id": "google",
    "model_id": "google/gemini-embedding-2-preview",
    "dimensions": 3072,
    "task_type": "RETRIEVAL_DOCUMENT",
    "profile_version": 1,
    "extra_settings": {
      "max_input_tokens": 8192
    }
  }
}
```

After that, text splitting runs normally. Source copies are preserved, chunk files are written one at a time, and the chunk progress manifest is updated only after each chunk file is safely saved. Once a book's chunk files exist, the embedding stage starts.

The embedding stage opens the shared local Qdrant store, loads the per-book embedding manifest, and reconciles that manifest against both the current chunk files and the live Qdrant points. If the manifest says a chunk is embedded but Qdrant is missing the point, that chunk is reset to be embedded again. If the chunk text hash no longer matches the stored vector payload, the stale point is deleted before overwrite.

Each remaining chunk becomes one embedding work item. Only `chunk_text` is embedded. `overlap_text` stays in the chunk JSON for later graph extraction or context stitching, but it is deliberately excluded from the embedding hash and from the provider request.

Before VySol sends a chunk to the provider, it checks the locked max input token budget from the world's embedding profile. That check is currently a fast local estimate rather than Google's exact token-counting API. VySol estimates tokens as roughly one token per four characters of `chunk_text`, compares that estimate to the locked `max_input_tokens`, and blocks the request locally with `EMBEDDING_CHUNK_TOO_LARGE` if the estimated chunk size is already over the model's ceiling. This is a fast preflight designed to stop obviously oversized chunks before a provider call, not a claim that the local estimate is a byte-perfect tokenization match for every possible text shape.

If the chunk fits that preflight, VySol sends one text per request, but it can run multiple single-chunk requests concurrently across the book.

The provider call returns a vector, and that vector is written into Qdrant under a stable point id derived from the world UUID, book number, and chunk number. The point id does not include the text hash, which means the same logical chunk slot is overwritten when the text changes instead of creating a second logical copy.

Only after Qdrant confirms the upsert does VySol mark the chunk as embedded in the embedding manifest. That order matters because the manifest is not allowed to claim retrieval data exists before the vector store has actually confirmed it.

```json
{
  "world_id": "My World",
  "world_uuid": "b1934f2b-7d5e-4e1f-9d55-7d7b4f454e42",
  "source_filename": "chapter-one.txt",
  "book_number": 1,
  "total_chunks": 3,
  "profile": {
    "provider_id": "google",
    "model_id": "google/gemini-embedding-2-preview",
    "dimensions": 3072,
    "task_type": "RETRIEVAL_DOCUMENT",
    "profile_version": 1,
    "extra_settings": {
      "max_input_tokens": 8192
    }
  },
  "chunk_states": [
    {
      "chunk_number": 1,
      "point_id": "0f0d9c35-6e65-54ff-93e9-4b91f1f4bbaa",
      "status": "embedded",
      "text_hash": "9fd72d4eb18a4f8df4a9fe9de718d2558c7ee2f4d40fe2f329f7de7f5d0dff0f",
      "retry_count": 0
    }
  ]
}
```

Provider cooldown state is stored beside the key store instead of living only in memory. That allows resume to keep respecting machine-clock-based cooldowns after restart, including temporary per-minute cooldowns and run-scoped per-day exhaustion.

## Why VySol Locks The Embedding Contract

The world-level lock exists so retrieval data stays coherent. If one world quietly mixed vectors from different task types, different output dimensions, or different token-budget assumptions, later retrieval quality would degrade in ways that are hard to explain and even harder to debug.

That is why task type is not a normal user-facing toggle here. For chunk embeddings, VySol fixes it to `RETRIEVAL_DOCUMENT`. That is also why dimensions and maximum input tokens are not left floating as soft defaults. They are pinned to the selected model's maximum supported shape and then stored in the world's embedding profile so the same world keeps using the same vector contract until an explicit future re-embed action changes it.

The separate embedding manifest exists for the same reason. Chunk persistence and vector persistence can fail independently, so they need independent truth. The chunk manifest answers, "does the chunk file exist?" The embedding manifest answers, "does the confirmed vector for this exact chunk text exist?" Keeping those answers separate is what makes resume trustworthy.

Finally, the shared local Qdrant store is used because retrieval wants one durable vector layer that can filter by world UUID while still supporting future growth into millions of chunk or node records. Worlds remain exportable and deletable because the storage identity is the stable world UUID, while the visible world name remains editable metadata.
