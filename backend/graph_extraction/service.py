"""Async graph extraction orchestration for persisted chunks."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Lock

from backend.ingestion.txt_splitting.models import OperationEvent
from backend.ingestion.txt_splitting.storage import read_chunk_file
from backend.logger import get_logger
from backend.provider_keys import ProviderKeyScheduler, ProviderRateLimitFailure, default_provider_keys_root

from .errors import GraphExtractionError, GraphExtractionParseError
from .models import (
    ExtractionPassRecord,
    ExtractionProviderFailure,
    ExtractionProviderSuccess,
    GraphExtractionBookResult,
    GraphExtractionChunkState,
    GraphExtractionConfig,
    GraphExtractionManifest,
    GraphExtractionRunCancellation,
    GraphExtractionWorkItem,
    RawExtractedEdge,
    RawExtractedNode,
)
from .parser import merge_pass_records, parse_extraction_response
from .prompts import build_gleaning_prompt, build_initial_prompt
from .providers import create_graph_extraction_provider
from .storage import (
    chunk_text_hash,
    extraction_manifest_file_path,
    load_extraction_manifest,
    save_extraction_manifest,
)

_MAX_RETRIES_PER_CHUNK = 3
_DEFAULT_EXTRACTION_CONCURRENCY = 5

logger = get_logger(__name__)


def extract_book_chunks(
    *,
    world_id: str,
    world_uuid: str,
    ingestion_run_id: str,
    book_dir: Path,
    book_number: int,
    source_filename: str,
    chunk_paths: list[str],
    config: GraphExtractionConfig,
    provider_keys_root: Path | None = None,
    cancellation: GraphExtractionRunCancellation | None = None,
) -> tuple[GraphExtractionBookResult, list[OperationEvent]]:
    """Extract raw graph candidates from one book's chunks."""
    # BLOCK 1: Validate app-owned identity fields before provider work starts and mark the chunks failed if the app lost the run boundary
    # WHY: Missing world or run identity is an app invariant failure, but saving failed chunk states keeps resume able to retest after the app bug is fixed instead of silently losing the work
    if not world_uuid or not ingestion_run_id:
        manifest_path = extraction_manifest_file_path(book_dir)
        failed_manifest = GraphExtractionManifest.create(
            world_id=world_id,
            world_uuid=world_uuid,
            ingestion_run_id=ingestion_run_id,
            source_filename=source_filename,
            book_number=book_number,
            chunk_paths=chunk_paths,
            config=config,
        )
        for state in failed_manifest.chunk_states:
            state.status = "failed"
            state.last_error_code = "GRAPH_EXTRACTION_RUN_IDENTITY_MISSING"
            state.last_error_message = "Graph extraction requires both world_uuid and ingestion_run_id."
        save_extraction_manifest(manifest_path, failed_manifest)
        logger.error(
            "Graph extraction could not start because required run identity was missing: world_id=%s book=%s world_uuid_present=%s ingestion_run_id_present=%s manifest_name=%s",
            world_id,
            book_number,
            bool(world_uuid),
            bool(ingestion_run_id),
            manifest_path.name,
        )
        _log_extraction_run_finish(manifest=failed_manifest, manifest_path=manifest_path)
        return _result_from_manifest(failed_manifest, manifest_path), []

    # BLOCK 2: Resolve shared scheduler/provider dependencies and load the per-book extraction manifest
    # VARS: manifest_path = stable per-book extraction manifest file, manifest = mutable per-book extraction state used for resume
    # WHY: The manifest is the trusted boundary for crash-safe agent calls, so it must exist before the first provider request is dispatched
    resolved_keys_root = provider_keys_root if provider_keys_root is not None else default_provider_keys_root()
    cancellation_handle = cancellation if cancellation is not None else GraphExtractionRunCancellation()
    manifest_path = extraction_manifest_file_path(book_dir)
    manifest = _load_or_create_manifest(
        manifest_path=manifest_path,
        world_id=world_id,
        world_uuid=world_uuid,
        ingestion_run_id=ingestion_run_id,
        source_filename=source_filename,
        book_number=book_number,
        chunk_paths=chunk_paths,
        config=config,
    )
    manifest_lock = Lock()
    save_extraction_manifest(manifest_path, manifest)
    _log_extraction_run_start(manifest=manifest)
    scheduler = ProviderKeyScheduler.for_model(
        provider_id=manifest.config.provider_id,
        model_id=manifest.config.model_id,
        provider_keys_root=resolved_keys_root,
    )
    warnings: list[OperationEvent] = []

    # BLOCK 3: Pause cleanly when the selected extraction model has no local eligible keys
    # WHY: The user wanted local key checks without provider test calls, so missing keys should leave extraction resumable instead of burning chunk retry counts
    if not scheduler.credentials:
        warning = OperationEvent(
            code="EXTRACTION_PROVIDER_KEYS_MISSING",
            message="No provider credentials are configured for the selected extraction model, so graph extraction was left pending.",
            severity="warning",
            book_number=book_number,
            source_filename=source_filename,
        )
        logger.warning(
            "Graph extraction left chunks pending because no configured provider credentials were found: world_uuid=%s ingestion_run_id=%s book=%s pending_chunks=%s provider=%s model=%s",
            manifest.world_uuid,
            manifest.ingestion_run_id,
            manifest.book_number,
            manifest.pending_chunks,
            manifest.config.provider_id,
            manifest.config.model_id,
        )
        warnings.append(warning)
        manifest.warnings.append(warning.to_dict())
        save_extraction_manifest(manifest_path, manifest)
        result = _result_from_manifest(manifest, manifest_path)
        _log_extraction_run_finish(manifest=manifest, manifest_path=manifest_path)
        return result, warnings

    # BLOCK 4: Run chunk extraction jobs concurrently while each chunk saves after every trusted provider pass
    # WHY: Different chunks can progress independently, but each chunk's initial extraction and gleaning passes must remain sequential because later calls depend on earlier saved output
    _run_extraction_loop(
        manifest=manifest,
        manifest_path=manifest_path,
        manifest_lock=manifest_lock,
        chunk_paths=chunk_paths,
        provider_keys_root=resolved_keys_root,
        cancellation=cancellation_handle,
    )
    scheduler.save_runtime_states()
    save_extraction_manifest(manifest_path, manifest)
    result = _result_from_manifest(manifest, manifest_path)
    _log_extraction_run_finish(manifest=manifest, manifest_path=manifest_path)
    return result, warnings


def _load_or_create_manifest(
    *,
    manifest_path: Path,
    world_id: str,
    world_uuid: str,
    ingestion_run_id: str,
    source_filename: str,
    book_number: int,
    chunk_paths: list[str],
    config: GraphExtractionConfig,
) -> GraphExtractionManifest:
    # BLOCK 1: Reuse an existing manifest for the same run or create a fresh run snapshot for this book
    # WHY: Resume must preserve the run-locked gleaning count and prompt/model metadata rather than silently adopting later world-level config edits
    try:
        existing_manifest = load_extraction_manifest(manifest_path)
    except GraphExtractionError as error:
        if error.code != "GRAPH_EXTRACTION_MANIFEST_CORRUPT":
            raise
        rebuilt_manifest = GraphExtractionManifest.create(
            world_id=world_id,
            world_uuid=world_uuid,
            ingestion_run_id=ingestion_run_id,
            source_filename=source_filename,
            book_number=book_number,
            chunk_paths=chunk_paths,
            config=config,
        )
        rebuilt_manifest.warnings.append(
            {
                "code": "GRAPH_EXTRACTION_MANIFEST_CORRUPT",
                "message": "The graph extraction manifest was corrupt, so graph extraction for this book was reset.",
                "severity": "warning",
                "book_number": book_number,
                "source_filename": source_filename,
            }
        )
        logger.warning(
            "Graph extraction manifest was corrupt and the book will restart from a clean manifest: manifest_name=%s world_uuid=%s ingestion_run_id=%s book=%s",
            manifest_path.name,
            world_uuid,
            ingestion_run_id,
            book_number,
        )
        return rebuilt_manifest
    if existing_manifest is None:
        return GraphExtractionManifest.create(
            world_id=world_id,
            world_uuid=world_uuid,
            ingestion_run_id=ingestion_run_id,
            source_filename=source_filename,
            book_number=book_number,
            chunk_paths=chunk_paths,
            config=config,
        )
    # BLOCK 2: Reset saved extraction state when this book now belongs to a different run snapshot or chunk layout
    # WHY: A new ingestion run must rebuild raw graph candidates from scratch instead of crashing on the previous run's manifest, or fresh runs can never resume past completed older work
    if (
        existing_manifest.world_uuid != world_uuid
        or existing_manifest.ingestion_run_id != ingestion_run_id
        or existing_manifest.book_number != book_number
        or existing_manifest.source_filename != source_filename
        or existing_manifest.total_chunks != len(chunk_paths)
    ):
        rebuilt_manifest = GraphExtractionManifest.create(
            world_id=world_id,
            world_uuid=world_uuid,
            ingestion_run_id=ingestion_run_id,
            source_filename=source_filename,
            book_number=book_number,
            chunk_paths=chunk_paths,
            config=config,
        )
        rebuilt_manifest.warnings.append(
            {
                "code": "GRAPH_EXTRACTION_MANIFEST_RESET",
                "message": "The graph extraction manifest belonged to an older run or chunk layout, so graph extraction for this book was reset.",
                "severity": "warning",
                "book_number": book_number,
                "source_filename": source_filename,
            }
        )
        logger.warning(
            "Graph extraction manifest was reset because it belonged to a different run snapshot: manifest_name=%s saved_world_uuid=%s saved_ingestion_run_id=%s current_world_uuid=%s current_ingestion_run_id=%s book=%s",
            manifest_path.name,
            existing_manifest.world_uuid,
            existing_manifest.ingestion_run_id,
            world_uuid,
            ingestion_run_id,
            book_number,
        )
        return rebuilt_manifest

    # BLOCK 3: Refresh only the settings that are allowed to change while a run is paused
    # WHY: Gleaning count and parser shape are locked to the run, but concurrency and future fresh-call model/preset choices can change without rewriting already saved extraction passes
    existing_manifest.config = GraphExtractionConfig(
        provider_id=config.provider_id,
        model_id=config.model_id,
        gleaning_count=existing_manifest.config.gleaning_count,
        extraction_concurrency=config.extraction_concurrency,
        prompt_preset_id=config.prompt_preset_id,
        prompt_preset_version=config.prompt_preset_version,
        parser_version=existing_manifest.config.parser_version,
    )
    _reconcile_manifest_chunk_states(manifest=existing_manifest, chunk_paths=chunk_paths)
    return existing_manifest


def _reconcile_manifest_chunk_states(
    *,
    manifest: GraphExtractionManifest,
    chunk_paths: list[str],
) -> None:
    # BLOCK 1: Reset only chunk states whose saved extraction data no longer matches the current chunk files or run shape
    # VARS: expected_hash = hash of the current chunk body, state = saved extraction state for the same chunk slot
    # WHY: Resume should keep trusted passes, but stale or incomplete per-chunk extraction state must be redone without throwing away other chunks in the same book
    repaired_chunks: list[int] = []
    valid_statuses = {"pending", "partial", "failed", "skipped", "extracted"}
    if len(manifest.chunk_states) != len(chunk_paths):
        manifest.chunk_states = [
            manifest.chunk_states[index]
            if index < len(manifest.chunk_states)
            else GraphExtractionChunkState(chunk_number=index + 1, chunk_file=str(chunk_paths[index]))
            for index in range(len(chunk_paths))
        ]
        repaired_chunks = [index for index in range(1, len(chunk_paths) + 1)]
    for index, chunk_path in enumerate(chunk_paths, start=1):
        state = manifest.chunk_states[index - 1]
        payload = read_chunk_file(Path(chunk_path))
        expected_hash = chunk_text_hash(str(payload["chunk_text"]))
        should_reset = (
            state.chunk_number != index
            or state.chunk_file != str(chunk_path)
            or state.status not in valid_statuses
            or (state.text_hash is not None and state.text_hash != expected_hash)
            or (state.status in {"partial", "extracted"} and state.initial_pass is None and str(payload["chunk_text"]).strip())
            or len(state.glean_passes) > manifest.config.gleaning_count
        )
        if should_reset:
            manifest.chunk_states[index - 1] = GraphExtractionChunkState(
                chunk_number=index,
                chunk_file=str(chunk_path),
                last_error_code="GRAPH_EXTRACTION_CHUNK_STATE_REBUILT",
                last_error_message="The saved graph extraction state for this chunk was incomplete or stale, so it will be redone.",
            )
            repaired_chunks.append(index)
            continue
        if state.text_hash is None:
            state.text_hash = expected_hash
        # BLOCK 2: Give incomplete chunks a fresh retry budget whenever a later run resumes them
        # WHY: Provider setup, parser prompts, or transient model failures can be fixed between runs, so resume must keep the trusted saved passes while clearing the old retry ceiling that would otherwise leave the chunk stuck forever
        if state.status != "extracted" and state.status != "skipped":
            state.retry_count = 0
            state.glean_retry_count = 0
        if state.status == "extracted" and len(state.glean_passes) < manifest.config.gleaning_count and str(payload["chunk_text"]).strip():
            state.status = "partial"
            repaired_chunks.append(index)
    if repaired_chunks:
        manifest.warnings.append(
            {
                "code": "GRAPH_EXTRACTION_CHUNK_STATE_REBUILT",
                "message": "One or more graph extraction chunks had incomplete or stale state and will be redone.",
                "severity": "warning",
                "book_number": manifest.book_number,
                "source_filename": manifest.source_filename,
            }
        )
        logger.warning(
            "Graph extraction rebuilt stale or incomplete chunk state before resuming: world_uuid=%s ingestion_run_id=%s book=%s repaired_chunks=%s total_chunks=%s",
            manifest.world_uuid,
            manifest.ingestion_run_id,
            manifest.book_number,
            len(repaired_chunks),
            manifest.total_chunks,
        )


def _run_extraction_loop(
    *,
    manifest: GraphExtractionManifest,
    manifest_path: Path,
    manifest_lock: Lock,
    chunk_paths: list[str],
    provider_keys_root: Path,
    cancellation: GraphExtractionRunCancellation,
) -> None:
    # BLOCK 1: Build the queue of chunks that still need extraction after reading current chunk files
    # VARS: pending_items = chunks whose extraction is pending, partial, or failed and should be attempted in this run
    # WHY: Resume should skip trusted extracted chunks while retrying failed or incomplete chunks without requiring a full re-ingest
    work_items = _build_work_items(manifest=manifest, chunk_paths=chunk_paths)
    pending_items = [
        work_item
        for work_item, state in zip(work_items, manifest.chunk_states, strict=True)
        if state.status != "extracted" and state.status != "skipped"
    ]
    if not pending_items:
        logger.info(
            "Graph extraction skipped provider work because every chunk is already complete: world_uuid=%s ingestion_run_id=%s book=%s completed_chunks=%s total_chunks=%s",
            manifest.world_uuid,
            manifest.ingestion_run_id,
            manifest.book_number,
            manifest.extracted_chunks,
            manifest.total_chunks,
        )
        return

    futures: dict[Future[None], GraphExtractionWorkItem] = {}
    pending_queue = pending_items[:]
    scheduler_cache: dict[tuple[str, str], ProviderKeyScheduler] = {
        (manifest.config.provider_id, manifest.config.model_id): ProviderKeyScheduler.for_model(
            provider_id=manifest.config.provider_id,
            model_id=manifest.config.model_id,
            provider_keys_root=provider_keys_root,
        )
    }
    provider_cache: dict[str, object] = {}
    cache_lock = Lock()
    with ThreadPoolExecutor(max_workers=max(1, manifest.config.extraction_concurrency or _DEFAULT_EXTRACTION_CONCURRENCY)) as executor:
        while pending_queue or futures:
            # BLOCK 2: Fill available extraction worker slots from the pending chunk queue
            # WHY: The worker count controls chunk-level orchestration, while the shared provider-key scheduler controls how many provider calls can actually be in flight per key
            while not cancellation.is_cancelled and len(futures) < max(1, manifest.config.extraction_concurrency or _DEFAULT_EXTRACTION_CONCURRENCY) and pending_queue:
                work_item = pending_queue.pop(0)
                futures[
                    executor.submit(
                        _process_chunk,
                        manifest=manifest,
                        manifest_path=manifest_path,
                        manifest_lock=manifest_lock,
                        work_item=work_item,
                        provider_keys_root=provider_keys_root,
                        scheduler_cache=scheduler_cache,
                        provider_cache=provider_cache,
                        cache_lock=cache_lock,
                        cancellation=cancellation,
                    )
                ] = work_item

            if cancellation.is_cancelled:
                logger.warning(
                    "Graph extraction paused by cancellation: world_uuid=%s ingestion_run_id=%s book=%s completed_chunks=%s pending_chunks=%s failed_chunks=%s queued_chunks=%s in_flight_workers=%s",
                    manifest.world_uuid,
                    manifest.ingestion_run_id,
                    manifest.book_number,
                    manifest.extracted_chunks,
                    manifest.pending_chunks,
                    manifest.failed_chunks,
                    len(pending_queue),
                    len(futures),
                )
                break

            if not futures:
                break

            done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED)
            for future in done:
                futures.pop(future)
                future.result()


def _build_work_items(
    *,
    manifest: GraphExtractionManifest,
    chunk_paths: list[str],
) -> list[GraphExtractionWorkItem]:
    # BLOCK 1: Turn persisted chunk JSON files into extraction work items with overlap and source metadata
    # WHY: Chunk files are the source of truth for resume, and extraction needs overlap for reference-only pronoun/title resolution while embeddings intentionally did not use it
    work_items: list[GraphExtractionWorkItem] = []
    for chunk_path in chunk_paths:
        payload = read_chunk_file(Path(chunk_path))
        chunk_text = str(payload["chunk_text"])
        work_items.append(
            GraphExtractionWorkItem(
                world_uuid=manifest.world_uuid,
                ingestion_run_id=manifest.ingestion_run_id,
                source_filename=str(payload["source_filename"]),
                book_number=manifest.book_number,
                chunk_number=int(payload["chunk_number"]),
                chunk_file=str(chunk_path),
                chunk_position=str(payload["chunk_position"]),
                chunk_text=chunk_text,
                overlap_text=str(payload.get("overlap_text", "")),
                text_hash=chunk_text_hash(chunk_text),
            )
        )
    return work_items


def _process_chunk(
    *,
    manifest: GraphExtractionManifest,
    manifest_path: Path,
    manifest_lock: Lock,
    work_item: GraphExtractionWorkItem,
    provider_keys_root: Path,
    scheduler_cache: dict[tuple[str, str], ProviderKeyScheduler],
    provider_cache: dict[str, object],
    cache_lock: Lock,
    cancellation: GraphExtractionRunCancellation,
) -> None:
    # BLOCK 1: Mark empty chunks as skipped without contacting the provider
    # WHY: Whitespace-only chunks cannot produce useful graph candidates, and skipping them keeps retry budgets focused on real provider or parser failures
    state = manifest.chunk_states[work_item.chunk_number - 1]
    if not work_item.chunk_text.strip():
        state.status = "skipped"
        state.text_hash = work_item.text_hash
        logger.info(
            "Graph extraction skipped empty chunk: world_uuid=%s ingestion_run_id=%s book=%s chunk=%s",
            work_item.world_uuid,
            work_item.ingestion_run_id,
            work_item.book_number,
            work_item.chunk_number,
        )
        _save_manifest_threadsafe(manifest_path=manifest_path, manifest=manifest, manifest_lock=manifest_lock)
        return

    # BLOCK 2: Run or resume the initial extraction pass before any gleaning passes
    # WHY: Gleaning depends on saved initial output, so a missing initial pass means the whole chunk must redo initial extraction first
    if state.initial_pass is None:
        prompt = build_initial_prompt(chunk_text=work_item.chunk_text, overlap_text=work_item.overlap_text)
        pass_record = _call_provider_for_pass(
            manifest=manifest,
            state=state,
            work_item=work_item,
            prompt=prompt,
            pass_type="initial",
            pass_number=0,
            provider_keys_root=provider_keys_root,
            scheduler_cache=scheduler_cache,
            provider_cache=provider_cache,
            cache_lock=cache_lock,
            cancellation=cancellation,
        )
        if pass_record is None:
            if cancellation.is_cancelled:
                logger.warning(
                    "Graph extraction chunk remained pending because the run was cancelled before the initial pass finished: world_uuid=%s ingestion_run_id=%s book=%s chunk=%s",
                    work_item.world_uuid,
                    work_item.ingestion_run_id,
                    work_item.book_number,
                    work_item.chunk_number,
                )
            _save_manifest_threadsafe(manifest_path=manifest_path, manifest=manifest, manifest_lock=manifest_lock)
            return
        state.initial_pass = pass_record
        state.retry_count = 0
        state.status = "partial"
        state.text_hash = work_item.text_hash
        _refresh_final_candidates(state, manifest=manifest)
        _save_manifest_threadsafe(manifest_path=manifest_path, manifest=manifest, manifest_lock=manifest_lock)

    # BLOCK 3: Continue saved gleaning sequentially until the run-locked gleaning target is reached
    # WHY: Each glean needs the current merged extraction state, and a crash after any call must leave enough saved data for the next run to continue without rerunning trusted passes
    while len(state.glean_passes) < manifest.config.gleaning_count:
        if cancellation.is_cancelled:
            logger.warning(
                "Graph extraction chunk remained partial because the run was cancelled between gleaning passes: world_uuid=%s ingestion_run_id=%s book=%s chunk=%s completed_gleans=%s target_gleans=%s",
                work_item.world_uuid,
                work_item.ingestion_run_id,
                work_item.book_number,
                work_item.chunk_number,
                len(state.glean_passes),
                manifest.config.gleaning_count,
            )
            return
        previous_passes = _pass_records_for_state(state)
        prompt = build_gleaning_prompt(
            chunk_text=work_item.chunk_text,
            overlap_text=work_item.overlap_text,
            previous_passes=previous_passes,
            current_nodes=state.nodes,
            current_edges=state.edges,
        )
        pass_number = len(state.glean_passes) + 1
        pass_record = _call_provider_for_pass(
            manifest=manifest,
            state=state,
            work_item=work_item,
            prompt=prompt,
            pass_type="glean",
            pass_number=pass_number,
            provider_keys_root=provider_keys_root,
            scheduler_cache=scheduler_cache,
            provider_cache=provider_cache,
            cache_lock=cache_lock,
            cancellation=cancellation,
        )
        if pass_record is None:
            state.status = "partial" if state.initial_pass is not None else "failed"
            if cancellation.is_cancelled:
                logger.warning(
                    "Graph extraction chunk remained %s because the run was cancelled during a gleaning pass: world_uuid=%s ingestion_run_id=%s book=%s chunk=%s completed_gleans=%s target_gleans=%s",
                    state.status,
                    work_item.world_uuid,
                    work_item.ingestion_run_id,
                    work_item.book_number,
                    work_item.chunk_number,
                    len(state.glean_passes),
                    manifest.config.gleaning_count,
                )
            _save_manifest_threadsafe(manifest_path=manifest_path, manifest=manifest, manifest_lock=manifest_lock)
            return
        state.glean_passes.append(pass_record)
        state.glean_retry_count = 0
        _refresh_final_candidates(state, manifest=manifest)
        _save_manifest_threadsafe(manifest_path=manifest_path, manifest=manifest, manifest_lock=manifest_lock)

    # BLOCK 4: Mark the chunk extracted only after every run-locked gleaning pass has completed and final candidates have been rebuilt
    # WHY: Later manifestation should trust only chunks whose complete initial-plus-glean pass set is present in the extraction manifest
    state.status = "extracted"
    state.last_error_code = None
    state.last_error_message = None
    _refresh_final_candidates(state, manifest=manifest)
    _save_manifest_threadsafe(manifest_path=manifest_path, manifest=manifest, manifest_lock=manifest_lock)
    logger.info(
        "Graph extraction chunk completed: world_uuid=%s ingestion_run_id=%s book=%s chunk=%s glean_passes=%s node_count=%s edge_count=%s",
        work_item.world_uuid,
        work_item.ingestion_run_id,
        work_item.book_number,
        work_item.chunk_number,
        len(state.glean_passes),
        len(state.nodes),
        len(state.edges),
    )


def _call_provider_for_pass(
    *,
    manifest: GraphExtractionManifest,
    state: GraphExtractionChunkState,
    work_item: GraphExtractionWorkItem,
    prompt: str,
    pass_type: str,
    pass_number: int,
    provider_keys_root: Path,
    scheduler_cache: dict[tuple[str, str], ProviderKeyScheduler],
    provider_cache: dict[str, object],
    cache_lock: Lock,
    cancellation: GraphExtractionRunCancellation,
) -> ExtractionPassRecord | None:
    # BLOCK 1: Retry non-rate-limit provider/parser failures up to the chunk budget while leaving rate-limit handling to the shared scheduler
    # VARS: attempts = normal non-rate-limit attempts spent on this pass, token_estimate = approximate prompt cost reserved before dispatch
    # WHY: Missing completion markers and malformed JSON are chunk/model-output failures, while provider quota failures should cool down keys without burning the chunk's ordinary retry budget
    attempts = state.retry_count if pass_type == "initial" else state.glean_retry_count
    token_estimate = _estimate_tokens(prompt)
    call_config = _config_for_pass(manifest=manifest, state=state, pass_type=pass_type)
    scheduler = _scheduler_for_config(
        config=call_config,
        provider_keys_root=provider_keys_root,
        scheduler_cache=scheduler_cache,
        cache_lock=cache_lock,
    )
    provider = _provider_for_config(config=call_config, provider_cache=provider_cache, cache_lock=cache_lock)
    while attempts < _MAX_RETRIES_PER_CHUNK and not cancellation.is_cancelled:
        credential = scheduler.select_credential(token_estimate=token_estimate)
        if credential is None:
            if not scheduler.has_future_credential_availability():
                state.last_error_code = "EXTRACTION_PROVIDER_KEYS_UNAVAILABLE"
                state.last_error_message = "No eligible extraction provider credentials are currently usable."
                logger.warning(
                    "Graph extraction chunk is waiting for usable provider credentials: world_uuid=%s ingestion_run_id=%s book=%s chunk=%s pass_type=%s pass_number=%s next_status=%s provider=%s model=%s",
                    work_item.world_uuid,
                    work_item.ingestion_run_id,
                    work_item.book_number,
                    work_item.chunk_number,
                    pass_type,
                    pass_number,
                    "partial" if state.initial_pass is not None else "pending",
                    call_config.provider_id,
                    call_config.model_id,
                )
                return None
            scheduler.wait_for_next_available_credential()
            continue
        outcome = provider.extract(
            credential=credential,
            config=call_config,
            prompt=prompt,
            log_context={
                "world_uuid": work_item.world_uuid,
                "ingestion_run_id": work_item.ingestion_run_id,
                "book": work_item.book_number,
                "chunk": work_item.chunk_number,
                "pass_type": pass_type,
                "pass_number": pass_number,
            },
        )
        if cancellation.is_cancelled:
            scheduler.abandon_inflight(scope_key=credential.quota_scope, token_estimate=token_estimate)
            return None
        if isinstance(outcome, ExtractionProviderSuccess):
            scheduler.record_success(scope_key=outcome.quota_scope, token_estimate=token_estimate)
            try:
                nodes, edges = parse_extraction_response(outcome.response_text)
            except GraphExtractionParseError as error:
                attempts += 1
                _set_retry_count(state=state, pass_type=pass_type, attempts=attempts)
                state.last_error_code = error.code
                state.last_error_message = error.message
                continue
            return ExtractionPassRecord(
                pass_type=pass_type,
                pass_number=pass_number,
                nodes=nodes,
                edges=edges,
                provider_id=call_config.provider_id,
                model_id=call_config.model_id,
                prompt_preset_id=call_config.prompt_preset_id,
                prompt_preset_version=call_config.prompt_preset_version,
            )
        _handle_provider_failure(
            state=state,
            pass_type=pass_type,
            attempts=attempts,
            credential=credential,
            failure=outcome,
            scheduler=scheduler,
            token_estimate=token_estimate,
        )
        attempts = state.retry_count if pass_type == "initial" else state.glean_retry_count
    if not cancellation.is_cancelled:
        logger.warning(
            "Graph extraction pass exhausted retries and left the chunk incomplete: world_uuid=%s ingestion_run_id=%s book=%s chunk=%s pass_type=%s pass_number=%s attempts=%s next_status=%s error_code=%s provider=%s model=%s",
            work_item.world_uuid,
            work_item.ingestion_run_id,
            work_item.book_number,
            work_item.chunk_number,
            pass_type,
            pass_number,
            attempts,
            "failed" if pass_type == "initial" else "partial",
            state.last_error_code,
            call_config.provider_id,
            call_config.model_id,
        )
    state.status = "failed" if pass_type == "initial" else "partial"
    return None


def _handle_provider_failure(
    *,
    state: GraphExtractionChunkState,
    pass_type: str,
    attempts: int,
    credential,
    failure: ExtractionProviderFailure,
    scheduler: ProviderKeyScheduler,
    token_estimate: int,
) -> None:
    # BLOCK 1: Apply provider cooldowns separately from ordinary chunk retry accounting
    # WHY: Quota failures describe key availability, not bad chunk content, so they should move scheduling to another key without consuming malformed-output retry attempts
    state.last_error_code = failure.code
    state.last_error_message = failure.message
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
        return

    # BLOCK 2: Roll back the scheduler reservation and spend one normal attempt for non-rate-limit failures
    # WHY: Provider crashes, timeouts, and malformed completions should retry the chunk, but they should not make the key look quota-exhausted
    scheduler.release_reservation(
        scope_key=failure.quota_scope,
        token_estimate=token_estimate,
    )
    attempts += 1
    _set_retry_count(state=state, pass_type=pass_type, attempts=attempts)


def _config_for_pass(
    *,
    manifest: GraphExtractionManifest,
    state: GraphExtractionChunkState,
    pass_type: str,
) -> GraphExtractionConfig:
    # BLOCK 1: Keep gleaning calls on the same provider/model/preset snapshot as the chunk's saved initial extraction
    # WHY: Paused runs can change defaults for future fresh chunks, but a glean prompt must continue the same instruction/model context that produced the initial pass it is extending
    if pass_type == "glean" and state.initial_pass is not None:
        return GraphExtractionConfig(
            provider_id=state.initial_pass.provider_id,
            model_id=state.initial_pass.model_id,
            gleaning_count=manifest.config.gleaning_count,
            extraction_concurrency=manifest.config.extraction_concurrency,
            prompt_preset_id=state.initial_pass.prompt_preset_id,
            prompt_preset_version=state.initial_pass.prompt_preset_version,
            parser_version=manifest.config.parser_version,
        )
    return manifest.config


def _scheduler_for_config(
    *,
    config: GraphExtractionConfig,
    provider_keys_root: Path,
    scheduler_cache: dict[tuple[str, str], ProviderKeyScheduler],
    cache_lock: Lock,
) -> ProviderKeyScheduler:
    # BLOCK 1: Reuse or create the scheduler for the exact provider/model used by the current pass
    # WHY: A paused run can change the extraction model for future chunks while already-started chunks keep using their original model for gleaning
    cache_key = (config.provider_id, config.model_id)
    with cache_lock:
        scheduler = scheduler_cache.get(cache_key)
        if scheduler is None:
            scheduler = ProviderKeyScheduler.for_model(
                provider_id=config.provider_id,
                model_id=config.model_id,
                provider_keys_root=provider_keys_root,
            )
            scheduler_cache[cache_key] = scheduler
        return scheduler


def _provider_for_config(
    *,
    config: GraphExtractionConfig,
    provider_cache: dict[str, object],
    cache_lock: Lock,
):
    # BLOCK 1: Reuse provider adapter instances by provider id within one extraction run
    # WHY: Provider adapters are stateless runtime boundaries, so caching avoids rebuilding them for every pass while still allowing future mixed-provider model support
    with cache_lock:
        provider = provider_cache.get(config.provider_id)
        if provider is None:
            provider = create_graph_extraction_provider(config.provider_id)
            provider_cache[config.provider_id] = provider
        return provider


def _set_retry_count(*, state: GraphExtractionChunkState, pass_type: str, attempts: int) -> None:
    # BLOCK 1: Store retry counts on the pass family that is currently running
    # WHY: A saved initial extraction should not lose its trusted output just because a later glean pass has to retry or resume
    if pass_type == "initial":
        state.retry_count = attempts
    else:
        state.glean_retry_count = attempts


def _refresh_final_candidates(
    state: GraphExtractionChunkState,
    *,
    manifest: GraphExtractionManifest,
) -> None:
    # BLOCK 1: Rebuild final local UUID candidates from the saved initial and gleaning passes
    # WHY: Final validation happens after all currently saved passes so edges can survive when a later glean supplies an endpoint that was missing earlier
    nodes, edges = merge_pass_records(
        _pass_records_for_state(state),
        world_uuid=manifest.world_uuid,
        ingestion_run_id=manifest.ingestion_run_id,
        book_number=manifest.book_number,
        chunk_number=state.chunk_number,
    )
    state.nodes = nodes
    state.edges = edges


def _pass_records_for_state(state: GraphExtractionChunkState) -> list[ExtractionPassRecord]:
    # BLOCK 1: Return trusted saved passes in the exact order they were produced
    # WHY: Gleaning context and final merge behavior should be stable across resume instead of depending on dictionary or filesystem ordering
    passes: list[ExtractionPassRecord] = []
    if state.initial_pass is not None:
        passes.append(state.initial_pass)
    passes.extend(state.glean_passes)
    return passes


def _save_manifest_threadsafe(
    *,
    manifest_path: Path,
    manifest: GraphExtractionManifest,
    manifest_lock: Lock,
) -> None:
    # BLOCK 1: Serialize concurrent worker updates through one manifest write lock
    # WHY: Each provider pass must be saved before the next agentic call, but parallel chunk workers cannot safely replace the same manifest file at the same time
    with manifest_lock:
        save_extraction_manifest(manifest_path, manifest)


def _log_extraction_run_start(*, manifest: GraphExtractionManifest) -> None:
    # BLOCK 1: Log one safe book-level snapshot before any chunk workers or provider calls start
    # WHY: Graph extraction can resume partially completed books, so operators need the opening counts and locked model settings at the orchestration boundary instead of inferring them from chunk-level events
    chunk_counts = _chunk_status_counts(manifest)
    logger.info(
        "Graph extraction run starting: world_uuid=%s ingestion_run_id=%s book=%s total_chunks=%s extracted_chunks=%s skipped_chunks=%s pending_chunks=%s partial_chunks=%s failed_chunks=%s provider=%s model=%s concurrency=%s",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        manifest.total_chunks,
        chunk_counts["extracted"],
        chunk_counts["skipped"],
        chunk_counts["pending"],
        chunk_counts["partial"],
        chunk_counts["failed"],
        manifest.config.provider_id,
        manifest.config.model_id,
        manifest.config.extraction_concurrency,
    )


def _log_extraction_run_finish(*, manifest: GraphExtractionManifest, manifest_path: Path) -> None:
    # BLOCK 1: Log the final book-level extraction counts once this call has finished mutating the manifest
    # WHY: The caller returns only coarse result counts, so the orchestration boundary should record the final state that explains whether the book completed, failed, or stayed resumable
    chunk_counts = _chunk_status_counts(manifest)
    log_method = logger.info if manifest.status == "completed" else logger.warning
    log_method(
        "Graph extraction run finished: world_uuid=%s ingestion_run_id=%s book=%s status=%s total_chunks=%s extracted_chunks=%s skipped_chunks=%s pending_chunks=%s partial_chunks=%s failed_chunks=%s warning_count=%s manifest_name=%s",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        manifest.status,
        manifest.total_chunks,
        chunk_counts["extracted"],
        chunk_counts["skipped"],
        chunk_counts["pending"],
        chunk_counts["partial"],
        chunk_counts["failed"],
        len(manifest.warnings),
        manifest_path.name,
    )
    # BLOCK 2: Emit one concise per-book summary that includes the saved candidate totals for downstream manifestation debugging
    # WHY: A book can be partial even when several chunks finished, so the final summary should show how much raw graph data exists without exposing any chunk text or model output
    log_method(
        "Graph extraction book summary: world_uuid=%s ingestion_run_id=%s book=%s node_count=%s edge_count=%s completed_chunks=%s resumable_chunks=%s failed_chunks=%s",
        manifest.world_uuid,
        manifest.ingestion_run_id,
        manifest.book_number,
        sum(len(state.nodes) for state in manifest.chunk_states),
        sum(len(state.edges) for state in manifest.chunk_states),
        chunk_counts["extracted"] + chunk_counts["skipped"],
        chunk_counts["pending"] + chunk_counts["partial"],
        chunk_counts["failed"],
    )


def _chunk_status_counts(manifest: GraphExtractionManifest) -> dict[str, int]:
    # BLOCK 1: Count chunk states in one place so every summary log uses the same status breakdown
    # WHY: The manifest exposes only combined completed and pending properties, but the requested logs need extracted, skipped, pending, partial, and failed counts separately
    counts = {
        "pending": 0,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
        "extracted": 0,
    }
    for state in manifest.chunk_states:
        counts[state.status] = counts.get(state.status, 0) + 1
    return counts


def _result_from_manifest(manifest: GraphExtractionManifest, manifest_path: Path) -> GraphExtractionBookResult:
    return GraphExtractionBookResult(
        status=manifest.status,
        extracted_chunks=manifest.extracted_chunks,
        failed_chunks=manifest.failed_chunks,
        pending_chunks=manifest.pending_chunks,
        manifest_path=str(manifest_path),
    )


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)
