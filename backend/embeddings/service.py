"""Embedding orchestration for chunk ingestion."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from uuid import UUID, uuid5

from backend.ingestion.text_sources.models import OperationEvent
from backend.ingestion.text_sources.storage import read_chunk_file
from backend.logger import get_logger
from backend.provider_keys import ProviderKeyScheduler, ProviderRateLimitFailure, default_provider_keys_root

from .errors import EmbeddingConfigurationError, VectorStoreError
from .models import (
    EmbeddingBookResult,
    EmbeddingFailure,
    EmbeddingManifest,
    EmbeddingProfile,
    EmbeddingRunCancellation,
    EmbeddingSuccess,
    EmbeddingWorkItem,
    ProviderCredential,
    WorldMetadata,
)
from .qdrant_store import QdrantChunkStore
from .providers import create_embedding_provider
from .storage import (
    chunk_text_hash,
    default_vector_store_root,
    embedding_manifest_file_path,
    load_embedding_manifest,
    save_embedding_manifest,
    utc_now,
)

_MAX_RETRIES_PER_CHUNK = 3
_DEFAULT_GLOBAL_CONCURRENCY = 4

logger = get_logger(__name__)


def embed_book_chunks(
    *,
    world: WorldMetadata,
    book_dir: Path,
    book_number: int,
    source_filename: str,
    chunk_paths: list[str],
    provider_keys_root: Path | None = None,
    vector_store_root: Path | None = None,
    concurrency: int = _DEFAULT_GLOBAL_CONCURRENCY,
    cancellation: EmbeddingRunCancellation | None = None,
) -> tuple[EmbeddingBookResult, list[OperationEvent]]:
    """Embed one book's chunks into the shared Qdrant store."""
    # BLOCK 1: Resolve storage roots, the shared provider-key scheduler, and the shared vector store before any per-chunk embedding work begins
    # WHY: Failing on missing configuration or a broken vector store up front avoids launching provider requests that could never be persisted safely
    resolved_keys_root = provider_keys_root if provider_keys_root is not None else default_provider_keys_root()
    resolved_vector_root = vector_store_root if vector_store_root is not None else default_vector_store_root()
    warnings: list[OperationEvent] = []
    cancellation_handle = cancellation if cancellation is not None else EmbeddingRunCancellation()
    logger.info(
        "Embedding run started for world_uuid=%s book=%s source=%s model=%s concurrency=%s.",
        world.world_uuid,
        book_number,
        source_filename,
        world.embedding_profile.model_id,
        max(1, concurrency),
    )

    store = QdrantChunkStore(store_root=resolved_vector_root)
    try:
        store.ensure_collection(world.embedding_profile)
        scheduler = ProviderKeyScheduler.for_model(
            provider_id=world.embedding_profile.provider_id,
            model_id=world.embedding_profile.model_id,
            provider_keys_root=resolved_keys_root,
        )

        # BLOCK 2: Load or create the per-book embedding manifest for the active world run, then reconcile it against the current chunk files and Qdrant truth
        # VARS: manifest_path = stable per-book embedding manifest file, manifest = mutable per-book embedding state used for resume and reconciliation, ingestion_run_id = durable world-level run boundary shared by the unfinished ingest
        # WHY: Embedding progress must be tracked independently from chunk persistence, but it still has to snapshot the active ingestion run so stale saved state can be reset before resume trusts it
        manifest_path = embedding_manifest_file_path(book_dir)
        ingestion_run_id = world.active_ingestion_run_id or ""
        manifest = _load_or_create_manifest(
            manifest_path=manifest_path,
            world=world,
            ingestion_run_id=ingestion_run_id,
            book_number=book_number,
            source_filename=source_filename,
            chunk_paths=chunk_paths,
        )
        _reconcile_manifest_with_qdrant(
            manifest=manifest,
            ingestion_run_id=ingestion_run_id,
            chunk_paths=chunk_paths,
            store=store,
        )
        save_embedding_manifest(manifest_path, manifest)

        # BLOCK 3: Stop early with a warning if the user has not configured any eligible provider credentials yet
        # WHY: Missing keys are a user setup issue rather than a corrupt world state, so the ingest should stay resumable and report the reason cleanly
        if not scheduler.credentials:
            logger.warning(
                "Embedding run has no eligible credentials for world_uuid=%s book=%s provider=%s model=%s.",
                world.world_uuid,
                book_number,
                world.embedding_profile.provider_id,
                world.embedding_profile.model_id,
            )
            warning = OperationEvent(
                code="EMBEDDING_PROVIDER_KEYS_MISSING",
                message="No provider credentials are configured for the selected embedding model, so embeddings were left pending.",
                severity="warning",
                book_number=book_number,
                source_filename=source_filename,
            )
            warnings.append(warning)
            manifest.append_warning(warning.to_dict())
            save_embedding_manifest(manifest_path, manifest)
            return _result_from_manifest(manifest, manifest_path), warnings

        # BLOCK 4: Run one-text embedding requests concurrently while serializing Qdrant writes and manifest updates in the main thread
        # WHY: Provider calls benefit from concurrency, but manifest and vector-store writes need one trusted confirmation path so resume state never races ahead of persisted data
        _run_embedding_loop(
            manifest=manifest,
            manifest_path=manifest_path,
            world=world,
            chunk_paths=chunk_paths,
            store=store,
            scheduler=scheduler,
            concurrency=concurrency,
            cancellation=cancellation_handle,
            warnings=warnings,
        )
        scheduler.save_runtime_states()
        save_embedding_manifest(manifest_path, manifest)
        result = _result_from_manifest(manifest, manifest_path)
        logger.info(
            "Embedding run finished for world_uuid=%s book=%s status=%s embedded=%s failed=%s pending=%s warnings=%s.",
            world.world_uuid,
            book_number,
            result.status,
            result.embedded_chunks,
            result.failed_chunks,
            result.pending_chunks,
            len(warnings),
        )
        return result, warnings
    finally:
        store.close()


def _load_or_create_manifest(
    *,
    manifest_path: Path,
    world: WorldMetadata,
    ingestion_run_id: str,
    book_number: int,
    source_filename: str,
    chunk_paths: list[str],
) -> EmbeddingManifest:
    # BLOCK 1: Build the fresh manifest shape for this book and ingestion run before deciding whether any saved state can be reused
    # VARS: point_ids = stable chunk-slot ids that stay fixed across retries, fresh_manifest = brand-new run snapshot used when saved state is missing or stale
    # WHY: Comparing old state against one authoritative manifest shape keeps run resets deterministic instead of editing older manifests piecemeal
    existing_manifest = load_embedding_manifest(manifest_path)
    point_ids = [_chunk_point_id(world_uuid=world.world_uuid, book_number=book_number, chunk_number=index) for index in range(1, len(chunk_paths) + 1)]
    fresh_manifest = EmbeddingManifest.create(
        world_id=world.world_id,
        world_uuid=world.world_uuid,
        ingestion_run_id=ingestion_run_id,
        source_filename=source_filename,
        book_number=book_number,
        total_chunks=len(chunk_paths),
        profile=world.embedding_profile,
        point_ids=point_ids,
    )
    if existing_manifest is None:
        return fresh_manifest
    if existing_manifest.profile != world.embedding_profile:
        raise EmbeddingConfigurationError(
            code="WORLD_EMBEDDING_PROFILE_LOCKED",
            message="The world already has a locked embedding profile that does not match this request.",
            details={"book_number": book_number, "source_filename": source_filename},
        )
    # BLOCK 2: Reset saved embedding progress when it belongs to an older or missing ingestion run boundary
    # WHY: The active ingest run is the provenance boundary for later pipeline stages, so a stale manifest must be redone instead of being treated as trustworthy current progress
    if existing_manifest.ingestion_run_id != ingestion_run_id:
        fresh_manifest.append_warning(
            {
                "code": "EMBEDDING_RUN_STATE_RESET",
                "message": "The embedding manifest belonged to a different ingestion run, so embeddings for this book were reset.",
                "severity": "warning",
                "book_number": book_number,
                "source_filename": source_filename,
            }
        )
        return fresh_manifest
    if existing_manifest.total_chunks != len(chunk_paths):
        raise EmbeddingConfigurationError(
            code="EMBEDDING_MANIFEST_CONFLICT",
            message="The embedding manifest does not match the current chunk set for this book.",
            details={"book_number": book_number, "source_filename": source_filename},
        )
    return existing_manifest


def _reconcile_manifest_with_qdrant(
    *,
    manifest: EmbeddingManifest,
    ingestion_run_id: str,
    chunk_paths: list[str],
    store: QdrantChunkStore,
) -> None:
    # BLOCK 1: Compare the manifest and current chunk files to Qdrant truth so stale hashes, stale run ids, and missing points are repaired before any new provider calls start
    # VARS: existing_points = live Qdrant records keyed by stable point id, stale_point_ids = vectors that no longer match the current chunk text or active run boundary and must be deleted before overwrite
    # WHY: Resume must trust only fully confirmed per-chunk completion, which means both the manifest and Qdrant have to agree with the current chunk text hash and ingestion run id
    existing_points = store.retrieve_existing_points([state.point_id for state in manifest.chunk_states])
    stale_point_ids: list[str] = []
    for state, chunk_path in zip(manifest.chunk_states, chunk_paths, strict=True):
        chunk_payload = read_chunk_file(Path(chunk_path))
        current_hash = chunk_text_hash(str(chunk_payload["chunk_text"]))
        current_point = existing_points.get(state.point_id)

        # BLOCK 2: Reset retry counters for all incomplete chunks so a new run gets a fresh three-attempt budget without erasing the last visible error message
        # WHY: Failed chunks are meant to be resumable on later runs, so carrying forward old retry counts would permanently block chunks after one bad run
        if state.status != "embedded":
            state.retry_count = 0

        if current_point is not None:
            payload_hash = current_point.payload.get("text_hash") if current_point.payload is not None else None
            payload_run_id = current_point.payload.get("ingestion_run_id") if current_point.payload is not None else None
            if payload_hash == current_hash and payload_run_id == ingestion_run_id:
                if state.status != "embedded" or state.text_hash != current_hash:
                    logger.info(
                        "Embedding manifest reconciled to confirmed Qdrant point for world_uuid=%s book=%s chunk=%s point_id=%s.",
                        manifest.world_uuid,
                        manifest.book_number,
                        state.chunk_number,
                        state.point_id,
                    )
                state.status = "embedded"
                state.text_hash = current_hash
                state.last_error_code = None
                state.last_error_message = None
                continue
            logger.warning(
                "Stale Qdrant point detected for world_uuid=%s run=%s book=%s chunk=%s point_id=%s; deleting before overwrite.",
                manifest.world_uuid,
                ingestion_run_id,
                manifest.book_number,
                state.chunk_number,
                state.point_id,
            )
            stale_point_ids.append(state.point_id)

        if state.status == "embedded" and current_point is None:
            logger.warning(
                "Embedding manifest claimed a chunk was embedded but Qdrant point is missing for world_uuid=%s book=%s chunk=%s point_id=%s.",
                manifest.world_uuid,
                manifest.book_number,
                state.chunk_number,
                state.point_id,
            )
        state.status = "pending"
        state.text_hash = current_hash
        state.last_embedded_at = None
    store.delete_points(stale_point_ids)


def _run_embedding_loop(
    *,
    manifest: EmbeddingManifest,
    manifest_path: Path,
    world: WorldMetadata,
    chunk_paths: list[str],
    store: QdrantChunkStore,
    scheduler: ProviderKeyScheduler,
    concurrency: int,
    cancellation: EmbeddingRunCancellation,
    warnings: list[OperationEvent],
) -> None:
    # BLOCK 1: Build the queue of chunks that still need embeddings after reconciliation and skip work completely when every chunk is already embedded
    # VARS: pending_items = chunks that are still pending or failed and should be attempted in this run
    # WHY: Re-reading only the remaining chunk files keeps resume efficient and avoids unnecessary provider calls for chunks that Qdrant already proves are complete
    pending_items = [
        work_item
        for work_item, state in zip(_build_work_items(world=world, book_number=manifest.book_number, chunk_paths=chunk_paths), manifest.chunk_states, strict=True)
        if state.status != "embedded"
    ]
    if not pending_items:
        logger.info(
            "Embedding run skipped for world_uuid=%s book=%s because all chunks are already embedded.",
            world.world_uuid,
            manifest.book_number,
        )
        return

    provider = create_embedding_provider(world.embedding_profile.provider_id)
    futures: dict[Future[EmbeddingSuccess | EmbeddingFailure], tuple[EmbeddingWorkItem, ProviderCredential]] = {}
    pending_queue = pending_items[:]

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        while pending_queue or futures:
            # BLOCK 2: Fill available concurrency slots with chunks that have an eligible credential right now
            # WHY: The run-level concurrency cap smooths provider load, while credential-aware dispatch keeps one cooling-down credential from stalling the entire book
            while not cancellation.is_cancelled and len(futures) < max(1, concurrency) and pending_queue:
                work_item = pending_queue[0]
                credential = scheduler.select_credential(
                    token_estimate=_estimate_tokens(work_item.chunk_text),
                )
                if credential is None:
                    break
                pending_queue.pop(0)
                logger.info(
                    "Dispatching embedding request for world_uuid=%s book=%s chunk=%s using credential=%s quota_scope=%s.",
                    world.world_uuid,
                    work_item.book_number,
                    work_item.chunk_number,
                    credential.display_name,
                    credential.quota_scope,
                )
                future = executor.submit(
                    provider.embed_text,
                    credential=credential,
                    profile=world.embedding_profile,
                    work_item=work_item,
                )
                futures[future] = (work_item, credential)

            if cancellation.is_cancelled:
                # BLOCK 3: Leave any unfinished chunks pending when the caller cancels the run so no late provider response can advance trusted progress state
                # WHY: Cancellation must invalidate in-flight work that returns later, otherwise resume could not tell whether a chunk finished before or after the user stopped the run
                logger.warning(
                    "Embedding run cancelled for world_uuid=%s book=%s with %s request(s) still in flight.",
                    world.world_uuid,
                    manifest.book_number,
                    len(futures),
                )
                for pending_future, (pending_work_item, pending_credential) in list(futures.items()):
                    scheduler.abandon_inflight(
                        scope_key=pending_credential.quota_scope,
                        token_estimate=_estimate_tokens(pending_work_item.chunk_text),
                    )
                    pending_future.cancel()
                futures.clear()
                break

            if not futures:
                if not scheduler.has_future_credential_availability():
                    logger.warning(
                        "Embedding run paused with pending chunks because no credentials are currently usable for world_uuid=%s book=%s.",
                        world.world_uuid,
                        manifest.book_number,
                    )
                    break
                scheduler.wait_for_next_available_credential()
                continue

            done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED)
            for future in done:
                work_item, credential = futures.pop(future)
                outcome = future.result()
                if cancellation.is_cancelled:
                    logger.warning(
                        "Ignoring late embedding response after cancellation for world_uuid=%s book=%s chunk=%s credential=%s.",
                        world.world_uuid,
                        work_item.book_number,
                        work_item.chunk_number,
                        credential.display_name,
                    )
                    scheduler.abandon_inflight(
                        scope_key=credential.quota_scope,
                        token_estimate=_estimate_tokens(work_item.chunk_text),
                    )
                    continue
                if isinstance(outcome, EmbeddingSuccess):
                    scheduler.record_success(
                        scope_key=outcome.quota_scope,
                        token_estimate=_estimate_tokens(work_item.chunk_text),
                    )
                    _persist_embedding_success(
                        manifest=manifest,
                        manifest_path=manifest_path,
                        world=world,
                        store=store,
                        success=outcome,
                    )
                    continue
                _handle_embedding_failure(
                    manifest=manifest,
                    manifest_path=manifest_path,
                    work_item=work_item,
                    credential=credential,
                    failure=outcome,
                    pending_queue=pending_queue,
                    scheduler=scheduler,
                    warnings=warnings,
                )


def _build_work_items(
    *,
    world: WorldMetadata,
    book_number: int,
    chunk_paths: list[str],
) -> list[EmbeddingWorkItem]:
    # BLOCK 1: Turn persisted chunk JSON files into embedding work items so only the chunk text, not the overlap text, reaches the embedding provider
    # WHY: Chunk files are the source of truth for resume, so rebuilding work items from them guarantees the embedding hash always matches what the retriever will later inspect on disk
    work_items: list[EmbeddingWorkItem] = []
    for chunk_path in chunk_paths:
        payload = read_chunk_file(Path(chunk_path))
        chunk_number = int(payload["chunk_number"])
        work_items.append(
            EmbeddingWorkItem(
                book_number=book_number,
                chunk_number=chunk_number,
                point_id=_chunk_point_id(world_uuid=world.world_uuid, book_number=book_number, chunk_number=chunk_number),
                chunk_text=str(payload["chunk_text"]),
                text_hash=chunk_text_hash(str(payload["chunk_text"])),
                source_filename=str(payload["source_filename"]),
                chunk_path=Path(chunk_path),
                chunk_position=str(payload["chunk_position"]),
            )
        )
    return work_items


def _persist_embedding_success(
    *,
    manifest: EmbeddingManifest,
    manifest_path: Path,
    world: WorldMetadata,
    store: QdrantChunkStore,
    success: EmbeddingSuccess,
) -> None:
    # BLOCK 1: Upsert the confirmed vector into Qdrant and only then mark the chunk embedded in the manifest
    # WHY: The embedding manifest must never move ahead of the vector store, or resume would incorrectly believe retrieval data exists when it does not
    state = manifest.chunk_states[success.work_item.chunk_number - 1]
    store.upsert_chunk_embedding(
        world=world,
        ingestion_run_id=manifest.ingestion_run_id,
        work_item=success.work_item,
        vector=success.vector,
        profile=world.embedding_profile,
    )
    state.status = "embedded"
    state.text_hash = success.work_item.text_hash
    state.retry_count = 0
    state.last_error_code = None
    state.last_error_message = None
    state.last_embedded_at = utc_now().isoformat()
    save_embedding_manifest(manifest_path, manifest)
    logger.info(
        "Embedding persisted for world_uuid=%s book=%s chunk=%s point_id=%s credential=%s.",
        world.world_uuid,
        success.work_item.book_number,
        success.work_item.chunk_number,
        success.work_item.point_id,
        success.credential_name,
    )


def _handle_embedding_failure(
    *,
    manifest: EmbeddingManifest,
    manifest_path: Path,
    work_item: EmbeddingWorkItem,
    credential: ProviderCredential,
    failure: EmbeddingFailure,
    pending_queue: list[EmbeddingWorkItem],
    scheduler: ProviderKeyScheduler,
    warnings: list[OperationEvent],
) -> None:
    # BLOCK 1: Update runtime cooldown state and only spend chunk retries for non-quota provider failures
    # WHY: Rate limits are key availability problems that should fail over to another credential, while timeouts and provider crashes are ordinary chunk attempts that can exhaust the three-try budget
    state = manifest.chunk_states[work_item.chunk_number - 1]
    state.last_error_code = failure.code
    state.last_error_message = failure.message
    if failure.rate_limit_type is None:
        state.retry_count += 1
    logger.warning(
        "Embedding request failed for world_uuid=%s book=%s chunk=%s credential=%s code=%s retryable=%s retry_count=%s.",
        manifest.world_uuid,
        work_item.book_number,
        work_item.chunk_number,
        credential.display_name,
        failure.code,
        failure.retryable,
        state.retry_count,
    )

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
        warning = OperationEvent(
            code="EMBEDDING_PROVIDER_RATE_LIMITED",
            message=f"{failure.credential_name} hit {failure.rate_limit_type.upper()} limits and was cooled down.",
            severity="warning",
            book_number=manifest.book_number,
            source_filename=manifest.source_filename,
        )
        warnings.append(warning)
        manifest.append_warning(warning.to_dict())
        logger.warning(
            "Credential=%s quota_scope=%s hit %s for world_uuid=%s book=%s chunk=%s.",
            credential.display_name,
            credential.quota_scope,
            failure.rate_limit_type.upper(),
            manifest.world_uuid,
            work_item.book_number,
            work_item.chunk_number,
        )
    else:
        # BLOCK 2: Roll back the scheduler reservation when the failure is not a quota signal
        # WHY: The scheduler reserves before dispatch now, and non-rate-limit failures should not make that key/model look artificially exhausted
        scheduler.release_reservation(
            scope_key=failure.quota_scope,
            token_estimate=failure.billable_token_estimate,
        )

    if failure.retryable and state.retry_count < _MAX_RETRIES_PER_CHUNK:
        state.status = "pending"
        pending_queue.append(work_item)
        logger.info(
            "Requeued chunk for embedding retry world_uuid=%s book=%s chunk=%s next_attempt=%s.",
            manifest.world_uuid,
            work_item.book_number,
            work_item.chunk_number,
            state.retry_count + 1,
        )
    else:
        state.status = "failed"
        logger.error(
            "Chunk embedding marked failed for world_uuid=%s book=%s chunk=%s after %s attempt(s).",
            manifest.world_uuid,
            work_item.book_number,
            work_item.chunk_number,
            state.retry_count,
        )
    save_embedding_manifest(manifest_path, manifest)


def _result_from_manifest(manifest: EmbeddingManifest, manifest_path: Path) -> EmbeddingBookResult:
    return EmbeddingBookResult(
        status=manifest.status,
        embedded_chunks=manifest.embedded_chunks,
        failed_chunks=manifest.failed_chunks,
        pending_chunks=manifest.pending_chunks,
        manifest_path=str(manifest_path),
    )


def _chunk_point_id(*, world_uuid: str, book_number: int, chunk_number: int) -> str:
    # BLOCK 1: Derive a deterministic UUID point id from the stable world UUID plus the chunk slot coordinates
    # WHY: Qdrant local mode accepts UUID point ids cleanly, and deriving them from slot coordinates keeps overwrites and resume behavior stable without baking the text hash into the identity
    return str(uuid5(UUID(world_uuid), f"book:{book_number}:chunk:{chunk_number}"))


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)
