"""Shared Google AI Studio error translation helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class GoogleAIStudioErrorInfo:
    """Provider error metadata normalized for backend workflows."""

    status_code: int | None
    code: str
    message: str
    retryable: bool
    rate_limit_type: str | None = None
    rate_limit_scope: str = "model"
    retry_after_seconds: int | None = None


def parse_google_ai_studio_api_error(error: object) -> GoogleAIStudioErrorInfo:
    """Translate a Google SDK API error into workflow-neutral metadata."""
    # BLOCK 1: Pull the stable fields exposed by Google SDK APIError without depending on one concrete workflow
    # VARS: status_code = provider HTTP status if the SDK exposed it, message = human-readable provider failure text
    # WHY: Embeddings, future chat, and future extraction adapters need the same quota/error interpretation without importing each other's failure models
    status_code = _status_code_from_error(error)
    message = str(getattr(error, "message", None) or str(error))
    rate_limit_type = None
    rate_limit_scope = "model"

    # BLOCK 2: Classify Google rate-limit failures from known message markers and default unknown 429s to model-local RPM
    # WHY: Google quota messages are not guaranteed to expose the same structured shape everywhere, so text markers give precise buckets when available while unknown 429s still cool down safely
    if status_code == 429:
        upper_message = message.upper()
        rate_limit_scope = _rate_limit_scope_from_message(upper_message)
        if "REQUESTS_PER_DAY" in upper_message or "PER DAY" in upper_message or "RPD" in upper_message:
            rate_limit_type = "rpd"
        elif "TOKENS_PER_MINUTE" in upper_message or "TPM" in upper_message:
            rate_limit_type = "tpm"
        else:
            rate_limit_type = "rpm"

    return GoogleAIStudioErrorInfo(
        status_code=status_code,
        code=f"GOOGLE_AI_STUDIO_{status_code}" if status_code is not None else "GOOGLE_AI_STUDIO_UNKNOWN",
        message=message,
        retryable=(status_code is not None and status_code >= 500) or status_code == 429,
        rate_limit_type=rate_limit_type,
        rate_limit_scope=rate_limit_scope,
        retry_after_seconds=_retry_after_seconds_from_error(error),
    )


def _status_code_from_error(error: object) -> int | None:
    # BLOCK 1: Normalize the SDK status code when it is present
    # WHY: Test doubles and SDK versions may expose numeric codes with slightly different runtime types, so callers need one safe integer-or-empty value
    raw_code = getattr(error, "code", None)
    if raw_code is None:
        return None
    try:
        return int(raw_code)
    except (TypeError, ValueError):
        return None


def _retry_after_seconds_from_error(error: object) -> int | None:
    # BLOCK 1: Read Retry-After from the provider response headers when Google includes it
    # WHY: Scheduler cooldowns should follow provider timing when available instead of always falling back to VySol's default wait
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    retry_after_header = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after_header is None:
        return None
    try:
        return int(float(retry_after_header))
    except ValueError:
        return None


def _rate_limit_scope_from_message(upper_message: str) -> str:
    # BLOCK 1: Widen the cooldown scope only when the provider message explicitly points at project or credential quota
    # WHY: Ordinary model quota failures should not disable unrelated models on the same key, but explicit project/key quota failures affect the shared provider bucket
    if "PROJECT" in upper_message:
        return "project"
    if "API KEY" in upper_message or "API_KEY" in upper_message or "CREDENTIAL" in upper_message:
        return "credential"
    return "model"
