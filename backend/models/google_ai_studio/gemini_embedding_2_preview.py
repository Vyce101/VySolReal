"""Google AI Studio runtime adapter for Gemini Embedding 2."""

from __future__ import annotations

import time

from google import genai
from google.genai import types
from google.genai.errors import APIError

from backend.embeddings.catalog import get_supported_embedding_model
from backend.embeddings.errors import EmbeddingProviderError
from backend.embeddings.models import EmbeddingFailure, EmbeddingProfile, EmbeddingSuccess, EmbeddingWorkItem
from backend.logger import get_logger
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
        # BLOCK 1: Send one chunk text to the Google embedding model using the world's locked embedding profile
        # WHY: The ingestion contract is one text per request, which keeps failure isolation clear and lets retry logic reason about one chunk at a time
        supported_model = get_supported_embedding_model(profile.model_id)
        # BLOCK 2: Reject chunks that exceed the world's locked max token budget before any provider request is sent
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
        client = genai.Client(api_key=credential.api_key)
        request_started_at = time.perf_counter()
        logger.info(
            "Embedding request started for provider=%s credential=%s quota_scope=%s model=%s book=%s chunk=%s.",
            credential.provider_id,
            credential.display_name,
            credential.quota_scope,
            profile.model_id,
            work_item.book_number,
            work_item.chunk_number,
        )
        try:
            response = client.models.embed_content(
                model=supported_model.call_name,
                contents=work_item.chunk_text,
                config=types.EmbedContentConfig(
                    task_type=profile.task_type,
                    output_dimensionality=profile.dimensions,
                    title=profile.title,
                ),
            )
        except APIError as error:
            logger.warning(
                "Embedding request failed with provider API error for credential=%s quota_scope=%s model=%s book=%s chunk=%s status=%s duration_ms=%s.",
                credential.display_name,
                credential.quota_scope,
                profile.model_id,
                work_item.book_number,
                work_item.chunk_number,
                error.code,
                int((time.perf_counter() - request_started_at) * 1000),
            )
            return _failure_from_api_error(
                credential=credential,
                work_item=work_item,
                error=error,
            )
        except Exception as exc:
            logger.error(
                "Embedding request crashed for credential=%s quota_scope=%s model=%s book=%s chunk=%s duration_ms=%s reason=%s.",
                credential.display_name,
                credential.quota_scope,
                profile.model_id,
                work_item.book_number,
                work_item.chunk_number,
                int((time.perf_counter() - request_started_at) * 1000),
                str(exc),
            )
            return EmbeddingFailure(
                work_item=work_item,
                credential_name=credential.display_name,
                quota_scope=credential.quota_scope,
                code="EMBEDDING_PROVIDER_FAILED",
                message=str(exc),
                retryable=True,
                billable_token_estimate=token_estimate,
            )
        finally:
            client.close()

        if not response.embeddings or not response.embeddings[0].values:
            raise EmbeddingProviderError(
                code="EMBEDDING_PROVIDER_EMPTY",
                message="The embedding provider returned no vector values for the chunk.",
                details={"chunk_number": work_item.chunk_number},
            )

        logger.info(
            "Embedding request completed for credential=%s quota_scope=%s model=%s book=%s chunk=%s duration_ms=%s billable_characters=%s.",
            credential.display_name,
            credential.quota_scope,
            profile.model_id,
            work_item.book_number,
            work_item.chunk_number,
            int((time.perf_counter() - request_started_at) * 1000),
            response.metadata.billable_character_count if response.metadata is not None else None,
        )
        return EmbeddingSuccess(
            work_item=work_item,
            credential_name=credential.display_name,
            quota_scope=credential.quota_scope,
            vector=list(response.embeddings[0].values),
            billable_character_count=response.metadata.billable_character_count if response.metadata is not None else None,
        )


def _failure_from_api_error(
    *,
    credential: ProviderCredential,
    work_item: EmbeddingWorkItem,
    error: APIError,
) -> EmbeddingFailure:
    # BLOCK 1: Translate provider HTTP errors into retryable chunk-level failures with rate-limit metadata when it is available
    # WHY: The embedding scheduler needs structured cooldown hints instead of raw exceptions so it can decide whether to retry the same chunk now, later, or on resume
    retry_after_value = None
    rate_limit_type = None
    headers = getattr(error.response, "headers", {}) if error.response is not None else {}
    retry_after_header = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after_header:
        try:
            retry_after_value = int(float(retry_after_header))
        except ValueError:
            retry_after_value = None
    message = error.message or str(error)
    if error.code == 429:
        upper_message = message.upper()
        if "REQUESTS_PER_DAY" in upper_message or "PER DAY" in upper_message or "RPD" in upper_message:
            rate_limit_type = "rpd"
        elif "TOKENS_PER_MINUTE" in upper_message or "TPM" in upper_message:
            rate_limit_type = "tpm"
        else:
            rate_limit_type = "rpm"
    return EmbeddingFailure(
        work_item=work_item,
        credential_name=credential.display_name,
        quota_scope=credential.quota_scope,
        code=f"EMBEDDING_PROVIDER_{error.code}",
        message=message,
        retryable=error.code >= 500 or error.code == 429,
        rate_limit_type=rate_limit_type,
        retry_after_seconds=retry_after_value,
        billable_token_estimate=_estimate_tokens(work_item.chunk_text),
    )


def _estimate_tokens(text: str) -> int:
    # BLOCK 1: Approximate input tokens from character count for scheduler guidance when the provider does not expose a precise tokenizer path here
    # WHY: User-entered TPM limits are only soft scheduling guidance, so a lightweight estimate is good enough to reduce avoidable spikes without adding a second model-specific tokenization dependency
    return max(1, (len(text) + 3) // 4)
