"""Runtime embedding provider adapter registry."""

from __future__ import annotations

from typing import Protocol

from backend.models.google_ai_studio.gemini_embedding_2_preview import GoogleAIStudioEmbeddingProvider

from .errors import EmbeddingConfigurationError
from .models import EmbeddingFailure, EmbeddingProfile, EmbeddingSuccess, EmbeddingWorkItem, ProviderCredential


class EmbeddingProviderAdapter(Protocol):
    """Runtime interface for provider-specific embedding calls."""

    def embed_text(
        self,
        *,
        credential: ProviderCredential,
        profile: EmbeddingProfile,
        work_item: EmbeddingWorkItem,
    ) -> EmbeddingSuccess | EmbeddingFailure:
        """Embed one chunk of text."""


_PROVIDER_FACTORIES = {
    "google": GoogleAIStudioEmbeddingProvider,
}


def create_embedding_provider(provider_id: str) -> EmbeddingProviderAdapter:
    """Create the runtime adapter for one embedding provider."""
    # BLOCK 1: Resolve the provider-specific adapter from a small backend runtime registry
    # WHY: Shared model metadata can describe providers before runtime support exists, so embedding execution must fail clearly instead of falling back to a Google-specific implementation
    provider_factory = _PROVIDER_FACTORIES.get(provider_id)
    if provider_factory is None:
        raise EmbeddingConfigurationError(
            code="UNSUPPORTED_EMBEDDING_PROVIDER",
            message="The selected embedding provider is not supported by the backend embedding runtime.",
            details={"provider_id": provider_id},
        )
    return provider_factory()
