"""Integration-style tests for TXT splitter ingestion."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.ingestion.txt_splitting.models import BookManifest, SplitterConfig
from backend.ingestion.txt_splitting.service import ingest_sources, ingest_sources_into_existing_world
from backend.ingestion.txt_splitting.storage import book_directory, chunk_file_path, manifest_file_path


class IngestSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.worlds_root = self.temp_dir / "user" / "worlds"
        self.sources_dir = self.temp_dir / "fixtures"
        self.sources_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_creates_world_from_single_txt(self) -> None:
        source_path = self._write_source("book-one.txt", "Alpha beta.\n\nGamma delta? Final line.")

        result = ingest_sources(
            world_name="World One",
            source_files=[source_path],
            chunk_size=12,
            max_lookback=8,
            overlap_size=3,
            worlds_root=self.worlds_root,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.world_id, "World One")
        self.assertEqual(len(result.books), 1)

        copied_source = self.worlds_root / "World One" / "source files" / "book_01" / source_path.name
        self.assertEqual(copied_source.read_bytes(), source_path.read_bytes())

        chunk_payload = json.loads(Path(result.books[0].chunk_paths[0]).read_text(encoding="utf-8"))
        self.assertEqual(chunk_payload["world_id"], "World One")
        self.assertEqual(chunk_payload["source_filename"], "book-one.txt")

    def test_creates_one_world_with_many_books(self) -> None:
        first = self._write_source("first.txt", "First book text. Another line.")
        second = self._write_source("second.txt", "Second book text. Another line.")

        result = ingest_sources(
            world_name="Library",
            source_files=[first, second],
            chunk_size=10,
            max_lookback=5,
            overlap_size=2,
            worlds_root=self.worlds_root,
        )

        self.assertTrue(result.success)
        self.assertEqual([book.book_number for book in result.books], [1, 2])

    def test_rejects_duplicate_world_name(self) -> None:
        self._write_source("book.txt", "Hello world.")
        duplicate_dir = self.worlds_root / "Taken World"
        duplicate_dir.mkdir(parents=True, exist_ok=True)

        result = ingest_sources(
            world_name="Taken World",
            source_files=[self.sources_dir / "book.txt"],
            chunk_size=10,
            max_lookback=5,
            overlap_size=0,
            worlds_root=self.worlds_root,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.errors[0].code, "WORLD_NAME_EXISTS")

    def test_rejects_unsupported_extension(self) -> None:
        source_path = self._write_source("script.docx", "Not supported.")

        result = ingest_sources(
            world_name="Bad Input",
            source_files=[source_path],
            chunk_size=10,
            max_lookback=5,
            overlap_size=0,
            worlds_root=self.worlds_root,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.errors[0].code, "UNSUPPORTED_FILE_TYPE")

    def test_rejects_empty_or_whitespace_only_source(self) -> None:
        source_path = self._write_source("blank.txt", " \n\t ")

        result = ingest_sources(
            world_name="Blank",
            source_files=[source_path],
            chunk_size=10,
            max_lookback=5,
            overlap_size=0,
            worlds_root=self.worlds_root,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.errors[0].code, "SOURCE_EMPTY")

    def test_resumes_from_last_completed_chunk(self) -> None:
        source_path = self._write_source("resume.txt", "Alpha beta gamma delta epsilon zeta eta theta iota kappa")
        world_dir = self.worlds_root / "Resume World"
        world_dir.mkdir(parents=True, exist_ok=True)

        config = SplitterConfig(chunk_size=12, max_lookback=5, overlap_size=3)
        first_run = ingest_sources_into_existing_world(
            world_name="Resume World",
            source_files=[source_path],
            config=config,
            world_dir=world_dir,
        )
        self.assertTrue(first_run.success)

        book_dir = book_directory(world_dir, 1)
        manifest_path = manifest_file_path(book_dir)
        manifest = BookManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
        manifest.last_completed_chunk = 1
        for state in manifest.chunk_states[1:]:
            state.completed = False
        manifest_path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

        chunk_two = chunk_file_path(book_dir, 1, 2)
        if chunk_two.exists():
            chunk_two.unlink()

        resumed = ingest_sources_into_existing_world(
            world_name="Resume World",
            source_files=[source_path],
            config=config,
            world_dir=world_dir,
        )

        self.assertTrue(resumed.success)
        manifest = BookManifest.from_dict(json.loads(manifest_path.read_text(encoding="utf-8")))
        self.assertEqual(manifest.last_completed_chunk, manifest.total_chunks)
        self.assertTrue(chunk_two.exists())

    def test_switches_to_backup_when_working_source_disappears(self) -> None:
        source_path = self._write_source(
            "backup.txt",
            "Alpha beta gamma. Delta epsilon zeta. Eta theta iota.",
        )
        world_dir = self.worlds_root / "Backup World"
        world_dir.mkdir(parents=True, exist_ok=True)
        config = SplitterConfig(chunk_size=12, max_lookback=6, overlap_size=2)

        from backend.ingestion.txt_splitting import service as service_module

        original_chunk_file_path = service_module.chunk_file_path
        call_count = {"value": 0}

        def chunk_path_with_drop(book_dir: Path, book_number: int, chunk_number: int) -> Path:
            path = original_chunk_file_path(book_dir, book_number, chunk_number)
            call_count["value"] += 1
            if call_count["value"] == 2:
                working_source = world_dir / "source files" / "book_01" / source_path.name
                if working_source.exists():
                    working_source.unlink()
            return path

        service_module.chunk_file_path = chunk_path_with_drop
        try:
            result = ingest_sources_into_existing_world(
                world_name="Backup World",
                source_files=[source_path],
                config=config,
                world_dir=world_dir,
            )
        finally:
            service_module.chunk_file_path = original_chunk_file_path

        self.assertTrue(result.success)
        warning_codes = [warning.code for warning in result.warnings]
        self.assertIn("SOURCE_MISSING_SWITCHED_TO_BACKUP", warning_codes)

    def test_reports_single_chunk_and_blank_overlap(self) -> None:
        source_path = self._write_source("small.txt", "Small doc.")

        result = ingest_sources(
            world_name="Tiny",
            source_files=[source_path],
            chunk_size=100,
            max_lookback=10,
            overlap_size=10,
            worlds_root=self.worlds_root,
        )

        chunk_payload = json.loads(Path(result.books[0].chunk_paths[0]).read_text(encoding="utf-8"))
        self.assertEqual(chunk_payload["chunk_position"], "1/1")
        self.assertEqual(chunk_payload["overlap_text"], "")

    def _write_source(self, filename: str, content: str) -> Path:
        source_path = self.sources_dir / filename
        source_path.write_text(content, encoding="utf-8")
        return source_path


if __name__ == "__main__":
    unittest.main()
