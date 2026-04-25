"""Google AI Studio exact input token counting helpers."""

from __future__ import annotations

from google import genai
from google.genai import types

from backend.models.registry import load_default_model_registry
from backend.token_counting.errors import (
    TokenCountingConfigurationError,
    TokenCountingProviderError,
)
from backend.token_counting.models import TokenCountRequest, TokenCountResult


class GoogleAIStudioTokenCounter:
    """Count Google AI Studio input tokens through the provider SDK."""

    def count_input_tokens(self, *, request: TokenCountRequest) -> TokenCountResult:
        """Return the exact Google token count for one request."""
        # BLOCK 1: Resolve the shared catalog model into the Google call name and provider-safe countTokens config
        # VARS: shared_model = registry entry that owns the provider call name for this stable model id, count_config = optional Google token counting config built only from request fields the SDK supports here
        # WHY: Future chat and current embeddings should share one Google token counter, so model lookup must stay tied to the shared catalog instead of a second runtime-only allowlist
        shared_model = load_default_model_registry().get_model(request.model_id)
        if shared_model is None or shared_model.provider_id != request.provider_id:
            raise TokenCountingConfigurationError(
                code="GOOGLE_TOKEN_COUNT_MODEL_UNSUPPORTED",
                message="The selected Google model is not available for exact token counting.",
                details={"provider_id": request.provider_id, "model_id": request.model_id},
            )
        count_config = _count_tokens_config_from_request(request)

        # BLOCK 2: Call Google's countTokens endpoint with the same credential and model that the real request would use
        # WHY: Max input token enforcement is only trustworthy when the provider counts the exact request shape VySol intends to send, using the same model selection path as generation or embedding
        client = None
        try:
            client = genai.Client(api_key=request.credential.api_key)
            response = client.models.count_tokens(
                model=shared_model.call_name,
                contents=request.contents,
                config=count_config,
            )
        except Exception as exc:
            raise TokenCountingProviderError(
                code="GOOGLE_TOKEN_COUNT_FAILED",
                message="VySol could not count the Google AI Studio input tokens exactly, so the request was blocked.",
                details={
                    "provider_id": request.provider_id,
                    "model_id": request.model_id,
                    "reason": str(exc),
                },
            ) from exc
        finally:
            if client is not None:
                client.close()

        # BLOCK 3: Fail closed when Google returns no total token count instead of guessing from text length
        # WHY: Once exact counting support exists, missing provider count data should block the request rather than reviving the old heuristic path
        total_tokens = getattr(response, "total_tokens", None)
        if total_tokens is None:
            raise TokenCountingProviderError(
                code="GOOGLE_TOKEN_COUNT_EMPTY",
                message="Google AI Studio did not return a total token count, so the request was blocked.",
                details={"provider_id": request.provider_id, "model_id": request.model_id},
            )
        return TokenCountResult(total_tokens=int(total_tokens))


def _count_tokens_config_from_request(request: TokenCountRequest) -> types.CountTokensConfig | None:
    # BLOCK 1: Build the optional Google countTokens config only from shared request fields that affect prompt tokenization here
    # WHY: System instructions and tools can change chat token counts, but embeddings currently send only contents and should avoid unnecessary config noise
    if request.system_instruction is None and not request.tools:
        return None
    return types.CountTokensConfig(
        system_instruction=request.system_instruction,
        tools=list(request.tools),
    )
