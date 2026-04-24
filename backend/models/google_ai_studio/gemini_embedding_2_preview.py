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
        # BLOCK 1: Reject chunks that exceed the world's locked max token budget before calling the shared Google client
        # VARS: token_estimate = lightweight approximation of the chunk's token count used for the hard preflight ceiling
        # WHY: The world profile is supposed to lock the model's input contract, so sending oversized chunks to the provider would break that guarantee and turn a predictable local validation error into a remote failure
        token_estimate = _estimate_tokens(work_item.chunk_text)
        if profile.max_input_tokens is not None and token_estimate > profile.max_input_tokens:
            logger.error(
                "Embedding request blocked because chunk exceeds locked max_input_tokens for model=%s book=%s chunk=%s estimated_tokens=%s max_input_tokens=%s.",
                profile.model_id,
                work_item.book_number,
                work_item.chunk_number,
                token_estimate,
                profile.max_input_tokens,
            )
            return EmbeddingFailure(
                work_item=work_item,
                credential_name=credential.display_name,
                quota_scope=credential.quota_scope,
                code="EMBEDDING_CHUNK_TOO_LARGE",
                message="The chunk exceeds the locked maximum input token limit for this embedding model.",
                retryable=False,
                billable_token_estimate=token_estimate,
            )

        # BLOCK 2: Send one chunk text to the Google embedding model using the world's locked embedding profile
        # WHY: The ingestion contract is one text per request, which keeps failure isolation clear and lets retry logic reason about one chunk at a time
        outcome = embed_content(
            GoogleEmbeddingRequest(
                credential=credential,
                profile=profile,
                text=work_item.chunk_text,
                task_type=profile.task_type,
                title=profile.title,
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
        # BLOCK 1: Send one user query to the same embedding model using Google's query-specific retrieval task type
        # VARS: token_estimate = lightweight approximation of the query's token count used for scheduler reservation and max-input preflight
        # WHY: Query vectors must share the world's model and dimensions with chunk vectors, but Google exposes a separate retrieval-query mode that is better suited for search inputs
        token_estimate = _estimate_tokens(query)
        if profile.max_input_tokens is not None and token_estimate > profile.max_input_tokens:
            logger.error(
                "Query embedding blocked because query exceeds locked max_input_tokens for model=%s estimated_tokens=%s max_input_tokens=%s.",
                profile.model_id,
                token_estimate,
                profile.max_input_tokens,
            )
            return QueryEmbeddingFailure(
                credential_name=credential.display_name,
                quota_scope=credential.quota_scope,
                code="RETRIEVAL_QUERY_TOO_LARGE",
                message="The query exceeds the locked maximum input token limit for this embedding model.",
                retryable=False,
                billable_token_estimate=token_estimate,
            )

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
        billable_token_estimate=_estimate_tokens(work_item.chunk_text),
    )


def _query_failure_from_api_error(
    *,
    credential: ProviderCredential,
    query: str,
    error: GoogleEmbeddingError,
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
        billable_token_estimate=_estimate_tokens(query),
    )


def _estimate_tokens(text: str) -> int:
    # BLOCK 1: Approximate input tokens from character count for scheduler guidance when the provider does not expose a precise tokenizer path here
    # WHY: Reservations need a cheap pre-dispatch estimate, and adding a second model-specific tokenizer just for scheduling would make the boundary heavier than the provider call contract needs
    return max(1, (len(text) + 3) // 4)
