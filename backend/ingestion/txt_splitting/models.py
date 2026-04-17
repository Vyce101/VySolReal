"""Shared models for TXT splitter ingestion."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import IngestionError


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
    source_filename: str
    book_number: int
    total_chunks: int
    last_completed_chunk: int
    chunk_states: list[ChunkState]
    warnings: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "world_id": self.world_id,
            "source_filename": self.source_filename,
            "book_number": self.book_number,
            "total_chunks": self.total_chunks,
            "last_completed_chunk": self.last_completed_chunk,
            "chunk_states": [state.to_dict() for state in self.chunk_states],
            "warnings": list(self.warnings),
        }

    @classmethod
    def create(
        cls,
        *,
        world_id: str,
        source_filename: str,
        book_number: int,
        total_chunks: int,
    ) -> "BookManifest":
        return cls(
            world_id=world_id,
            source_filename=source_filename,
            book_number=book_number,
            total_chunks=total_chunks,
            last_completed_chunk=0,
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
            source_filename=str(payload["source_filename"]),
            book_number=int(payload["book_number"]),
            total_chunks=int(payload["total_chunks"]),
            last_completed_chunk=int(payload.get("last_completed_chunk", 0)),
            chunk_states=chunk_states,
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

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class IngestionResult:
    """Top-level structured response."""

    success: bool
    world_id: str | None
    world_path: str | None
    books: list[BookIngestionResult] = field(default_factory=list)
    warnings: list[OperationEvent] = field(default_factory=list)
    errors: list[IngestionError] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "world_id": self.world_id,
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
