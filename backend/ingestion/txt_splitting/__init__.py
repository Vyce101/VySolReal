"""TXT splitter ingestion feature."""

from .models import IngestionResult, SplitterConfig

__all__ = [
    "IngestionResult",
    "SplitterConfig",
    "ingest_sources",
    "ingest_sources_into_existing_world",
]


def __getattr__(name: str):
    # BLOCK 1: Load service entrypoints only when callers ask for them
    # WHY: Storage helpers are imported by embeddings, and eager service imports create a circular path before embeddings.storage finishes initializing
    if name in {"ingest_sources", "ingest_sources_into_existing_world"}:
        from .service import ingest_sources, ingest_sources_into_existing_world

        return {
            "ingest_sources": ingest_sources,
            "ingest_sources_into_existing_world": ingest_sources_into_existing_world,
        }[name]
    raise AttributeError(name)
