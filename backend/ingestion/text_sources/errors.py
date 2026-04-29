"""Structured errors for TXT splitter ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class IngestionError(Exception):
    """Machine-readable ingestion error."""

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
