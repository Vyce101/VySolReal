"""Models for raw graph extraction and resume manifests."""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field


@dataclass(slots=True, frozen=True)
class GraphExtractionConfig:
    """World-level graph extraction settings used by one run snapshot."""

    provider_id: str
    model_id: str
    gleaning_count: int = 1
    extraction_concurrency: int = 5
    prompt_preset_id: str = "default"
    prompt_preset_version: int = 1
    parser_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "GraphExtractionConfig":
        # BLOCK 1: Rebuild the saved run configuration from manifest JSON
        # WHY: Extraction settings are locked per run, so resume must use the manifest snapshot rather than whatever world-level defaults changed later
        return cls(
            provider_id=str(payload["provider_id"]),
            model_id=str(payload["model_id"]),
            gleaning_count=int(payload.get("gleaning_count", 1)),
            extraction_concurrency=int(payload.get("extraction_concurrency", 5)),
            prompt_preset_id=str(payload.get("prompt_preset_id", "default")),
            prompt_preset_version=int(payload.get("prompt_preset_version", 1)),
            parser_version=int(payload.get("parser_version", 1)),
        )


@dataclass(slots=True)
class RawExtractedNode:
    """One raw node candidate after final pass merging."""

    node_id: str
    display_name: str
    description: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "RawExtractedNode":
        return cls(
            node_id=str(payload["node_id"]),
            display_name=str(payload["display_name"]),
            description=str(payload["description"]),
        )


@dataclass(slots=True)
class RawExtractedEdge:
    """One raw edge candidate after final pass validation."""

    edge_id: str
    source_node_id: str
    target_node_id: str
    source_display_name: str
    target_display_name: str
    description: str
    strength: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "RawExtractedEdge":
        return cls(
            edge_id=str(payload["edge_id"]),
            source_node_id=str(payload["source_node_id"]),
            target_node_id=str(payload["target_node_id"]),
            source_display_name=str(payload["source_display_name"]),
            target_display_name=str(payload["target_display_name"]),
            description=str(payload["description"]),
            strength=int(payload["strength"]),
        )


@dataclass(slots=True)
class ExtractionPassRecord:
    """Trusted parsed data from one LLM extraction or gleaning call."""

    pass_type: str
    pass_number: int
    nodes: list[dict[str, str]]
    edges: list[dict[str, object]]
    provider_id: str
    model_id: str
    prompt_preset_id: str
    prompt_preset_version: int

    def to_dict(self) -> dict[str, object]:
        return {
            "pass_type": self.pass_type,
            "pass_number": self.pass_number,
            "nodes": [dict(node) for node in self.nodes],
            "edges": [dict(edge) for edge in self.edges],
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "prompt_preset_id": self.prompt_preset_id,
            "prompt_preset_version": self.prompt_preset_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ExtractionPassRecord":
        return cls(
            pass_type=str(payload["pass_type"]),
            pass_number=int(payload["pass_number"]),
            nodes=[dict(node) for node in payload.get("nodes", [])],
            edges=[dict(edge) for edge in payload.get("edges", [])],
            provider_id=str(payload.get("provider_id", "google")),
            model_id=str(payload["model_id"]),
            prompt_preset_id=str(payload["prompt_preset_id"]),
            prompt_preset_version=int(payload["prompt_preset_version"]),
        )


@dataclass(slots=True)
class GraphExtractionChunkState:
    """Extraction progress for one chunk."""

    chunk_number: int
    chunk_file: str
    status: str = "pending"
    retry_count: int = 0
    glean_retry_count: int = 0
    text_hash: str | None = None
    initial_pass: ExtractionPassRecord | None = None
    glean_passes: list[ExtractionPassRecord] = field(default_factory=list)
    nodes: list[RawExtractedNode] = field(default_factory=list)
    edges: list[RawExtractedEdge] = field(default_factory=list)
    last_error_code: str | None = None
    last_error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        # BLOCK 1: Serialize optional chunk state fields only when they carry useful resume data
        # WHY: The extraction manifest is user-local support state, so compact JSON makes failed/resumed chunks easier to inspect without losing pass history
        payload: dict[str, object] = {
            "chunk_number": self.chunk_number,
            "chunk_file": self.chunk_file,
            "status": self.status,
            "retry_count": self.retry_count,
            "glean_retry_count": self.glean_retry_count,
            "glean_passes": [glean_pass.to_dict() for glean_pass in self.glean_passes],
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }
        if self.text_hash is not None:
            payload["text_hash"] = self.text_hash
        if self.initial_pass is not None:
            payload["initial_pass"] = self.initial_pass.to_dict()
        if self.last_error_code is not None:
            payload["last_error_code"] = self.last_error_code
        if self.last_error_message is not None:
            payload["last_error_message"] = self.last_error_message
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "GraphExtractionChunkState":
        # BLOCK 1: Rebuild a chunk's pass history and final candidates from manifest JSON
        # WHY: Resume must continue incomplete gleans without rerunning trusted initial extraction data that was already saved before a crash
        initial_payload = payload.get("initial_pass")
        return cls(
            chunk_number=int(payload["chunk_number"]),
            chunk_file=str(payload["chunk_file"]),
            status=str(payload.get("status", "pending")),
            retry_count=int(payload.get("retry_count", 0)),
            glean_retry_count=int(payload.get("glean_retry_count", 0)),
            text_hash=str(payload["text_hash"]) if payload.get("text_hash") is not None else None,
            initial_pass=ExtractionPassRecord.from_dict(dict(initial_payload)) if initial_payload is not None else None,
            glean_passes=[
                ExtractionPassRecord.from_dict(dict(pass_payload))
                for pass_payload in payload.get("glean_passes", [])
            ],
            nodes=[
                RawExtractedNode.from_dict(dict(node_payload))
                for node_payload in payload.get("nodes", [])
            ],
            edges=[
                RawExtractedEdge.from_dict(dict(edge_payload))
                for edge_payload in payload.get("edges", [])
            ],
            last_error_code=str(payload["last_error_code"]) if payload.get("last_error_code") is not None else None,
            last_error_message=str(payload["last_error_message"]) if payload.get("last_error_message") is not None else None,
        )


@dataclass(slots=True)
class GraphExtractionManifest:
    """Per-book graph extraction progress metadata."""

    world_id: str
    world_uuid: str
    ingestion_run_id: str
    source_filename: str
    book_number: int
    total_chunks: int
    config: GraphExtractionConfig
    chunk_states: list[GraphExtractionChunkState]
    warnings: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "world_id": self.world_id,
            "world_uuid": self.world_uuid,
            "ingestion_run_id": self.ingestion_run_id,
            "source_filename": self.source_filename,
            "book_number": self.book_number,
            "total_chunks": self.total_chunks,
            "config": self.config.to_dict(),
            "chunk_states": [state.to_dict() for state in self.chunk_states],
            "warnings": list(self.warnings),
        }

    @classmethod
    def create(
        cls,
        *,
        world_id: str,
        world_uuid: str,
        ingestion_run_id: str,
        source_filename: str,
        book_number: int,
        chunk_paths: list[str],
        config: GraphExtractionConfig,
    ) -> "GraphExtractionManifest":
        return cls(
            world_id=world_id,
            world_uuid=world_uuid,
            ingestion_run_id=ingestion_run_id,
            source_filename=source_filename,
            book_number=book_number,
            total_chunks=len(chunk_paths),
            config=config,
            chunk_states=[
                GraphExtractionChunkState(
                    chunk_number=index,
                    chunk_file=chunk_path,
                )
                for index, chunk_path in enumerate(chunk_paths, start=1)
            ],
        )

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "GraphExtractionManifest":
        return cls(
            world_id=str(payload["world_id"]),
            world_uuid=str(payload["world_uuid"]),
            ingestion_run_id=str(payload["ingestion_run_id"]),
            source_filename=str(payload["source_filename"]),
            book_number=int(payload["book_number"]),
            total_chunks=int(payload["total_chunks"]),
            config=GraphExtractionConfig.from_dict(dict(payload["config"])),
            chunk_states=[
                GraphExtractionChunkState.from_dict(dict(state_payload))
                for state_payload in payload.get("chunk_states", [])
            ],
            warnings=list(payload.get("warnings", [])),
        )

    @property
    def extracted_chunks(self) -> int:
        return sum(1 for state in self.chunk_states if state.status in {"extracted", "skipped"})

    @property
    def failed_chunks(self) -> int:
        return sum(1 for state in self.chunk_states if state.status == "failed")

    @property
    def pending_chunks(self) -> int:
        return sum(1 for state in self.chunk_states if state.status in {"pending", "partial"})

    @property
    def status(self) -> str:
        # BLOCK 1: Collapse per-chunk extraction states into one book-level status for callers
        # WHY: The future UI needs one summary state without re-deriving counts from every chunk record
        if self.extracted_chunks == self.total_chunks:
            return "completed"
        if self.failed_chunks > 0 and self.pending_chunks == 0 and self.extracted_chunks == 0:
            return "failed"
        return "partial"


@dataclass(slots=True)
class GraphExtractionBookResult:
    """Book-level extraction summary."""

    status: str
    extracted_chunks: int
    failed_chunks: int
    pending_chunks: int
    manifest_path: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class GraphExtractionWorkItem:
    """One chunk waiting for graph extraction."""

    world_uuid: str
    ingestion_run_id: str
    source_filename: str
    book_number: int
    chunk_number: int
    chunk_file: str
    chunk_position: str
    chunk_text: str
    overlap_text: str
    text_hash: str


@dataclass(slots=True)
class ExtractionProviderSuccess:
    """Provider-produced extraction text before parser validation."""

    response_text: str
    credential_name: str
    quota_scope: str


@dataclass(slots=True)
class ExtractionProviderFailure:
    """Provider failure for one graph extraction request."""

    credential_name: str
    quota_scope: str
    code: str
    message: str
    retryable: bool
    rate_limit_type: str | None = None
    rate_limit_scope: str = "model"
    retry_after_seconds: int | None = None
    billable_token_estimate: int = 0


class GraphExtractionRunCancellation:
    """Thread-safe pause/cancellation handle for one graph extraction run."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()
