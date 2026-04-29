---
order: 300
---

# Text Splitting

Text Splitting is the source-ingestion subsystem that converts TXT, PDF, and EPUB books into durable chunk records. It is the first content-shaping step in world ingestion: later embeddings, graph extraction, graph manifestation, and retrieval all depend on the chunk files and progress metadata it creates.

## Why Text Splitting Exists

VySol needs long source books broken into smaller records without losing provenance, resumability, or rebuild safety. Text Splitting exists to preserve enough reading structure for later AI systems while giving the backend stable files it can resume from after interruption.

It also protects ingestion from the user's original file location. After a source is selected, this system works from world-owned source copies so later user-side file moves or deletes do not directly corrupt an active run.

## Who This Page Is For

This page is for technical users, power users, and AI coding agents that need to understand how source files become trusted world chunks before editing ingestion, converters, chunk boundaries, resume behavior, embeddings, graph extraction, or retrieval.

## What Text Splitting Owns

Text Splitting owns:

- validating splitter settings before chunk work begins
- selecting the converter for TXT, PDF, and EPUB inputs
- decoding or extracting plain text from supported source formats
- rejecting unsupported, missing, undecodable, or empty sources with structured ingestion errors
- choosing chunk boundaries with the configured chunk size and lookback window
- calculating overlap text for each chunk
- writing chunk JSON files for each book
- writing and repairing per-book progress manifests
- preserving working source copies and backup source copies inside the world
- falling back from the working source copy to the backup copy when that recovery path is safe

## What Text Splitting Does Not Own

Text Splitting does not own:

- world-facing navigation or Retype page grouping
- embedding provider calls
- embedding profile definitions
- provider key scheduling
- Qdrant vector storage
- graph extraction prompts
- graph extraction model output parsing
- graph manifestation writes
- retrieval ranking
- chat context assembly
- user-interface decisions about how to display ingestion errors or warnings

## Normal Flow

For a new world, ingestion first validates the requested splitter settings, requires a locked embedding profile, checks that at least one eligible embedding credential exists, and rejects duplicate world names before creating the world directory.

For an existing world, ingestion loads the world metadata, keeps the locked splitter and embedding profile stable, starts or resumes the active ingestion run, and plans whether each requested source resumes an existing stored book or appends as a new book.

Each source is copied into a world-owned working source location and a backup source location. The converter is chosen by file extension. TXT is decoded through an ordered encoding fallback chain, PDF text is extracted page by page, and EPUB text is extracted from readable spine entries.

After conversion, whitespace-only text is rejected. Usable text is split with the configured chunk size. When a chunk boundary lands before the end of the document, the splitter searches backward inside the lookback window for the cleanest available break: paragraph break, line break, sentence punctuation, whitespace, then hard cut.

After all boundaries are known, each chunk receives overlap text from the text immediately before that chunk. The first chunk keeps the same field shape, but its overlap is empty.

Each chunk is written as its own JSON file. The progress manifest is advanced only after the corresponding chunk file is safely written. Once chunks are complete, the broader ingestion flow can embed chunks, extract graph candidates, and manifest the graph if those downstream stages are eligible.

## Inputs

Text Splitting receives:

- world name, world UUID, and book number
- source file paths or stored source paths
- splitter settings for chunk size, maximum lookback, and overlap size
- supported source formats identified by file extension
- world metadata, including locked splitter and embedding settings
- existing per-book progress manifests when resuming
- cancellation and downstream configuration passed through the wider ingestion flow

## Outputs

Text Splitting produces:

- world-owned source copies and backup source copies
- per-book chunk JSON files
- chunk metadata for world identity, source filename, book number, chunk number, and chunk position
- `chunk_text`, which is the text downstream embeddings should use
- `overlap_text`, which gives neighboring context to systems that need it
- per-book progress manifests
- structured ingestion errors
- recoverable warning events, such as switching from the working source copy to the backup copy

## Saved State And Resume Behavior

The per-book progress manifest records total chunks, completed chunks, splitter settings, source identity, and warnings for that book. Resume trusts only the completed chunk states that also have real chunk files on disk. If the manifest claims progress that the filesystem cannot prove, resume repairs the in-memory progress back to the last contiguous completed chunk.

Chunk files and manifests are written atomically. A chunk is written before the manifest marks it complete, so a crash or write failure should not leave the system believing a missing chunk is safe.

Full-world re-ingest is the separate rebuild path. It reloads the world's stored source copies, clears derived outputs for the selected books, permits replacing locked splitter or embedding settings, and rebuilds chunks and downstream state from the preserved sources.

## Retry / Pause / Abort Behavior

Text Splitting itself does not retry provider calls, but it participates in a resumable ingestion run. It resumes chunk writing from the first untrusted chunk and leaves the active world run unfinished when downstream embedding, graph extraction, or graph manifestation remains partial.

Cancellation is passed to the embedding stage, not to the pure chunk splitter. If cancellation or a downstream partial result prevents the whole pipeline from completing, the world run remains resumable instead of being marked complete.

## Failure Behavior

Text Splitting returns structured ingestion errors instead of raw exceptions at the service boundary. It fails before chunking when splitter settings are invalid, a selected source path is missing, the source type is unsupported, a source cannot be decoded or converted, or converted text contains only whitespace.

It also fails when resume state no longer matches the current source or splitter settings. A normal existing-world ingest cannot silently replace a stored book source or change the locked splitter profile; those changes require full-world re-ingest.

If the working source copy disappears but the backup copy is still present, the system switches to the backup and records a warning. If neither trusted copy is available, it stops instead of inventing or partially trusting source content.

Filesystem write failures are translated into stable ingestion errors. Disk-full failures are called out separately from generic file-write failures so callers can distinguish capacity problems from other write errors.

## System Interactions

Text Splitting interacts with:

- [World Storage](world-storage.md), which stores source copies, world metadata, chunk files, and progress manifests
- [Vector Storage And Chunk Embeddings](vector-storage-and-chunk-embeddings.md), which embeds the produced `chunk_text`
- [Knowledge Graph Extraction Pipeline](knowledge-graph-extraction-pipeline.md), which reads both `chunk_text` and `overlap_text`
- [Graph Manifestation](graph-manifestation.md), which depends on graph extraction output tied back to the chunk run
- [Chunk Retrieval](../retrieval/chunk-retrieval.md), which later reads trusted chunk text from World Storage
- [Provider Key Scheduler](../shared-backend-systems/provider-key-scheduler.md), which must have eligible credentials before automatic embeddings and graph work can proceed
- [Model Registry](../shared-backend-systems/model-registry.md), which defines the embedding model metadata used when locking a world profile

## Internal Edge Cases

- Chunk size must be greater than zero; lookback and overlap must not be negative.
- Very small sources produce one chunk with blank overlap.
- A zero lookback window disables separator search and uses the hard chunk boundary.
- If no paragraph, line, punctuation, or space separator exists inside the allowed lookback window, the splitter hard cuts while still making forward progress.
- Sentence punctuation selected as a boundary stays with the chunk that just closed.
- TXT decoding tries a small ordered encoding fallback chain instead of assuming every text file is UTF-8.
- PDF conversion can fail before chunking if the file cannot be opened or read as a PDF.
- EPUB conversion follows readable spine items and can fail before chunking if the archive cannot be read as an EPUB.
- Whitespace-only decoded or converted content is treated as empty source content.
- A manifest whose completed chunk count is ahead of the actual chunk files is repaired back to the last contiguous chunk file that exists.
- Existing progress metadata with different splitter settings or a different total chunk count blocks resume as a state conflict.
- Stored source lookup is ambiguous if a book slot has multiple working copies, multiple backup copies, or mismatched working and backup filenames.

## Cross-System Edge Cases

- New-world ingestion requires a locked embedding profile and at least one eligible embedding credential before creating a world folder.
- Existing-world ingestion must keep the locked splitter settings and embedding profile stable; changing either requires full-world re-ingest.
- A selected source with the same filename as a stored book resumes only when the stored source bytes match. A same-name but different-content source requires full-world re-ingest.
- Appending books must allocate the next unused book number across stored sources, backups, and derived book folders so a partial prior run does not get overwritten.
- Graph extraction default edits are rejected while the world run is active, because in-flight graph work must not silently switch settings.
- If embeddings finish but graph extraction or graph manifestation is partial, the world run remains paused or active for resume instead of being marked complete.
- Full-world re-ingest clears derived chunk, vector, node-vector, and graph outputs while preserving stored source copies.
- Full-world re-ingest may still rebuild local chunk and vector state if old Neo4j rows cannot be cleaned up because the graph store is unavailable.
- `chunk_text` is used for document embeddings; `overlap_text` can support graph extraction context but must not be embedded as part of the chunk vector.

## Implementation Landmarks

Text splitting behavior lives under `backend/ingestion/text_sources`.

- `chunking.py` contains boundary and overlap logic.
- `converters.py` contains TXT, PDF, and EPUB conversion.
- `storage.py` contains source-copy, backup, chunk-file, manifest, and atomic-write helpers.
- `service.py` orchestrates world ingestion, resume, full-world re-ingest, embeddings, graph extraction, and graph manifestation handoff.
- `models.py` and `errors.py` define the structured ingestion contract.

## What AI/Coders Must Check Before Changing This System

Before changing Text Splitting, check:

- splitter setting validation
- separator priority and punctuation placement
- single-chunk and hard-cut behavior
- TXT decoding and PDF/EPUB conversion behavior
- empty-source rejection
- source-copy and backup recovery behavior
- progress manifest creation, repair, and state-conflict checks
- existing-world append versus resume planning
- full-world re-ingest cleanup and source preservation
- embedding, graph extraction, graph manifestation, and retrieval assumptions about chunk metadata

## Invariants That Must Not Be Broken

- Chunk files must be written before the manifest marks them complete.
- Resume must trust both manifest state and actual chunk files, not either one alone.
- Preserved source copies must not be rewritten just because conversion used a different text encoding.
- Stored source replacement must go through full-world re-ingest, not normal append or resume.
- Chunk metadata must remain stable enough for Qdrant payloads, graph manifests, and retrieval provenance.
- `chunk_text` is the embeddable body; `overlap_text` is contextual support.
- Source-copy fallback must never silently continue when no trusted source copy exists.
- Splitter changes that alter chunk layout must be treated as downstream state invalidation risks.
- The active ingestion run must not be marked complete while embeddings, graph extraction, or graph manifestation are still partial.
