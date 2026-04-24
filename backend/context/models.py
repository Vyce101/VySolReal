"""Shared model-facing context payloads."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class ModelContext:
    """Text that is safe to send to a model."""

    chunks: list[str]
    text: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
