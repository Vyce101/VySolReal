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
- Shared provider key scheduler with optional `enabled` flags and a dedicated concept page for future UI-controlled key disabling.
- Internal documentation pages for World Storage, Vector Storage And Chunk Embeddings, and Qdrant Vector Store.
- Backend chunk retrieval with Qdrant similarity search, query embeddings, cleaned model context, and stale or missing chunk repair warnings.
- Retrieval documentation under Features and a single Architecture system flow page.
- Resumable knowledge graph extraction and graph manifestation backend modules with saved manifests, provider calls, parser/prompt handling, and Neo4j graph writes.
- Graph node vectors in Qdrant, portable local Neo4j bootstrap support, and pinned `neo4j==6.1.0` backend dependency.
- Focused backend tests for graph extraction, graph manifestation, node vectors, and the expanded ingestion run-boundary cases.
- Local Worlds Hub app shell with a FastAPI API, React/Vite frontend, app-owned Hub assets, and local world asset serving.

### Changed

- The Worlds Hub header branding and world-card hover frames now use the updated polished UI treatment from the current local frontend pass.
- Retype now publishes only `docs/documentation/`.
- Provider key scheduling now uses model-aware quota buckets, reserves requests before dispatch, and ignores deprecated user-entered key limits.
- Model metadata now loads from a shared JSON catalog used by both the TypeScript registry and backend embedding runtime.
- Model registry metadata now uses provider-owned settings, chat/embedding-only surfaces, generic model-limit binding, and shared Google AI Studio quota error parsing.
- Model setting definitions now resolve `maxInputTokens` from per-model limits and remove unsupported Google-model controls from the current registry entries.
- New-world ingestion now requires an explicit embedding model and eligible provider keys before world creation begins.
- Qdrant chunk vectors now use embedding-profile-specific collections so worlds with different embedding dimensions can coexist in one local vector store.
- The architecture page now reflects the chunk-to-vector pipeline with container-level storage boundaries.
- Google AI Studio embedding requests now share provider-level request, logging, and error-normalization code across chunk and query embeddings.
- Google AI Studio max-input enforcement now uses exact provider token counting and blocks when counting fails instead of using the old local estimate.
- World ingestion now keeps a world-level splitter lock, reuses paused ingestion runs, carries `ingestion_run_id` through embedding manifests and vector payloads, and appends new books without reusing old book slots.
- Feature documentation is now grouped into `World Ingestion Pipeline`, `Storage Layers`, `Shared Backend Systems`, and `Retrieval`, with new pages for the graph pipeline and manifestation flow.
- The system flow diagram now shows the current graph extraction and manifestation path, shared provider-key scheduling, Neo4j storage, and a separate legend above the main diagram.
- `run.bat` now starts the backend and frontend together, checks Python and Node.js versions, installs pinned local dependencies, and prints clearer setup guidance for developers.
- Quickstart and preview imagery now reflect the Worlds Hub startup flow.

### Fixed

- Graph manifestation now retries Neo4j edge batch failures, rebuilds surviving chunk candidates after chunk-level cleanup, and logs boundary-stage progress without raw model output.

### Removed

- Legacy docs files `docs/FEATURES.md` and `docs/ARCHITECTURE.md`.
- Google AI Studio embedding title metadata from the active embedding model settings and backend request path.
