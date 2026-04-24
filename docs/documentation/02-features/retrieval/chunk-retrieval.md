# Chunk Retrieval

Chunk Retrieval finds the most similar embedded chunks inside one world and prepares chunk-text-only context for model calls.

## Why Chunk Retrieval Exists

VySol stores chunk vectors during ingestion so later systems can find relevant world text without rereading every chunk file. Chunk Retrieval is the first retrieval path built on top of that vector store.

It also exists as a simple base layer for later GraphRAG retrieval. Future retrieval can combine chunks, graph facts, summaries, memories, or scene state, while this page covers only the vector chunk path.

## How Chunk Retrieval Works

The caller passes a world directory, a query string, a maximum chunk count, and a similarity minimum. The default maximum chunk count is `10`, and the default similarity minimum is `0.15`. A maximum chunk count of `0` is valid and returns no chunks after the world metadata is validated.

Retrieval loads the world's locked embedding profile from World Storage. The query is embedded with the same provider, model, and output dimensions as the chunk vectors, but it uses the provider's query-specific embedding mode. For Google AI Studio, chunks use `RETRIEVAL_DOCUMENT` and queries use `RETRIEVAL_QUERY`.

The Qdrant query searches the collection for that locked embedding profile, filters by the world's UUID, and passes the similarity minimum directly as Qdrant's `score_threshold`. That means Qdrant returns up to the requested chunk count that already meet the threshold, instead of returning a limited set first and filtering it afterward.

```json
{
  "top_k": 10,
  "similarity_minimum": 0.15,
  "world_filter": {
    "world_uuid": "b1934f2b-7d5e-4e1f-9d55-7d7b4f454e42"
  }
}
```

Each returned vector point is then checked against the chunk file in World Storage. Qdrant provides the score and retrieval metadata. The chunk file provides the trusted `chunk_text` and `overlap_text`.

## What Retrieval Returns

The retrieval response has two useful pieces.

`results` is the rich debug and UI shape. It includes the world UUID, point id, score, book number, chunk number, chunk position, source filename, chunk text, and overlap text.

`model_context` is the model-facing shape. It only includes `chunk_text`. Overlap text, source names, scores, and positions are kept out of this context so callers do not accidentally send retrieval metadata as prompt context.

## Repair Behavior

If Qdrant points to a missing chunk file, that result is skipped and the chunk is marked pending in the embedding manifest. A future resume action can repair it.

If Qdrant's stored text hash does not match the current chunk file, that result is skipped, the stale Qdrant point is deleted, and the chunk is marked pending in the embedding manifest. This keeps retrieval from returning text that does not match the vector.

## Why It Works This Way

Chunk Retrieval keeps Qdrant as the vector index and World Storage as the source of truth for chunk text. That preserves the existing storage split: Qdrant finds likely chunks quickly, while file-backed world storage remains inspectable, resumable, and repairable.

The retrieval layer returns both rich results and clean model context because those are different jobs. The future UI needs scores and source metadata. The model needs only the selected chunk text.
