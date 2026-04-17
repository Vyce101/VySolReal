"""TXT splitter ingestion feature."""

from .models import IngestionResult, SplitterConfig
from .service import ingest_sources, ingest_sources_into_existing_world

__all__ = [
    "IngestionResult",
    "SplitterConfig",
    "ingest_sources",
    "ingest_sources_into_existing_world",
]
