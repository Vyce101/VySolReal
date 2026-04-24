"""Shared provider and model metadata loaded from the app catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(slots=True, frozen=True)
class SharedModelDefinition:
    """One model definition from the shared app catalog."""

    provider_id: str
    id: str
    display_name: str
    call_name: str
    surfaces: frozenset[str]
    limits: dict[str, int]


@dataclass(slots=True, frozen=True)
class SharedProviderDefinition:
    """One provider definition from the shared app catalog."""

    id: str
    display_name: str
    api_key_file_path: str
    models: tuple[SharedModelDefinition, ...]


@dataclass(slots=True, frozen=True)
class SharedModelRegistry:
    """All provider and model definitions available to both app layers."""

    providers: tuple[SharedProviderDefinition, ...]

    def get_model(self, model_id: str) -> SharedModelDefinition | None:
        # BLOCK 1: Search the loaded provider models for the requested stable model id
        # WHY: Backend model lookup must use the same model ids as the UI registry so provider support cannot drift between layers
        for provider in self.providers:
            for model in provider.models:
                if model.id == model_id:
                    return model
        return None


def default_catalog_root() -> Path:
    """Return the repository-local shared model catalog root."""
    return Path(__file__).resolve().parents[2] / "models" / "catalog"


@lru_cache(maxsize=1)
def load_default_model_registry() -> SharedModelRegistry:
    """Load the default shared model registry."""
    return load_model_registry(default_catalog_root())


def load_model_registry(catalog_root: Path) -> SharedModelRegistry:
    """Load provider and model definitions from one catalog root."""
    # BLOCK 1: Read the provider manifest first, then resolve each model file relative to the shared catalog root
    # VARS: catalog_root = root folder that contains providers.json and provider-owned JSON files
    # WHY: The provider manifest is the one join point between provider metadata and model files, so both backend and frontend can add support by pointing at the same JSON files
    manifest_path = catalog_root / "providers.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    providers: list[SharedProviderDefinition] = []
    for provider_payload in manifest.get("providers", []):
        provider_id = str(provider_payload["id"])
        models = tuple(
            _load_model_definition(
                provider_id=provider_id,
                model_path=catalog_root / str(model_file),
            )
            for model_file in provider_payload.get("modelFiles", [])
        )
        providers.append(
            SharedProviderDefinition(
                id=provider_id,
                display_name=str(provider_payload["displayName"]),
                api_key_file_path=str(provider_payload["apiKeyFilePath"]),
                models=models,
            )
        )
    return SharedModelRegistry(providers=tuple(providers))


def _load_model_definition(*, provider_id: str, model_path: Path) -> SharedModelDefinition:
    # BLOCK 1: Convert one shared model JSON file into the backend's small runtime view
    # WHY: Embedding runtime only needs stable provider/model ids, provider call names, surfaces, and numeric limits, while UI-specific display metadata can remain in the same source file without leaking into embedding logic
    payload = json.loads(model_path.read_text(encoding="utf-8"))
    return SharedModelDefinition(
        provider_id=provider_id,
        id=str(payload["id"]),
        display_name=str(payload["displayName"]),
        call_name=str(payload["callName"]),
        surfaces=frozenset(str(surface) for surface in payload.get("surfaces", [])),
        limits={
            str(limit_name): int(limit_value)
            for limit_name, limit_value in dict(payload.get("limits", {})).items()
        },
    )
