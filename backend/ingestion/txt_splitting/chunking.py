"""Character-based recursive splitting logic."""

from __future__ import annotations

from .models import ChunkDraft, SplitterConfig

_PUNCTUATION = ("!", ".", "?")


def split_text(text: str, config: SplitterConfig) -> list[ChunkDraft]:
    """Split text into chunks while preserving original content."""
    # BLOCK 1: If the full text already fits inside one chunk, return it as-is without splitting
    # WHY: This avoids creating fake boundaries and keeps the first chunk's overlap blank, which matches the ingestion contract for small documents
    if len(text) <= config.chunk_size:
        return [
            ChunkDraft(
                chunk_number=1,
                total_chunks=1,
                overlap_text="",
                chunk_text=text,
            )
        ]

    # BLOCK 2: Walk through the text and figure out the start and end positions for every chunk before building chunk objects
    # VARS: ranges = character start/end positions for each future chunk, start_index = where the current chunk begins, proposed_end = hard size limit for this chunk, split_end = cleaned-up split point after looking for separators
    # WHY: Ranges are collected first so total chunk count is known before any ChunkDraft is created; building drafts too early would require backtracking later to patch total_chunks
    ranges: list[tuple[int, int]] = []
    start_index = 0

    while start_index < len(text):
        proposed_end = min(start_index + config.chunk_size, len(text))
        if proposed_end == len(text):
            split_end = len(text)
        else:
            split_end = _find_split_end(
                text=text,
                start_index=start_index,
                proposed_end=proposed_end,
                max_lookback=config.max_lookback,
            )
        if split_end <= start_index:
            split_end = proposed_end
        ranges.append((start_index, split_end))
        start_index = split_end

    # BLOCK 3: Build the final chunk objects and attach the fixed overlap text for each one
    # VARS: overlap_start = earliest character position allowed for this chunk's overlap
    # WHY: Overlap is derived from finalized ranges instead of during the first pass so it always lines up with the real split boundaries
    total_chunks = len(ranges)
    drafts: list[ChunkDraft] = []
    for chunk_number, (start_index, end_index) in enumerate(ranges, start=1):
        overlap_start = max(0, start_index - config.overlap_size)
        drafts.append(
            ChunkDraft(
                chunk_number=chunk_number,
                total_chunks=total_chunks,
                overlap_text=text[overlap_start:start_index],
                chunk_text=text[start_index:end_index],
            )
        )
    return drafts


def _find_split_end(
    *,
    text: str,
    start_index: int,
    proposed_end: int,
    max_lookback: int,
) -> int:
    # BLOCK 1: If lookback is disabled, keep the hard cut point exactly where the chunk size limit landed
    # WHY: When there is no allowed lookback window, searching for separators would violate the caller's configured boundary rules
    if max_lookback == 0:
        return proposed_end

    # BLOCK 2: Limit the search area to only the allowed backward window inside the current chunk
    # VARS: lookback_start = earliest character position allowed for separator searching, search_window = text slice that may contain a cleaner split point
    # WHY: Restricting the search window prevents the splitter from drifting too far back and producing chunks much smaller than the configured size
    lookback_start = max(start_index, proposed_end - max_lookback)
    search_window = text[lookback_start:proposed_end]

    # BLOCK 3: Prefer paragraph and line boundaries first so the chunk stays as readable as possible
    # VARS: relative_index = separator position inside the limited search window
    # WHY: Newline boundaries preserve document structure better than punctuation or spaces, so they need first priority in the recursive separator order
    for separator in ("\n\n", "\n"):
        relative_index = search_window.rfind(separator)
        if relative_index != -1:
            return lookback_start + relative_index + len(separator)

    # BLOCK 4: If no newline boundary exists, try to end at sentence punctuation while keeping the punctuation in the closing chunk
    # VARS: punctuation_index = last punctuation mark found inside the lookback window
    # WHY: The chunk contract requires punctuation to stay with the sentence that just ended; splitting before punctuation would create awkward next-chunk openings
    punctuation_index = max(search_window.rfind(mark) for mark in _PUNCTUATION)
    if punctuation_index != -1:
        return lookback_start + punctuation_index + 1

    # BLOCK 5: If no stronger separator exists, fall back to the last blank space before using a hard cut
    # VARS: space_index = last blank space found inside the lookback window
    # WHY: Cutting at a space is less disruptive than splitting through the middle of a word, so it is the final soft-boundary fallback
    space_index = search_window.rfind(" ")
    if space_index != -1:
        return lookback_start + space_index + 1

    # BLOCK 6: If no separator exists in the allowed window, keep the original hard cut point
    # WHY: This guarantees forward progress for dense text like long identifiers or compressed prose that has no clean break characters nearby
    return proposed_end
