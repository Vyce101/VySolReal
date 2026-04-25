"""Structured errors for provider token counting."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TokenCountingError(Exception):
    """Machine-readable token counting error."""

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


class TokenCountingConfigurationError(TokenCountingError):
    """Configuration mismatch while preparing exact token counting."""


class TokenCountingProviderError(TokenCountingError):
    """Provider-side failure while counting tokens."""


class MaxInputTokensExceededError(TokenCountingError):
    """Exact input token count exceeded the configured maximum."""
