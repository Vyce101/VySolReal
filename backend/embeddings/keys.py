"""Provider credential loading for embedding runs."""

from __future__ import annotations

import json
from pathlib import Path

from backend.logger import get_logger

from .errors import EmbeddingConfigurationError
from .models import CredentialModelLimits, ProviderCredential

logger = get_logger(__name__)


def default_provider_keys_root() -> Path:
    """Resolve the default provider key storage root."""
    return Path(__file__).resolve().parents[2] / "user" / "keys"


def load_provider_credentials(
    *,
    provider_id: str,
    provider_keys_root: Path | None = None,
) -> list[ProviderCredential]:
    """Load provider credentials from the app-level key store."""
    # BLOCK 1: Resolve the provider credential folder and quietly return no credentials when the user has not configured any yet
    # WHY: Missing keys are a normal first-run state for embeddings, so the ingestion flow should surface a warning later instead of treating an empty credential folder as malformed data
    resolved_root = provider_keys_root if provider_keys_root is not None else default_provider_keys_root()
    provider_dir = resolved_root / _provider_directory_name(provider_id)
    if not provider_dir.exists():
        logger.warning(
            "Provider credential directory is missing for provider=%s path=%s.",
            provider_id,
            provider_dir,
        )
        return []

    # BLOCK 2: Load every JSON credential file for the provider into structured scheduler credentials
    # VARS: credential_files = sorted provider credential files so runtime behavior stays deterministic across runs
    # WHY: Deterministic file ordering keeps scheduler selection and warning output stable, which makes rate-limit debugging easier for users and tests
    credential_files = sorted(provider_dir.glob("*.json"))
    logger.info(
        "Loading %s provider credential file(s) for provider=%s from %s.",
        len(credential_files),
        provider_id,
        provider_dir,
    )
    credentials: list[ProviderCredential] = []
    for credential_path in credential_files:
        payload = json.loads(credential_path.read_text(encoding="utf-8"))
        credentials.append(_credential_from_payload(provider_id=provider_id, payload=payload, credential_path=credential_path))
    return credentials


def _provider_directory_name(provider_id: str) -> str:
    if provider_id == "google":
        return "google-ai-studio"
    return provider_id


def _credential_from_payload(
    *,
    provider_id: str,
    payload: dict[str, object],
    credential_path: Path,
) -> ProviderCredential:
    # BLOCK 1: Validate the minimal credential file contract and turn it into one runtime credential object
    # WHY: Embedding runs depend on stable credential names, provider grouping, and optional limit data, so malformed key files must fail clearly before provider calls begin
    api_key = str(payload.get("api_key", "")).strip()
    if not api_key:
        logger.error("Provider credential file is missing an API key at %s.", credential_path)
        raise EmbeddingConfigurationError(
            code="PROVIDER_KEY_INVALID",
            message="A provider credential file is missing its API key.",
            details={"credential_path": str(credential_path)},
        )

    allowed_models_payload = payload.get("allowed_models", [])
    allowed_models = frozenset(str(model_id) for model_id in allowed_models_payload) if allowed_models_payload else frozenset()
    raw_limits = dict(payload.get("limits", {}))
    model_limits = {
        str(model_id): CredentialModelLimits.from_dict(dict(limit_payload))
        for model_id, limit_payload in raw_limits.items()
    }
    credential_name = str(payload.get("name") or credential_path.stem)
    project_id = str(payload["project_id"]) if payload.get("project_id") is not None else None
    logger.info(
        "Loaded provider credential name=%s provider=%s project_id=%s allowed_models=%s model_limit_entries=%s.",
        credential_name,
        provider_id,
        project_id,
        len(allowed_models),
        len(model_limits),
    )
    return ProviderCredential(
        provider_id=provider_id,
        credential_name=credential_name,
        api_key=api_key,
        project_id=project_id,
        allowed_models=allowed_models,
        model_limits=model_limits,
    )
