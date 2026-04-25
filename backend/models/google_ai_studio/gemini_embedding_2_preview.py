"""Google AI Studio runtime adapter for Gemini Embedding 2."""

from __future__ import annotations

from backend.embeddings.models import (
    EmbeddingFailure,
    EmbeddingProfile,
    EmbeddingSuccess,
    EmbeddingWorkItem,
    QueryEmbeddingFailure,
    QueryEmbeddingSuccess,
)
from backend.logger import get_logger
from backend.models.google_ai_studio.embedding_client import (
    GoogleEmbeddingError,
    GoogleEmbeddingRequest,
    embed_content,
)
from backend.provider_keys.models import ProviderCredential
from backend.token_counting import (
    MaxInputTokensExceededError,
    TokenCountRequest,
    TokenCountingError,
    ensure_within_max_input_tokens,
)

logger = get_logger(__name__)


class GoogleAIStudioEmbeddingProvider:
    """Generate Gemini Embedding 2 vectors through Google AI Studio."""

    def embed_text(
        self,
        *,
        credential: ProviderCredential,
        profile: EmbeddingProfile,
        work_item: EmbeddingWorkItem,
    ) -> EmbeddingSuccess | EmbeddingFailure:
        # BLOCK 1: Count the exact Google embedding input and block locally when it exceeds the world's locked max token budget
        # VARS: counted_tokens = exact Google token count for the chunk text that would be sent to the provider
        # WHY: The embedding contract must block oversized input without falling back to the old /4 heuristic once Google exact counting support exists
        counted_tokens_or_failure = _count_chunk_tokens(
            credential=credential,
            profile=profile,
            work_item=work_item,
        )
        if isinstance(counted_tokens_or_failure, EmbeddingFailure):
            return counted_tokens_or_failure
        counted_tokens = counted_tokens_or_failure

        # BLOCK 2: Send one chunk text to the Google embedding model using the world's locked embedding profile
        # WHY: The ingestion contract is one text per request, which keeps failure isolation clear and lets retry logic reason about one chunk at a time
        outcome = embed_content(
            GoogleEmbeddingRequest(
                credential=credential,
                profile=profile,
                text=work_item.chunk_text,
                task_type=profile.task_type,
                request_label="Chunk",
                empty_error_code="EMBEDDING_PROVIDER_EMPTY",
                empty_error_message="The embedding provider returned no vector values for the chunk.",
                log_context={
                    "book": work_item.book_number,
                    "chunk": work_item.chunk_number,
                },
            )
        )
        if isinstance(outcome, GoogleEmbeddingError):
            return _failure_from_api_error(
                credential=credential,
                work_item=work_item,
                error=outcome,
                counted_input_tokens=counted_tokens,
            )
        return EmbeddingSuccess(
            work_item=work_item,
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            vector=outcome.vector,
            billable_character_count=outcome.billable_character_count,
        )

    def embed_query(
        self,
        *,
        credential: ProviderCredential,
        profile: EmbeddingProfile,
        query: str,
    ) -> QueryEmbeddingSuccess | QueryEmbeddingFailure:
        # BLOCK 1: Count the exact Google query input and block locally when it exceeds the world's locked max token budget
        # VARS: counted_tokens = exact Google token count for the query text that would be sent to the provider
        # WHY: Retrieval should use the same exact Google token enforcement path as chunk embeddings instead of reviving the old character-count estimate
        counted_tokens_or_failure = _count_query_tokens(
            credential=credential,
            profile=profile,
            query=query,
        )
        if isinstance(counted_tokens_or_failure, QueryEmbeddingFailure):
            return counted_tokens_or_failure
        counted_tokens = counted_tokens_or_failure

        outcome = embed_content(
            GoogleEmbeddingRequest(
                credential=credential,
                profile=profile,
                text=query,
                task_type="RETRIEVAL_QUERY",
                request_label="Query",
                empty_error_code="RETRIEVAL_QUERY_PROVIDER_EMPTY",
                empty_error_message="The embedding provider returned no vector values for the query.",
                log_context={},
            )
        )
        if isinstance(outcome, GoogleEmbeddingError):
            return _query_failure_from_api_error(
                credential=credential,
                query=query,
                error=outcome,
                counted_input_tokens=counted_tokens,
            )
        return QueryEmbeddingSuccess(
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            vector=outcome.vector,
            billable_character_count=outcome.billable_character_count,
        )


def _failure_from_api_error(
    *,
    credential: ProviderCredential,
    work_item: EmbeddingWorkItem,
    error: GoogleEmbeddingError,
    counted_input_tokens: int,
) -> EmbeddingFailure:
    # BLOCK 1: Convert the shared Google provider error shape into the embedding workflow failure contract
    # WHY: Google rate-limit parsing is provider-wide behavior, while chunk retry state and manifest updates belong to the embedding workflow
    return EmbeddingFailure(
        work_item=work_item,
        credential_name=credential.display_name,
        quota_scope=credential.quota_scope,
        code=f"EMBEDDING_PROVIDER_{error.code_suffix}",
        message=error.message,
        retryable=error.retryable,
        rate_limit_type=error.rate_limit_type,
        rate_limit_scope=error.rate_limit_scope,
        retry_after_seconds=error.retry_after_seconds,
        billable_token_estimate=counted_input_tokens,
    )


def _query_failure_from_api_error(
    *,
    credential: ProviderCredential,
    query: str,
    error: GoogleEmbeddingError,
    counted_input_tokens: int,
) -> QueryEmbeddingFailure:
    # BLOCK 1: Convert the shared Google provider error shape into the retrieval query failure contract
    # WHY: Retrieval owns query-level error reporting, while provider-wide rate-limit parsing should remain shared with chunk embedding calls
    return QueryEmbeddingFailure(
        credential_name=credential.display_name,
        quota_scope=credential.quota_scope,
        code=f"RETRIEVAL_QUERY_PROVIDER_{error.code_suffix}",
        message=error.message,
        retryable=error.retryable,
        rate_limit_type=error.rate_limit_type,
        rate_limit_scope=error.rate_limit_scope,
        retry_after_seconds=error.retry_after_seconds,
        billable_token_estimate=counted_input_tokens,
    )


def _count_chunk_tokens(
    *,
    credential: ProviderCredential,
    profile: EmbeddingProfile,
    work_item: EmbeddingWorkItem,
) -> int | EmbeddingFailure:
    # BLOCK 1: Run the shared exact max-input enforcement path for one chunk embedding request
    # WHY: Shared enforcement keeps the provider-specific Google counter behind a provider-agnostic interface that future chat or provider adapters can reuse
    try:
        count_result = ensure_within_max_input_tokens(
            request=TokenCountRequest(
                provider_id=profile.provider_id,
                model_id=profile.model_id,
                credential=credential,
                contents=work_item.chunk_text,
            ),
            max_input_tokens=profile.max_input_tokens,
        )
    except MaxInputTokensExceededError as error:
        counted_tokens = int(error.details.get("total_tokens", 0))
        logger.error(
            "Embedding request blocked because chunk exceeds locked max_input_tokens for model=%s book=%s chunk=%s counted_tokens=%s max_input_tokens=%s.",
            profile.model_id,
            work_item.book_number,
            work_item.chunk_number,
            counted_tokens,
            profile.max_input_tokens,
        )
        return EmbeddingFailure(
            work_item=work_item,
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            code="EMBEDDING_CHUNK_TOO_LARGE",
            message="The chunk exceeds the locked maximum input token limit for this embedding model.",
            retryable=False,
            billable_token_estimate=counted_tokens,
        )
    except TokenCountingError as error:
        logger.error(
            "Embedding request blocked because exact token counting failed for model=%s book=%s chunk=%s code=%s reason=%s.",
            profile.model_id,
            work_item.book_number,
            work_item.chunk_number,
            error.code,
            error.message,
        )
        return EmbeddingFailure(
            work_item=work_item,
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            code="EMBEDDING_TOKEN_COUNT_FAILED",
            message="The embedding request was blocked because VySol could not count the provider input tokens exactly.",
            retryable=False,
        )

    # BLOCK 2: Return the counted token total for downstream retry bookkeeping and provider error shaping
    # WHY: Once the provider has already counted the request exactly, downstream failure paths should reuse that exact number instead of recalculating an estimate
    return count_result.total_tokens if count_result is not None else 0


def _count_query_tokens(
    *,
    credential: ProviderCredential,
    profile: EmbeddingProfile,
    query: str,
) -> int | QueryEmbeddingFailure:
    # BLOCK 1: Run the shared exact max-input enforcement path for one retrieval query embedding request
    # WHY: Query embedding should block on the same exact provider count contract as chunk embedding, while still returning retrieval-specific failure codes
    try:
        count_result = ensure_within_max_input_tokens(
            request=TokenCountRequest(
                provider_id=profile.provider_id,
                model_id=profile.model_id,
                credential=credential,
                contents=query,
            ),
            max_input_tokens=profile.max_input_tokens,
        )
    except MaxInputTokensExceededError as error:
        counted_tokens = int(error.details.get("total_tokens", 0))
        logger.error(
            "Query embedding blocked because query exceeds locked max_input_tokens for model=%s counted_tokens=%s max_input_tokens=%s.",
            profile.model_id,
            counted_tokens,
            profile.max_input_tokens,
        )
        return QueryEmbeddingFailure(
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            code="RETRIEVAL_QUERY_TOO_LARGE",
            message="The query exceeds the locked maximum input token limit for this embedding model.",
            retryable=False,
            billable_token_estimate=counted_tokens,
        )
    except TokenCountingError as error:
        logger.error(
            "Query embedding blocked because exact token counting failed for model=%s code=%s reason=%s.",
            profile.model_id,
            error.code,
            error.message,
        )
        return QueryEmbeddingFailure(
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            code="RETRIEVAL_QUERY_TOKEN_COUNT_FAILED",
            message="The retrieval query was blocked because VySol could not count the provider input tokens exactly.",
            retryable=False,
        )

    # BLOCK 2: Return the counted token total for downstream retry bookkeeping and provider error shaping
    # WHY: Retrieval error handling should reuse the exact token count it already paid to compute instead of restoring the previous /4 estimate path
    return count_result.total_tokens if count_result is not None else 0
