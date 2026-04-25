---
order: 200
---

# Neo4j Graph Store

Neo4j Graph Store is the graph database layer reserved for finalized extracted nodes and edges with traversal-friendly structure and source provenance.

## Why It Needs Its Own Store

Graph queries and vector retrieval solve different problems. Qdrant is good at nearest-neighbor search across vectors. It is not the right place to model chains of relationships, inspect graph neighborhoods, or store richly linked node and edge records with graph-native traversal behavior.

World Storage is also not the right place for that job. World Storage keeps local files, manifests, and resumable extraction state. It is meant to be inspectable and repairable, not to answer graph queries efficiently.

That is why the graph side deserves its own store just like the vector side already does.

## What The Graph Store Holds

Every finalized graph write carries the world UUID, the ingestion run id, source file metadata, and chunk-level provenance so the graph can always be traced back to the text that produced it.

Nodes carry backend-owned UUIDs plus their extracted display names and descriptions. Edges carry backend-owned UUIDs plus:

- `source_node_id`
- `target_node_id`
- `source_display_name`
- `target_display_name`
- `description`
- `strength`
- source file and chunk metadata

The important detail is that edge endpoints point to the local extracted node UUIDs chosen by the backend, not to any older node that only shares the same display name. Cross-chunk and cross-book merging is a later entity-resolution problem, not something the raw graph store should guess from names alone.

The current writer uses generic raw labels and relationship types: `:ExtractedNode` for nodes and `:EXTRACTED_RELATION` for edges. That keeps the raw graph honest about what it is today: extracted candidates, not already-resolved canonical entities.

## Current Boundary

Today the runtime writes raw candidates into `graph_extraction.json`, then records manifestation progress in `graph_manifestation.json`.

After raw extraction completes for a book, VySol embeds extracted node candidates with the world's locked embedding profile, stores those node vectors in a separate Qdrant node collection, and batch-writes graph nodes and relationships to Neo4j.

The local Neo4j bootstrap is also real. `run.bat` prepares a portable Neo4j Community database under ignored `user/` folders and starts it without installing a Windows service. If Neo4j is not available, ingestion can still finish chunking, chunk embeddings, and raw extraction, while [Graph Manifestation](../world-ingestion-pipeline/graph-manifestation.md) remains pending for resume.

Raw extraction success, node-side manifestation, and edge-side manifestation are not the same truth. A node is manifested only after its Qdrant node vector and Neo4j node write both succeed. An edge is manifested only after both endpoint nodes are manifested and the Neo4j relationship write succeeds.

Neo4j cleanup is chunk-aware too. When one chunk's extracted candidates change on a redo, the manifestation layer deletes only the Neo4j records for that exact `world_uuid + ingestion_run_id + book_number + chunk_number` boundary and then rewrites the surviving candidates for that chunk from pending state.

If Neo4j is temporarily unavailable, manifestation leaves those writes pending with a warning instead of marking them failed immediately. If Neo4j rejects a dependency-ready edge batch for another write reason, the manifestation layer retries that batch on later passes until the retry budget is exhausted.

## Why Neo4j Stays Separate From The Other Stores

[World Storage](../world-ingestion-pipeline/world-storage.md) keeps source copies, chunk files, config, manifests, and raw extraction state.

[Qdrant Vector Store](qdrant-vector-store.md) keeps vectors and retrieval metadata.

Neo4j Graph Store keeps traversable graph facts.

That split keeps each layer understandable:

- World Storage answers, "What files, configs, and resumable local state exist?"
- Qdrant answers, "What vectors can I search?"
- Neo4j answers, "What graph entities and relationships can I traverse?"
