"""Service entrypoints for TXT splitter ingestion."""

from __future__ import annotations

from pathlib import Path

from backend.logger import get_logger

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
    ensure_world_does_not_exist,
    load_manifest,
    manifest_file_path,
    persist_completed_chunk,
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
        ensure_world_does_not_exist(world_dir)
        world_dir.mkdir(parents=True, exist_ok=False)
        logger.info("Created new world directory for ingestion: world_dir=%s", world_dir)
        return ingest_sources_into_existing_world(
            world_name=world_name,
            source_files=source_files,
            config=config,
            world_dir=world_dir,
        )
    except IngestionError as error:
        # BLOCK 3: Return a structured failure result instead of throwing raw exceptions beyond the ingestion boundary
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
            world_path=str(world_dir),
            errors=[error],
        )


def ingest_sources_into_existing_world(
    *,
    world_name: str,
    source_files: list[str | Path],
    config: SplitterConfig,
    world_dir: str | Path,
) -> IngestionResult:
    """Ingest books into an already-created world, resuming safely per book."""
    # BLOCK 1: Make sure the world folder exists and prepare result collectors for completed books and warnings
    # WHY: This entrypoint supports resuming into an existing world, so it must not assume the folder was just created by the caller
    resolved_world_dir = Path(world_dir)
    resolved_world_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "TXT ingestion run starting inside world: world_name=%s source_file_count=%s world_dir=%s",
        world_name,
        len(source_files),
        resolved_world_dir,
    )

    books: list[BookIngestionResult] = []
    warnings: list[OperationEvent] = []

    try:
        # BLOCK 2: Process each selected source file in user-supplied order so book numbering stays stable across the whole ingest
        # VARS: book_result = structured result for one ingested book, book_warnings = recoverable events raised while processing that book
        # WHY: Book order is part of the product contract, and changing iteration order would break chunk naming, metadata, and later retrieval assumptions
        for book_number, source_file in enumerate(source_files, start=1):
            logger.info(
                "Book ingestion starting: world_name=%s book_number=%s source_path=%s",
                world_name,
                book_number,
                source_file,
            )
            book_result, book_warnings = _ingest_single_book(
                world_name=world_name,
                world_dir=resolved_world_dir,
                source_path=Path(source_file),
                book_number=book_number,
                config=config,
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
        # BLOCK 3: Stop the ingestion run as soon as a blocking error happens and return whatever already completed safely
        # WHY: The current contract is fail-fast for hard errors; continuing past a bad book would require a different retry and partial-success policy
        logger.error(
            "TXT ingestion stopped by blocking error: world_name=%s error_code=%s details=%s",
            world_name,
            error.code,
            error.details,
        )
        return IngestionResult(
            success=False,
            world_id=world_name,
            world_path=str(resolved_world_dir),
            books=books,
            warnings=warnings,
            errors=[error],
        )

    # BLOCK 4: Return the successful world-level result once every book finishes
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
        world_path=str(resolved_world_dir),
        books=books,
        warnings=warnings,
    )


def _ingest_single_book(
    *,
    world_name: str,
    world_dir: Path,
    source_path: Path,
    book_number: int,
    config: SplitterConfig,
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
            source_filename=stored_source.source_filename,
            book_number=book_number,
            total_chunks=len(chunk_drafts),
        )
        save_manifest(manifest_path, manifest)
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
    return (
        BookIngestionResult(
            book_number=book_number,
            source_filename=stored_source.source_filename,
            total_chunks=manifest.total_chunks,
            completed_chunks=manifest.last_completed_chunk,
            manifest_path=str(manifest_path),
            chunk_paths=chunk_paths,
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
