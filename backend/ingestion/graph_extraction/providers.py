"""Runtime graph extraction provider adapter registry."""

from __future__ import annotations

import time
from typing import Protocol

from google import genai
from google.genai.errors import APIError

from backend.logger import get_logger
from backend.models.google_ai_studio.errors import parse_google_ai_studio_api_error
from backend.models.registry import load_default_model_registry
from backend.provider_keys.models import ProviderCredential
from backend.token_counting import (
    MaxInputTokensExceededError,
    TokenCountRequest,
    TokenCountingError,
    ensure_within_max_input_tokens,
)

from .errors import GraphExtractionError
from .models import ExtractionProviderFailure, ExtractionProviderSuccess, GraphExtractionConfig

logger = get_logger(__name__)


class GraphExtractionProviderAdapter(Protocol):
    """Runtime interface for provider-specific graph extraction calls."""

    def extract(
        self,
        *,
        credential: ProviderCredential,
        config: GraphExtractionConfig,
        prompt: str,
        log_context: dict[str, object],
    ) -> ExtractionProviderSuccess | ExtractionProviderFailure:
        """Run one extraction or gleaning prompt."""


class GoogleAIStudioGraphExtractionProvider:
    """Google AI Studio graph extraction adapter."""

    def extract(
        self,
        *,
        credential: ProviderCredential,
        config: GraphExtractionConfig,
        prompt: str,
        log_context: dict[str, object],
    ) -> ExtractionProviderSuccess | ExtractionProviderFailure:
        # BLOCK 1: Resolve the shared model call name before any provider work starts
        # VARS: context_text = safe log-only metadata string
        # WHY: Extraction should reuse the shared model catalog so model ids, limits, key scheduling, and runtime provider calls stay aligned with embeddings
        model = load_default_model_registry().get_model(config.model_id)
        if model is None or "chat" not in model.surfaces:
            raise GraphExtractionError(
                code="UNSUPPORTED_EXTRACTION_MODEL",
                message="The selected extraction model is not available for chat-style graph extraction.",
                details={"model_id": config.model_id},
            )
        context_text = _format_log_context(log_context)

        # BLOCK 2: Count the exact Google input tokens before sending the extraction or gleaning prompt
        # VARS: token_count = exact provider-reported token count or a structured local failure
        # WHY: Google does not enforce max input through the generation config here, so oversized prompts must be blocked locally before a billable provider request is sent
        token_count = _count_prompt_tokens(
            credential=credential,
            config=config,
            prompt=prompt,
            max_input_tokens=model.limits.get("maxInputTokens"),
            context_text=context_text,
        )
        if isinstance(token_count, ExtractionProviderFailure):
            return token_count

        # BLOCK 3: Send the counted prompt through the Google generation API
        # VARS: request_started_at = monotonic timestamp used to report provider boundary duration
        # WHY: The provider request should only happen after model support and exact input size have both been verified
        client = genai.Client(api_key=credential.api_key)
        request_started_at = time.perf_counter()
        logger.info(
            "Graph extraction request started for provider=%s credential=%s quota_scope=%s model=%s prompt_length=%s input_tokens=%s%s.",
            credential.provider_id,
            credential.display_name,
            credential.quota_scope,
            config.model_id,
            len(prompt),
            token_count,
            context_text,
        )
        try:
            response = client.models.generate_content(
                model=model.call_name,
                contents=prompt,
            )
        except APIError as error:
            logger.warning(
                "Graph extraction request failed with provider API error for credential=%s quota_scope=%s model=%s status=%s duration_ms=%s%s.",
                credential.display_name,
                credential.quota_scope,
                config.model_id,
                error.code,
                int((time.perf_counter() - request_started_at) * 1000),
                context_text,
            )
            error_info = parse_google_ai_studio_api_error(error)
            return ExtractionProviderFailure(
                credential_name=credential.display_name,
                quota_scope=credential.quota_scope,
                code=error_info.code,
                message=error_info.message,
                retryable=error_info.retryable,
                rate_limit_type=error_info.rate_limit_type,
                rate_limit_scope=error_info.rate_limit_scope,
                retry_after_seconds=error_info.retry_after_seconds,
                billable_token_estimate=token_count,
            )
        except Exception as exc:
            safe_reason = type(exc).__name__
            logger.error(
                "Graph extraction request crashed for credential=%s quota_scope=%s model=%s duration_ms=%s error_type=%s%s.",
                credential.display_name,
                credential.quota_scope,
                config.model_id,
                int((time.perf_counter() - request_started_at) * 1000),
                safe_reason,
                context_text,
            )
            return ExtractionProviderFailure(
                credential_name=credential.display_name,
                quota_scope=credential.quota_scope,
                code="EXTRACTION_PROVIDER_FAILED",
                message="The graph extraction provider request failed before returning a usable response.",
                retryable=True,
                billable_token_estimate=token_count,
            )
        finally:
            client.close()

        logger.info(
            "Graph extraction request completed for credential=%s quota_scope=%s model=%s duration_ms=%s%s.",
            credential.display_name,
            credential.quota_scope,
            config.model_id,
            int((time.perf_counter() - request_started_at) * 1000),
            context_text,
        )
        return ExtractionProviderSuccess(
            response_text=response.text or "",
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
        )


_PROVIDER_FACTORIES = {
    "google": GoogleAIStudioGraphExtractionProvider,
}


def create_graph_extraction_provider(provider_id: str) -> GraphExtractionProviderAdapter:
    """Create the runtime adapter for one graph extraction provider."""
    # BLOCK 1: Resolve the provider-specific graph extraction adapter from a small backend runtime registry
    # WHY: Shared model metadata can describe chat models before runtime support exists, so execution must fail clearly instead of falling back to a Google-specific implementation
    provider_factory = _PROVIDER_FACTORIES.get(provider_id)
    if provider_factory is None:
        raise GraphExtractionError(
            code="UNSUPPORTED_EXTRACTION_PROVIDER",
            message="The selected extraction provider is not supported by the backend graph extraction runtime.",
            details={"provider_id": provider_id},
        )
    return provider_factory()


def _format_log_context(context: dict[str, object]) -> str:
    # BLOCK 1: Render only caller-approved metadata into provider logs
    # WHY: Chunk text, overlap text, raw responses, and keys must never enter logs, so the provider helper accepts explicit safe metadata instead of inspecting prompt content
    if not context:
        return ""
    return " " + " ".join(f"{key}={value}" for key, value in context.items())


def _count_prompt_tokens(
    *,
    credential: ProviderCredential,
    config: GraphExtractionConfig,
    prompt: str,
    max_input_tokens: int | None,
    context_text: str,
) -> int | ExtractionProviderFailure:
    # BLOCK 1: Block graph extraction if the selected model has no max-input limit to check against
    # WHY: A missing limit would turn exact counting into advisory metadata, but this path needs a hard pre-send guard like chunk and query embeddings
    if max_input_tokens is None:
        logger.error(
            "Graph extraction request was blocked because the selected model has no max-input limit: credential=%s quota_scope=%s model=%s%s.",
            credential.display_name,
            credential.quota_scope,
            config.model_id,
            context_text,
        )
        return ExtractionProviderFailure(
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            code="EXTRACTION_TOKEN_LIMIT_MISSING",
            message="The selected graph extraction model has no configured max input token limit.",
            retryable=False,
        )

    # BLOCK 2: Ask the provider token counter to validate the exact prompt shape used by generation
    # VARS: token_request = provider-agnostic token counting request, count_result = exact provider-reported input tokens
    # WHY: Local estimates can drift from Google tokenization, so the prompt must use the same shared exact counter that guards embeddings
    token_request = TokenCountRequest(
        provider_id=config.provider_id,
        model_id=config.model_id,
        credential=credential,
        contents=prompt,
    )
    try:
        count_result = ensure_within_max_input_tokens(request=token_request, max_input_tokens=max_input_tokens)
    except MaxInputTokensExceededError as error:
        total_tokens = _int_detail(error.details.get("total_tokens"))
        logger.warning(
            "Graph extraction request was blocked because the exact input token count exceeded the model limit: credential=%s quota_scope=%s model=%s input_tokens=%s max_input_tokens=%s%s.",
            credential.display_name,
            credential.quota_scope,
            config.model_id,
            total_tokens,
            max_input_tokens,
            context_text,
        )
        return ExtractionProviderFailure(
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            code="EXTRACTION_PROMPT_TOO_LARGE",
            message="The graph extraction prompt exceeds the selected model's max input token limit.",
            retryable=False,
            billable_token_estimate=total_tokens,
        )
    except TokenCountingError as error:
        logger.error(
            "Graph extraction request was blocked because exact token counting failed: credential=%s quota_scope=%s model=%s error_code=%s%s.",
            credential.display_name,
            credential.quota_scope,
            config.model_id,
            error.code,
            context_text,
        )
        return ExtractionProviderFailure(
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            code="EXTRACTION_TOKEN_COUNT_FAILED",
            message="The graph extraction request was blocked because VySol could not count provider input tokens exactly.",
            retryable=False,
        )
    return count_result.total_tokens


def _int_detail(value: object) -> int | None:
    # BLOCK 1: Convert structured error details into an optional integer for logs and manifests
    # WHY: Token-count provider errors can come from external boundaries, so defensive conversion avoids masking the original local block reason
    if isinstance(value, int):
        return value
    return None
