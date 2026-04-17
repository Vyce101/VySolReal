"""Source conversion hooks for TXT splitter ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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


class PlaceholderConverter(Converter):
    """Accepted file types that will be implemented later."""

    def __init__(self, source_type: str) -> None:
        self._source_type = source_type

    def convert(self, source_path: Path) -> ConvertedDocument:
        logger.error(
            "Converter requested for unsupported implementation state: source_type=%s source_path=%s",
            self._source_type,
            source_path,
        )
        raise IngestionError(
            code="CONVERTER_NOT_IMPLEMENTED",
            message=f"{self._source_type.upper()} ingestion is not implemented yet.",
            details={"source_path": str(source_path)},
        )


def get_converter(source_path: Path) -> Converter:
    # BLOCK 1: Choose the converter based on the file extension so the rest of ingestion can stay format-agnostic
    # WHY: Routing by file type here keeps format-specific behavior out of the service layer and makes future PDF/EPUB support plug into the same pipeline
    suffix = source_path.suffix.lower()
    logger.info("Selecting converter for source file: source_path=%s file_type=%s", source_path, suffix or "<none>")
    if suffix == ".txt":
        return TxtConverter()
    if suffix == ".pdf":
        return PlaceholderConverter("pdf")
    if suffix == ".epub":
        return PlaceholderConverter("epub")
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
