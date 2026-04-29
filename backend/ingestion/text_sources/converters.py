"""Source conversion hooks for TXT splitter ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import ebooklib
import pymupdf
from ebooklib import epub

from backend.logger import get_logger

from .errors import IngestionError

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class ConvertedDocument:
    """Decoded document ready for chunking."""

    text: str
    encoding: str
    source_type: str


class Converter:
    """Abstract converter contract."""

    def convert(self, source_path: Path) -> ConvertedDocument:
        raise NotImplementedError


class TxtConverter(Converter):
    """Decoder for TXT sources."""

    def convert(self, source_path: Path) -> ConvertedDocument:
        logger.info("Converting TXT source to text for splitting: source_path=%s", source_path)
        raw_bytes = source_path.read_bytes()
        text, encoding = decode_text_bytes(raw_bytes)
        return ConvertedDocument(text=text, encoding=encoding, source_type="txt")


class PdfConverter(Converter):
    """Text extractor for PDF sources."""

    def convert(self, source_path: Path) -> ConvertedDocument:
        logger.info("Converting PDF source to text for splitting: source_path=%s", source_path)
        try:
            # BLOCK 1: Open the PDF and extract plain text from every page in document order
            # VARS: page_texts = extracted page text segments that will later be joined into one splitter input
            # WHY: The splitter expects one contiguous text string, so page-level extraction has to be flattened before the shared chunking pipeline can run
            with pymupdf.open(source_path) as document:
                page_texts = [page.get_text("text") for page in document]
        except Exception as exc:
            logger.error("PDF conversion failed: source_path=%s error=%s", source_path, exc)
            raise IngestionError(
                code="PDF_CONVERSION_FAILED",
                message="The PDF file could not be converted into text.",
                details={"source_path": str(source_path), "conversion_error": str(exc)},
            ) from exc

        # BLOCK 2: Preserve page boundaries with blank-line separators when joining the final text
        # WHY: Without separators, adjacent pages can merge into one artificial sentence boundary that changes how later chunk splitting behaves
        joined_text = "\n\n".join(page_texts)
        logger.info("PDF conversion completed successfully: source_path=%s page_count=%s", source_path, len(page_texts))
        return ConvertedDocument(text=joined_text, encoding="utf-8", source_type="pdf")


class EpubConverter(Converter):
    """Text extractor for EPUB sources."""

    def convert(self, source_path: Path) -> ConvertedDocument:
        logger.info("Converting EPUB source to text for splitting: source_path=%s", source_path)
        try:
            # BLOCK 1: Read the EPUB and follow the spine so text comes out in reading order instead of archive-file order
            # VARS: spine_texts = extracted text segments for each readable document item in the EPUB spine
            # WHY: EPUBs are containers with many internal files, so using the spine is the safest way to keep the text in book order
            book = epub.read_epub(str(source_path))
            spine_texts = self._extract_spine_text(book)
        except IngestionError:
            raise
        except Exception as exc:
            logger.error("EPUB conversion failed: source_path=%s error=%s", source_path, exc)
            raise IngestionError(
                code="EPUB_CONVERSION_FAILED",
                message="The EPUB file could not be converted into text.",
                details={"source_path": str(source_path), "conversion_error": str(exc)},
            ) from exc

        # BLOCK 2: Preserve spine-item boundaries with blank-line separators when joining the final text
        # WHY: Joining items without separators would blur chapter and document boundaries before chunking starts
        joined_text = "\n\n".join(spine_texts)
        logger.info("EPUB conversion completed successfully: source_path=%s item_count=%s", source_path, len(spine_texts))
        return ConvertedDocument(text=joined_text, encoding="utf-8", source_type="epub")

    def _extract_spine_text(self, book: epub.EpubBook) -> list[str]:
        # BLOCK 1: Walk every spine entry, resolve it back to the book item, and convert readable XHTML into plain text
        # VARS: item_identifier = spine item id from the EPUB manifest, item = resolved EPUB document item, extracted_text = plain-text content for one readable spine item
        # WHY: Some spine entries refer to non-document items, so each entry has to be resolved and filtered before its content is trusted as text
        spine_texts: list[str] = []
        for spine_entry in book.spine:
            item_identifier = spine_entry[0] if isinstance(spine_entry, tuple) else spine_entry
            item = book.get_item_with_id(item_identifier)
            if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
                continue
            extracted_text = _extract_html_text(item.get_content().decode("utf-8", errors="ignore"))
            spine_texts.append(extracted_text)
        return spine_texts


def get_converter(source_path: Path) -> Converter:
    # BLOCK 1: Choose the converter based on the file extension so the rest of ingestion can stay format-agnostic
    # WHY: Routing by file type here keeps format-specific behavior out of the service layer and makes future PDF/EPUB support plug into the same pipeline
    suffix = source_path.suffix.lower()
    logger.info("Selecting converter for source file: source_path=%s file_type=%s", source_path, suffix or "<none>")
    if suffix == ".txt":
        return TxtConverter()
    if suffix == ".pdf":
        return PdfConverter()
    if suffix == ".epub":
        return EpubConverter()
    raise IngestionError(
        code="UNSUPPORTED_FILE_TYPE",
        message="Only .txt, .pdf, and .epub files are supported.",
        details={"source_path": str(source_path)},
    )


def decode_text_bytes(raw_bytes: bytes) -> tuple[str, str]:
    """Decode bytes using a small ordered fallback chain."""
    # BLOCK 1: Try a short ordered list of common encodings until the source can be read as text
    # VARS: attempted_encodings = decoder order from most expected to most permissive fallback
    # WHY: TXT inputs can come from different tools and regions, so a single hardcoded decoder would reject otherwise valid user files
    attempted_encodings = (
        "utf-8",
        "utf-8-sig",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        "cp1252",
        "latin-1",
    )
    for encoding in attempted_encodings:
        try:
            logger.info("Decoded source text successfully: encoding=%s", encoding)
            return raw_bytes.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # BLOCK 2: Fail with a structured error if no text decoder can safely read the file
    # WHY: Letting undecodable bytes pass through would break chunking and produce misleading downstream errors later in the pipeline
    logger.error("Source text decoding failed after trying supported encodings.")
    raise IngestionError(
        code="TEXT_DECODE_FAILED",
        message="The source file could not be decoded into text.",
    )


def has_usable_text(text: str) -> bool:
    """Return True when content contains more than whitespace."""
    # BLOCK 1: Treat whitespace-only content as empty so blank files are rejected before chunking
    # WHY: The feature contract says spaces and newlines alone do not count as real source content, even if the file technically has bytes in it
    return bool(text.strip())


class _HtmlTextExtractor(HTMLParser):
    """Minimal EPUB HTML-to-text parser."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _extract_html_text(html: str) -> str:
    # BLOCK 1: Strip EPUB XHTML tags into plain text while preserving simple block boundaries with newlines
    # WHY: EPUB content is stored as HTML documents, so a lightweight parser is needed to feed only text into the shared splitter without adding a new dependency
    parser = _HtmlTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()
