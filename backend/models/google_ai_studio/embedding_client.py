"""Shared Google AI Studio embedding request helper."""

from __future__ import annotations

import time
from dataclasses import dataclass

from google import genai
from google.genai import types
from google.genai.errors import APIError

from backend.embeddings.catalog import get_supported_embedding_model
from backend.embeddings.errors import EmbeddingProviderError
from backend.embeddings.models import EmbeddingProfile
from backend.logger import get_logger
from backend.models.google_ai_studio.errors import parse_google_ai_studio_api_error
from backend.provider_keys.models import ProviderCredential

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class GoogleEmbeddingRequest:
    """Provider request shape shared by Google embedding adapters."""

    credential: ProviderCredential
    profile: EmbeddingProfile
    text: str
    task_type: str
    request_label: str
    empty_error_code: str
    empty_error_message: str
    log_context: dict[str, object]


@dataclass(slots=True, frozen=True)
class GoogleEmbeddingResponse:
    """Successful Google embedding response normalized for app adapters."""

    vector: list[float]
    billable_character_count: int | None


@dataclass(slots=True, frozen=True)
class GoogleEmbeddingError:
    """Google embedding failure normalized for app adapters."""

    code_suffix: str
    message: str
    retryable: bool
    status_code: int | None = None
    rate_limit_type: str | None = None
    rate_limit_scope: str = "model"
    retry_after_seconds: int | None = None


def embed_content(request: GoogleEmbeddingRequest) -> GoogleEmbeddingResponse | GoogleEmbeddingError:
    """Call Google AI Studio embeddings and normalize the provider result."""
    # BLOCK 1: Resolve the Google model call name and send one embedding request through the shared provider client path
    # VARS: context_text = safe log-only metadata string, request_started_at = monotonic timestamp used to report provider boundary duration
    # WHY: Google request setup, close, logging, and error parsing are provider-wide behavior, so model adapters should only decide which task type and result contract they need
    supported_model = get_supported_embedding_model(request.profile.model_id)
    context_text = _format_log_context(request.log_context)
    client = genai.Client(api_key=request.credential.api_key)
    request_started_at = time.perf_counter()
    logger.info(
        "%s embedding request started for provider=%s credential=%s quota_scope=%s model=%s text_length=%s%s.",
        request.request_label,
        request.credential.provider_id,
        request.credential.display_name,
        request.credential.quota_scope,
        request.profile.model_id,
        len(request.text),
        context_text,
    )
    try:
        response = client.models.embed_content(
            model=supported_model.call_name,
            contents=request.text,
            config=types.EmbedContentConfig(
                task_type=request.task_type,
                output_dimensionality=request.profile.dimensions,
            ),
        )
    except APIError as error:
        logger.warning(
            "%s embedding request failed with provider API error for credential=%s quota_scope=%s model=%s status=%s duration_ms=%s%s.",
            request.request_label,
            request.credential.display_name,
            request.credential.quota_scope,
            request.profile.model_id,
            error.code,
            int((time.perf_counter() - request_started_at) * 1000),
            context_text,
        )
        error_info = parse_google_ai_studio_api_error(error)
        return GoogleEmbeddingError(
            code_suffix=str(error_info.status_code) if error_info.status_code is not None else "UNKNOWN",
            status_code=error_info.status_code,
            message=error_info.message,
            retryable=error_info.retryable,
            rate_limit_type=error_info.rate_limit_type,
            rate_limit_scope=error_info.rate_limit_scope,
            retry_after_seconds=error_info.retry_after_seconds,
        )
    except Exception as exc:
        logger.error(
            "%s embedding request crashed for credential=%s quota_scope=%s model=%s duration_ms=%s reason=%s%s.",
            request.request_label,
            request.credential.display_name,
            request.credential.quota_scope,
            request.profile.model_id,
            int((time.perf_counter() - request_started_at) * 1000),
            str(exc),
            context_text,
        )
        return GoogleEmbeddingError(
            code_suffix="FAILED",
            message=str(exc),
            retryable=True,
        )
    finally:
        client.close()

    if not response.embeddings or not response.embeddings[0].values:
        raise EmbeddingProviderError(
            code=request.empty_error_code,
            message=request.empty_error_message,
            details=dict(request.log_context),
        )

    logger.info(
        "%s embedding request completed for credential=%s quota_scope=%s model=%s duration_ms=%s billable_characters=%s%s.",
        request.request_label,
        request.credential.display_name,
        request.credential.quota_scope,
        request.profile.model_id,
        int((time.perf_counter() - request_started_at) * 1000),
        response.metadata.billable_character_count if response.metadata is not None else None,
        context_text,
    )
    return GoogleEmbeddingResponse(
        vector=list(response.embeddings[0].values),
        billable_character_count=response.metadata.billable_character_count if response.metadata is not None else None,
    )


def _format_log_context(context: dict[str, object]) -> str:
    # BLOCK 1: Render only caller-approved metadata into provider logs
    # WHY: Embedding text, overlap text, vectors, and keys must never enter logs, so the shared helper accepts explicit safe metadata instead of inspecting request bodies
    if not context:
        return ""
    return " " + " ".join(f"{key}={value}" for key, value in context.items())
