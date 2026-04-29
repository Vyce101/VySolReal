"""Chunk vector similarity retrieval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.context import build_model_context_from_chunk_texts
from backend.embeddings.errors import EmbeddingConfigurationError, EmbeddingProviderError, VectorStoreError
from backend.embeddings.models import (
    QueryEmbeddingFailure,
    QueryEmbeddingSuccess,
    WorldMetadata,
)
from backend.embeddings.providers import create_embedding_provider
from backend.embeddings.qdrant_store import QdrantChunkStore
from backend.embeddings.storage import (
    chunk_text_hash,
    default_vector_store_root,
    embedding_manifest_file_path,
    load_embedding_manifest,
    save_embedding_manifest,
    world_metadata_file_path,
)
from backend.ingestion.text_sources.storage import read_chunk_file
from backend.logger import get_logger
from backend.provider_keys import ProviderKeyScheduler, ProviderRateLimitFailure, default_provider_keys_root
from backend.provider_keys.errors import ProviderKeyConfigurationError

from .models import ChunkRetrievalResponse, RetrievedChunk, RetrievalEvent

DEFAULT_TOP_K = 10
DEFAULT_SIMILARITY_MINIMUM = 0.15

logger = get_logger(__name__)


def retrieve_similar_chunks(
    *,
    world_dir: str | Path,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    similarity_minimum: float = DEFAULT_SIMILARITY_MINIMUM,
    provider_keys_root: str | Path | None = None,
    vector_store_root: str | Path | None = None,
) -> ChunkRetrievalResponse:
    """Return the most similar embedded chunks for one world."""
    # BLOCK 1: Validate caller-controlled retrieval settings before any provider or vector-store work starts
    # WHY: Invalid settings are request problems, and returning structured errors here keeps provider calls from hiding simple caller mistakes
    validation_errors = _validate_settings(
        top_k=top_k,
        similarity_minimum=similarity_minimum,
    )
    if validation_errors:
        return _response(
            success=False,
            world_uuid=None,
            requested_top_k=top_k if isinstance(top_k, int) else 0,
            top_k=0,
            similarity_minimum=float(similarity_minimum) if isinstance(similarity_minimum, int | float) else DEFAULT_SIMILARITY_MINIMUM,
            embedded_chunk_count=0,
            errors=validation_errors,
        )

    # BLOCK 2: Load the world metadata and count embedded chunk states so retrieval can stay tied to the world's locked embedding contract
    # VARS: resolved_world_dir = app-owned world folder, embedded_chunk_count = number of chunks whose embedding manifest says vector-side data exists
    # WHY: Qdrant collections are profile-specific and shared across worlds, so retrieval needs both the profile and world UUID before it can search safely
    resolved_world_dir = Path(world_dir)
    try:
        world = _load_world_metadata(resolved_world_dir)
    except EmbeddingConfigurationError as error:
        return _response(
            success=False,
            world_uuid=None,
            requested_top_k=top_k,
            top_k=0,
            similarity_minimum=similarity_minimum,
            embedded_chunk_count=0,
            errors=[_event_from_exception(error)],
        )

    embedded_chunk_count = _count_embedded_chunks(resolved_world_dir)
    clamped_top_k = min(top_k, embedded_chunk_count)
    logger.info(
        "Chunk retrieval requested world_uuid=%s top_k=%s clamped_top_k=%s similarity_minimum=%s query_length=%s embedded_chunks=%s.",
        world.world_uuid,
        top_k,
        clamped_top_k,
        similarity_minimum,
        len(query.strip()),
        embedded_chunk_count,
    )

    # BLOCK 3: Support explicit no-chunk retrieval after world validation without spending a provider call or touching Qdrant
    # WHY: Future GraphRAG callers may intentionally request no vector chunks while still needing the same response shape
    if top_k == 0:
        return _response(
            success=True,
            world_uuid=world.world_uuid,
            requested_top_k=top_k,
            top_k=0,
            similarity_minimum=similarity_minimum,
            embedded_chunk_count=embedded_chunk_count,
        )

    if not query.strip():
        return _response(
            success=False,
            world_uuid=world.world_uuid,
            requested_top_k=top_k,
            top_k=clamped_top_k,
            similarity_minimum=similarity_minimum,
            embedded_chunk_count=embedded_chunk_count,
            errors=[
                RetrievalEvent(
                    code="EMPTY_RETRIEVAL_QUERY",
                    message="Retrieval query text must not be empty.",
                    severity="error",
                )
            ],
        )

    # BLOCK 4: Return an empty retrieval result before provider work when the world has no confirmed embedded chunks
    # WHY: Query embedding cannot produce useful chunk results without any searchable vectors, so skipping the provider call avoids unnecessary key usage
    if embedded_chunk_count == 0:
        return _response(
            success=True,
            world_uuid=world.world_uuid,
            requested_top_k=top_k,
            top_k=0,
            similarity_minimum=similarity_minimum,
            embedded_chunk_count=embedded_chunk_count,
            warnings=[
                RetrievalEvent(
                    code="WORLD_HAS_NO_EMBEDDED_CHUNKS",
                    message="The world has no embedded chunks available for retrieval.",
                )
            ],
        )

    query_embedding, embedding_error = _embed_query(
        world=world,
        query=query.strip(),
        provider_keys_root=Path(provider_keys_root) if provider_keys_root is not None else None,
    )
    if embedding_error is not None:
        return _response(
            success=False,
            world_uuid=world.world_uuid,
            requested_top_k=top_k,
            top_k=clamped_top_k,
            similarity_minimum=similarity_minimum,
            embedded_chunk_count=embedded_chunk_count,
            errors=[embedding_error],
        )

    # BLOCK 5: Ask Qdrant for the world's best chunks using the threshold inside the vector query, then validate each returned file-backed chunk
    # WHY: Qdrant owns similarity ranking and score filtering, while World Storage remains the trusted source for chunk text returned to callers
    warnings: list[RetrievalEvent] = []
    store = QdrantChunkStore(
        store_root=Path(vector_store_root) if vector_store_root is not None else default_vector_store_root()
    )
    try:
        store.ensure_collection(world.embedding_profile)
        scored_points = store.query_similar_chunks(
            query_vector=query_embedding,
            world_uuid=world.world_uuid,
            limit=clamped_top_k,
            score_threshold=similarity_minimum,
        )
        results = _validated_results_from_points(
            world_dir=resolved_world_dir,
            world=world,
            scored_points=scored_points,
            store=store,
            warnings=warnings,
        )
    except VectorStoreError as error:
        return _response(
            success=False,
            world_uuid=world.world_uuid,
            requested_top_k=top_k,
            top_k=clamped_top_k,
            similarity_minimum=similarity_minimum,
            embedded_chunk_count=embedded_chunk_count,
            errors=[_event_from_exception(error)],
        )
    finally:
        store.close()

    results.sort(key=lambda result: (-result.score, result.book_number, result.chunk_number))
    model_context = build_model_context_from_chunk_texts([result.chunk_text for result in results])
    logger.info(
        "Chunk retrieval completed world_uuid=%s requested_top_k=%s returned_chunks=%s warnings=%s.",
        world.world_uuid,
        top_k,
        len(results),
        len(warnings),
    )
    return _response(
        success=True,
        world_uuid=world.world_uuid,
        requested_top_k=top_k,
        top_k=clamped_top_k,
        similarity_minimum=similarity_minimum,
        embedded_chunk_count=embedded_chunk_count,
        results=results,
        warnings=warnings,
        model_context=model_context,
    )


def _validate_settings(*, top_k: int, similarity_minimum: float) -> list[RetrievalEvent]:
    # BLOCK 1: Validate only retrieval settings that are independent of world state
    # WHY: Keeping request validation separate from world loading lets callers get precise errors for malformed retrieval knobs
    errors: list[RetrievalEvent] = []
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 0:
        errors.append(
            RetrievalEvent(
                code="INVALID_TOP_K",
                message="top_k must be zero or greater.",
                severity="error",
            )
        )
    if not isinstance(similarity_minimum, int | float) or isinstance(similarity_minimum, bool) or not 0.0 <= float(similarity_minimum) <= 1.0:
        errors.append(
            RetrievalEvent(
                code="INVALID_SIMILARITY_MINIMUM",
                message="similarity_minimum must be between 0.0 and 1.0.",
                severity="error",
            )
        )
    return errors


def _load_world_metadata(world_dir: Path) -> WorldMetadata:
    # BLOCK 1: Read the existing locked world metadata without creating or modifying the world
    # WHY: Retrieval must operate on an already-ingested world, and silently creating metadata here would hide a broken storage state
    metadata_path = world_metadata_file_path(world_dir)
    if not metadata_path.exists():
        raise EmbeddingConfigurationError(
            code="WORLD_METADATA_MISSING",
            message="The world metadata file is missing.",
            details={"world_dir": str(world_dir)},
        )
    return WorldMetadata.from_dict(json.loads(metadata_path.read_text(encoding="utf-8")))


def _count_embedded_chunks(world_dir: Path) -> int:
    # BLOCK 1: Count confirmed embedded chunk states across every book embedding manifest in the world
    # WHY: top_k should clamp to what the embedding manifests say is currently available rather than an arbitrary global cap
    total = 0
    for manifest_path in sorted(world_dir.glob("books/book_*/embeddings.json")):
        manifest = load_embedding_manifest(manifest_path)
        if manifest is not None:
            total += manifest.embedded_chunks
    return total


def _embed_query(
    *,
    world: WorldMetadata,
    query: str,
    provider_keys_root: Path | None,
) -> tuple[list[float], RetrievalEvent | None]:
    # BLOCK 1: Use the shared provider-key scheduler to embed one retrieval query with the world's locked embedding model
    # WHY: Query retrieval must respect the same enabled-key, failover, and cooldown contract as chunk embedding without duplicating key-selection logic
    resolved_keys_root = provider_keys_root if provider_keys_root is not None else default_provider_keys_root()
    try:
        scheduler = ProviderKeyScheduler.for_model(
            provider_id=world.embedding_profile.provider_id,
            model_id=world.embedding_profile.model_id,
            provider_keys_root=resolved_keys_root,
        )
    except ProviderKeyConfigurationError as error:
        return [], _event_from_exception(error)

    if not scheduler.credentials:
        return [], RetrievalEvent(
            code="RETRIEVAL_PROVIDER_KEYS_MISSING",
            message="No provider credentials are configured for the selected embedding model.",
            severity="error",
            details={
                "provider_id": world.embedding_profile.provider_id,
                "model_id": world.embedding_profile.model_id,
            },
        )

    token_estimate = _estimate_tokens(query)
    credential = scheduler.select_credential(token_estimate=token_estimate)
    if credential is None:
        return [], RetrievalEvent(
            code="RETRIEVAL_PROVIDER_UNAVAILABLE",
            message="No provider credential is currently available for query embedding.",
            severity="error",
            details={
                "provider_id": world.embedding_profile.provider_id,
                "model_id": world.embedding_profile.model_id,
            },
        )

    provider = create_embedding_provider(world.embedding_profile.provider_id)
    try:
        outcome = provider.embed_query(
            credential=credential,
            profile=world.embedding_profile,
            query=query,
        )
    except (EmbeddingConfigurationError, EmbeddingProviderError) as error:
        scheduler.release_reservation(scope_key=credential.quota_scope, token_estimate=token_estimate)
        return [], _event_from_exception(error)
    except Exception as exc:
        scheduler.release_reservation(scope_key=credential.quota_scope, token_estimate=token_estimate)
        logger.error(
            "Query embedding crashed for world_uuid=%s provider=%s model=%s reason=%s.",
            world.world_uuid,
            world.embedding_profile.provider_id,
            world.embedding_profile.model_id,
            str(exc),
        )
        return [], RetrievalEvent(
            code="RETRIEVAL_QUERY_PROVIDER_FAILED",
            message=str(exc),
            severity="error",
        )

    if isinstance(outcome, QueryEmbeddingSuccess):
        scheduler.record_success(scope_key=outcome.quota_scope, token_estimate=token_estimate)
        return outcome.vector, None
    return [], _handle_query_embedding_failure(
        scheduler=scheduler,
        credential=credential,
        failure=outcome,
    )


def _handle_query_embedding_failure(
    *,
    scheduler: ProviderKeyScheduler,
    credential: Any,
    failure: QueryEmbeddingFailure,
) -> RetrievalEvent:
    # BLOCK 1: Update shared scheduler state from a failed query embedding before returning the retrieval error
    # WHY: Provider rate limits affect future AI workflows too, so retrieval must persist cooldowns instead of treating the query failure as local-only
    if failure.rate_limit_type is not None:
        scheduler.apply_rate_limit_failure(
            credential=credential,
            failure=ProviderRateLimitFailure(
                rate_limit_type=failure.rate_limit_type,
                message=failure.message,
                retry_after_seconds=failure.retry_after_seconds,
                limit_scope=failure.rate_limit_scope,
            ),
        )
    else:
        scheduler.release_reservation(
            scope_key=failure.quota_scope,
            token_estimate=failure.billable_token_estimate,
        )
    return RetrievalEvent(
        code=failure.code,
        message=failure.message,
        severity="error",
        details={"credential_name": failure.credential_name},
    )


def _validated_results_from_points(
    *,
    world_dir: Path,
    world: WorldMetadata,
    scored_points: list[Any],
    store: QdrantChunkStore,
    warnings: list[RetrievalEvent],
) -> list[RetrievedChunk]:
    # BLOCK 1: Turn Qdrant score hits into file-backed chunks while repairing stale or missing vector truth
    # WHY: Qdrant finds candidate vectors, but chunk files remain the source of truth for the text that can be sent back to callers
    results: list[RetrievedChunk] = []
    for point in scored_points:
        payload = dict(point.payload or {})
        point_id = str(point.id)
        book_number = int(payload.get("book_number", 0))
        chunk_number = int(payload.get("chunk_number", 0))
        chunk_path = world_dir / str(payload.get("chunk_file", ""))
        if not chunk_path.exists():
            warning = RetrievalEvent(
                code="RETRIEVAL_CHUNK_FILE_MISSING",
                message="A retrieved vector pointed to a missing chunk file and was skipped.",
                details={"point_id": point_id, "book_number": book_number, "chunk_number": chunk_number},
            )
            logger.warning(
                "Retrieved Qdrant point skipped because chunk file is missing world_uuid=%s point_id=%s book=%s chunk=%s.",
                world.world_uuid,
                point_id,
                book_number,
                chunk_number,
            )
            warnings.append(warning)
            _mark_embedding_pending(
                world_dir=world_dir,
                book_number=book_number,
                chunk_number=chunk_number,
                code=warning.code,
                message=warning.message,
                text_hash=None,
            )
            continue

        chunk_payload = read_chunk_file(chunk_path)
        current_hash = chunk_text_hash(str(chunk_payload["chunk_text"]))
        if payload.get("text_hash") != current_hash:
            warning = RetrievalEvent(
                code="RETRIEVAL_CHUNK_VECTOR_STALE",
                message="A retrieved vector no longer matches its chunk file and was skipped.",
                details={"point_id": point_id, "book_number": book_number, "chunk_number": chunk_number},
            )
            logger.warning(
                "Retrieved Qdrant point skipped and deleted because text hash is stale world_uuid=%s point_id=%s book=%s chunk=%s.",
                world.world_uuid,
                point_id,
                book_number,
                chunk_number,
            )
            warnings.append(warning)
            store.delete_points([point_id])
            _mark_embedding_pending(
                world_dir=world_dir,
                book_number=book_number,
                chunk_number=chunk_number,
                code=warning.code,
                message=warning.message,
                text_hash=current_hash,
            )
            continue

        results.append(
            RetrievedChunk(
                world_uuid=world.world_uuid,
                point_id=point_id,
                score=float(point.score),
                book_number=book_number,
                chunk_number=chunk_number,
                chunk_position=str(chunk_payload["chunk_position"]),
                source_filename=str(chunk_payload["source_filename"]),
                chunk_text=str(chunk_payload["chunk_text"]),
                overlap_text=str(chunk_payload["overlap_text"]),
            )
        )
    return results


def _mark_embedding_pending(
    *,
    world_dir: Path,
    book_number: int,
    chunk_number: int,
    code: str,
    message: str,
    text_hash: str | None,
) -> None:
    # BLOCK 1: Mark one embedding manifest chunk state as pending after retrieval proves vector truth is stale or incomplete
    # WHY: The future UI needs the manifest to show that a chunk can be repaired by resuming embeddings instead of trusting a broken vector hit
    manifest_path = embedding_manifest_file_path(world_dir / "books" / f"book_{book_number:02d}")
    manifest = load_embedding_manifest(manifest_path)
    if manifest is None or chunk_number < 1 or chunk_number > len(manifest.chunk_states):
        logger.warning(
            "Could not mark embedding pending because manifest state was missing world_dir=%s book=%s chunk=%s code=%s.",
            world_dir,
            book_number,
            chunk_number,
            code,
        )
        return
    state = manifest.chunk_states[chunk_number - 1]
    state.status = "pending"
    state.text_hash = text_hash
    state.last_embedded_at = None
    state.last_error_code = code
    state.last_error_message = message
    manifest.append_warning(
        RetrievalEvent(
            code=code,
            message=message,
            details={"book_number": book_number, "chunk_number": chunk_number},
        ).to_dict()
    )
    save_embedding_manifest(manifest_path, manifest)


def _response(
    *,
    success: bool,
    world_uuid: str | None,
    requested_top_k: int,
    top_k: int,
    similarity_minimum: float,
    embedded_chunk_count: int,
    results: list[RetrievedChunk] | None = None,
    model_context: Any | None = None,
    warnings: list[RetrievalEvent] | None = None,
    errors: list[RetrievalEvent] | None = None,
) -> ChunkRetrievalResponse:
    return ChunkRetrievalResponse(
        success=success,
        world_uuid=world_uuid,
        requested_top_k=requested_top_k,
        top_k=top_k,
        similarity_minimum=similarity_minimum,
        embedded_chunk_count=embedded_chunk_count,
        results=results if results is not None else [],
        model_context=model_context if model_context is not None else build_model_context_from_chunk_texts([]),
        warnings=warnings if warnings is not None else [],
        errors=errors if errors is not None else [],
    )


def _event_from_exception(error: Any) -> RetrievalEvent:
    return RetrievalEvent(
        code=error.code,
        message=error.message,
        severity="error",
        details=dict(getattr(error, "details", {})),
    )


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)
