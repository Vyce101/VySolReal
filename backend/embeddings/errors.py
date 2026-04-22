"""Structured errors for embedding storage and ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class EmbeddingError(Exception):
    """Machine-readable embedding error."""

    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


class EmbeddingConfigurationError(EmbeddingError):
    """Configuration or contract mismatch for embeddings."""


class EmbeddingProviderError(EmbeddingError):
    """Provider failure while generating embeddings."""


class VectorStoreError(EmbeddingError):
    """Vector store failure while reading or writing embeddings."""
