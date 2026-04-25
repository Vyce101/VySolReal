"""Service entrypoints for TXT splitter ingestion."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid5

from backend.embeddings.errors import EmbeddingConfigurationError, VectorStoreError
from backend.embeddings.models import EmbeddingProfile, EmbeddingRunCancellation, WorldSplitterConfig
from backend.embeddings.qdrant_store import QdrantChunkStore
from backend.embeddings.service import embed_book_chunks
from backend.embeddings.storage import (
    begin_world_ingestion_run,
    default_vector_store_root,
    ensure_world_metadata,
    finish_world_ingestion_run,
    load_world_metadata,
    save_world_metadata,
)
from backend.graph_extraction import GraphExtractionConfig, extract_book_chunks
from backend.graph_extraction.errors import GraphExtractionError
from backend.graph_extraction.storage import load_extraction_manifest, load_graph_config, load_or_create_graph_config, save_graph_config
from backend.graph_manifestation.adapters import (
    QdrantGraphNodeVectorStore,
    ScheduledNodeEmbedder,
    create_default_graph_writer,
)
from backend.graph_manifestation.errors import GraphManifestationError, GraphStoreUnavailable
from backend.graph_manifestation.service import manifest_extracted_graph
from backend.logger import get_logger
from backend.provider_keys.errors import ProviderKeyConfigurationError
from backend.provider_keys.keys import load_eligible_provider_credentials

from .chunking import split_text
from .converters import get_converter, has_usable_text
from .errors import IngestionError
from .models import (
    BookIngestionResult,
    BookManifest,
    ChunkRecord,
    IngestionResult,
    OperationEvent,
    SplitterConfig,
)
from .storage import (
    SourceSession,
    book_directory,
    chunk_file_path,
    copy_source_into_world,
    default_worlds_root,
    existing_book_numbers,
    ensure_world_does_not_exist,
    load_stored_source_paths,
    load_manifest,
    manifest_file_path,
    next_book_number,
    persist_completed_chunk,
    remove_book_output_directory,
    save_manifest,
)

logger = get_logger(__name__)


def ingest_sources(
    *,
    world_name: str,
    source_files: list[str | Path],
    chunk_size: int,
    max_lookback: int,
    overlap_size: int,
    worlds_root: str | Path | None = None,
    embedding_profile: EmbeddingProfile | None = None,
    embedding_concurrency: int = 5,
    graph_extraction_config: GraphExtractionConfig | None = None,
    extraction_concurrency: int = 5,
    cancellation: EmbeddingRunCancellation | None = None,
    provider_keys_root: str | Path | None = None,
    vector_store_root: str | Path | None = None,
) -> IngestionResult:
    """Create a new world and ingest the provided source files."""
    # BLOCK 1: Turn the raw numeric settings into a validated splitter configuration object
    # WHY: Centralizing validation in the config model catches invalid runtime settings before any folders or files are created
    config = SplitterConfig(
        chunk_size=chunk_size,
        max_lookback=max_lookback,
        overlap_size=overlap_size,
    )
    # BLOCK 2: Resolve the target world path and create a brand-new world before any ingestion starts
    # WHY: World creation must happen once up front so duplicate names fail fast instead of halfway through a multi-book ingest
    resolved_worlds_root = Path(worlds_root) if worlds_root is not None else default_worlds_root()
    world_dir = resolved_worlds_root / world_name
    logger.info(
        "TXT ingestion requested: world_name=%s source_file_count=%s world_dir=%s",
        world_name,
        len(source_files),
        world_dir,
    )

    try:
        # BLOCK 3: Confirm the user chose an embedding profile and has at least one eligible provider credential before creating any new world folders
        # VARS: resolved_embedding_profile = the explicit locked embedding profile that this new world will store if ingestion is allowed to proceed
        # WHY: The user asked for missing-key failures before the world directory exists, so new-world ingestion has to fail on embedding prerequisites before touching the filesystem
        resolved_embedding_profile = _require_new_world_embedding_profile(
            world_name=world_name,
            embedding_profile=embedding_profile,
        )
        _ensure_embedding_credentials_available(
            embedding_profile=resolved_embedding_profile,
            provider_keys_root=Path(provider_keys_root) if provider_keys_root is not None else None,
        )
        ensure_world_does_not_exist(world_dir)
        world_dir.mkdir(parents=True, exist_ok=False)
        logger.info("Created new world directory for ingestion: world_dir=%s", world_dir)
        return ingest_sources_into_existing_world(
            world_name=world_name,
            source_files=source_files,
            config=config,
            world_dir=world_dir,
            embedding_profile=resolved_embedding_profile,
            embedding_concurrency=embedding_concurrency,
            graph_extraction_config=graph_extraction_config,
            extraction_concurrency=extraction_concurrency,
            cancellation=cancellation,
            provider_keys_root=provider_keys_root,
            vector_store_root=vector_store_root,
        )
    except IngestionError as error:
        # BLOCK 4: Return a structured failure result instead of throwing raw exceptions beyond the ingestion boundary
        # WHY: The future UI needs machine-readable success, warning, and error payloads to decide how to present failures without backend-owned popups
        logger.error(
            "TXT ingestion failed before completion: world_name=%s error_code=%s details=%s",
            world_name,
            error.code,
            error.details,
        )
        return IngestionResult(
            success=False,
            world_id=world_name,
            world_uuid=None,
            world_path=str(world_dir),
            errors=[error],
        )


def ingest_sources_into_existing_world(
    *,
    world_name: str,
    source_files: list[str | Path],
    config: SplitterConfig,
    world_dir: str | Path,
    embedding_profile: EmbeddingProfile | None = None,
    embedding_concurrency: int = 5,
    graph_extraction_config: GraphExtractionConfig | None = None,
    extraction_concurrency: int = 5,
    cancellation: EmbeddingRunCancellation | None = None,
    provider_keys_root: str | Path | None = None,
    vector_store_root: str | Path | None = None,
) -> IngestionResult:
    """Ingest books into an already-created world, resuming safely per book."""
    # BLOCK 1: Prepare shared result state before any world-level validation starts
    # VARS: run_started = whether this call already activated or resumed the world's ingestion run and therefore must pause it again on failure
    # WHY: Setup-time lock failures should return the same structured result shape as per-book failures without accidentally changing a previously active run
    resolved_world_dir = Path(world_dir)
    resolved_world_dir.mkdir(parents=True, exist_ok=True)
    books: list[BookIngestionResult] = []
    warnings: list[OperationEvent] = []
    world_metadata = None
    run_started = False

    try:
        # BLOCK 2: Load the world locks and the current graph defaults before any per-book work begins
        # VARS: requested_splitter_lock = the splitter settings this call wants the world to keep locked, graph_config = editable world defaults for future unsent extraction calls
        # WHY: Existing-world ingest must reject incompatible splitter/profile changes early and only allow graph-default edits when the world is idle or paused
        requested_splitter_lock = _world_splitter_lock(config)
        world_metadata = _ensure_locked_world_metadata(
            world_dir=resolved_world_dir,
            world_name=world_name,
            embedding_profile=embedding_profile,
            splitter_config=requested_splitter_lock,
        )
        _ensure_embedding_credentials_available(
            embedding_profile=world_metadata.embedding_profile,
            provider_keys_root=Path(provider_keys_root) if provider_keys_root is not None else None,
        )
        if graph_extraction_config is not None:
            _ensure_graph_config_editable(world_metadata=world_metadata)
            graph_config = graph_extraction_config
            save_graph_config(resolved_world_dir, graph_config)
        else:
            graph_config = load_or_create_graph_config(
                world_dir=resolved_world_dir,
                extraction_concurrency=extraction_concurrency,
            )

        # BLOCK 3: Start or resume the world's durable ingestion run before the first book is processed
        # VARS: ingestion_run_id = stable run boundary shared by chunk embeddings, extraction manifests, and graph manifestation work in this unfinished world run
        # WHY: Appended books must join the same paused run until every stage finishes, otherwise one world would leak work across unrelated run ids
        ingestion_run_id = begin_world_ingestion_run(
            world_dir=resolved_world_dir,
            metadata=world_metadata,
        )
        run_started = True
        logger.info(
            "TXT ingestion run starting inside world: world_name=%s source_file_count=%s world_dir=%s ingestion_run_id=%s",
            world_name,
            len(source_files),
            resolved_world_dir,
            ingestion_run_id,
        )

        # BLOCK 4: Resolve whether each requested source resumes an existing stored book or appends into the next free book slot
        # VARS: planned_books = ordered (book_number, source_path) pairs chosen for this call without reusing an occupied slot by accident
        # WHY: Existing-world ingest has to keep resume working for already stored books while still appending truly new books after the highest claimed slot
        planned_books = _plan_requested_books(
            world_dir=resolved_world_dir,
            source_files=source_files,
        )

        # BLOCK 5: Process the planned books in user-supplied order after the book-number plan is fixed
        # VARS: book_result = structured result for one ingested book, book_warnings = recoverable events raised while processing that book
        # WHY: The numbering plan must be decided before the loop so append-safe numbering stays stable even when the same call mixes resumes and new books
        for book_number, source_file in planned_books:
            logger.info(
                "Book ingestion starting: world_name=%s book_number=%s source_path=%s",
                world_name,
                book_number,
                source_file,
            )
            book_result, book_warnings = _ingest_single_book(
                world_name=world_name,
                world_uuid=world_metadata.world_uuid,
                world_dir=resolved_world_dir,
                source_path=Path(source_file),
                book_number=book_number,
                ingestion_run_id=ingestion_run_id,
                config=config,
                embedding_profile=world_metadata.embedding_profile,
                embedding_concurrency=embedding_concurrency,
                graph_config=graph_config,
                cancellation=cancellation,
                provider_keys_root=Path(provider_keys_root) if provider_keys_root is not None else None,
                vector_store_root=Path(vector_store_root) if vector_store_root is not None else None,
            )
            books.append(book_result)
            warnings.extend(book_warnings)
            logger.info(
                "Book ingestion completed: world_name=%s book_number=%s completed_chunks=%s total_chunks=%s",
                world_name,
                book_result.book_number,
                book_result.completed_chunks,
                book_result.total_chunks,
            )
    except IngestionError as error:
        # BLOCK 6: Stop only the run this call actually started, then return a structured failure payload with any safe partial progress
        # WHY: Setup-time validation errors should not silently pause someone else's already-active run, but once this call started work the run must stay resumable instead of looking completed
        if run_started and world_metadata is not None:
            finish_world_ingestion_run(
                world_dir=resolved_world_dir,
                metadata=world_metadata,
                completed=False,
            )
        logger.error(
            "TXT ingestion stopped by blocking error: world_name=%s error_code=%s details=%s",
            world_name,
            error.code,
            error.details,
        )
        return IngestionResult(
            success=False,
            world_id=world_name,
            world_uuid=world_metadata.world_uuid if world_metadata is not None else None,
            world_path=str(resolved_world_dir),
            books=books,
            warnings=warnings,
            errors=[error],
        )

    # BLOCK 7: Mark the active run completed only if every selected book reached both embedding and raw graph extraction completion
    # WHY: Missing extraction keys or cancellation can still return a structured ingestion result, but the world run must remain active for resume until all current work finishes
    run_completed = all(_book_pipeline_completed(book) for book in books)
    finish_world_ingestion_run(
        world_dir=resolved_world_dir,
        metadata=world_metadata,
        completed=run_completed,
    )
    # BLOCK 8: Return the successful world-level result once every book finishes
    # WHY: The caller needs one top-level payload that includes all per-book outputs and any warnings collected during the run
    logger.info(
        "TXT ingestion completed successfully: world_name=%s ingested_books=%s warnings=%s",
        world_name,
        len(books),
        len(warnings),
    )
    return IngestionResult(
        success=True,
        world_id=world_name,
        world_uuid=world_metadata.world_uuid,
        world_path=str(resolved_world_dir),
        books=books,
        warnings=warnings,
    )


def reingest_world_from_stored_sources(
    *,
    world_name: str,
    config: SplitterConfig,
    world_dir: str | Path,
    embedding_profile: EmbeddingProfile | None = None,
    embedding_concurrency: int = 5,
    graph_extraction_config: GraphExtractionConfig | None = None,
    extraction_concurrency: int = 5,
    cancellation: EmbeddingRunCancellation | None = None,
    provider_keys_root: str | Path | None = None,
    vector_store_root: str | Path | None = None,
) -> IngestionResult:
    """Rebuild every stored book in a world from the world's preserved source copies."""
    resolved_world_dir = Path(world_dir)
    current_metadata = load_world_metadata(resolved_world_dir)
    if current_metadata is None:
        return IngestionResult(
            success=False,
            world_id=world_name,
            world_uuid=None,
            world_path=str(resolved_world_dir),
            errors=[
                IngestionError(
                    code="WORLD_METADATA_MISSING",
                    message="Full-world re-ingest requires existing world metadata.",
                    details={"world_dir": str(resolved_world_dir)},
                )
            ],
        )

    requested_profile = embedding_profile if embedding_profile is not None else current_metadata.embedding_profile
    requested_splitter_lock = _world_splitter_lock(config)
    resolved_graph_config = graph_extraction_config
    if resolved_graph_config is None:
        existing_graph_config = load_graph_config(resolved_world_dir)
        resolved_graph_config = existing_graph_config if existing_graph_config is not None else load_or_create_graph_config(
            world_dir=resolved_world_dir,
            extraction_concurrency=extraction_concurrency,
        )

    try:
        _ensure_embedding_credentials_available(
            embedding_profile=requested_profile,
            provider_keys_root=Path(provider_keys_root) if provider_keys_root is not None else None,
        )
        stored_book_numbers = existing_book_numbers(resolved_world_dir)
        if not stored_book_numbers:
            raise IngestionError(
                code="STORED_SOURCE_MISSING",
                message="Full-world re-ingest requires at least one stored source copy in the world.",
                details={"world_dir": str(resolved_world_dir)},
            )

        # BLOCK 1: Capture the stored source list before any derived outputs are deleted
        # VARS: stored_sources = per-book source copies sorted by their durable world-local book number
        # WHY: Full-world re-ingest must preserve original source copies while rebuilding only derived outputs from those same saved artifacts
        stored_sources = [
            (book_number, load_stored_source_paths(world_dir=resolved_world_dir, book_number=book_number))
            for book_number in stored_book_numbers
        ]

        # BLOCK 2: Best-effort clean up old derived outputs before the fresh run starts
        # WHY: A full-world re-ingest must replace old chunks, vectors, and raw graph data so the new run does not leave stale derived state behind
        _cleanup_full_reingest_outputs(
            world_dir=resolved_world_dir,
            metadata=current_metadata,
            stored_book_numbers=stored_book_numbers,
            vector_store_root=Path(vector_store_root) if vector_store_root is not None else None,
        )

        # BLOCK 3: Write the requested new world-level locks and clear the prior run boundary before the rebuild starts
        # WHY: A full-world re-ingest is the one allowed path that can replace the world's locked splitter contract and embedding profile
        current_metadata.embedding_profile = requested_profile
        current_metadata.splitter_config = requested_splitter_lock
        current_metadata.active_ingestion_run_id = None
        current_metadata.active_ingestion_run_status = None
        save_world_metadata(resolved_world_dir / "world.json", current_metadata)
        save_graph_config(resolved_world_dir, resolved_graph_config)
        ingestion_run_id = begin_world_ingestion_run(world_dir=resolved_world_dir, metadata=current_metadata)

        books: list[BookIngestionResult] = []
        warnings: list[OperationEvent] = []
        for book_number, stored_source in stored_sources:
            source_path = stored_source.primary_path if stored_source.primary_path.exists() else stored_source.backup_path
            book_result, book_warnings = _ingest_single_book(
                world_name=world_name,
                world_uuid=current_metadata.world_uuid,
                world_dir=resolved_world_dir,
                source_path=source_path,
                book_number=book_number,
                ingestion_run_id=ingestion_run_id,
                config=config,
                embedding_profile=current_metadata.embedding_profile,
                embedding_concurrency=embedding_concurrency,
                graph_config=resolved_graph_config,
                cancellation=cancellation,
                provider_keys_root=Path(provider_keys_root) if provider_keys_root is not None else None,
                vector_store_root=Path(vector_store_root) if vector_store_root is not None else None,
            )
            books.append(book_result)
            warnings.extend(book_warnings)

        run_completed = all(_book_pipeline_completed(book) for book in books)
        finish_world_ingestion_run(
            world_dir=resolved_world_dir,
            metadata=current_metadata,
            completed=run_completed,
        )
        return IngestionResult(
            success=True,
            world_id=world_name,
            world_uuid=current_metadata.world_uuid,
            world_path=str(resolved_world_dir),
            books=books,
            warnings=warnings,
        )
    except IngestionError as error:
        finish_world_ingestion_run(
            world_dir=resolved_world_dir,
            metadata=current_metadata,
            completed=False,
        )
        return IngestionResult(
            success=False,
            world_id=world_name,
            world_uuid=current_metadata.world_uuid,
            world_path=str(resolved_world_dir),
            errors=[error],
        )


def _ingest_single_book(
    *,
    world_name: str,
    world_uuid: str,
    world_dir: Path,
    source_path: Path,
    book_number: int,
    ingestion_run_id: str,
    config: SplitterConfig,
    embedding_profile: EmbeddingProfile,
    embedding_concurrency: int,
    graph_config: GraphExtractionConfig,
    cancellation: EmbeddingRunCancellation | None,
    provider_keys_root: Path | None,
    vector_store_root: Path | None,
) -> tuple[BookIngestionResult, list[OperationEvent]]:
    # BLOCK 1: Reject missing source files before any world-local copies or manifests are created
    # WHY: Failing before setup prevents stale partial world state when the user-selected file path is already invalid
    if not source_path.exists():
        logger.error(
            "Source file missing before ingestion setup: world_name=%s book_number=%s source_path=%s",
            world_name,
            book_number,
            source_path,
        )
        raise IngestionError(
            code="SOURCE_FILE_MISSING",
            message="The selected source file does not exist.",
            details={"source_path": str(source_path)},
        )

    # BLOCK 2: Copy the original source into the world and create a backup copy, then open a tracked source session for recovery
    # VARS: stored_source = paths for the working copy and backup plus the preserved original filename, session = helper that can switch from working copy to backup if needed
    # WHY: The splitter must work from app-owned copies so user-side file changes or removals do not directly corrupt the ingest process
    stored_source = copy_source_into_world(
        world_dir=world_dir,
        source_path=source_path,
        book_number=book_number,
    )
    session = SourceSession(
        primary_path=stored_source.primary_path,
        backup_path=stored_source.backup_path,
        book_number=book_number,
        source_filename=stored_source.source_filename,
    )

    # BLOCK 3: Choose the right converter and turn the stored source into text before any chunking begins
    # VARS: source_event = recovery warning if the session had to switch to backup before reading, converter_source = whichever stored file is currently active
    # WHY: Conversion must happen against the app-owned copy or backup, not the original user path, so resume and recovery behavior stay consistent
    converter = get_converter(stored_source.primary_path)
    raw_bytes, source_event = session.read_active_bytes()
    converter_source = session.active_path

    converted_document = converter.convert(converter_source)
    warnings: list[OperationEvent] = []
    if source_event is not None:
        warnings.append(source_event)
        logger.warning(
            "Recoverable source warning raised during conversion: world_name=%s book_number=%s warning_code=%s",
            world_name,
            book_number,
            source_event.code,
        )

    # BLOCK 4: Stop if the decoded content has no real text beyond whitespace
    # WHY: Chunking blank content would create meaningless chunks and violate the agreed rule that spaces and newlines alone do not count as usable text
    if not has_usable_text(converted_document.text):
        logger.error(
            "Source file rejected because it has no usable text: world_name=%s book_number=%s source_filename=%s",
            world_name,
            book_number,
            stored_source.source_filename,
        )
        raise IngestionError(
            code="SOURCE_EMPTY",
            message="The source file does not contain any usable text.",
            details={
                "source_filename": stored_source.source_filename,
                "book_number": book_number,
            },
        )

    # BLOCK 5: Build the chunk drafts in memory, then load or create the per-book progress manifest
    # VARS: chunk_drafts = all future chunks before they are written to disk, manifest = per-book progress metadata used for resume safety
    # WHY: The manifest needs the final total chunk count up front, which means chunk boundaries must be known before progress metadata is initialized
    chunk_drafts = split_text(converted_document.text, config)
    book_dir = book_directory(world_dir, book_number)
    manifest_path = manifest_file_path(book_dir)
    manifest = load_manifest(manifest_path)
    if manifest is None:
        manifest = BookManifest.create(
            world_id=world_name,
            world_uuid=world_uuid,
            source_filename=stored_source.source_filename,
            book_number=book_number,
            total_chunks=len(chunk_drafts),
            splitter_config=config,
        )
        save_manifest(manifest_path, manifest)
    elif manifest.splitter_config is None:
        manifest.splitter_config = config
        save_manifest(manifest_path, manifest)
    elif manifest.splitter_config != config:
        raise IngestionError(
            code="RESUME_STATE_CONFLICT",
            message="The existing progress metadata uses different chunking settings.",
            details={
                "book_number": book_number,
                "source_filename": stored_source.source_filename,
            },
        )
    elif manifest.total_chunks != len(chunk_drafts):
        raise IngestionError(
            code="RESUME_STATE_CONFLICT",
            message="The existing progress metadata does not match the current source and config.",
            details={
                "book_number": book_number,
                "source_filename": stored_source.source_filename,
            },
        )

    # BLOCK 6: Leave preserved source files untouched even when the decoded text did not start as UTF-8
    # WHY: The requirement is to keep exact copied originals and only use temporary text conversion for the active split operation
    if raw_bytes and converted_document.encoding.lower() != "utf-8":
        # Conversion happens in-memory only; this intentionally leaves the copied files untouched.
        pass

    # BLOCK 7: Figure out where a resumed run should restart, then save each remaining chunk and update progress after every completed write
    # VARS: start_chunk_number = first chunk number that still needs to be written, availability_event = warning raised if the session had to switch from working source to backup during processing, record = payload saved for one completed chunk
    # WHY: Chunk-by-chunk persistence is what makes resume safe; waiting until the whole book finishes would lose work after crashes and break progress tracking
    start_chunk_number = _resolve_resume_start(manifest=manifest, book_dir=book_dir)
    logger.info(
        "Chunk persistence starting for book: world_name=%s book_number=%s resume_chunk=%s total_chunks=%s",
        world_name,
        book_number,
        start_chunk_number,
        manifest.total_chunks,
    )

    for draft in chunk_drafts[start_chunk_number - 1 :]:
        availability_event = session.ensure_available()
        if availability_event is not None:
            warnings.append(availability_event)
            manifest.append_warning(availability_event)
            logger.warning(
                "Recoverable source warning raised during chunk persistence: world_name=%s book_number=%s warning_code=%s",
                world_name,
                book_number,
                availability_event.code,
            )
            save_manifest(manifest_path, manifest)

        record = ChunkRecord(
            world_id=world_name,
            world_uuid=world_uuid,
            source_filename=stored_source.source_filename,
            book_number=book_number,
            chunk_number=draft.chunk_number,
            chunk_position=f"{draft.chunk_number}/{draft.total_chunks}",
            overlap_text=draft.overlap_text,
            chunk_text=draft.chunk_text,
        )
        persist_completed_chunk(
            chunk_path=chunk_file_path(book_dir, book_number, draft.chunk_number),
            record=record,
            manifest_path=manifest_path,
            manifest=manifest,
        )

    # BLOCK 8: Return the book-level result with generated chunk paths after the book finishes successfully
    # WHY: The caller needs a stable summary of what was created without re-scanning the filesystem after ingestion
    chunk_paths = [
        str(chunk_file_path(book_dir, book_number, chunk_number))
        for chunk_number in range(1, manifest.total_chunks + 1)
    ]
    embedding_result, embedding_warnings = _embed_book_chunks_for_world(
        world_name=world_name,
        world_uuid=world_uuid,
        ingestion_run_id=ingestion_run_id,
        book_dir=book_dir,
        book_number=book_number,
        source_filename=stored_source.source_filename,
        chunk_paths=chunk_paths,
        embedding_profile=embedding_profile,
        embedding_concurrency=embedding_concurrency,
        cancellation=cancellation,
        provider_keys_root=provider_keys_root,
        vector_store_root=vector_store_root,
    )
    warnings.extend(embedding_warnings)
    graph_extraction_result = None

    # BLOCK 9: Log the graph extraction stage transition only when embeddings finished and extraction is actually eligible to run
    # WHY: The new graph pipeline needs visible book-stage boundaries in terminal logs, but logging the stage before embeddings complete would misrepresent work that never started
    if embedding_result.status == "completed":
        logger.info(
            "Graph extraction starting for world_uuid=%s run=%s book=%s chunk_count=%s provider=%s model=%s gleaning_count=%s.",
            world_uuid,
            ingestion_run_id,
            book_number,
            len(chunk_paths),
            graph_config.provider_id,
            graph_config.model_id,
            graph_config.gleaning_count,
        )
        graph_extraction_result, graph_warnings = _extract_book_graph_for_world(
            world_name=world_name,
            world_uuid=world_uuid,
            ingestion_run_id=ingestion_run_id,
            book_dir=book_dir,
            book_number=book_number,
            source_filename=stored_source.source_filename,
            chunk_paths=chunk_paths,
            graph_config=graph_config,
            provider_keys_root=provider_keys_root,
        )
        warnings.extend(graph_warnings)
        logger.info(
            "Graph extraction finished for world_uuid=%s run=%s book=%s status=%s extracted=%s failed=%s pending=%s warnings=%s.",
            world_uuid,
            ingestion_run_id,
            book_number,
            graph_extraction_result.status,
            graph_extraction_result.extracted_chunks,
            graph_extraction_result.failed_chunks,
            graph_extraction_result.pending_chunks,
            len(graph_warnings),
        )
    else:
        logger.info(
            "Graph extraction skipped for world_uuid=%s run=%s book=%s because embeddings are not completed: embedding_status=%s.",
            world_uuid,
            ingestion_run_id,
            book_number,
            embedding_result.status,
        )
    graph_manifestation_result = None

    # BLOCK 10: Log the graph manifestation stage transition only when extraction finished for the whole book
    # WHY: Graph manifestation trusts only completed extraction manifests, so logging a start before that point would hide the true dependency boundary
    if graph_extraction_result is not None and graph_extraction_result.status == "completed":
        logger.info(
            "Graph manifestation starting for world_uuid=%s run=%s book=%s extraction_manifest=%s.",
            world_uuid,
            ingestion_run_id,
            book_number,
            Path(graph_extraction_result.manifest_path).name,
        )
        graph_manifestation_result, manifestation_warnings = _manifest_book_graph_for_world(
            world_name=world_name,
            world_dir=world_dir,
            book_dir=book_dir,
            graph_extraction_manifest_path=Path(graph_extraction_result.manifest_path),
            embedding_profile=embedding_profile,
            embedding_concurrency=embedding_concurrency,
            provider_keys_root=provider_keys_root,
            vector_store_root=vector_store_root,
        )
        warnings.extend(manifestation_warnings)
        logger.info(
            "Graph manifestation finished for world_uuid=%s run=%s book=%s status=%s manifested_nodes=%s failed_nodes=%s pending_nodes=%s manifested_edges=%s failed_edges=%s pending_edges=%s warnings=%s.",
            world_uuid,
            ingestion_run_id,
            book_number,
            graph_manifestation_result.status,
            graph_manifestation_result.manifested_nodes,
            graph_manifestation_result.failed_nodes,
            graph_manifestation_result.pending_nodes,
            graph_manifestation_result.manifested_edges,
            graph_manifestation_result.failed_edges,
            graph_manifestation_result.pending_edges,
            len(manifestation_warnings),
        )
    elif graph_extraction_result is None:
        logger.info(
            "Graph manifestation skipped for world_uuid=%s run=%s book=%s because graph extraction did not run.",
            world_uuid,
            ingestion_run_id,
            book_number,
        )
    else:
        logger.info(
            "Graph manifestation skipped for world_uuid=%s run=%s book=%s because graph extraction is not completed: extraction_status=%s.",
            world_uuid,
            ingestion_run_id,
            book_number,
            graph_extraction_result.status,
        )
    return (
        BookIngestionResult(
            book_number=book_number,
            source_filename=stored_source.source_filename,
            total_chunks=manifest.total_chunks,
            completed_chunks=manifest.last_completed_chunk,
            manifest_path=str(manifest_path),
            chunk_paths=chunk_paths,
            embedding=embedding_result,
            graph_extraction=graph_extraction_result,
            graph_manifestation=graph_manifestation_result,
        ),
        warnings,
    )


def _resolve_resume_start(*, manifest: BookManifest, book_dir: Path) -> int:
    # BLOCK 1: Count how many chunks are both marked complete in the manifest and actually present on disk
    # VARS: contiguous_completed = number of trustworthy completed chunks from the start of the book with no gaps
    # WHY: Resume must trust only chunks that have both completion metadata and real files; either signal alone could be stale after a crash
    contiguous_completed = 0
    for state in manifest.chunk_states:
        if not state.completed:
            break
        if not chunk_file_path(book_dir, manifest.book_number, state.chunk_number).exists():
            break
        contiguous_completed += 1

    # BLOCK 2: Repair the manifest in memory if it claims more completed chunks than the filesystem can prove
    # WHY: This prevents half-finished or manually deleted chunk files from causing the next run to skip work that still needs to be redone
    if contiguous_completed != manifest.last_completed_chunk:
        manifest.last_completed_chunk = contiguous_completed
        for state in manifest.chunk_states[contiguous_completed:]:
            state.completed = False

    # BLOCK 3: Resume at the first chunk after the last trustworthy completed one
    # WHY: Restarting earlier would redo safe work unnecessarily, while restarting later could miss data if the manifest was ahead of the files
    return contiguous_completed + 1


def _book_pipeline_completed(book: BookIngestionResult) -> bool:
    # BLOCK 1: Decide whether one book finished every ingestion stage that belongs to the active ingestion run
    # WHY: The world run id should remain active when graph extraction or graph manifestation is pending because later resume must keep writing to the same run boundary
    return (
        book.embedding is not None
        and book.embedding.status == "completed"
        and book.graph_extraction is not None
        and book.graph_extraction.status == "completed"
        and book.graph_manifestation is not None
        and book.graph_manifestation.status == "completed"
    )


def _world_splitter_lock(config: SplitterConfig) -> WorldSplitterConfig:
    # BLOCK 1: Convert the per-call splitter settings into the world-level lock shape stored in world metadata
    # WHY: The world lock must live in `world.json`, but chunking still uses the ingestion module's runtime config object during actual splitting
    return WorldSplitterConfig(
        chunk_size=config.chunk_size,
        max_lookback=config.max_lookback,
        overlap_size=config.overlap_size,
    )


def _ensure_graph_config_editable(*, world_metadata) -> None:
    # BLOCK 1: Reject graph extraction default edits while a world run is already active
    # WHY: The plan only allows extraction-model and prompt changes while paused or idle so in-flight work cannot silently switch defaults mid-run
    if world_metadata.active_ingestion_run_status == "active":
        raise IngestionError(
            code="GRAPH_CONFIG_RUN_ACTIVE",
            message="Graph extraction defaults can only be changed while the world is paused or idle.",
            details={"world_uuid": world_metadata.world_uuid, "world_name": world_metadata.world_name},
        )


def _plan_requested_books(*, world_dir: Path, source_files: list[str | Path]) -> list[tuple[int, Path]]:
    # BLOCK 1: Build one append-safe plan that either resumes matching stored books or allocates fresh book slots for new sources
    # VARS: existing_sources_by_filename = saved world-local source slots grouped by original filename, allocated_book_numbers = slots already chosen during this one call
    # WHY: Existing-world ingest must preserve resume behavior for already stored sources while preventing a later append from reusing `book_01` and overwriting prior world data
    existing_sources_by_filename: dict[str, list[int]] = {}
    for book_number in existing_book_numbers(world_dir):
        try:
            stored_source = load_stored_source_paths(world_dir=world_dir, book_number=book_number)
        except IngestionError:
            continue
        existing_sources_by_filename.setdefault(stored_source.source_filename, []).append(book_number)

    allocated_book_numbers: set[int] = set()
    next_new_book_number = next_book_number(world_dir)
    planned_books: list[tuple[int, Path]] = []
    for raw_source in source_files:
        source_path = Path(raw_source)
        if not source_path.exists():
            raise IngestionError(
                code="SOURCE_FILE_MISSING",
                message="The selected source file does not exist.",
                details={"source_path": str(source_path)},
            )
        matching_book_number = _matching_stored_book_number(
            world_dir=world_dir,
            existing_sources_by_filename=existing_sources_by_filename,
            source_path=source_path,
        )
        if matching_book_number is None:
            book_number = next_new_book_number
            next_new_book_number += 1
        else:
            book_number = matching_book_number
        if book_number in allocated_book_numbers:
            raise IngestionError(
                code="STORED_SOURCE_AMBIGUOUS",
                message="The requested sources mapped to the same stored book slot.",
                details={"book_number": book_number, "source_path": str(source_path)},
            )
        allocated_book_numbers.add(book_number)
        planned_books.append((book_number, source_path))
    return planned_books


def _matching_stored_book_number(
    *,
    world_dir: Path,
    existing_sources_by_filename: dict[str, list[int]],
    source_path: Path,
) -> int | None:
    # BLOCK 1: Match an incoming source to one stored book only when the saved world-local source copy is byte-for-byte the same file
    # WHY: Reusing a book number should mean true resume, while changing a stored book's source requires the dedicated full-world re-ingest path instead of silent selective replacement
    matching_numbers = existing_sources_by_filename.get(source_path.name, [])
    if not matching_numbers:
        return None
    if len(matching_numbers) > 1:
        raise IngestionError(
            code="STORED_SOURCE_AMBIGUOUS",
            message="More than one stored book uses this source filename, so resume could not choose a single slot safely.",
            details={"source_filename": source_path.name, "world_dir": str(world_dir)},
        )
    book_number = matching_numbers[0]
    stored_source = load_stored_source_paths(world_dir=world_dir, book_number=book_number)
    if _stored_source_matches_request(stored_source=stored_source, source_path=source_path):
        return book_number
    raise IngestionError(
        code="WORLD_REINGEST_REQUIRED",
        message="Replacing a stored book source requires a full-world re-ingest.",
        details={"book_number": book_number, "source_filename": source_path.name},
    )


def _stored_source_matches_request(*, stored_source, source_path: Path) -> bool:
    # BLOCK 1: Compare the incoming source bytes to the world's saved source copy for the same book slot
    # WHY: Filename matches alone are too weak; resume must only reuse a stored slot when the caller is pointing at the same source content that created it
    stored_path = stored_source.primary_path if stored_source.primary_path.exists() else stored_source.backup_path
    if not stored_path.exists():
        return False
    return stored_path.read_bytes() == source_path.read_bytes()


def _cleanup_full_reingest_outputs(
    *,
    world_dir: Path,
    metadata,
    stored_book_numbers: list[int],
    vector_store_root: Path | None,
) -> None:
    # BLOCK 1: Remove old chunk vectors, node vectors, Neo4j rows, and per-book output folders before the fresh world-wide rebuild starts
    # VARS: extraction_manifests = saved raw graph manifests grouped by book so each cleanup backend can run in sequence without reopening world files repeatedly
    # WHY: The local Qdrant stores both lock the same storage path, so chunk-vector cleanup and node-vector cleanup must happen in separate passes instead of holding two live clients at once
    resolved_vector_store_root = vector_store_root if vector_store_root is not None else default_vector_store_root()
    graph_writer = create_default_graph_writer(world_dir=world_dir)
    extraction_manifests: list[tuple[int, object]] = []
    try:
        for book_number in stored_book_numbers:
            book_dir = book_directory(world_dir, book_number)
            try:
                extraction_manifest = load_extraction_manifest(book_dir / "graph_extraction.json")
            except GraphExtractionError:
                extraction_manifest = None
            if extraction_manifest is not None:
                extraction_manifests.append((book_number, extraction_manifest))

        # BLOCK 2: Delete old chunk embeddings first, then close that local Qdrant client before touching node-vector storage
        # WHY: Local Qdrant uses a filesystem lock, so sharing one storage root across two open clients in the same process causes avoidable re-ingest failures
        chunk_store = QdrantChunkStore(store_root=resolved_vector_store_root)
        try:
            chunk_store.ensure_collection(metadata.embedding_profile)
            for book_number in stored_book_numbers:
                book_dir = book_directory(world_dir, book_number)
                manifest = load_manifest(manifest_file_path(book_dir))
                if manifest is None:
                    continue
                chunk_store.delete_points(
                    [
                        _chunk_point_id(
                            world_uuid=metadata.world_uuid,
                            book_number=book_number,
                            chunk_number=chunk_number,
                        )
                        for chunk_number in range(1, manifest.total_chunks + 1)
                    ]
                )
        finally:
            chunk_store.close()

        # BLOCK 3: Delete manifested node vectors only after the chunk-vector store client is closed
        # WHY: Full-world re-ingest must clear stale graph node vectors too, but sequential store access keeps the shared local Qdrant path usable
        node_vector_store = QdrantGraphNodeVectorStore(
            world=metadata,
            vector_store_root=resolved_vector_store_root,
        )
        try:
            for _, extraction_manifest in extraction_manifests:
                for chunk_state in extraction_manifest.chunk_states:
                    node_vector_store.delete_chunk_node_vectors(
                        world_uuid=extraction_manifest.world_uuid,
                        ingestion_run_id=extraction_manifest.ingestion_run_id,
                        book_number=extraction_manifest.book_number,
                        chunk_number=chunk_state.chunk_number,
                    )
        finally:
            node_vector_store.close()

        # BLOCK 4: Delete old Neo4j rows after vector cleanup, then remove each book's derived output folder
        # WHY: The source copies must survive full-world re-ingest, but manifests and chunk files need to be rebuilt from scratch for the new run
        for book_number, extraction_manifest in extraction_manifests:
            for chunk_state in extraction_manifest.chunk_states:
                try:
                    graph_writer.delete_chunk(
                        world_uuid=extraction_manifest.world_uuid,
                        ingestion_run_id=extraction_manifest.ingestion_run_id,
                        book_number=extraction_manifest.book_number,
                        chunk_number=chunk_state.chunk_number,
                    )
                except GraphStoreUnavailable as error:
                    logger.warning(
                        "Full-world re-ingest left old graph rows in place because Neo4j was unavailable: world_uuid=%s book=%s chunk=%s code=%s",
                        extraction_manifest.world_uuid,
                        extraction_manifest.book_number,
                        chunk_state.chunk_number,
                        error.code,
                    )
            remove_book_output_directory(world_dir=world_dir, book_number=book_number)
        for book_number in stored_book_numbers:
            if all(saved_book_number != book_number for saved_book_number, _ in extraction_manifests):
                remove_book_output_directory(world_dir=world_dir, book_number=book_number)
    finally:
        close_writer = getattr(graph_writer, "close", None)
        if callable(close_writer):
            close_writer()


def _chunk_point_id(*, world_uuid: str, book_number: int, chunk_number: int) -> str:
    # BLOCK 1: Rebuild the stable chunk-vector point id used by embedding storage so full-world re-ingest can delete old vectors safely
    # WHY: Old chunk vectors may outlive deleted book folders, so cleanup needs the same deterministic id contract the embedding stage used originally
    return str(uuid5(UUID(world_uuid), f"book:{book_number}:chunk:{chunk_number}"))


def _ensure_locked_world_metadata(
    *,
    world_dir: Path,
    world_name: str,
    embedding_profile: EmbeddingProfile | None,
    splitter_config: WorldSplitterConfig,
):
    # BLOCK 1: Make sure every world directory has a stable UUID and one locked embedding profile before chunk ingestion starts
    # WHY: Vector identity and future rename safety both depend on world metadata existing before any chunk or embedding payload is written
    try:
        return ensure_world_metadata(
            world_dir=world_dir,
            world_name=world_name,
            embedding_profile=embedding_profile,
            splitter_config=splitter_config,
        )
    except EmbeddingConfigurationError as error:
        if error.code in {"WORLD_EMBEDDING_PROFILE_LOCKED", "WORLD_SPLITTER_CONFIG_LOCKED"}:
            raise IngestionError(
                code="WORLD_REINGEST_REQUIRED",
                message="The requested world settings require a full-world re-ingest instead of a normal existing-world ingest.",
                details=error.details,
            ) from error
        raise IngestionError(
            code=error.code,
            message=error.message,
            details=error.details,
        ) from error


def _require_new_world_embedding_profile(
    *,
    world_name: str,
    embedding_profile: EmbeddingProfile | None,
) -> EmbeddingProfile:
    # BLOCK 1: Stop brand-new world creation when no explicit embedding profile was supplied
    # WHY: New worlds are required to lock a user-chosen embedding model up front, and delaying that failure until after folder creation would leave stray world directories behind
    if embedding_profile is None:
        raise IngestionError(
            code="EMBEDDING_PROFILE_REQUIRED",
            message="A new world must be created with an explicit embedding profile.",
            details={"world_name": world_name},
        )
    return embedding_profile


def _ensure_embedding_credentials_available(
    *,
    embedding_profile: EmbeddingProfile,
    provider_keys_root: Path | None,
) -> None:
    # BLOCK 1: Check for at least one credential that can serve the world's locked embedding model before any source copying or chunk writes begin
    # WHY: The user asked for missing-key failures before ingestion work starts, so the run must stop before creating partial chunk state that can never be embedded
    try:
        eligible_credentials = load_eligible_provider_credentials(
            provider_id=embedding_profile.provider_id,
            model_id=embedding_profile.model_id,
            provider_keys_root=provider_keys_root,
        )
    except ProviderKeyConfigurationError as error:
        raise IngestionError(
            code=error.code,
            message=error.message,
            details=error.details,
        ) from error
    if not eligible_credentials:
        logger.error(
            "TXT ingestion blocked before chunking because no provider credentials can serve model=%s provider=%s.",
            embedding_profile.model_id,
            embedding_profile.provider_id,
        )
        raise IngestionError(
            code="EMBEDDING_PROVIDER_KEYS_MISSING",
            message="No provider credentials are configured for the selected embedding model.",
            details={
                "provider_id": embedding_profile.provider_id,
                "model_id": embedding_profile.model_id,
            },
        )


def _embed_book_chunks_for_world(
    *,
    world_name: str,
    world_uuid: str,
    ingestion_run_id: str,
    book_dir: Path,
    book_number: int,
    source_filename: str,
    chunk_paths: list[str],
    embedding_profile: EmbeddingProfile,
    embedding_concurrency: int,
    cancellation: EmbeddingRunCancellation | None,
    provider_keys_root: Path | None,
    vector_store_root: Path | None,
):
    # BLOCK 1: Run the book-level embedding stage and convert any hard vector-store or profile errors into the existing ingestion error contract
    # WHY: Automatic embeddings are part of ingestion now, so blocking embedding infrastructure failures must surface through the same structured result pathway as chunking failures
    try:
        embedding_result, embedding_warnings = embed_book_chunks(
            world=ensure_world_metadata(
                world_dir=book_dir.parents[1],
                world_name=world_name,
                embedding_profile=embedding_profile,
            ),
            book_dir=book_dir,
            book_number=book_number,
            source_filename=source_filename,
            chunk_paths=chunk_paths,
            provider_keys_root=provider_keys_root,
            vector_store_root=vector_store_root,
            concurrency=embedding_concurrency,
            cancellation=cancellation,
        )
    except (EmbeddingConfigurationError, ProviderKeyConfigurationError, VectorStoreError) as error:
        raise IngestionError(
            code=error.code,
            message=error.message,
            details={**error.details, "world_uuid": world_uuid, "book_number": book_number},
        ) from error
    return embedding_result, embedding_warnings


def _extract_book_graph_for_world(
    *,
    world_name: str,
    world_uuid: str,
    ingestion_run_id: str,
    book_dir: Path,
    book_number: int,
    source_filename: str,
    chunk_paths: list[str],
    graph_config: GraphExtractionConfig,
    provider_keys_root: Path | None,
):
    # BLOCK 1: Run graph extraction after embeddings and convert hard extraction setup failures into ingestion errors
    # WHY: Missing extraction credentials are resumable warnings, but malformed provider-key files or unsupported extraction configuration should still fail through the structured ingestion contract
    try:
        return extract_book_chunks(
            world_id=world_name,
            world_uuid=world_uuid,
            ingestion_run_id=ingestion_run_id,
            book_dir=book_dir,
            book_number=book_number,
            source_filename=source_filename,
            chunk_paths=chunk_paths,
            config=graph_config,
            provider_keys_root=provider_keys_root,
        )
    except (GraphExtractionError, ProviderKeyConfigurationError) as error:
        raise IngestionError(
            code=error.code,
            message=error.message,
            details={**error.details, "world_uuid": world_uuid, "book_number": book_number},
        ) from error


def _manifest_book_graph_for_world(
    *,
    world_name: str,
    world_dir: Path,
    book_dir: Path,
    graph_extraction_manifest_path: Path,
    embedding_profile: EmbeddingProfile,
    embedding_concurrency: int,
    provider_keys_root: Path | None,
    vector_store_root: Path | None,
):
    # BLOCK 1: Build the default backend-only graph manifestation adapters for this book
    # VARS: node_embedder = locked-profile provider adapter, vector_store = node-vector Qdrant adapter, graph_writer = Neo4j writer or pending-state fallback
    # WHY: Sweep 2 has no UI, so ingestion must assemble the persistence stage from backend services while keeping Neo4j unavailability resumable
    world = ensure_world_metadata(
        world_dir=world_dir,
        world_name=world_name,
        embedding_profile=embedding_profile,
    )
    vector_store = QdrantGraphNodeVectorStore(
        world=world,
        vector_store_root=vector_store_root,
    )
    graph_writer = create_default_graph_writer(world_dir=world_dir)
    try:
        return manifest_extracted_graph(
            extraction_manifest_path=graph_extraction_manifest_path,
            node_embedder=ScheduledNodeEmbedder(
                world=world,
                provider_keys_root=provider_keys_root,
                concurrency=embedding_concurrency,
            ),
            vector_store=vector_store,
            graph_writer=graph_writer,
        )
    except (GraphManifestationError, VectorStoreError, ProviderKeyConfigurationError) as error:
        raise IngestionError(
            code=error.code,
            message=error.message,
            details={**error.details, "book_dir": str(book_dir)},
        ) from error
    finally:
        vector_store.close()
        close_writer = getattr(graph_writer, "close", None)
        if callable(close_writer):
            close_writer()
