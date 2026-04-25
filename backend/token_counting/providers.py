"""Provider-aware token counting adapter registry."""

from __future__ import annotations

from typing import Protocol

from .errors import TokenCountingConfigurationError
from .models import TokenCountRequest, TokenCountResult


class TokenCounterAdapter(Protocol):
    """Runtime interface for provider-specific exact token counting."""

    def count_input_tokens(self, *, request: TokenCountRequest) -> TokenCountResult:
        """Return the exact provider token count for one request."""


def create_token_counter(provider_id: str) -> TokenCounterAdapter:
    """Create the runtime adapter for one provider token counter."""
    # BLOCK 1: Resolve the provider-specific token counter from a small runtime registry
    # WHY: Max input token enforcement must stay provider agnostic in shared logic, but exact counting is owned by each provider implementation
    if provider_id == "google":
        from backend.models.google_ai_studio.token_counting import GoogleAIStudioTokenCounter

        return GoogleAIStudioTokenCounter()

    raise TokenCountingConfigurationError(
        code="UNSUPPORTED_TOKEN_COUNTING_PROVIDER",
        message="The selected provider does not support exact input token counting in the backend.",
        details={"provider_id": provider_id},
    )
