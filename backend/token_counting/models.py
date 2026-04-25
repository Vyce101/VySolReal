"""Shared token counting request and response models."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.provider_keys.models import ProviderCredential


@dataclass(slots=True, frozen=True)
class TokenCountRequest:
    """Provider-agnostic request shape for exact input token counting."""

    provider_id: str
    model_id: str
    credential: ProviderCredential
    contents: object
    system_instruction: object | None = None
    tools: tuple[object, ...] = field(default_factory=tuple)


@dataclass(slots=True, frozen=True)
class TokenCountResult:
    """Exact input token count returned by a provider-aware counter."""

    total_tokens: int
