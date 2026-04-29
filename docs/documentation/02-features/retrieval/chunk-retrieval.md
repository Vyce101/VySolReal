---
order: 100
---

# Chunk Retrieval

Chunk Retrieval is the backend retrieval system that turns a user or caller query into a vector search over one world's embedded chunks, verifies the returned chunk files, and builds chunk-text-only context for model calls.

## Why Chunk Retrieval Exists

VySol stores chunk vectors during ingestion so later systems can find relevant world text without rereading every chunk file. Chunk Retrieval is the current contract for using those vectors safely.

It also keeps retrieval split into clear responsibilities: Qdrant finds likely candidates, World Storage provides trusted text, and the model context receives only the selected chunk text.

## Who This Page Is For

This page is for contributors, power users, and AI coding agents that need to change retrieval filters, query embeddings, score thresholds, returned context, stale-vector repair, or GraphRAG integration points.

## What Chunk Retrieval Owns

Chunk Retrieval owns:

- query embedding for chunk search
- world-scoped Qdrant search
- similarity threshold validation and pass-through
- maximum result count validation and clamping
- chunk file verification after vector search
- stale or missing chunk repair signals
- rich retrieval results
- clean model context built from chunk text only

## What Chunk Retrieval Does Not Own

Chunk Retrieval does not own:

- source ingestion
- chunk boundary creation
- chunk embedding writes
- provider key policy
- Qdrant storage internals
- graph traversal
- broader prompt assembly outside the chunk-text context payload

## Normal Flow

The caller provides a world reference, query text, maximum result count, and similarity minimum. Chunk Retrieval validates the retrieval settings, loads the world's locked embedding profile from World Storage, and counts how many chunks are currently marked embedded in embedding manifests.

The requested result count is clamped to the number of embedded chunks. If the caller requests zero chunks, the system returns the normal empty response shape after world metadata is validated.

For non-empty retrieval, VySol embeds the stripped query with the same provider, model, and dimensions as the chunk vectors, but with the provider's query-specific embedding mode. For Google AI Studio, chunk vectors use `RETRIEVAL_DOCUMENT` and query vectors use `RETRIEVAL_QUERY`.

Qdrant searches the collection for the locked embedding profile, filters by the world's UUID, and applies the similarity minimum as the Qdrant score threshold.

Each returned point is checked against the chunk file in World Storage. Qdrant supplies score and metadata; World Storage supplies trusted chunk text. Valid results are sorted by score descending, then book number and chunk number, before the model-facing context is built from chunk text only.

## Inputs

Chunk Retrieval receives world identity, query text, requested result count, similarity minimum, locked embedding profile metadata, provider query-embedding responses, Qdrant search results, embedding manifests, and chunk files.

## Outputs

Chunk Retrieval produces rich retrieval results, model-facing `chunk_text` context, repair signals for stale embeddings, skipped stale results, and structured retrieval errors.

## Saved State / Repair Behavior

Chunk Retrieval does not own ingestion resume, but it can update embedding manifests when retrieval proves vector state is stale or incomplete.

If a Qdrant point references a missing chunk file, the matching chunk state is marked pending with a retrieval warning code. If a Qdrant point's saved text hash no longer matches the current chunk file, the stale point is deleted from Qdrant and the matching chunk state is marked pending with the current text hash.

Those manifest changes tell embedding resume work that the chunk can be repaired by embedding it again.

## Failure Behavior

Invalid retrieval settings, such as a negative result count or a similarity minimum outside the accepted range, fail before world metadata, provider, or Qdrant work starts. Empty query text fails after world metadata is loaded, but before query embedding.

If the world has no embedded chunks, retrieval returns an empty successful result with a warning and does not spend a provider call.

Provider-key configuration failures, unavailable credentials, query token-limit failures, provider failures, and vector-store failures return structured retrieval errors. These failures must not modify trusted chunk text.

## System Interactions

Chunk Retrieval interacts with:

- [World Storage](../world-ingestion-pipeline/world-storage.md), which owns chunk text and embedding profile metadata
- [Vector Storage And Chunk Embeddings](../world-ingestion-pipeline/vector-storage-and-chunk-embeddings.md), which creates chunk vectors
- [Qdrant Vector Store](../storage-layers/qdrant-vector-store.md), which searches vector points
- [Provider Key Scheduler](../shared-backend-systems/provider-key-scheduler.md), which schedules query embedding calls
- [Model Registry](../shared-backend-systems/model-registry.md), which supplies embedding model metadata

## User-Facing Behavior

Users or UI surfaces may see fewer returned chunks than the requested maximum when the world has fewer embedded chunks, scores are below threshold, or stale records are skipped.

The rich result payload can include scores, source filename, chunk position, chunk text, and overlap text. The model-facing context intentionally excludes scores, filenames, and overlap text.

## Internal Edge Cases

- A maximum result count of zero returns no chunks after world metadata is validated and does not call the provider or Qdrant.
- Negative `top_k`, boolean `top_k`, boolean similarity values, and similarity values outside `0.0` through `1.0` fail as retrieval setting errors.
- Empty or whitespace-only query text fails before query embedding.
- A world with no embedded chunks returns an empty successful response with a warning and does not call the provider.
- The requested result count is clamped to the number of chunks that embedding manifests currently mark as embedded.
- Qdrant can return fewer results than requested because the score threshold is applied inside the vector search.
- A vector point can exist while its chunk file is missing.
- A vector point can have an old text hash after source material is reprocessed.
- Missing or stale chunk backing data is skipped and marked for embedding repair instead of being returned.

## Cross-System Edge Cases

- Query embeddings must use the same profile dimensions as stored chunk vectors.
- Google query embeddings must pass exact max-input-token enforcement before the provider call.
- Provider cooldowns or disabled/missing credentials can block query embedding before Qdrant search starts.
- Qdrant collection schema must match the world's locked embedding profile.
- Retrieval must not return stale chunk text just because Qdrant found a point.
- GraphRAG systems should treat Chunk Retrieval as one retrieval source, not the whole retrieval architecture.

## Implementation Landmarks

Chunk retrieval behavior lives under `backend/retrieval/chunks`. It reads world metadata and chunk files through world storage helpers, calls embedding provider adapters for query vectors, builds model context through `backend/context`, and searches Qdrant through vector storage helpers in `backend/embeddings`.

## What AI/Coders Must Check Before Changing This System

Before changing Chunk Retrieval, check query embedding mode, exact query-token enforcement, world UUID filtering, score threshold semantics, result sorting, model context shape, stale-vector repair behavior, and GraphRAG callers.

## Invariants That Must Not Be Broken

- Qdrant finds candidate chunks; World Storage supplies trusted chunk text.
- Query vectors must match the world's locked embedding profile.
- Retrieval must stay scoped to one world UUID.
- Score thresholding must happen inside the Qdrant query.
- Stale or missing chunk backing data must be skipped, not returned.
- Stale vector repair must not overwrite chunk text.
- Model context must contain only selected chunk text.
