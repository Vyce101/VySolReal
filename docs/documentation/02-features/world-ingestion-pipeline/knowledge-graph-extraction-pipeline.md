---
order: 200
---

# Knowledge Graph Extraction

Knowledge Graph Extraction is the resumable LLM stage that turns persisted chunk files into raw node and edge candidates with saved pass history.

## Why Graph Extraction Exists

VySol needs graph-shaped knowledge about characters, places, objects, factions, and relationships. Chunk vectors can find similar text, but they do not produce structured graph candidates by themselves.

Graph Extraction exists to create a crash-safe raw extraction boundary before graph manifestation, node-vector storage, Neo4j writes, or future entity resolution. It preserves what the model claimed from each chunk without treating those claims as final graph records.

## Who This Page Is For

This page is for contributors, power users, and AI coding agents that need to change graph prompts, parsing, gleaning, extraction manifests, provider calls, token-limit enforcement, or graph pipeline resume behavior.

## What Graph Extraction Owns

Graph Extraction owns:

- initial extraction prompts
- gleaning prompts
- exact token preflight for graph extraction prompts where supported
- pass sequencing within each chunk
- parsing model output into raw candidates
- local raw node and edge ids
- per-book `graph_extraction.json` state
- chunk-local duplicate merging
- retryable malformed-output handling

## What Graph Extraction Does Not Own

Graph Extraction does not own:

- chunk creation
- chunk embeddings
- embedding-profile selection
- world-level source storage
- Qdrant node-vector writes
- Neo4j node or edge writes
- cross-chunk entity resolution
- final canonical graph identities
- graph retrieval or context assembly
- provider key policy beyond requesting scheduled credentials

## Normal Flow

Graph Extraction starts after chunk files and chunk embeddings complete for a book. Each work item reads `chunk_text` plus saved `overlap_text` from World Storage.

The chunk body is the only text allowed to justify new candidates. Overlap text exists only to help with nearby references such as pronouns, aliases, and titles.

For each non-empty chunk, VySol runs one initial extraction pass and then the run-locked number of gleaning passes. Passes inside one chunk are sequential because a glean depends on earlier saved results. Different chunks can run concurrently, but the provider key scheduler still controls credential availability and quota cooldowns.

Every trusted pass is parsed, merged into local raw candidates, and saved to the extraction manifest before the next provider call for that chunk starts. The saved manifest is the resume boundary. Graph Manifestation only starts when the book-level extraction result is completed.

## Inputs

Graph Extraction receives chunk files, overlap text, world graph defaults, run-snapshotted extraction config, prompt templates, provider credentials selected by the scheduler, model metadata, exact token-count responses, provider responses, and existing extraction manifests.

## Outputs

Graph Extraction produces raw node candidates, raw edge candidates, deterministic local candidate ids when run identity is available, saved pass history, chunk statuses, structured warnings or errors, and per-book extraction manifests.

## Saved State And Resume Behavior

The extraction manifest stores the world identity, ingestion run id, source metadata, run config, chunk states, pass state, raw nodes, raw edges, retry counters, and warnings. Resume uses that manifest to avoid rerunning trusted passes.

If a saved manifest belongs to another world/run/book/source, has a different chunk count, is corrupt, or contains stale chunk state, VySol rebuilds the affected extraction state from current chunk files. Existing incomplete chunks get a fresh retry budget on resume, but trusted saved passes are kept when their chunk state still matches the current chunk body.

## Retry, Pause, And Abort Behavior

Malformed JSON, invalid response schema, missing completion markers, and retryable non-rate-limit provider failures spend the normal per-pass retry budget. Non-retryable local provider failures stop the pass immediately.

Rate limits are reported to the [Provider Key Scheduler](../shared-backend-systems/provider-key-scheduler.md) and do not spend the same retry budget as malformed model output.

Pause is safe because trusted passes are saved before the next pass begins. If cancellation happens while a provider call is in flight, the scheduler reservation is abandoned and the chunk can remain pending or partial without spending a retry attempt for a late result.

## Failure Behavior

Oversized or uncountable provider inputs are blocked locally when exact counting is required. VySol must not send an extraction request after a failed exact-count preflight.

Missing extraction credentials leave extraction pending and return a warning so the ingestion run can be resumed later. Malformed provider-key files, unsupported extraction providers, unsupported extraction models, and missing required run identity are blocking errors.

Provider failures, parse failures, and manifest mismatch failures must leave enough saved state for resume, retry, or reset.

## User-Facing Behavior

Users currently see Graph Extraction through ingestion progress, structured book results, warnings, and saved world state. A book can finish chunking and embedding while graph extraction remains partial or pending because of missing keys, provider cooldowns, cancellation, parse failures, or token-limit blocks.

## System Interactions

Graph Extraction interacts with:

- [World Storage](world-storage.md), which stores chunks and extraction manifests
- [Text Splitting](text-splitting.md), which creates chunk text and overlap text
- [Vector Storage And Chunk Embeddings](vector-storage-and-chunk-embeddings.md), which prepares the world for extraction
- [Provider Key Scheduler](../shared-backend-systems/provider-key-scheduler.md), which schedules provider calls
- [Model Registry](../shared-backend-systems/model-registry.md), which supplies model limits and provider metadata
- [Graph Manifestation](graph-manifestation.md), which consumes completed raw candidates and writes graph/node-vector state

## Internal Edge Cases

- Empty or whitespace-only chunks are skipped without contacting a provider.
- A chunk can have a saved initial pass but unfinished glean passes.
- A model response can be valid JSON but still missing the required completion marker.
- A model response can include markdown fences or small text framing around the JSON and still be parsed if the required marker is present.
- Exact duplicate display names inside one chunk are merged, but only within that chunk.
- Similar names are not fuzzy-merged during raw extraction.
- Edges are kept only when both endpoints resolve to final local nodes from the same chunk result.
- Edge strength must be an integer from 1 through 10 or that edge is dropped.
- A saved edge from an earlier pass can become valid after a later glean adds the missing endpoint node.
- A chunk can be locally too large for the selected extraction model.
- Corrupt manifests, stale chunk hashes, invalid chunk statuses, missing initial passes on partial/extracted chunks, and excess saved glean passes can force chunk or manifest rebuild.
- Missing world/run identity marks extraction failed instead of writing ambiguous candidate ids.

## Cross-System Edge Cases

- Graph Extraction only runs after chunk embeddings complete; incomplete embeddings skip extraction for that book.
- Chunk layout changes, full-world re-ingest, or changed source content can stale extraction state and require rebuilding raw candidates.
- Missing extraction keys leave chunks pending so the active ingestion run can resume when credentials are available.
- Provider cooldowns can delay extraction without corrupting saved pass history.
- Paused runs keep the saved gleaning count and parser version, while future fresh calls can use updated provider/model/prompt defaults from the current graph config.
- Gleaning for a chunk continues with the provider/model/prompt snapshot from that chunk's saved initial pass.
- Graph Manifestation must not start from partial extraction manifests or treat raw extraction candidates as already written graph records.
- Entity resolution must not assume raw same-name candidates are already canonical entities.
- Token-limit checks must use provider-backed exact counting before Google generation requests.

## Implementation Landmarks

Graph extraction behavior lives under `backend/ingestion/graph_extraction`. Prompt defaults, parser behavior, provider adapters, storage, models, and service orchestration are separated inside that package. The ingestion stage wires extraction from `backend/ingestion/text_sources`, and graph manifestation consumes completed extraction manifests from `backend/ingestion/graph_manifestation`.

## What AI/Coders Must Check Before Changing This System

Before changing Graph Extraction, check prompt readability, parser completion rules, exact token preflight, run-snapshotted config, glean sequencing, manifest reset behavior, provider scheduler interaction, ingestion run completion rules, and manifestation expectations.

## Invariants That Must Not Be Broken

- Raw extraction is not graph manifestation.
- Every trusted pass must be saved before the next pass depends on it.
- Overlap text can help interpretation but must not justify new candidates by itself.
- Cross-chunk entity resolution must not happen inside raw extraction.
- Local raw candidate ids are backend-owned, not model-owned.
- Partial extraction manifests must remain resumable.
- Graph Manifestation must only consume completed extraction output.
- Exact provider token-count failure must block the generation request instead of falling back to an estimate.
