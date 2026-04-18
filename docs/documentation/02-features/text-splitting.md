# Text Splitting

The text splitter is the ingestion subsystem that turns TXT, PDF, and EPUB source books into resumable text chunks for later extraction, embedding, and retrieval.

## Why This Layer Exists

VySol does not just need smaller strings. It needs chunked text that is still faithful to the source material, safe to resume after interruption, and structured enough for later knowledge graph extraction and retrieval work. That means the splitter has to do more than cut text at a fixed character count.

This layer also protects the original source material inside each world. Before splitting begins, the selected file is copied into the world's private storage and a backup copy is created alongside it. From that point onward, ingestion works from app-owned copies instead of the user's original location, regardless of whether the source started as TXT, PDF, or EPUB.

## How A Source Book Becomes Chunks

The flow starts when ingestion receives a world name, an ordered list of source files, and the chunk settings. One ingestion run creates one world, and each selected source file becomes one book inside that world.

The source file is copied into `user/worlds/<world_name>/source files/` and a second copy is stored as the backup. TXT, PDF, and EPUB all enter through the same ingestion boundary, but PDF and EPUB first pass through format-specific converters that extract their readable text into the same shared splitter pipeline.

TXT input is decoded directly into text for the active split operation. PDF input is read page by page and EPUB input is read document by document in spine order, then those extracted pieces are joined into one text string with separators between them. If conversion fails, ingestion stops with a structured converter error. If conversion succeeds but the resulting text is only whitespace or newlines, ingestion stops with the same structured empty-content error the TXT path uses.

Once the source text is ready, the splitter counts forward to the configured chunk size. If that point lands before the end of the document, the splitter looks backward within the configured lookback window for the cleanest available separator. The search order is paragraph break, then line break, then sentence punctuation, then blank space, and only then a hard cut at the original limit. When punctuation wins, the punctuation stays at the end of the chunk that just closed.

After the chunk boundaries are known, each chunk gets a fixed overlap slice pulled from the text immediately before that chunk. The first chunk still includes the overlap field, but it is blank because there is no earlier text to borrow from.

Each completed chunk is saved as its own JSON file using a stable generated name such as `book_01_chunk_0004.json`. Alongside those chunk files, VySol stores a per-book progress manifest that records the total chunk count, the last completed chunk, and the completion state for every chunk in that book.

```json
{
  "world_id": "My World",
  "source_filename": "chapter-one.txt",
  "book_number": 1,
  "chunk_number": 4,
  "chunk_position": "4/37",
  "overlap_text": "the last part of the previous chunk",
  "chunk_text": "the current chunk text"
}
```

## Why It Is Built This Way

The most important design choice is that chunk files are written one at a time and the progress manifest is updated only after a chunk has been fully saved. That makes resume trustworthy. If the app stops halfway through ingestion, VySol can look at the manifest, confirm which chunk files actually exist, and continue from the first incomplete chunk instead of starting the whole book again or trusting half-written output.

The second major choice is preserving exact source copies instead of rewriting them in place. UTF-8 conversion is only used for the active splitting operation when needed. The stored source copies remain byte-for-byte preserved so the world keeps an auditable original and a recovery backup.

The third major choice is using generated chunk filenames rather than building filenames from the original source name. Original filenames are still preserved in the source-copy storage and in metadata, but generated names make sorting, resuming, and chunk lookup more stable, especially when source filenames contain unusual characters.

The fallback behavior is also deliberate. If the working source copy disappears during splitting, ingestion switches to the backup copy and records a warning. If both copies are unavailable, ingestion stops with a blocking error. That balance keeps recovery possible without silently pretending corrupted or missing source material is safe to continue from.
