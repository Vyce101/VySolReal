"""Shared models for embedding storage and ingestion."""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(slots=True, frozen=True)
class EmbeddingProfile:
    """Locked embedding contract for a world."""

    provider_id: str
    model_id: str
    dimensions: int
    task_type: str
    profile_version: int
    title: str | None = None
    extra_settings: dict[str, object] = field(default_factory=dict)

    @property
    def max_input_tokens(self) -> int | None:
        """Return the locked maximum input tokens for the embedding model."""
        # BLOCK 1: Read the locked max-input-token budget from the profile settings when it exists
        # WHY: Max input tokens are model-contract data rather than one-off runtime state, so exposing them through the profile keeps provider enforcement tied to the same world-level lock as dimensions and task type
        raw_value = self.extra_settings.get("max_input_tokens")
        if raw_value is None:
            return None
        return int(raw_value)

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "dimensions": self.dimensions,
            "task_type": self.task_type,
            "profile_version": self.profile_version,
            "title": self.title,
            "extra_settings": dict(self.extra_settings),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "EmbeddingProfile":
        return cls(
            provider_id=str(payload["provider_id"]),
            model_id=str(payload["model_id"]),
            dimensions=int(payload["dimensions"]),
            task_type=str(payload["task_type"]),
            profile_version=int(payload["profile_version"]),
            title=str(payload["title"]) if payload.get("title") is not None else None,
            extra_settings=dict(payload.get("extra_settings", {})),
        )


@dataclass(slots=True)
class WorldMetadata:
    """Stored world identity and embedding contract."""

    world_id: str
    world_uuid: str
    world_name: str
    embedding_profile: EmbeddingProfile

    def to_dict(self) -> dict[str, object]:
        return {
            "world_id": self.world_id,
            "world_uuid": self.world_uuid,
            "world_name": self.world_name,
            "embedding_profile": self.embedding_profile.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "WorldMetadata":
        return cls(
            world_id=str(payload["world_id"]),
            world_uuid=str(payload["world_uuid"]),
            world_name=str(payload["world_name"]),
            embedding_profile=EmbeddingProfile.from_dict(dict(payload["embedding_profile"])),
        )


@dataclass(slots=True)
class EmbeddingChunkState:
    """Embedding progress for one chunk."""

    chunk_number: int
    point_id: str
    status: str = "pending"
    text_hash: str | None = None
    retry_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    last_embedded_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "EmbeddingChunkState":
        return cls(
            chunk_number=int(payload["chunk_number"]),
            point_id=str(payload["point_id"]),
            status=str(payload.get("status", "pending")),
            text_hash=str(payload["text_hash"]) if payload.get("text_hash") is not None else None,
            retry_count=int(payload.get("retry_count", 0)),
            last_error_code=str(payload["last_error_code"]) if payload.get("last_error_code") is not None else None,
            last_error_message=str(payload["last_error_message"]) if payload.get("last_error_message") is not None else None,
            last_embedded_at=str(payload["last_embedded_at"]) if payload.get("last_embedded_at") is not None else None,
        )


@dataclass(slots=True)
class EmbeddingManifest:
    """Per-book embedding progress metadata."""

    world_id: str
    world_uuid: str
    source_filename: str
    book_number: int
    total_chunks: int
    profile: EmbeddingProfile
    chunk_states: list[EmbeddingChunkState]
    warnings: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "world_id": self.world_id,
            "world_uuid": self.world_uuid,
            "source_filename": self.source_filename,
            "book_number": self.book_number,
            "total_chunks": self.total_chunks,
            "profile": self.profile.to_dict(),
            "chunk_states": [state.to_dict() for state in self.chunk_states],
            "warnings": list(self.warnings),
        }

    @classmethod
    def create(
        cls,
        *,
        world_id: str,
        world_uuid: str,
        source_filename: str,
        book_number: int,
        total_chunks: int,
        profile: EmbeddingProfile,
        point_ids: list[str],
    ) -> "EmbeddingManifest":
        return cls(
            world_id=world_id,
            world_uuid=world_uuid,
            source_filename=source_filename,
            book_number=book_number,
            total_chunks=total_chunks,
            profile=profile,
            chunk_states=[
                EmbeddingChunkState(
                    chunk_number=index,
                    point_id=point_ids[index - 1],
                )
                for index in range(1, total_chunks + 1)
            ],
        )

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "EmbeddingManifest":
        return cls(
            world_id=str(payload["world_id"]),
            world_uuid=str(payload["world_uuid"]),
            source_filename=str(payload["source_filename"]),
            book_number=int(payload["book_number"]),
            total_chunks=int(payload["total_chunks"]),
            profile=EmbeddingProfile.from_dict(dict(payload["profile"])),
            chunk_states=[
                EmbeddingChunkState.from_dict(dict(state_payload))
                for state_payload in payload.get("chunk_states", [])
            ],
            warnings=list(payload.get("warnings", [])),
        )

    @property
    def embedded_chunks(self) -> int:
        return sum(1 for state in self.chunk_states if state.status == "embedded")

    @property
    def failed_chunks(self) -> int:
        return sum(1 for state in self.chunk_states if state.status == "failed")

    @property
    def pending_chunks(self) -> int:
        return sum(1 for state in self.chunk_states if state.status == "pending")

    @property
    def status(self) -> str:
        # BLOCK 1: Collapse per-chunk embedding states into one book-level status for result payloads
        # WHY: The caller needs one summary status that distinguishes fully finished books from incomplete books without re-deriving the counts itself
        if self.embedded_chunks == self.total_chunks:
            return "completed"
        if self.failed_chunks > 0 and self.pending_chunks == 0 and self.embedded_chunks == 0:
            return "failed"
        return "partial"

    def append_warning(self, warning_payload: dict[str, object]) -> None:
        # BLOCK 1: Keep warning payloads unique so repeated retries do not bloat the embedding manifest with duplicate notices
        # WHY: Resume and rate-limit logic can surface the same recoverable condition more than once, and duplicate warning entries would make the manifest noisy without adding new information
        if warning_payload not in self.warnings:
            self.warnings.append(warning_payload)


@dataclass(slots=True)
class EmbeddingBookResult:
    """Book-level embedding result summary."""

    status: str
    embedded_chunks: int
    failed_chunks: int
    pending_chunks: int
    manifest_path: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class CredentialModelLimits:
    """Optional per-model scheduler guidance for one credential."""

    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    requests_per_day: int | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CredentialModelLimits":
        return cls(
            requests_per_minute=int(payload["requests_per_minute"]) if payload.get("requests_per_minute") is not None else None,
            tokens_per_minute=int(payload["tokens_per_minute"]) if payload.get("tokens_per_minute") is not None else None,
            requests_per_day=int(payload["requests_per_day"]) if payload.get("requests_per_day") is not None else None,
        )


@dataclass(slots=True, frozen=True)
class ProviderCredential:
    """One provider credential loaded from the user key store."""

    provider_id: str
    credential_name: str
    api_key: str
    project_id: str | None
    allowed_models: frozenset[str]
    model_limits: dict[str, CredentialModelLimits]

    @property
    def quota_scope(self) -> str:
        # BLOCK 1: Collapse credentials that share one provider quota pool into one scheduler scope key
        # WHY: Google AI Studio quotas are project-shared, so scheduling per raw key would overestimate available capacity when several keys point at the same project
        if self.project_id:
            return f"{self.provider_id}:project:{self.project_id}"
        return f"{self.provider_id}:credential:{self.credential_name}"

    @property
    def display_name(self) -> str:
        return self.credential_name or self.api_key

    def supports_model(self, model_id: str) -> bool:
        return not self.allowed_models or model_id in self.allowed_models


@dataclass(slots=True)
class ProviderRuntimeState:
    """Persisted provider cooldown state shared across runs."""

    scope_key: str
    provider_id: str
    credential_name: str
    project_id: str | None = None
    last_limit_type: str | None = None
    cooldown_until_utc: str | None = None
    last_error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ProviderRuntimeState":
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
        if self.cooldown_until_utc is None:
            return None
        return datetime.fromisoformat(self.cooldown_until_utc)


@dataclass(slots=True)
class EmbeddingWorkItem:
    """One chunk waiting for provider embedding."""

    book_number: int
    chunk_number: int
    point_id: str
    chunk_text: str
    text_hash: str
    source_filename: str
    chunk_path: Path
    chunk_position: str


@dataclass(slots=True)
class EmbeddingSuccess:
    """Provider-produced embedding payload before vector-store persistence."""

    work_item: EmbeddingWorkItem
    credential_name: str
    quota_scope: str
    vector: list[float]
    billable_character_count: int | None = None


@dataclass(slots=True)
class EmbeddingFailure:
    """Provider failure for one chunk embedding request."""

    work_item: EmbeddingWorkItem
    credential_name: str
    quota_scope: str
    code: str
    message: str
    retryable: bool
    rate_limit_type: str | None = None
    retry_after_seconds: int | None = None
    billable_token_estimate: int = 0


class EmbeddingRunCancellation:
    """Thread-safe cancellation handle for one embedding run."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()
