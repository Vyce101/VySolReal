"""Provider credential loading for shared backend AI scheduling."""

from __future__ import annotations

import json
from pathlib import Path

from backend.logger import get_logger

from .errors import ProviderKeyConfigurationError
from .models import ProviderCredential
from .storage import default_provider_keys_root

logger = get_logger(__name__)


def load_provider_credentials(
    *,
    provider_id: str,
    provider_keys_root: Path | None = None,
) -> list[ProviderCredential]:
    """Load enabled provider credentials from the app-level key store."""
    # BLOCK 1: Resolve the provider credential folder and quietly return no credentials when the user has not configured any yet
    # WHY: Missing keys are a normal first-run state, so callers should be able to surface workflow-specific warnings instead of treating an empty folder as malformed data
    resolved_root = provider_keys_root if provider_keys_root is not None else default_provider_keys_root()
    provider_dir = resolved_root / _provider_directory_name(provider_id)
    if not provider_dir.exists():
        logger.warning(
            "Provider credential directory is missing for provider=%s path=%s.",
            provider_id,
            provider_dir,
        )
        return []

    # BLOCK 2: Load JSON credential files in stable order, then drop disabled credentials before any scheduler sees them
    # VARS: credential_files = sorted provider credential files so runtime behavior stays deterministic across runs
    # WHY: Disabled keys must behave as unavailable everywhere, while deterministic ordering preserves the existing first-usable failover behavior
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
        enabled = _enabled_from_payload(payload=payload, credential_path=credential_path)
        if not enabled:
            logger.info(
                "Skipped disabled provider credential name=%s provider=%s.",
                str(payload.get("name") or credential_path.stem),
                provider_id,
            )
            continue
        credential = _credential_from_payload(provider_id=provider_id, payload=payload, credential_path=credential_path)
        credentials.append(credential)
    return credentials


def load_eligible_provider_credentials(
    *,
    provider_id: str,
    model_id: str,
    provider_keys_root: Path | None = None,
) -> list[ProviderCredential]:
    """Load enabled credentials that can serve one provider model."""
    # BLOCK 1: Filter enabled provider credentials down to the requested model contract
    # WHY: AI workflows should not duplicate allow-list filtering, or future systems could accidentally send requests through keys the user intended for another model
    return [
        credential
        for credential in load_provider_credentials(
            provider_id=provider_id,
            provider_keys_root=provider_keys_root,
        )
        if credential.supports_model(model_id)
    ]


def _provider_directory_name(provider_id: str) -> str:
    # BLOCK 1: Map stable provider ids to their on-disk key folder names
    # WHY: The registry exposes provider ids, while existing user storage already uses a Google-specific folder name that must remain compatible
    if provider_id == "google":
        return "google-ai-studio"
    return provider_id


def _credential_from_payload(
    *,
    provider_id: str,
    payload: dict[str, object],
    credential_path: Path,
) -> ProviderCredential:
    # BLOCK 1: Validate the credential file contract and turn it into one runtime credential object
    # WHY: Backend AI calls depend on stable credential names, provider grouping, and enabled state, so malformed key files must fail clearly before provider calls begin while deprecated limit hints stay backward-compatible
    api_key = str(payload.get("api_key", "")).strip()
    if not api_key:
        logger.error("Provider credential file is missing an API key at %s.", credential_path)
        raise ProviderKeyConfigurationError(
            code="PROVIDER_KEY_INVALID",
            message="A provider credential file is missing its API key.",
            details={"credential_path": str(credential_path)},
        )

    allowed_models_payload = payload.get("allowed_models", [])
    allowed_models = frozenset(str(model_id) for model_id in allowed_models_payload) if allowed_models_payload else frozenset()
    deprecated_limits = dict(payload.get("limits", {}))
    credential_name = str(payload.get("name") or credential_path.stem)
    project_id = str(payload["project_id"]) if payload.get("project_id") is not None else None
    logger.info(
        "Loaded provider credential name=%s provider=%s project_id=%s allowed_models=%s deprecated_limit_entries=%s enabled=%s.",
        credential_name,
        provider_id,
        project_id,
        len(allowed_models),
        len(deprecated_limits),
        True,
    )
    return ProviderCredential(
        provider_id=provider_id,
        credential_name=credential_name,
        api_key=api_key,
        project_id=project_id,
        allowed_models=allowed_models,
        enabled=True,
    )


def _enabled_from_payload(*, payload: dict[str, object], credential_path: Path) -> bool:
    # BLOCK 1: Read and validate the optional enabled flag before validating the rest of the key file
    # WHY: Disabled keys should be ignored even if their secret body is incomplete, but a non-boolean enabled flag would make future UI behavior ambiguous
    enabled_payload = payload.get("enabled", True)
    if not isinstance(enabled_payload, bool):
        logger.error("Provider credential file has a non-boolean enabled flag at %s.", credential_path)
        raise ProviderKeyConfigurationError(
            code="PROVIDER_KEY_INVALID",
            message="A provider credential file has an enabled value that is not true or false.",
            details={"credential_path": str(credential_path)},
        )
    return enabled_payload
