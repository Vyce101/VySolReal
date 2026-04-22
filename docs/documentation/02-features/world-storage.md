# World Storage

World Storage is the local file-backed data store that keeps each world's source copies, chunk files, manifests, and locked world metadata together in one resumable place.

![VySol workspace preview](../../assets/social.png)

## Why Each World Has Its Own Stored State

This storage layer exists because VySol needs more than temporary processing output. Once a source book has been selected, the app needs a durable place where the preserved source copy, the generated chunks, the world identity, and the progress metadata can all live together even if the app closes halfway through ingestion.

It also exists to keep the user's original files out of the critical path after ingestion starts. VySol works from app-owned copies so that changes, deletions, or path problems in the original location do not quietly corrupt the world's internal state.

## How A World Is Stored On Disk

Every world gets its own directory. Inside that directory, VySol stores one locked `world.json` file, one source-file area, and one per-book area. The world file carries the stable world UUID and the locked embedding profile so renames do not break the world's storage identity.

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

The source-file area keeps the preserved working copy and backup copy for each book. The per-book area keeps the progress manifest, the chunk files, and the embedding manifest. Chunk files are the source of truth for the chunk text that later extraction and retrieval-related systems inspect.

```json
{
  "world_id": "My World",
  "world_uuid": "b1934f2b-7d5e-4e1f-9d55-7d7b4f454e42",
  "source_filename": "chapter-one.txt",
  "book_number": 1,
  "chunk_number": 4,
  "chunk_position": "4/37",
  "overlap_text": "the last part of the previous chunk",
  "chunk_text": "the current chunk text"
}
```

The chunk progress manifest answers whether the chunk file has been safely written. The embedding manifest answers whether that chunk's vector has been safely confirmed. They are stored separately because those two truths can diverge after crashes, provider failures, or manual deletion of generated files.

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

## Why VySol Stores Worlds This Way

The most important design choice is that the filesystem remains the durable source of truth for world-owned content. Chunk text is not treated as disposable intermediate data. That makes resume possible, keeps the system inspectable, and avoids a design where retrieval depends on data that only ever lived in memory.

The second major choice is storing chunk progress and embedding progress separately. Text splitting and vector storage do not fail in the same ways, so one shared manifest would blur two different states and make recovery less trustworthy.

The third major choice is using a stable world UUID instead of the world name as the storage identity. The world name can change later, but the storage identity stays fixed, which makes vector point ids, manifests, and future exports safer to reason about.
