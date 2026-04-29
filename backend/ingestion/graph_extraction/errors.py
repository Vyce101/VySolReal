"""Structured errors for graph extraction."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class GraphExtractionError(Exception):
    """Machine-readable graph extraction failure."""

    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        # BLOCK 1: Return a compact API-safe error payload with details only when they exist
        # WHY: Extraction failures need the same machine-readable shape as ingestion and embedding errors without forcing callers to parse exception text
        payload: dict[str, object] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class GraphExtractionParseError(GraphExtractionError):
    """Raised when a provider response cannot become trusted extraction data."""
