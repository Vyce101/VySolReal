---
order: 400
---

# Knowledge Graph Extraction Pipeline

Knowledge Graph Extraction Pipeline is the resumable LLM stage that turns persisted chunk files into raw node and edge candidates with saved pass history.

## Why It Exists

VySol wants more than chunk retrieval. It needs a graph-shaped layer that can remember people, places, factions, objects, and their relationships without rereading every source file from scratch every time a later system needs that structure.

It also needs that work to survive interruption. LLM extraction is agentic work, so an initial pass can succeed while a glean fails, a provider can rate-limit one call while others succeed, or the app can pause halfway through a book. This pipeline exists so those partial states can be saved and resumed instead of lost.

## How Raw Graph Extraction Works Today

The pipeline runs after chunk embeddings have been confirmed for a book. It starts from chunk files that already live in [World Storage](world-storage.md). Each extraction work item loads the chunk body plus the saved overlap text from the previous chunk. The overlap is reference-only context for pronouns, names, and titles. The chunk body is still the only text that is allowed to justify new graph candidates.

Every world can store graph extraction defaults in `graph_config.json`.

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

The extraction model, prompt preset metadata, and concurrency can change only while the world is paused or idle, and those edits affect only future unsent calls. Each book's extraction manifest still snapshots the run-owned settings that already-started chunk work is using.

For each chunk, VySol runs one initial extraction pass and then the run-locked number of gleaning passes. Different chunks can run at the same time across the book, but a chunk's own passes stay sequential because each glean depends on the saved results from the earlier passes.

Every trusted pass is parsed and saved immediately into the per-book `graph_extraction.json` manifest before the next provider call is allowed to start. That manifest uses the world's active ingestion run id, so resumed books and books added before the run finishes keep writing to the same raw extraction boundary. That saved state is what makes pause, crash recovery, and resume trustworthy.

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
      "status": "extracted",
      "initial_pass": {
        "pass_type": "initial",
        "pass_number": 0
      },
      "glean_passes": [
        {
          "pass_type": "glean",
          "pass_number": 1
        }
      ],
      "nodes": [],
      "edges": []
    }
  ]
}
```

If the model returns malformed JSON or misses the required `---COMPLETE---` marker, the current pass is treated as incomplete and retried up to the normal non-rate-limit retry budget. If the provider rate-limits the request, VySol hands that back to the shared [Provider Key Scheduler](../shared-backend-systems/provider-key-scheduler.md), cools down the affected key or quota bucket, and retries on another usable key without spending the chunk's ordinary retry budget.

If a saved extraction manifest belongs to an older ingestion run, a different chunk layout, or corrupt chunk state, VySol resets that saved extraction state and rebuilds it from the current chunk files instead of crashing on the mismatch. That reset keeps new runs resumable without pretending older saved pass history still belongs to the current run.

## How Raw Candidates Are Shaped

The model returns node names and descriptions plus edge endpoint display names, descriptions, and strengths. The backend, not the model, creates the local UUIDs for both nodes and edges.

Within one chunk, exact duplicate display names are merged into one local node candidate and their descriptions are combined. That merge does not cross chunk boundaries. If two different chunks both extract `Rudeus`, they still remain separate raw candidates at this stage.

Edges are only kept when both endpoints resolve to nodes that were extracted for that same chunk. That matters because a local edge should point to the local node candidate from the same extraction result, not to an older node elsewhere in the world that happens to share the same display name.

## Why VySol Separates Raw Extraction From Graph Manifestation

This pipeline writes raw extraction manifests in World Storage first. That is the crash-safe extraction boundary: saved model passes, merged raw candidates, and local node/edge ids.

The later [Graph Manifestation](graph-manifestation.md) step has a different job. It embeds node candidates, stores node vectors in [Qdrant Vector Store](../storage-layers/qdrant-vector-store.md), and writes traversable graph structure into [Neo4j Graph Store](../storage-layers/neo4j-graph-store.md). Keeping those layers separate stops one store from pretending to be chunk cache, vector index, and graph database all at once.
