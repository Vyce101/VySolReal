"""Supported backend embedding model catalog."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import EmbeddingConfigurationError
from .models import EmbeddingProfile

DEFAULT_EMBEDDING_PROFILE_VERSION = 1


@dataclass(slots=True, frozen=True)
class SupportedEmbeddingModel:
    """Backend-owned embedding model definition."""

    provider_id: str
    model_id: str
    call_name: str
    default_dimensions: int
    max_dimensions: int
    max_input_tokens: int


SUPPORTED_EMBEDDING_MODELS: dict[str, SupportedEmbeddingModel] = {
    "google/gemini-embedding-2-preview": SupportedEmbeddingModel(
        provider_id="google",
        model_id="google/gemini-embedding-2-preview",
        call_name="gemini-embedding-2-preview",
        default_dimensions=3072,
        max_dimensions=3072,
        max_input_tokens=8192,
    ),
}


def get_supported_embedding_model(model_id: str) -> SupportedEmbeddingModel:
    """Return the backend-owned embedding model definition."""
    # BLOCK 1: Resolve the requested embedding model from the backend-owned catalog and fail fast if it is unsupported
    # VARS: supported_model = backend definition that maps a stable VySol model id to the provider call name and dimensional limits
    # WHY: The embedding pipeline must not trust arbitrary model ids from user data because Qdrant schema, provider requests, and resume behavior all depend on a known contract
    supported_model = SUPPORTED_EMBEDDING_MODELS.get(model_id)
    if supported_model is None:
        raise EmbeddingConfigurationError(
            code="UNSUPPORTED_EMBEDDING_MODEL",
            message="The selected embedding model is not supported by the backend embedding pipeline.",
            details={"model_id": model_id},
        )
    return supported_model


def create_embedding_profile(*, model_id: str) -> EmbeddingProfile:
    """Build an explicit embedding profile for a user-chosen supported model."""
    # BLOCK 1: Build a locked embedding profile from the exact supported model the caller selected
    # WHY: New worlds must not silently choose an embedder on the user's behalf, so profile creation has to require an explicit model id every time
    supported_model = get_supported_embedding_model(model_id)
    return EmbeddingProfile(
        provider_id=supported_model.provider_id,
        model_id=supported_model.model_id,
        dimensions=supported_model.max_dimensions,
        task_type="RETRIEVAL_DOCUMENT",
        profile_version=DEFAULT_EMBEDDING_PROFILE_VERSION,
        extra_settings={"max_input_tokens": supported_model.max_input_tokens},
    )


def lock_profile_to_model_maxima(profile: EmbeddingProfile) -> EmbeddingProfile:
    """Return the profile with backend-owned model maxima enforced."""
    # BLOCK 1: Normalize the profile to the backend-owned maximum dimensions and input-token settings for that model
    # WHY: Worlds are supposed to lock to one precise embedding contract, so older or partially populated profiles must be upgraded to the same model maxima the backend would choose for new worlds
    supported_model = get_supported_embedding_model(profile.model_id)
    normalized_settings = dict(profile.extra_settings)
    normalized_settings["max_input_tokens"] = supported_model.max_input_tokens
    return EmbeddingProfile(
        provider_id=profile.provider_id,
        model_id=profile.model_id,
        dimensions=supported_model.max_dimensions,
        task_type=profile.task_type,
        profile_version=profile.profile_version,
        title=profile.title,
        extra_settings=normalized_settings,
    )
