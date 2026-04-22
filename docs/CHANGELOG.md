## Unreleased

### Added

- GitHub Pages workflow for automatic Retype publishing.
- Dedicated Retype setup under `retype/` with pinned `retypeapp@4.5.3`.
- Dedicated Retype content root at `docs/documentation/`.
- Backend TXT ingestion and text splitting pipeline.
- PDF and EPUB converters for the shared text-splitting ingestion pipeline.
- Resumable chunk persistence with backup fallback and rotating backend logs.
- Internal documentation pages for Text Splitting and the current ingestion architecture.
- Automatic chunk embedding during ingestion with locked world embedding profiles.
- Shared local Qdrant vector storage with per-book embedding manifests and resumable vector reconciliation.
- Internal documentation pages for World Storage, Vector Storage And Chunk Embeddings, and Qdrant Vector Store.

### Changed

- Retype now publishes only `docs/documentation/`.
- Model setting definitions now resolve `maxInputTokens` from per-model limits and remove unsupported Google-model controls from the current registry entries.
- New-world ingestion now requires an explicit embedding model and eligible provider keys before world creation begins.
- The architecture page now reflects the chunk-to-vector pipeline with container-level storage boundaries.

### Removed

- Legacy docs files `docs/FEATURES.md` and `docs/ARCHITECTURE.md`.
