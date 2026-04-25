"""Shared max-input enforcement backed by provider-exact token counting."""

from __future__ import annotations

from .errors import MaxInputTokensExceededError
from .models import TokenCountRequest, TokenCountResult
from .providers import create_token_counter


def count_input_tokens(*, request: TokenCountRequest) -> TokenCountResult:
    """Return the exact input token count for one provider request."""
    # BLOCK 1: Delegate exact token counting to the provider-specific adapter selected by provider id
    # WHY: The backend needs one shared entrypoint for future chat and embeddings, while each provider owns the mechanics of counting its own request shape
    return create_token_counter(request.provider_id).count_input_tokens(request=request)


def ensure_within_max_input_tokens(
    *,
    request: TokenCountRequest,
    max_input_tokens: int | None,
) -> TokenCountResult | None:
    """Count tokens exactly and raise if they exceed the configured maximum."""
    # BLOCK 1: Skip counting entirely when no max input token limit applies to this request
    # WHY: Exact token counting adds latency and should only run when the request must be blocked against a real max-input contract
    if max_input_tokens is None:
        return None

    # BLOCK 2: Count the provider input exactly, then fail closed when it exceeds the configured maximum
    # VARS: count_result = exact provider token count for the request that would be sent
    # WHY: VySol must never trim silently or fall back to guesswork once a provider has exact counting support, so over-limit requests are blocked before generation or embedding
    count_result = count_input_tokens(request=request)
    if count_result.total_tokens > max_input_tokens:
        raise MaxInputTokensExceededError(
            code="MAX_INPUT_TOKENS_EXCEEDED",
            message="The request exceeds the configured maximum input token limit.",
            details={
                "provider_id": request.provider_id,
                "model_id": request.model_id,
                "max_input_tokens": max_input_tokens,
                "total_tokens": count_result.total_tokens,
            },
        )
    return count_result
