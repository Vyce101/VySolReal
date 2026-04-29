---
order: 100
---

# Qdrant Vector Store

Qdrant Vector Store is VySol's local vector database layer for embedded chunk vectors and manifested graph-node vectors. It stores vectors and retrieval metadata in local Qdrant collections. It is the vector store, not the source-text store or the graph relationship store.

## Why Qdrant Vector Store Exists

VySol needs fast similarity search over vectors without rereading every chunk file. Qdrant provides the vector index, score filtering, and payload filtering that chunk retrieval uses to find candidate text.

It also gives vector persistence its own truth boundary. World Storage can prove that chunk files exist. Qdrant can prove that a vector point exists for the current chunk text, embedding profile, and ingestion run.

## Who This Page Is For

This page is for contributors, power users, and AI coding agents that need to change vector collection naming, point ids, vector payloads, retrieval filtering, stale-vector cleanup, or graph-node vector storage.

## What Qdrant Vector Store Owns

Qdrant Vector Store owns:

- local on-disk Qdrant collections
- chunk vector points
- graph-node vector points
- vector payload metadata
- profile-specific collection separation
- chunk vector search by world UUID and score threshold
- stale vector deletion when called by higher-level systems

## What Qdrant Vector Store Does Not Own

Qdrant Vector Store does not own:

- full chunk text authority
- source files
- chunk boundary creation
- embedding provider calls
- graph relationships
- Neo4j traversal
- provider key scheduling

## Normal Flow

Chunk embedding opens the local Qdrant store, selects a collection derived from the world's locked embedding profile, and creates the collection if it does not exist yet. Worlds that share one compatible profile can share a collection, while `world_uuid` in the payload keeps their records isolated.

Each chunk uses a stable point id derived from the world UUID, book number, and chunk number. The text hash is payload metadata, not part of the point id, so updated text overwrites one logical point instead of creating a second logical chunk record.

Before a chunk is treated as embedded, the embedding workflow upserts the vector and waits for Qdrant confirmation. Only after that write succeeds does the embedding manifest mark the chunk as embedded.

Chunk Retrieval searches the matching profile collection, filters by `world_uuid`, and passes the similarity threshold into the Qdrant query. It then verifies returned points against World Storage before returning chunk text.

Graph Manifestation also stores extracted node vectors in graph-node collections. Those vectors share the local Qdrant database but remain separate from chunk vectors because they have a different payload contract.

## Inputs

Qdrant receives vectors, point ids, embedding profile metadata, world UUIDs, ingestion run ids, chunk metadata, graph-node metadata, text hashes, search vectors, filters, and score thresholds.

## Outputs

Qdrant returns vector upsert confirmations, retrieved point payloads, similarity scores, missing-point evidence, and delete confirmations that higher-level workflows use to update manifests.

## Saved State And Resume Behavior

Qdrant state is reconciled against embedding manifests and World Storage. A manifest alone is not enough to trust a vector; the expected point must exist and match the current text hash and ingestion run.

If the embedding manifest is missing but Qdrant still has valid matching points, the embedding workflow can rebuild progress without re-embedding the chunks. If Qdrant is missing a point that the manifest claimed was embedded, the workflow marks that chunk pending so it can be re-embedded.

If a point belongs to an older run or no longer matches the current text hash, higher-level systems delete or overwrite it before marking the current work complete.

## Failure Behavior

If Qdrant is unavailable, rejects a write, rejects a read, or rejects a delete, the vector store raises a structured vector-store error. The calling workflow must leave the relevant manifest state pending or failed instead of claiming vector persistence succeeded.

If retrieval finds stale or missing chunk backing data, the result is skipped and the embedding state is marked for repair. If a stale retrieved point has the wrong text hash, retrieval deletes that point before marking the chunk pending.

## System Interactions

Qdrant Vector Store interacts with:

- [Vector Storage And Chunk Embeddings](../world-ingestion-pipeline/vector-storage-and-chunk-embeddings.md), which writes chunk vectors
- [Graph Manifestation](../world-ingestion-pipeline/graph-manifestation.md), which writes graph-node vectors
- [World Storage](../world-ingestion-pipeline/world-storage.md), which owns trusted text and manifests
- [Chunk Retrieval](../retrieval/chunk-retrieval.md), which searches chunk vectors
- [Neo4j Graph Store](neo4j-graph-store.md), which owns traversable graph relationships

## Internal Edge Cases

- A Qdrant collection can only serve one vector shape, so a requested profile whose dimensions do not match an existing collection is a collection mismatch.
- Qdrant access is invalid until a chunk or node collection has been selected from an embedding profile.
- Different embedding providers, models, dimensions, task types, or profile versions must resolve to different compatible collection names.
- Stable point ids can point at stale payload metadata after a text or run-boundary change, so hash and run id checks must happen before trusting saved progress.
- Empty point-id lists are valid no-op reads or deletes.
- Chunk vectors and graph-node vectors must not be treated as interchangeable records, even when their embedding dimensions match.

## Cross-System Edge Cases

- World Storage can have chunks while Qdrant is missing vectors.
- Qdrant can have vectors while the backing chunk file has changed or disappeared; retrieval must skip those results and mark embedding state for repair.
- A missing embedding manifest can be rebuilt from Qdrant only when the Qdrant point still matches the current chunk hash and ingestion run.
- A changed world embedding profile requires full-world re-ingest instead of appending into the old profile's vector contract.
- A canceled embedding run can receive late provider results, but those results must not advance Qdrant or manifest state after cancellation.
- Graph Manifestation can have node vectors while Neo4j writes are still pending, so Qdrant node persistence must not be treated as graph persistence.
- Retrieval must verify Qdrant results against World Storage before returning text because Qdrant payloads intentionally do not duplicate full chunk text.

## Implementation Landmarks

Qdrant chunk and node store behavior lives under `backend/embeddings`. Chunk retrieval uses the store from `backend/retrieval/chunks`. Graph manifestation writes node vectors through ingestion graph-manifestation code that calls the shared embedding vector-store helpers.

## What AI/Coders Must Check Before Changing This System

Before changing Qdrant Vector Store, check collection naming, vector dimensions, point id stability, payload fields, world UUID filters, text-hash reconciliation, ingestion-run reconciliation, retrieval repair behavior, cancellation behavior, and graph-node vector separation.

## Invariants That Must Not Be Broken

- Qdrant is not the authority for full chunk text.
- Collection shape must match embedding profile dimensions.
- World isolation must use stable world identity.
- A vector write must be confirmed before the calling manifest marks it complete.
- Chunk vectors and graph-node vectors are separate concepts even when they share Qdrant.
