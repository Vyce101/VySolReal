"""Filesystem storage helpers for TXT splitter ingestion."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from backend.logger import get_logger

from .errors import IngestionError
from .models import BookManifest, ChunkRecord, OperationEvent, StoredSourcePaths

logger = get_logger(__name__)


def default_worlds_root() -> Path:
    """Resolve the default user worlds directory from the repo root."""
    return Path(__file__).resolve().parents[3] / "user" / "worlds"


def copy_source_into_world(
    *,
    world_dir: Path,
    source_path: Path,
    book_number: int,
) -> StoredSourcePaths:
    """Create the working source copy and the backup copy for a book."""
    # BLOCK 1: Create separate folders for the working source copy and the backup copy inside the world
    # WHY: Keeping the primary copy and backup in different locations makes it possible to recover if the working copy disappears during splitting
    source_dir = world_dir / "source files" / f"book_{book_number:02d}"
    backup_dir = world_dir / ".backups" / f"book_{book_number:02d}"
    source_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # BLOCK 2: Preserve the original filename exactly while making both the working copy and backup copy
    # VARS: primary_path = world-local source file used for normal processing, backup_path = recovery copy used if the working source disappears
    # WHY: The app needs byte-for-byte preserved originals for trust and future recovery, so these copies must not be renamed or rewritten
    primary_path = source_dir / source_path.name
    backup_path = backup_dir / source_path.name
    logger.info("Copying source into world storage: source_filename=%s book_number=%s", source_path.name, book_number)

    # BLOCK 3: Reuse the already stored source when re-ingest or resume points at the world's own primary copy
    # WHY: Full-world re-ingest rebuilds from stored sources, and copying a file onto itself would fail instead of preserving the trusted in-world copy
    try:
        source_resolved = source_path.resolve(strict=False)
        primary_resolved = primary_path.resolve(strict=False)
        backup_resolved = backup_path.resolve(strict=False)
    except OSError:
        source_resolved = source_path
        primary_resolved = primary_path
        backup_resolved = backup_path
    if source_resolved == primary_resolved:
        if not backup_path.exists():
            _copy_binary_file(primary_path, backup_path)
        return StoredSourcePaths(
            primary_path=primary_path,
            backup_path=backup_path,
            source_filename=source_path.name,
        )

    # BLOCK 4: Restore the primary copy from the backup when re-ingest has to start from the saved backup file
    # WHY: The active source session expects the primary slot to stay canonical, even when a previous run had to fall back to backup storage
    if source_resolved == backup_resolved:
        if not primary_path.exists():
            _copy_binary_file(backup_path, primary_path)
        return StoredSourcePaths(
            primary_path=primary_path,
            backup_path=backup_path,
            source_filename=source_path.name,
        )

    # BLOCK 5: Copy an external source into both world-owned source slots for a normal new-book ingest
    # WHY: New sources must still produce both the working copy and the backup copy so later resumes can survive source loss
    _copy_binary_file(source_path, primary_path)
    _copy_binary_file(source_path, backup_path)

    return StoredSourcePaths(
        primary_path=primary_path,
        backup_path=backup_path,
        source_filename=source_path.name,
    )


def ensure_world_does_not_exist(world_dir: Path) -> None:
    """Reject duplicate world creation."""
    # BLOCK 1: Stop world creation if the target world folder already exists
    # WHY: The product rule is to reject duplicate world names and let the future UI keep the user in a rename flow instead of overwriting existing data
    if world_dir.exists():
        logger.error("World creation blocked because the world already exists: world_name=%s", world_dir.name)
        raise IngestionError(
            code="WORLD_NAME_EXISTS",
            message="A world with this name already exists.",
            details={"world_path": str(world_dir)},
        )


def load_manifest(manifest_path: Path) -> BookManifest | None:
    """Load a manifest if it already exists."""
    if not manifest_path.exists():
        return None
    logger.info("Loading existing progress manifest: manifest_name=%s", manifest_path.name)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return BookManifest.from_dict(payload)


def save_manifest(manifest_path: Path, manifest: BookManifest) -> None:
    """Write manifest atomically."""
    logger.info(
        "Saving progress manifest: manifest_name=%s book_number=%s last_completed_chunk=%s",
        manifest_path.name,
        manifest.book_number,
        manifest.last_completed_chunk,
    )
    atomic_write_json(manifest_path, manifest.to_dict())


def persist_completed_chunk(
    *,
    chunk_path: Path,
    record: ChunkRecord,
    manifest_path: Path,
    manifest: BookManifest,
) -> None:
    """Persist a chunk and then atomically advance the manifest."""
    # BLOCK 1: Write the chunk payload to disk before marking it complete in progress metadata
    # WHY: If the manifest were updated first, a crash could leave the system believing a chunk exists when its file was never fully written
    logger.info(
        "Persisting completed chunk: chunk_name=%s book_number=%s chunk_number=%s",
        chunk_path.name,
        record.book_number,
        record.chunk_number,
    )
    atomic_write_json(chunk_path, record.to_dict())

    # BLOCK 2: Mark the chunk as completed only after the chunk file is safely on disk, then save the manifest atomically
    # VARS: state = the manifest entry that tracks completion for this chunk number
    # WHY: Chunk data and completion state must move together to keep resume logic trustworthy after interruptions or disk errors
    state = manifest.chunk_states[record.chunk_number - 1]
    state.completed = True
    manifest.last_completed_chunk = max(manifest.last_completed_chunk, record.chunk_number)
    save_manifest(manifest_path, manifest)


def atomic_write_json(target_path: Path, payload: dict[str, object]) -> None:
    """Atomically write a JSON file to disk."""
    # BLOCK 1: Serialize the payload first, then delegate to the atomic text writer
    # WHY: Keeping JSON formatting separate from file replacement logic makes the write path reusable for manifests and chunks without duplicating safety code
    target_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    _atomic_write_text(target_path, serialized)


def read_chunk_file(chunk_path: Path) -> dict[str, object]:
    """Read a chunk payload."""
    return json.loads(chunk_path.read_text(encoding="utf-8"))


def chunk_file_path(book_dir: Path, book_number: int, chunk_number: int) -> Path:
    """Stable generated chunk file path."""
    return book_dir / "chunks" / f"book_{book_number:02d}_chunk_{chunk_number:04d}.json"


def manifest_file_path(book_dir: Path) -> Path:
    """Per-book manifest file path."""
    return book_dir / "progress.json"


def book_directory(world_dir: Path, book_number: int) -> Path:
    """Per-book output directory."""
    return world_dir / "books" / f"book_{book_number:02d}"


def stored_source_directory(world_dir: Path, book_number: int) -> Path:
    """Return the per-book stored source directory."""
    return world_dir / "source files" / f"book_{book_number:02d}"


def stored_backup_directory(world_dir: Path, book_number: int) -> Path:
    """Return the per-book stored backup directory."""
    return world_dir / ".backups" / f"book_{book_number:02d}"


def existing_book_numbers(world_dir: Path) -> list[int]:
    """Return every discovered book number under source, backup, or derived output folders."""
    # BLOCK 1: Scan all known per-book folder roots so append numbering cannot reuse an occupied slot
    # VARS: discovered_numbers = stable set of numeric book slots already claimed anywhere inside the world
    # WHY: Stored source copies are the durable book identity, but partially created book outputs or backups should still reserve their slot instead of letting a new ingest overwrite it
    discovered_numbers: set[int] = set()
    for base_dir in (world_dir / "source files", world_dir / ".backups", world_dir / "books"):
        if not base_dir.exists():
            continue
        for child in base_dir.iterdir():
            if not child.is_dir():
                continue
            match = re.fullmatch(r"book_(\d{2,})", child.name)
            if match is None:
                continue
            discovered_numbers.add(int(match.group(1)))
    return sorted(discovered_numbers)


def next_book_number(world_dir: Path) -> int:
    """Return the next free per-world book number."""
    # BLOCK 1: Allocate the next book slot after the highest existing world-local book number
    # WHY: Appending new books must never reuse a prior slot, even if the caller provides only one new source while older book folders still exist
    existing_numbers = existing_book_numbers(world_dir)
    return (max(existing_numbers) + 1) if existing_numbers else 1


def load_stored_source_paths(*, world_dir: Path, book_number: int) -> StoredSourcePaths:
    """Return the saved source and backup paths for one stored book slot."""
    # BLOCK 1: Rebuild the stored source paths from world-local source and backup folders
    # WHY: Resume and full-world re-ingest both work from the app-owned copies, so they need one helper that tolerates a missing primary file when a backup still exists
    source_dir = stored_source_directory(world_dir, book_number)
    backup_dir = stored_backup_directory(world_dir, book_number)
    source_files = sorted(path for path in source_dir.iterdir() if path.is_file()) if source_dir.exists() else []
    backup_files = sorted(path for path in backup_dir.iterdir() if path.is_file()) if backup_dir.exists() else []
    if len(source_files) > 1 or len(backup_files) > 1:
        raise IngestionError(
            code="STORED_SOURCE_AMBIGUOUS",
            message="The stored book source could not be identified unambiguously.",
            details={"book_number": book_number, "world_dir": str(world_dir)},
        )
    if not source_files and not backup_files:
        raise IngestionError(
            code="STORED_SOURCE_MISSING",
            message="The stored book source is missing from both the working and backup locations.",
            details={"book_number": book_number, "world_dir": str(world_dir)},
        )
    source_filename = source_files[0].name if source_files else backup_files[0].name
    if source_files and backup_files and source_files[0].name != backup_files[0].name:
        raise IngestionError(
            code="STORED_SOURCE_AMBIGUOUS",
            message="The stored working and backup source copies do not agree on the original filename.",
            details={"book_number": book_number, "world_dir": str(world_dir)},
        )
    return StoredSourcePaths(
        primary_path=source_dir / source_filename,
        backup_path=backup_dir / source_filename,
        source_filename=source_filename,
    )


def remove_book_output_directory(*, world_dir: Path, book_number: int) -> None:
    """Delete only the derived output directory for one book."""
    # BLOCK 1: Remove regenerated per-book outputs without touching stored source copies or backups
    # WHY: Full-world re-ingest must rebuild chunks and manifests from the saved sources while preserving the original source artifacts inside the world
    book_dir = book_directory(world_dir, book_number)
    if book_dir.exists():
        shutil.rmtree(book_dir)


def _copy_binary_file(source_path: Path, destination_path: Path) -> None:
    # BLOCK 1: Copy the file as raw bytes so the preserved source remains identical to what the user selected
    # WHY: Rewriting through a text decoder here would change encodings and violate the requirement to keep exact source copies inside the world
    try:
        shutil.copyfile(source_path, destination_path)
    except OSError as exc:
        _raise_for_os_error(
            exc,
            default_code="SOURCE_COPY_FAILED",
            default_message="Failed to copy the source file into the world.",
            details={
                "source_path": str(source_path),
                "destination_path": str(destination_path),
            },
        )


def _atomic_write_text(target_path: Path, payload: str) -> None:
    # BLOCK 1: Write to a temporary file in the same folder and flush it fully before replacing the real file
    # WHY: Replacing the target in one final step prevents half-written JSON from being mistaken for a valid chunk or manifest after a crash
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=target_path.parent,
            suffix=".tmp",
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, target_path)
    except OSError as exc:
        _raise_for_os_error(
            exc,
            default_code="FILE_WRITE_FAILED",
            default_message="Failed to write a file during TXT splitting.",
            details={"target_path": str(target_path)},
        )


def _raise_for_os_error(
    exc: OSError,
    *,
    default_code: str,
    default_message: str,
    details: dict[str, object],
) -> None:
    # BLOCK 1: Translate low-level filesystem errors into structured ingestion errors the backend contract understands
    # WHY: The service layer and future UI need stable error codes like DISK_FULL rather than raw platform-specific OSError details
    if exc.errno == errno.ENOSPC:
        logger.error("Disk full while writing ingestion data: details=%s", _safe_log_details(details))
        raise IngestionError(
            code="DISK_FULL",
            message="The disk is full and the splitter cannot continue.",
            details=details,
        ) from exc
    logger.error(
        "Filesystem operation failed during ingestion: code=%s message=%s details=%s",
        default_code,
        default_message,
        {**_safe_log_details(details), "os_error": str(exc)},
    )
    raise IngestionError(
        code=default_code,
        message=default_message,
        details={**details, "os_error": str(exc)},
    ) from exc


def _safe_log_details(details: dict[str, object]) -> dict[str, object]:
    # BLOCK 1: Strip local directory information from details before they enter logs
    # WHY: Users may share log files, so routine backend logs should not expose full local filesystem paths
    safe_details: dict[str, object] = {}
    for key, value in details.items():
        if "path" in key or key.endswith("_dir"):
            safe_details[key] = Path(str(value)).name
            continue
        safe_details[key] = value
    return safe_details


class SourceSession:
    """Track working source availability and backup fallback."""

    def __init__(
        self,
        *,
        primary_path: Path,
        backup_path: Path,
        book_number: int,
        source_filename: str,
    ) -> None:
        self._primary_path = primary_path
        self._backup_path = backup_path
        self._book_number = book_number
        self._source_filename = source_filename
        self._using_backup = False

    @property
    def active_path(self) -> Path:
        return self._backup_path if self._using_backup else self._primary_path

    def read_active_bytes(self) -> tuple[bytes, OperationEvent | None]:
        # BLOCK 1: Make sure the active source is still available before reading bytes from it
        # WHY: Availability must be checked immediately before reads so the session can switch to backup at the moment the working source disappears
        event = self.ensure_available()
        logger.info("Reading active source bytes: source_filename=%s book_number=%s using_backup=%s", self._source_filename, self._book_number, self._using_backup)
        try:
            return self.active_path.read_bytes(), event
        except FileNotFoundError as exc:
            logger.error(
                "Active source disappeared before bytes could be read: source_filename=%s book_number=%s",
                self._source_filename,
                self._book_number,
            )
            raise IngestionError(
                code="BACKUP_MISSING",
                message="Both the working source and backup are unavailable.",
                details={
                    "source_filename": self._source_filename,
                    "primary_path": str(self._primary_path),
                    "backup_path": str(self._backup_path),
                },
            ) from exc

    def ensure_available(self) -> OperationEvent | None:
        # BLOCK 1: Keep using the working source while it still exists, otherwise switch to the backup copy if recovery is possible
        # WHY: The app should continue through recoverable source loss, but only if it can do so without silently losing the original document data
        if not self._using_backup:
            if self._primary_path.exists():
                return None
            if self._backup_path.exists():
                self._using_backup = True
                logger.warning(
                    "Working source copy disappeared and ingestion switched to backup: source_filename=%s book_number=%s",
                    self._source_filename,
                    self._book_number,
                )
                return OperationEvent(
                    code="SOURCE_MISSING_SWITCHED_TO_BACKUP",
                    message="The working source copy went missing, so splitting switched to the backup copy.",
                    book_number=self._book_number,
                    source_filename=self._source_filename,
                )
            raise IngestionError(
                code="BACKUP_MISSING",
                message="Both the working source and backup are unavailable.",
                details={
                    "source_filename": self._source_filename,
                    "primary_path": str(self._primary_path),
                    "backup_path": str(self._backup_path),
                },
            )
        # BLOCK 2: Once the session is already using the backup, treat backup loss as a blocking error
        # WHY: There is no second recovery path after the backup becomes the active source, so continuing would only hide corrupted state
        if not self._backup_path.exists():
            logger.error("Backup source became unavailable during ingestion: source_filename=%s book_number=%s", self._source_filename, self._book_number)
            raise IngestionError(
                code="BACKUP_MISSING",
                message="The backup source file is unavailable.",
                details={
                    "source_filename": self._source_filename,
                    "backup_path": str(self._backup_path),
                },
            )
        return None
