---
order: 500
---

# Graph Manifestation

Graph Manifestation is the resumable backend stage that takes completed raw extraction candidates and turns them into confirmed node vectors plus traversable Neo4j graph records.

## Why It Exists

Raw extraction is not the same thing as a usable graph. `graph_extraction.json` proves that the app saved trusted node and edge candidates, but it does not prove those nodes were embedded, that Neo4j accepted them, or that relationships were safe to write.

This layer exists so VySol can keep those later graph writes resumable too. Node vectors, Neo4j nodes, and Neo4j edges can all fail in different ways, so manifestation needs its own saved progress instead of pretending raw extraction already finished the whole job.

## How Manifestation Starts

Manifestation starts only after `graph_extraction.json` reports a completed book. From there, VySol loads or creates a per-book `graph_manifestation.json` file in World Storage and reconciles it against the current extracted candidates.

That manifestation manifest keeps separate progress for:

- node embeddings into Qdrant
- Neo4j node writes
- Neo4j edge writes
- dependency-driven edge waiting and failure states

```json
{
  "world_uuid": "b1934f2b-7d5e-4e1f-9d55-7d7b4f454e42",
  "ingestion_run_id": "run-2026-04-25-001",
  "book_number": 1,
  "source_filename": "chapter-one.txt",
  "node_states": [
    {
      "node_id": "node-1",
      "node_embedding_status": "embedded",
      "neo4j_node_status": "written",
      "status": "manifested"
    }
  ],
  "edge_states": [
    {
      "edge_id": "edge-1",
      "status": "waiting_dependency"
    }
  ]
}
```

## How Node Manifestation Works

Each extracted node becomes one node-embedding work item. The embedding text is the node's `display_name`, then two newlines, then its `description`. That text is embedded with the world's locked embedding profile, and the resulting vector is stored in a graph-node collection inside [Qdrant Vector Store](../storage-layers/qdrant-vector-store.md).

A node is only considered manifested after two things both succeed:

1. its Qdrant node vector is written
2. its Neo4j `:ExtractedNode` record is written

If the vector write succeeds but the Neo4j node write does not, the vector remains trusted and the Neo4j side stays resumable. That split keeps one successful storage layer from being thrown away just because the other one had a temporary problem.

## How Edge Manifestation Works

Edges are not embedded. They wait for both endpoint nodes to be fully manifested first.

That creates three important edge-side states:

- `pending`: the edge is ready or nearly ready to be written
- `waiting_dependency`: one or both endpoint nodes are still pending
- `failed_dependency`: one or both endpoint nodes failed manifestation

If a later manifestation pass repairs the endpoint nodes, those dependency states can move back to `pending` and the edge can be written normally. That recovery matters because edge success depends on node success, not only on the edge batch itself.

When dependency-ready edges are written, Neo4j writes them in batches. If Neo4j is temporarily unavailable, those edges stay pending with a warning so the run can resume later. If Neo4j rejects the batch for another write reason, VySol increments the edge retry count and keeps the edge pending until the retry budget is exhausted. Only then does the edge become a true failed write.

## How Chunk Redo Cleanup Works

Manifestation cleanup is chunk-aware, not just candidate-id-aware.

When one chunk's extracted node or edge set changes, VySol compares chunk-level candidate fingerprints, marks that chunk as stale, and deletes only the manifestation outputs for that exact chunk boundary:

- stale node vectors from Qdrant
- stale Neo4j records for that chunk

The delete boundary is `world_uuid + ingestion_run_id + book_number + chunk_number`. After that cleanup, the surviving candidates from that chunk are rebuilt from pending state and written again. This is what keeps chunk redo from leaving a manifest that says a node or edge is done even though its backing storage was already deleted during cleanup.

## Why It Stays Separate

[Knowledge Graph Extraction Pipeline](knowledge-graph-extraction-pipeline.md) is the raw candidate stage.

[Qdrant Vector Store](../storage-layers/qdrant-vector-store.md) is the vector layer.

[Neo4j Graph Store](../storage-layers/neo4j-graph-store.md) is the traversable graph layer.

Graph Manifestation is the bridge that turns one into the others without collapsing those jobs into one file or one storage system. That separation keeps failures easier to inspect, cleanup more targeted, and resume behavior much more trustworthy.
