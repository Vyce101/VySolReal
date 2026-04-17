"""Unit tests for chunking behavior."""

from __future__ import annotations

import unittest

from backend.ingestion.txt_splitting.chunking import split_text
from backend.ingestion.txt_splitting.models import SplitterConfig


class SplitTextTests(unittest.TestCase):
    def test_prefers_double_newline_then_newline_then_punctuation_then_space(self) -> None:
        text = "Alpha beta.\n\nGamma delta.\nTheta iota? Kappa lambda mu"
        config = SplitterConfig(chunk_size=22, max_lookback=12, overlap_size=4)

        chunks = split_text(text, config)

        self.assertEqual(chunks[0].chunk_text, "Alpha beta.\n\n")
        self.assertTrue(chunks[1].chunk_text.endswith("\n"))

    def test_keeps_punctuation_at_end_of_chunk(self) -> None:
        text = "No way! You did that?! Absolutely."
        config = SplitterConfig(chunk_size=12, max_lookback=6, overlap_size=0)

        chunks = split_text(text, config)

        self.assertEqual(chunks[0].chunk_text, "No way!")
        self.assertTrue(chunks[1].chunk_text.startswith(" "))

    def test_returns_single_chunk_when_text_is_smaller_than_chunk_size(self) -> None:
        text = "Tiny text."
        config = SplitterConfig(chunk_size=500, max_lookback=25, overlap_size=50)

        chunks = split_text(text, config)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].overlap_text, "")
        self.assertEqual(chunks[0].chunk_text, text)

    def test_uses_hard_cut_when_no_separator_is_found(self) -> None:
        text = "abcdefghijklmno"
        config = SplitterConfig(chunk_size=5, max_lookback=2, overlap_size=0)

        chunks = split_text(text, config)

        self.assertEqual([chunk.chunk_text for chunk in chunks], ["abcde", "fghij", "klmno"])


if __name__ == "__main__":
    unittest.main()
