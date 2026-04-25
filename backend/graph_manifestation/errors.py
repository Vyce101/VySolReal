"""Structured errors for graph manifestation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class GraphManifestationError(Exception):
    """Machine-readable graph manifestation failure."""

    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, object]:
        # BLOCK 1: Return a compact API-safe error payload with details only when they exist
        # WHY: The future UI needs stable codes and optional details without parsing exception strings
        payload: dict[str, object] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class GraphManifestationConfigurationError(GraphManifestationError):
    """Raised when manifestation cannot trust the requested inputs."""


class NodeEmbeddingManifestationError(GraphManifestationError):
    """Raised when node embedding work cannot be persisted."""


class GraphStoreUnavailable(GraphManifestationError):
    """Raised when Neo4j cannot be reached and the run should remain resumable."""


class GraphStoreWriteError(GraphManifestationError):
    """Raised when Neo4j rejects a write for a non-availability reason."""
