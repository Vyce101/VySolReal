"""Shared provider key models for backend AI request scheduling."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(slots=True, frozen=True)
class CredentialModelLimits:
    """Optional per-model scheduler guidance for one credential."""

    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    requests_per_day: int | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CredentialModelLimits":
        # BLOCK 1: Convert optional JSON limit fields into typed scheduler limits
        # WHY: Provider key files are user-editable JSON, so every caller needs one normalized limit shape before scheduling decisions begin
        return cls(
            requests_per_minute=int(payload["requests_per_minute"]) if payload.get("requests_per_minute") is not None else None,
            tokens_per_minute=int(payload["tokens_per_minute"]) if payload.get("tokens_per_minute") is not None else None,
            requests_per_day=int(payload["requests_per_day"]) if payload.get("requests_per_day") is not None else None,
        )


@dataclass(slots=True, frozen=True)
class ProviderCredential:
    """One enabled provider credential loaded from the user key store."""

    provider_id: str
    credential_name: str
    api_key: str
    project_id: str | None
    allowed_models: frozenset[str]
    model_limits: dict[str, CredentialModelLimits]
    enabled: bool = True

    @property
    def quota_scope(self) -> str:
        # BLOCK 1: Collapse credentials that share one provider quota pool into one scheduler scope key
        # WHY: Some providers share limits at project level, so scheduling per raw key can overestimate available capacity when several keys point at the same project
        if self.project_id:
            return f"{self.provider_id}:project:{self.project_id}"
        return f"{self.provider_id}:credential:{self.credential_name}"

    @property
    def display_name(self) -> str:
        return self.credential_name or self.api_key

    def supports_model(self, model_id: str) -> bool:
        # BLOCK 1: Treat an empty allow-list as usable for every model, otherwise require the exact model id
        # WHY: Existing key files did not always pin allowed models, so empty allow-lists must stay permissive for backward compatibility
        return not self.allowed_models or model_id in self.allowed_models


@dataclass(slots=True)
class ProviderRuntimeState:
    """Persisted provider cooldown state shared across backend AI runs."""

    scope_key: str
    provider_id: str
    credential_name: str
    project_id: str | None = None
    last_limit_type: str | None = None
    cooldown_until_utc: str | None = None
    last_error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        # BLOCK 1: Omit empty fields from persisted runtime state
        # WHY: The cooldown file is support metadata, so keeping it compact makes manual inspection easier without losing any active state
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ProviderRuntimeState":
        # BLOCK 1: Rebuild one persisted quota-scope cooldown record from JSON
        # WHY: Cooldowns must survive app restarts, so the scheduler needs a stable serialization boundary for provider runtime state
        return cls(
            scope_key=str(payload["scope_key"]),
            provider_id=str(payload["provider_id"]),
            credential_name=str(payload["credential_name"]),
            project_id=str(payload["project_id"]) if payload.get("project_id") is not None else None,
            last_limit_type=str(payload["last_limit_type"]) if payload.get("last_limit_type") is not None else None,
            cooldown_until_utc=str(payload["cooldown_until_utc"]) if payload.get("cooldown_until_utc") is not None else None,
            last_error_message=str(payload["last_error_message"]) if payload.get("last_error_message") is not None else None,
        )

    @property
    def cooldown_until(self) -> datetime | None:
        # BLOCK 1: Parse the persisted ISO timestamp only when a cooldown exists
        # WHY: Scheduler checks compare against real machine time, while the JSON file needs a portable string value
        if self.cooldown_until_utc is None:
            return None
        return datetime.fromisoformat(self.cooldown_until_utc)
