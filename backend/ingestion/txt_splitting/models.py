"""Shared models for TXT splitter ingestion."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from backend.embeddings.models import EmbeddingBookResult

from .errors import IngestionError

if TYPE_CHECKING:
    from backend.graph_extraction.models import GraphExtractionBookResult
    from backend.graph_manifestation.models import GraphManifestationBookResult


@dataclass(slots=True, frozen=True)
class SplitterConfig:
    """Runtime configuration for character-based splitting."""

    chunk_size: int
    max_lookback: int
    overlap_size: int

    def __post_init__(self) -> None:
        if self.chunk_size <= 0:
            raise IngestionError(
                code="INVALID_CHUNK_SIZE",
                message="Chunk size must be greater than zero.",
            )
        if self.max_lookback < 0:
            raise IngestionError(
                code="INVALID_LOOKBACK",
                message="Maximum lookback must be zero or greater.",
            )
        if self.overlap_size < 0:
            raise IngestionError(
                code="INVALID_OVERLAP",
                message="Overlap size must be zero or greater.",
            )

    def to_dict(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "SplitterConfig":
        return cls(
            chunk_size=int(payload["chunk_size"]),
            max_lookback=int(payload["max_lookback"]),
            overlap_size=int(payload["overlap_size"]),
        )


@dataclass(slots=True)
class OperationEvent:
    """Structured non-fatal event."""

    code: str
    message: str
    severity: str = "warning"
    book_number: int | None = None
    source_filename: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(slots=True)
class ChunkDraft:
    """In-memory chunk draft before persistence."""

    chunk_number: int
    total_chunks: int
    overlap_text: str
    chunk_text: str


@dataclass(slots=True)
class ChunkRecord:
    """Persisted chunk payload."""

    world_id: str
    world_uuid: str
    source_filename: str
    book_number: int
    chunk_number: int
    chunk_position: str
    overlap_text: str
    chunk_text: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ChunkState:
    """Manifest state for a chunk."""

    chunk_number: int
    chunk_file: str
    completed: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class BookManifest:
    """Per-book progress metadata."""

    world_id: str
    world_uuid: str
    source_filename: str
    book_number: int
    total_chunks: int
    last_completed_chunk: int
    chunk_states: list[ChunkState]
    splitter_config: SplitterConfig | None = None
    warnings: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "world_id": self.world_id,
            "world_uuid": self.world_uuid,
            "source_filename": self.source_filename,
            "book_number": self.book_number,
            "total_chunks": self.total_chunks,
            "last_completed_chunk": self.last_completed_chunk,
            "splitter_config": self.splitter_config.to_dict() if self.splitter_config is not None else None,
            "chunk_states": [state.to_dict() for state in self.chunk_states],
            "warnings": list(self.warnings),
        }

    @classmethod
    def create(
        cls,
        *,
        world_id: str,
        world_uuid: str,
        source_filename: str,
        book_number: int,
        total_chunks: int,
        splitter_config: SplitterConfig,
    ) -> "BookManifest":
        return cls(
            world_id=world_id,
            world_uuid=world_uuid,
            source_filename=source_filename,
            book_number=book_number,
            total_chunks=total_chunks,
            last_completed_chunk=0,
            splitter_config=splitter_config,
            chunk_states=[
                ChunkState(
                    chunk_number=index,
                    chunk_file=f"book_{book_number:02d}_chunk_{index:04d}.json",
                )
                for index in range(1, total_chunks + 1)
            ],
        )

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "BookManifest":
        chunk_states = [
            ChunkState(**state_payload)  # type: ignore[arg-type]
            for state_payload in payload.get("chunk_states", [])
        ]
        return cls(
            world_id=str(payload["world_id"]),
            world_uuid=str(payload.get("world_uuid", payload["world_id"])),
            source_filename=str(payload["source_filename"]),
            book_number=int(payload["book_number"]),
            total_chunks=int(payload["total_chunks"]),
            last_completed_chunk=int(payload.get("last_completed_chunk", 0)),
            chunk_states=chunk_states,
            splitter_config=SplitterConfig.from_dict(dict(payload["splitter_config"])) if payload.get("splitter_config") is not None else None,
            warnings=list(payload.get("warnings", [])),
        )

    def append_warning(self, event: OperationEvent) -> None:
        warning = event.to_dict()
        if warning not in self.warnings:
            self.warnings.append(warning)


@dataclass(slots=True)
class BookIngestionResult:
    """Book-level ingestion result."""

    book_number: int
    source_filename: str
    total_chunks: int
    completed_chunks: int
    manifest_path: str
    chunk_paths: list[str]
    embedding: EmbeddingBookResult | None = None
    graph_extraction: GraphExtractionBookResult | None = None
    graph_manifestation: GraphManifestationBookResult | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        if self.embedding is not None:
            payload["embedding"] = self.embedding.to_dict()
        if self.graph_extraction is not None:
            payload["graph_extraction"] = self.graph_extraction.to_dict()
        if self.graph_manifestation is not None:
            payload["graph_manifestation"] = self.graph_manifestation.to_dict()
        return payload


@dataclass(slots=True)
class IngestionResult:
    """Top-level structured response."""

    success: bool
    world_id: str | None
    world_uuid: str | None
    world_path: str | None
    books: list[BookIngestionResult] = field(default_factory=list)
    warnings: list[OperationEvent] = field(default_factory=list)
    errors: list[IngestionError] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "world_id": self.world_id,
            "world_uuid": self.world_uuid,
            "world_path": self.world_path,
            "books": [book.to_dict() for book in self.books],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "errors": [error.to_dict() for error in self.errors],
        }


@dataclass(slots=True, frozen=True)
class StoredSourcePaths:
    """World-local storage paths for a source file."""

    primary_path: Path
    backup_path: Path
    source_filename: str
