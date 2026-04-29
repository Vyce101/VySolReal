---
order: 500
---

# Graph Manifestation

Graph Manifestation is the resumable backend stage that turns completed raw graph extraction candidates into graph-node vectors and traversable Neo4j graph records.

It is not the same thing as extraction. Extraction creates trusted raw candidates. Manifestation proves which of those candidates have been embedded, written as graph nodes, and connected as graph relationships.

## Why Graph Manifestation Exists

VySol needs a separate boundary between "the model extracted these candidates" and "the local graph systems can use these candidates." Qdrant node-vector writes and Neo4j graph writes can fail independently, so extraction completion cannot be treated as graph-storage completion.

Graph Manifestation exists to make that boundary explicit, resumable, and safe to inspect after interruption.

## Who This Page Is For

This page is for contributors, power users, and AI coding agents that need to change graph-node embedding, Qdrant node-vector writes, Neo4j node or relationship writes, manifestation manifests, graph cleanup, or manifestation resume behavior.

## What Graph Manifestation Owns

Graph Manifestation owns:

- per-book `graph_manifestation.json` state
- graph-node embedding work for extracted node candidates
- Qdrant graph-node vector writes
- Neo4j extracted-node writes
- Neo4j extracted-relationship writes
- edge dependency tracking against manifested endpoint nodes
- chunk-scoped cleanup when raw graph candidates change
- manifestation warnings and result summaries

## What Graph Manifestation Does Not Own

Graph Manifestation does not own:

- source-file ingestion
- text splitting
- chunk vector creation
- raw graph extraction prompts, parsing, or gleaning
- provider key scheduling policy
- model registry metadata
- cross-chunk entity resolution
- canonical entity merging
- Neo4j startup or credential creation

## Normal Flow

Manifestation starts only after the book's graph extraction manifest is completed. The service loads the completed extraction manifest, then loads or creates the matching manifestation manifest for that book.

Each extracted node becomes one node state. VySol embeds the node text, writes the node vector to the Qdrant node-vector collection, then writes the corresponding `ExtractedNode` record to Neo4j. A node is only considered manifested after both the vector write and the Neo4j node write are confirmed.

Edges are not embedded. An edge waits until both endpoint nodes are manifested. When both endpoint nodes are ready, VySol writes the relationship to Neo4j as an extracted relationship.

The manifestation manifest records node embedding status, Neo4j node status, edge dependency status, retry counts, warnings, and summary counts so later runs can continue from saved state.

## Inputs

Graph Manifestation receives:

- completed graph extraction manifests
- world metadata and the locked embedding profile
- existing manifestation manifests
- node embedding provider outcomes
- Qdrant node-vector store state
- Neo4j graph-writer outcomes
- provider key scheduler availability through the node embedder

## Outputs

Graph Manifestation produces:

- graph-node vectors in Qdrant
- `ExtractedNode` records in Neo4j
- extracted relationship records in Neo4j
- per-book manifestation manifests
- book-level manifestation result summaries
- warning events when graph storage is unavailable
- chunk-scoped cleanup requests for stale graph outputs

## Saved State And Resume Behavior

The manifestation manifest is separate from the extraction manifest because manifestation can be partially complete even when extraction is fully complete.

If node vectors are already saved, a later manifestation pass does not embed those nodes again. If node vectors are saved but Neo4j node writes are still pending, a later pass retries only the Neo4j side before edge writes can proceed.

If the existing manifestation manifest matches the same world, ingestion run, book, and raw candidate fingerprints, VySol preserves trusted node and edge state. If a chunk's raw candidate fingerprint changes, manifestation resets that chunk's graph outputs and rebuilds its candidates from pending state.

## Retry, Pause, And Abort Behavior

Manifestation is safe to pause or resume because each write boundary saves state after confirmed progress.

Node embedding failures, Neo4j node write failures, and Neo4j edge write failures use retry counts inside the manifestation manifest. Failures that reach the retry limit are treated as failed for that pass, but later manifestation runs can reset those failed states and try again after credentials, provider availability, or Neo4j health changes.

Neo4j unavailability is handled differently from a rejected write. When Neo4j is missing, stopped, or unreachable, manifestation leaves graph writes pending and records a warning instead of turning completed extraction output into a hard failure.

## Failure Behavior

Manifestation rejects missing or incomplete extraction manifests before starting vector or graph writes.

Qdrant node-vector write failure is blocking because the node cannot be safely marked embedded until the vector store confirms the write.

Neo4j unavailability leaves node or edge writes pending. Neo4j write rejection can mark node or edge states failed after retries, but it must not rewrite raw extraction state as failed extraction.

Corrupt manifestation manifests are reset and rebuilt from the completed extraction manifest, with a warning saved in the new manifestation state.

## User-Facing Behavior

The current behavior is backend-facing. Users and future UI surfaces see manifestation through ingestion result summaries, warning events, and saved progress state rather than through a dedicated graph manifestation screen.

A book can report partial ingestion completion when extraction finished but graph manifestation is still pending or failed.

## System Interactions

Graph Manifestation interacts with:

- [Knowledge Graph Extraction Pipeline](knowledge-graph-extraction-pipeline.md), which produces completed raw node and edge candidates
- [World Storage](world-storage.md), which stores per-book manifestation manifests beside ingestion outputs
- [Vector Storage And Chunk Embeddings](vector-storage-and-chunk-embeddings.md), which provides the locked embedding profile used for node vectors
- [Qdrant Vector Store](../storage-layers/qdrant-vector-store.md), which stores graph-node vectors in profile-specific node collections
- [Neo4j Graph Store](../storage-layers/neo4j-graph-store.md), which stores extracted nodes and relationships
- [Provider Key Scheduler](../shared-backend-systems/provider-key-scheduler.md), which schedules node embedding calls through shared provider credentials

## Internal Edge Cases

- The extraction manifest can be missing or incomplete, so manifestation must reject it before writes start.
- A manifestation manifest can be corrupt, so it is rebuilt from completed extraction output.
- A node vector can be written while its Neo4j node write remains pending.
- A node embedder can return neither a vector nor a failure for a node, so the node is marked with a missing-embedding failure.
- An edge can wait because one or both endpoint nodes are not manifested yet.
- An edge can fail by dependency when an endpoint node fails.
- A dependency-ready edge write can fail and remain retryable until its retry budget is exhausted for that pass.
- A chunk's raw candidates can change, so stale node vectors and Neo4j rows for that chunk must be cleaned before rebuilt candidates are trusted.

## Cross-System Edge Cases

- Qdrant node vectors and Qdrant chunk vectors share local vector storage but must stay in separate profile-specific collections.
- Provider key cooldowns or missing eligible credentials can pause node embedding without corrupting extraction or chunk embedding state.
- Neo4j can be unavailable after extraction and node-vector work complete, so graph writes must remain pending without invalidating raw extraction.
- Full-world re-ingest must clean old chunk vectors, node vectors, Neo4j rows, and derived book outputs while preserving stored source copies.
- Future entity resolution must treat manifested records as extracted candidates, not canonical entities.

## Implementation Landmarks

Graph manifestation orchestration, models, adapters, storage helpers, structured errors, and Neo4j writing live under `backend/ingestion/graph_manifestation`.

Node-vector persistence is implemented through the Qdrant node-vector helpers in `backend/embeddings`. Ingestion wires manifestation after graph extraction through the text-source ingestion service.

## What AI/Coders Must Check Before Changing This System

Before changing Graph Manifestation, check:

- extraction manifest completion checks
- manifestation manifest reconciliation
- node embedding status transitions
- Neo4j node status transitions
- edge dependency transitions
- retry-count reset behavior across later passes
- chunk-scoped stale cleanup
- Qdrant node collection separation from chunk collections
- Neo4j unavailable versus Neo4j write-failed behavior
- ingestion's definition of a fully completed book pipeline

## Invariants That Must Not Be Broken

- Raw extraction completion is not manifestation completion.
- Manifestation must only consume completed extraction manifests.
- A node is manifested only after both its node vector and Neo4j node write are confirmed.
- An edge must not be written before both endpoint nodes are manifested.
- Node vectors and chunk vectors must not share the same Qdrant collection contract.
- Cleanup must stay scoped to the world, ingestion run, book, and chunk boundary being rebuilt.
- Neo4j unavailability must leave graph writes resumable instead of corrupting extraction state.
- Manifestation must not perform canonical entity resolution.
