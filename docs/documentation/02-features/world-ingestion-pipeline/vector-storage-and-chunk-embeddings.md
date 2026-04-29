---
order: 400
---

# Vector Storage and Chunk Embeddings

Vector Storage and Chunk Embeddings is the ingestion stage that turns persisted chunk text into confirmed Qdrant chunk vectors under a locked world embedding profile.

## Why Chunk Embeddings Exist

Chunk files alone are not enough for similarity retrieval. VySol needs numeric vectors that match the exact chunk text, use one known embedding contract, and can be repaired when chunks or vector records drift.

This stage exists so retrieval can search efficiently without treating vector storage as the authority for full text.

## Who This Page Is For

This page is for contributors, power users, and AI coding agents that need to change chunk embedding, embedding manifests, token-limit enforcement, provider-key usage, Qdrant writes, or retrieval readiness.

## What Vector Storage and Chunk Embeddings Owns

Vector Storage and Chunk Embeddings owns:

- embedding preflight for chunk text
- exact Google token counting before Google embedding dispatch
- one-text-per-request embedding calls for chunks
- embedding manifest reconciliation
- stable chunk vector point ids
- Qdrant chunk-vector upserts
- stale vector cleanup when chunk text, run id, or payload metadata no longer match
- chunk-level retry, cancellation, and resume state for embedding work

## What Vector Storage and Chunk Embeddings Does Not Own

Vector Storage and Chunk Embeddings does not own:

- source conversion or chunk boundary creation
- model catalog definitions
- provider key loading rules
- Qdrant collection internals beyond chunk-vector usage
- graph node embeddings created during graph manifestation
- retrieval query ranking
- full-world re-ingest orchestration, even though re-ingest may delete old chunk vectors through this storage contract

## Normal Flow

Before chunk ingestion begins, VySol checks that the world has a locked embedding profile and at least one eligible provider key for that profile. For a new world, that check happens before the world folder is created so a missing or disabled key does not leave a half-usable world behind.

After Text Splitting writes chunk files, the embedding stage loads the world embedding profile and the per-book embedding manifest. It reconciles that manifest against the current chunk files and live Qdrant points.

Each pending chunk embeds only `chunk_text`. For Google-backed embeddings, VySol counts the exact provider input before dispatch. Oversized or uncountable text is blocked locally instead of being sent to the provider.

The provider request is routed through the [Provider Key Scheduler](../shared-backend-systems/provider-key-scheduler.md). After the provider returns a vector, VySol writes the vector to the Qdrant collection for the locked embedding profile. The embedding manifest is marked complete only after Qdrant confirms the upsert.

## Inputs

This system receives world metadata, locked embedding profile metadata, chunk files, embedding manifests, provider key availability, provider responses, token-count responses, and live Qdrant point state.

## Outputs

This system produces embedding manifests, Qdrant chunk vector points, structured embedding errors, stale-point cleanup, and retrieval-ready vector state.

## Saved State And Resume Behavior

Embedding progress is stored per book. Resume compares the manifest, current chunk hashes, ingestion run id, and Qdrant payloads before deciding which chunks are already safe to trust.

If Qdrant is missing a point that the manifest claims exists, the chunk becomes pending again. If Qdrant has a stale point for the same logical chunk slot, the stale point is deleted before overwrite.

If a saved embedding manifest belongs to an older ingestion run, the book is rebuilt under the active run boundary instead of trusting old progress. Older world metadata can also be normalized to the current backend-owned embedding maxima so legacy profiles keep matching the locked model contract.

## Retry, Pause, And Abort Behavior

Embedding work can run multiple single-chunk requests concurrently. Provider rate limits are reported to the Provider Key Scheduler so another eligible key can be tried when possible.

Rate-limit failures cool down the credential without spending the chunk's ordinary retry budget. Non-rate-limit provider failures can retry the chunk up to the current per-run retry limit before the chunk is marked failed.

Paused or interrupted work remains resumable because each chunk is marked embedded only after its vector write is confirmed. If an embedding run is cancelled while provider requests are still in flight, late responses are ignored and unfinished chunks remain pending.

## Failure Behavior

Oversized Google inputs fail locally with a structured too-large error before the embedding provider is called. Token-count failures fail closed instead of falling back to an estimate.

Provider failures, missing keys, stale Qdrant points, vector-store read/write/delete failures, profile mismatches, and manifest conflicts must update state without pretending retrieval data is complete.

## User-Facing Behavior

The user-facing surface should treat embedding as part of ingestion progress. The backend exposes book-level embedding status, warning events, structured errors, and manifest paths; the UI decides how to present those states.

## System Interactions

Vector Storage and Chunk Embeddings interacts with:

- [World Storage](world-storage.md), which owns chunk text and embedding profile metadata
- [Text Splitting](text-splitting.md), which creates chunk files
- [Provider Key Scheduler](../shared-backend-systems/provider-key-scheduler.md), which selects provider keys and tracks cooldowns
- [Model Registry](../shared-backend-systems/model-registry.md), which describes model limits and embedding dimensions
- [Qdrant Vector Store](../storage-layers/qdrant-vector-store.md), which persists vectors
- [Chunk Retrieval](../retrieval/chunk-retrieval.md), which searches the vectors later

## Internal Edge Cases

- A manifest can be ahead of Qdrant after a failed or interrupted write.
- A missing embedding manifest can be rebuilt from confirmed Qdrant points without re-embedding.
- Qdrant can contain a point whose text hash no longer matches the current chunk file.
- A point can belong to an older ingestion run.
- A saved manifest can belong to an older ingestion run and must be reset for the current run.
- A saved manifest can disagree with the current chunk count and must not be silently reused.
- A chunk can be too large for the selected provider model.
- Exact token counting can fail before the embedding request is sent.
- A cancelled run can receive late provider responses that must not advance trusted state.
- A profile-specific Qdrant collection can be missing, unavailable, or have a vector schema mismatch.

## Cross-System Edge Cases

- Text Splitting changes can stale embedding state for every affected chunk.
- Existing-world profile or splitter changes require full-world re-ingest instead of normal append.
- Full-world re-ingest must delete old chunk vectors before rebuilding derived output from stored sources.
- Retrieval must skip stale or missing chunk records even when Qdrant returns a point.
- Missing or disabled provider keys block before new-world chunk ingestion begins.
- Provider-key cooldowns can pause embedding progress without corrupting chunk storage or spending ordinary chunk retries.
- Graph extraction reads chunk files, not Qdrant vectors, so embedding success must not be treated as graph extraction success.
- Graph node embeddings share the locked world embedding profile but use a separate node-vector collection and manifestation contract.

## Implementation Landmarks

Chunk embedding orchestration lives under `backend/embeddings`. Text ingestion calls the embedding stage from `backend/ingestion/text_sources`. Provider-specific embedding and token-count behavior lives under backend provider modules. Shared model metadata lives under `models/catalog`.

## What AI/Coders Must Check Before Changing This System

Before changing this system, check token-count enforcement, embedding profile locks, provider scheduler behavior, Qdrant collection naming, point id stability, manifest reconciliation, and retrieval repair behavior.

## Invariants That Must Not Be Broken

- The world embedding profile is locked for existing chunk vectors.
- Only `chunk_text` is embedded for chunk retrieval.
- The embedding manifest must not mark a chunk embedded before Qdrant confirms the vector write.
- Exact Google token-count failures must block dispatch.
- Stable point ids represent logical chunk slots, not text hash versions.
- Qdrant chunk-vector payloads must not store full `chunk_text`; chunk text remains owned by world storage.
- Chunk vectors and graph node vectors must stay in separate Qdrant collection namespaces.
