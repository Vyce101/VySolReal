---
order: 500
---

# World Storage

World Storage is the file-backed persistence boundary for one VySol world. It keeps the world's stable identity, saved source copies, generated book outputs, world-level ingestion locks, active run state, and per-stage manifests together so ingestion can resume without depending on the user's original file locations.

## Why World Storage Exists

VySol ingestion is not one database write. A world can move through source copying, text splitting, embeddings, graph extraction, graph manifestation, vector writes, and graph writes at different speeds.

World Storage exists to keep those stages inspectable and recoverable. It lets later systems decide whether a book, chunk, vector, extraction pass, or manifestation result is current for the active world contract instead of guessing from loose files or external stores.

## Who This Page Is For

This page is for contributors, power users, and AI coding agents that need to change ingestion, resume behavior, embeddings, retrieval, graph extraction, graph manifestation, or world lifecycle code.

Use it as system-contract context before editing code that reads or writes world-owned files.

## What World Storage Owns

World Storage owns:

- stable world identity through `world_uuid`
- editable display metadata such as the world name
- the locked embedding profile for the world
- the locked splitter settings once they exist for the world
- the active ingestion run id and run status
- preserved per-book source copies and backup copies
- generated chunk files and per-book chunk progress
- world-level graph extraction defaults
- per-book embedding, graph extraction, and graph manifestation manifests
- the local file boundary that tells ingestion systems which derived outputs belong to the same world

## What World Storage Does Not Own

World Storage does not own:

- provider key selection, cooldowns, or quota scheduling
- model/provider catalog metadata
- embedding request execution
- Qdrant collection internals or similarity search behavior
- Neo4j graph traversal behavior
- graph extraction prompt content or parsing rules
- user-facing troubleshooting instructions
- Hub-only presentation metadata, which is kept separately from ingestion locks

## Normal Flow

A new world ingestion run starts by validating the requested splitter settings, embedding profile, and provider-key availability before creating the world folder. If the world name already exists, creation is rejected instead of overwriting existing storage.

After the world boundary exists, VySol writes or loads `world.json`. That file stores the stable `world_uuid`, the locked embedding profile, the splitter lock, and the active ingestion run state.

Each selected source is copied into world-owned storage as a working copy and a backup copy before derived work starts. Book slots are numbered inside the world, and appending new sources uses the next unused slot instead of reusing old source, backup, or output folders.

Text splitting writes chunk files and advances the per-book chunk manifest only after each chunk file is safely saved. Embeddings, graph extraction, and graph manifestation then write their own per-book manifests beside the chunk output instead of overwriting chunk progress.

The active run id is reused while the world run is active or paused. It is marked completed only when the current book pipeline has completed embeddings, graph extraction, and graph manifestation. If work is still pending or fails in a resumable way, the run remains paused for the next resume attempt.

Full-world re-ingest is the dedicated path for rebuilding a world from its stored source copies. It can replace the locked splitter settings or embedding profile, clears old derived outputs, starts a new run id, and preserves the saved source artifacts.

## Inputs

World Storage receives source files, world names, splitter settings, embedding profile choices, graph extraction defaults, per-book source copies, chunk records, ingestion run state, and stage manifests produced by ingestion systems.

## Outputs

World Storage produces `world.json`, preserved source and backup files, generated chunk files, per-book progress manifests, graph config state, and stage-specific manifests that other systems use to decide whether work is complete, pending, stale, or safe to resume.

## Saved State And Resume Behavior

World Storage is the main resume boundary for ingestion. It saves world identity and active run state at the world level, then saves chunking, embedding, extraction, and manifestation progress at the per-book level.

Resume uses the active run id to keep unfinished chunk embeddings, raw graph extraction, and graph manifestation under the same world run. If the run was paused, the next ingestion attempt reactivates the same run id instead of creating a new one.

Normal existing-world ingestion must match the world's locked splitter settings and embedding profile. If those locks differ, the caller must use full-world re-ingest.

## Failure Behavior

World Storage reports blocking failures through structured ingestion errors. It does not silently overwrite duplicate worlds, missing sources, incompatible world locks, ambiguous stored sources, unsupported source types, empty source text, conversion failures, or filesystem write failures.

When the working source copy disappears during splitting, ingestion can switch to the backup copy and record a warning. If both the working copy and backup are missing, the book is blocked because the world no longer has a trusted source for that slot.

If a run starts and later hits a blocking ingestion error, the active run is marked paused rather than completed so later resume can continue from the saved boundary.

## System Interactions

World Storage interacts with:

- [Text Splitting](text-splitting.md), which creates chunk files and chunk progress
- [Vector Storage And Chunk Embeddings](vector-storage-and-chunk-embeddings.md), which reads chunk text and writes embedding progress
- [Knowledge Graph Extraction Pipeline](knowledge-graph-extraction-pipeline.md), which reads chunk text and writes raw extraction manifests
- [Graph Manifestation](graph-manifestation.md), which reads raw candidates and writes manifestation progress
- [Qdrant Vector Store](../storage-layers/qdrant-vector-store.md), which stores vectors derived from world-owned records
- [Neo4j Graph Store](../storage-layers/neo4j-graph-store.md), which stores manifested graph records derived from world-owned candidates
- the local Hub API, which can read world metadata for display but keeps UI-only metadata separate from ingestion locks

## Internal Edge Cases

- The visible world name can change, so durable identity must use `world_uuid`.
- Duplicate world names are rejected before ingestion writes over existing storage.
- A world can have an active, paused, or completed ingestion run; paused runs keep the same run id for resume.
- Existing worlds without a splitter lock can be backfilled the first time a caller provides one.
- Existing worlds with older embedding metadata can be normalized to the current locked model maxima.
- Book numbering must consider source folders, backup folders, and derived output folders so append cannot reuse an occupied slot.
- Stored source lookup is blocking when the working and backup folders are ambiguous or both missing.
- Working source loss is recoverable only while a backup copy still exists.
- Chunk manifests must not advance ahead of the chunk files they describe.
- Full-world re-ingest removes derived outputs while preserving stored source and backup copies.

## Cross-System Edge Cases

- Embedding state can become stale when chunk text, run id, or locked embedding profile no longer matches.
- Graph extraction state can become stale when chunk layout, run id, parser version, or extraction settings no longer matches.
- Graph manifestation state can become stale when extracted candidates or chunk ownership changes.
- Retrieval must treat chunk files as the source of truth for chunk text, even when Qdrant payloads exist.
- Provider-key failures can leave embedding, extraction, or manifestation work pending without completing the world run.
- Missing Neo4j can leave graph manifestation partial and keep the world run paused for resume.
- Graph extraction defaults can be changed only while the world is paused or idle, not while the run is active.
- Replacing a stored source through normal append is rejected; source replacement belongs to full-world re-ingest.
- Full-world re-ingest may clean local files and vector state even when old Neo4j rows cannot be deleted because the graph store is unavailable.

## Implementation Landmarks

World Storage behavior mainly lives in `backend/ingestion/text_sources`, `backend/embeddings/storage.py`, `backend/embeddings/models.py`, `backend/ingestion/graph_extraction`, and `backend/ingestion/graph_manifestation`. World-facing display loading lives near the backend world API.

## What AI/Coders Must Check Before Changing This System

Before changing World Storage, check whether the change affects:

- `world_uuid` identity
- `world.json` lock or run-state behavior
- source-copy and backup-copy preservation
- append-safe book numbering
- atomic chunk or manifest writes
- active, paused, and completed run transitions
- full-world re-ingest cleanup
- embedding manifest reconciliation
- graph extraction manifest reset behavior
- graph manifestation cleanup and resume behavior
- Qdrant and Neo4j stale-output cleanup
- local-path or user-data exposure in tracked docs or code

## Invariants That Must Not Be Broken

- `world_uuid` is the durable storage identity; display names are editable metadata.
- Source copies and backup copies must not be deleted by normal derived-output cleanup.
- Normal existing-world ingestion must not change the locked splitter settings or embedding profile.
- Full-world re-ingest is the path that can replace locked splitter settings or the locked embedding profile.
- Chunk files are the source of truth for chunk text.
- A manifest must not mark work complete before the backing output is confirmed.
- Chunk, embedding, extraction, and manifestation progress must remain separately inspectable.
- Unfinished work must preserve the active run boundary for resume.
- Normal append must not replace a stored source with different bytes.
- Implementation and documentation must not depend on absolute local source paths after ingestion starts.
