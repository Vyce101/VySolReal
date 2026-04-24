"""Structured payloads for chunk similarity retrieval."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from backend.context.models import ModelContext


@dataclass(slots=True)
class RetrievalEvent:
    """Machine-readable retrieval warning or error."""

    code: str
    message: str
    severity: str = "warning"
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        if not self.details:
            payload.pop("details")
        return payload


@dataclass(slots=True)
class RetrievedChunk:
    """One chunk returned from vector similarity retrieval."""

    world_uuid: str
    point_id: str
    score: float
    book_number: int
    chunk_number: int
    chunk_position: str
    source_filename: str
    chunk_text: str
    overlap_text: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ChunkRetrievalResponse:
    """Top-level chunk retrieval response."""

    success: bool
    world_uuid: str | None
    requested_top_k: int
    top_k: int
    similarity_minimum: float
    embedded_chunk_count: int
    results: list[RetrievedChunk] = field(default_factory=list)
    model_context: ModelContext = field(default_factory=lambda: ModelContext(chunks=[], text=""))
    warnings: list[RetrievalEvent] = field(default_factory=list)
    errors: list[RetrievalEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "world_uuid": self.world_uuid,
            "requested_top_k": self.requested_top_k,
            "top_k": self.top_k,
            "similarity_minimum": self.similarity_minimum,
            "embedded_chunk_count": self.embedded_chunk_count,
            "results": [result.to_dict() for result in self.results],
            "model_context": self.model_context.to_dict(),
            "warnings": [warning.to_dict() for warning in self.warnings],
            "errors": [error.to_dict() for error in self.errors],
        }
