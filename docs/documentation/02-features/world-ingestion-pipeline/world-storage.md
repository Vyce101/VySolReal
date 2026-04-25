---
order: 100
---

# World Storage

World Storage is the local file-backed data store that keeps each world's source copies, chunk files, manifests, graph extraction config, graph manifestation state, and locked world metadata together in one resumable place.

## Why Each World Has Its Own Stored State

This storage layer exists because VySol needs more than temporary processing output. Once a source book has been selected, the app needs a durable place where the preserved source copy, the generated chunks, the world identity, and the progress metadata can all live together even if the app closes halfway through ingestion.

It also exists to keep the user's original files out of the critical path after ingestion starts. VySol works from app-owned copies so that changes, deletions, or path problems in the original location do not quietly corrupt the world's internal state.

## How A World Is Stored On Disk

Every world gets its own directory. Inside that directory, VySol stores one locked `world.json` file, one world-level `graph_config.json` file, one source-file area, and one per-book area. The world file carries the stable world UUID, the locked embedding profile, and the locked splitter contract so renames do not break the world's storage identity and later appends cannot quietly change the chunk layout.

```json
{
  "world_id": "My World",
  "world_uuid": "b1934f2b-7d5e-4e1f-9d55-7d7b4f454e42",
  "world_name": "My World",
  "splitter_config": {
    "chunk_size": 1200,
    "max_lookback": 200,
    "overlap_size": 150
  },
  "active_ingestion_run_id": "run-2026-04-25-001",
  "active_ingestion_run_status": "paused",
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

The active ingestion run fields keep one durable run boundary while chunk embeddings, graph extraction, or graph manifestation are still unfinished. A world uses `active` while work is being dispatched, `paused` when unfinished work is waiting for resume, and `completed` only after the whole current run finishes.

The separate graph config keeps the world's default graph extraction settings. Those defaults can change for future unsent extraction calls, but only while the world is paused or idle. Each book's extraction manifest still snapshots the exact run settings that were used for already-started work.

Normal existing-world ingest reuses the paused unfinished run when the user appends another book before that run finishes. If the user wants to change the locked splitter settings or locked embedding profile instead, that normal append path is rejected and the world must use a full-world re-ingest from its preserved source copies.

```json
{
  "provider_id": "google",
  "model_id": "google/gemma-4-31b-it",
  "gleaning_count": 1,
  "extraction_concurrency": 5,
  "prompt_preset_id": "default",
  "prompt_preset_version": 1,
  "parser_version": 1
}
```

The source-file area keeps the preserved working copy and backup copy for each book. The per-book area keeps the progress manifest, the chunk files, the embedding manifest, the graph extraction manifest, and the graph manifestation manifest. Chunk files are the source of truth for the chunk text that later extraction and retrieval-related systems inspect.

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

The chunk progress manifest answers whether the chunk file has been safely written. The embedding manifest answers whether that chunk's vector has been safely confirmed. The graph extraction manifest answers whether the chunk's raw node and edge candidates have been safely extracted and saved. The graph manifestation manifest answers whether those extracted candidates have been turned into confirmed node vectors and Neo4j graph writes. They are stored separately because those truths can diverge after crashes, provider failures, or manual deletion of generated files.

```json
{
  "world_id": "My World",
  "world_uuid": "b1934f2b-7d5e-4e1f-9d55-7d7b4f454e42",
  "ingestion_run_id": "run-2026-04-25-001",
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

```json
{
  "world_id": "My World",
  "world_uuid": "b1934f2b-7d5e-4e1f-9d55-7d7b4f454e42",
  "ingestion_run_id": "run-2026-04-25-001",
  "source_filename": "chapter-one.txt",
  "book_number": 1,
  "total_chunks": 3,
  "config": {
    "provider_id": "google",
    "model_id": "google/gemma-4-31b-it",
    "gleaning_count": 1,
    "extraction_concurrency": 5,
    "prompt_preset_id": "default",
    "prompt_preset_version": 1,
    "parser_version": 1
  },
  "chunk_states": [
    {
      "chunk_number": 1,
      "status": "partial",
      "initial_pass": {
        "pass_type": "initial",
        "pass_number": 0
      },
      "glean_passes": [],
      "nodes": [],
      "edges": []
    }
  ]
}
```

## Why VySol Stores Worlds This Way

The most important design choice is that the filesystem remains the durable source of truth for world-owned content. Chunk text is not treated as disposable intermediate data. That makes resume possible, keeps the system inspectable, and avoids a design where retrieval depends on data that only ever lived in memory.

The second major choice is storing chunk progress, embedding progress, graph extraction progress, and graph manifestation progress separately. Those stages do not fail in the same ways, so one shared manifest would blur different states and make recovery less trustworthy.

The third major choice is using a stable world UUID instead of the world name as the storage identity. The world name can change later, but the storage identity stays fixed, which makes vector point ids, manifests, and future exports safer to reason about.

The fourth major choice is preserving the stored source copies even when the world is rebuilt. Full-world re-ingest clears and regenerates derived chunk, vector, and graph outputs, but it works from those saved in-world source copies so the app can rebuild the entire world without depending on the user's original file paths still existing.
