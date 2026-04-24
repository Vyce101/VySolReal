"""Structured errors for provider key loading and scheduling."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ProviderKeyConfigurationError(Exception):
    """Machine-readable provider key configuration error."""

    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, object]:
        # BLOCK 1: Return a compact API-safe error payload with details only when they exist
        # WHY: Provider key failures need the same machine-readable shape as ingestion and embedding errors without forcing every caller to know this exception class
        payload: dict[str, object] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload
