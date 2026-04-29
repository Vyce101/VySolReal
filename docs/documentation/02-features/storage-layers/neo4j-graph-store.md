---
order: 100
---

# Neo4j Graph Store

Neo4j Graph Store is VySol's local graph database write layer for manifested extracted nodes and relationships.

It stores graph-shaped records after graph extraction and graph manifestation have accepted them. The current records are raw extracted graph candidates with provenance, not canonical merged entities.

## Why Neo4j Graph Store Exists

Vector search and graph traversal solve different problems. Qdrant can find similar vectors, but it is not designed to inspect relationship neighborhoods, traverse graph paths, or store graph-native relationships.

Neo4j exists so manifested graph records have a dedicated graph store instead of being hidden inside JSON manifests or vector payloads. This keeps graph persistence separate from vector search while preserving enough source context for later graph traversal work.

## Who This Page Is For

This page is for contributors, power users, and AI coding agents that need to understand Neo4j writes, local Neo4j startup assumptions, graph provenance, manifestation behavior, or the boundary between Neo4j and Qdrant.

## What Neo4j Graph Store Owns

Neo4j Graph Store owns:

- persistence of manifested extracted nodes as Neo4j nodes
- persistence of manifested extracted relationships as Neo4j relationships
- schema support for the extracted-node merge key and relationship lookup
- chunk-scoped deletion of Neo4j graph records during manifestation reconciliation
- write-level confirmation or structured graph-store failure

## What Neo4j Graph Store Does Not Own

Neo4j Graph Store does not own:

- raw graph extraction
- graph manifestation orchestration
- node embedding generation
- Qdrant node-vector storage
- manifestation resume state
- chunk files
- source files
- canonical entity resolution
- provider calls
- graph retrieval or traversal APIs

## Normal Flow

Graph Manifestation writes to Neo4j only after the graph extraction manifest for a book is completed. Manifestation embeds extracted nodes first, stores their node vectors in Qdrant, then writes the corresponding extracted nodes to Neo4j.

Relationships are written after both endpoint nodes are fully manifested. If either endpoint is still pending or failed, the relationship stays waiting or fails by dependency instead of creating partial graph structure.

Neo4j upserts nodes by stable extracted node id and writes relationships between matching endpoint nodes in the same world. Each graph write carries world identity, ingestion run identity, source metadata, book metadata, chunk metadata, hashes, and backend-owned candidate ids so graph records remain traceable to the text that produced them.

## Inputs

Neo4j Graph Store receives:

- node write payloads from Graph Manifestation
- relationship write payloads from Graph Manifestation
- chunk deletion requests during stale-candidate cleanup
- local connection settings from the launcher bootstrap path
- extracted ids, display names, descriptions, strengths, hashes, and provenance fields

## Outputs

Neo4j Graph Store produces:

- persisted `ExtractedNode` records
- persisted `EXTRACTED_RELATION` relationships
- successful write completion back to Graph Manifestation
- structured unavailable or write-failed errors when Neo4j cannot accept a write

## Saved State And Resume Behavior

Neo4j is not the source of truth for extraction or manifestation progress. Graph Manifestation stores resume state in its own per-book manifestation manifest and uses that state to decide which node and relationship writes still need Neo4j.

If Neo4j is unavailable, manifestation keeps affected graph writes pending and records a warning. If a chunk's extracted candidates change, manifestation asks the graph writer to delete Neo4j records for that exact world, ingestion run, book, and chunk before rewriting the fresh candidates.

Neo4j itself keeps graph records, but it does not decide whether a book is completed, partial, failed, or safe to resume.

## Retry / Pause / Abort Behavior

Neo4j unavailability is treated as resumable. Node writes and edge writes can remain pending so another manifestation pass can continue after Neo4j becomes available.

Non-availability Neo4j write failures use Graph Manifestation's retry budget. Dependency-ready edge write failures remain pending until retries are exhausted; dependency-blocked edges wait for endpoint nodes instead of burning write retries.

## Failure Behavior

Temporary Neo4j unavailability must leave graph writes pending for resume. The adapter classifies driver unavailability, transient driver errors, operating-system connection errors, and missing optional driver support as graph-store unavailability where appropriate.

If Neo4j rejects a node or edge batch for a non-availability reason, Graph Manifestation records the structured write error on the affected node or edge states and keeps the result partial until the retry path succeeds or the retry budget is exhausted.

If Neo4j is unavailable during stale chunk cleanup, manifestation records a warning and defers graph cleanup instead of pretending the delete succeeded.

## System Interactions

Neo4j Graph Store interacts with:

- [Graph Manifestation](../world-ingestion-pipeline/graph-manifestation.md), which owns write orchestration and resume state
- [Knowledge Graph Extraction Pipeline](../world-ingestion-pipeline/knowledge-graph-extraction-pipeline.md), which produces raw candidates before manifestation
- [World Storage](../world-ingestion-pipeline/world-storage.md), which stores raw and manifestation manifests
- [Qdrant Vector Store](qdrant-vector-store.md), which stores node vectors separately from graph relationships
- local launcher scripts, which prepare a portable Neo4j runtime before backend graph writes need it

## User-Facing Behavior

Users may see graph work remain pending when Neo4j is unavailable. Chunking, chunk embeddings, and raw extraction can still complete while graph manifestation waits for Neo4j.

The local launcher prepares Neo4j as a portable local process with runtime files under ignored local folders. It does not install Neo4j as a Windows service.

## Internal Edge Cases

- The Neo4j driver package can be missing, so the adapter must fail as graph-store unavailable instead of making imports fail globally.
- Neo4j schema setup can fail before any node or edge write, so initialization errors must be classified through the same graph-store error boundary.
- A node can have a confirmed Qdrant vector while its Neo4j node write is still pending.
- A relationship can wait when one endpoint node is not yet manifested.
- A relationship can fail by dependency when one endpoint node reaches a terminal failed state.
- A chunk redo can require deletion of only that chunk's graph records for the same world, run, book, and chunk number.
- Local Neo4j runtime data can exist without a usable connection file; startup must stop instead of generating a new password that cannot unlock the existing database.

## Cross-System Edge Cases

- Neo4j writes depend on Graph Manifestation, not directly on raw extraction.
- Graph Manifestation must not start from an incomplete graph extraction manifest.
- Qdrant and Neo4j can temporarily disagree because node vectors are written before Neo4j nodes.
- Edge writes must respect node embedding and Neo4j node status because both storage systems define whether an endpoint is fully manifested.
- Manifestation reconciliation must clean stale Qdrant node vectors and stale Neo4j chunk records together when extracted candidates change.
- Entity resolution and future graph retrieval must not treat Neo4j extracted nodes as canonical merged entities.
- Source provenance must remain consistent with World Storage, extraction manifests, manifestation manifests, Qdrant node vectors, and Neo4j records.

## Implementation Landmarks

Neo4j write behavior lives in graph manifestation modules under `backend/ingestion/graph_manifestation`. The Neo4j adapter owns driver calls and Cypher write statements. The manifestation service owns ordering, retry state, dependency state, and stale chunk cleanup.

Local startup support is handled by launcher and bootstrap scripts under `scripts`, with runtime data kept under ignored local folders.

## What AI/Coders Must Check Before Changing This System

Before changing Neo4j Graph Store, check manifestation state transitions, endpoint dependency rules, retry behavior, chunk cleanup boundaries, local startup assumptions, schema setup, graph provenance fields, and Qdrant coordination.

## Invariants That Must Not Be Broken

- Neo4j stores graph structure, not vector search data.
- Neo4j graph records are extracted candidates, not canonical merged entities.
- Nodes must be upserted by stable backend-owned extracted node ids.
- Relationships must connect existing backend-owned extracted node ids in the same world.
- Relationships must not be written before both endpoint nodes are manifested.
- Graph records must remain traceable to world, run, source, book, and chunk provenance.
- Raw extraction success must not be treated as Neo4j write success.
- Qdrant vector success must not be treated as Neo4j write success.
- Chunk-scoped cleanup must not delete records from another world, ingestion run, book, or chunk.
