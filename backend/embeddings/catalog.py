"""Supported backend embedding model catalog."""

from __future__ import annotations

from dataclasses import dataclass

from backend.models.registry import SharedModelDefinition, load_default_model_registry

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


def get_supported_embedding_model(model_id: str) -> SupportedEmbeddingModel:
    """Return the backend-owned embedding model definition."""
    # BLOCK 1: Resolve the requested embedding model from the shared app registry and fail fast if it is not an embedding-capable model
    # VARS: shared_model = language-neutral model metadata loaded from the same JSON file the TypeScript registry uses
    # WHY: Embedding support must not depend on a second Python-only allowlist, or adding a model for the UI could silently fail in the backend
    shared_model = load_default_model_registry().get_model(model_id)
    if shared_model is None or "embedding" not in shared_model.surfaces:
        raise EmbeddingConfigurationError(
            code="UNSUPPORTED_EMBEDDING_MODEL",
            message="The selected embedding model is not supported by the backend embedding pipeline.",
            details={"model_id": model_id},
        )
    return _embedding_model_from_shared_definition(shared_model)


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
        provider_id=supported_model.provider_id,
        model_id=supported_model.model_id,
        dimensions=supported_model.max_dimensions,
        task_type=profile.task_type,
        profile_version=profile.profile_version,
        title=profile.title,
        extra_settings=normalized_settings,
    )


def _embedding_model_from_shared_definition(shared_model: SharedModelDefinition) -> SupportedEmbeddingModel:
    # BLOCK 1: Validate the embedding fields the backend needs, then project the shared model into the existing embedding contract
    # WHY: UI metadata can be broader than backend embedding metadata, but provider calls and Qdrant schemas require concrete call names, max input tokens, and vector dimensions
    max_input_tokens = shared_model.limits.get("maxInputTokens")
    max_dimensions = shared_model.limits.get("maxEmbeddingDimensions")
    if max_input_tokens is None or max_dimensions is None:
        raise EmbeddingConfigurationError(
            code="EMBEDDING_MODEL_CONTRACT_INCOMPLETE",
            message="The selected embedding model is missing backend-required embedding limits.",
            details={"model_id": shared_model.id},
        )
    return SupportedEmbeddingModel(
        provider_id=shared_model.provider_id,
        model_id=shared_model.id,
        call_name=shared_model.call_name,
        default_dimensions=max_dimensions,
        max_dimensions=max_dimensions,
        max_input_tokens=max_input_tokens,
    )
