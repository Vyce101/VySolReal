## Unreleased

### Added

- GitHub Pages workflow for automatic Retype publishing.
- Dedicated Retype setup under `retype/` with pinned `retypeapp@4.5.3`.
- Dedicated Retype content root at `docs/documentation/`.
- Backend TXT ingestion and text splitting pipeline.
- Resumable chunk persistence with backup fallback and rotating backend logs.
- Internal documentation pages for Text Splitting and the current ingestion architecture.

### Changed

- Retype now publishes only `docs/documentation/`.
- Model setting definitions now resolve `maxInputTokens` from per-model limits and remove unsupported Google-model controls from the current registry entries.

### Removed

- Legacy docs files `docs/FEATURES.md` and `docs/ARCHITECTURE.md`.
