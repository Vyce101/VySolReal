"""Build model-facing context from richer retrieval outputs."""

from __future__ import annotations

from .models import ModelContext


def build_model_context_from_chunk_texts(chunk_texts: list[str]) -> ModelContext:
    """Build model context from chunk text only."""
    # BLOCK 1: Keep only retrieved chunk text in the model-facing context payload
    # WHY: Retrieval results carry source metadata and overlap text for UI/debugging, but the current model contract intentionally sends only chunk text as context
    clean_chunks = [chunk_text for chunk_text in chunk_texts if chunk_text]
    return ModelContext(
        chunks=clean_chunks,
        text="\n\n".join(clean_chunks),
    )
