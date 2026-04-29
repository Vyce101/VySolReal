---
order: 100
---

# Model Registry

Model Registry is VySol's shared provider and model catalog contract. It turns provider-owned JSON catalog files into the model ids, provider call names, surfaces, limits, capabilities, and settings that other systems can safely ask for.

## Why Model Registry Exists

VySol uses the same model identities in the frontend and backend. Without a shared registry, the app can drift into separate UI lists, Python allowlists, duplicated provider names, or mismatched limits.

The registry exists so model metadata is added once, then read by each layer in the shape that layer actually needs.

## Who This Page Is For

This page is for contributors, power users, and AI coding agents that need to add a provider, add a model, expose a model setting, debug model visibility, or check whether a backend workflow is using the same model contract as the UI metadata.

## What Model Registry Owns

Model Registry owns:

- provider ids, display names, key-file locations, and provider-owned catalog file lists
- stable model ids and provider call names
- model surfaces, currently `chat` and `embedding`
- model limit metadata, such as max input tokens, max output tokens, and embedding dimensions
- model capability metadata
- shared setting definitions and provider-specific setting definitions
- model-level setting references and overrides
- the shared catalog readers used by TypeScript and Python

## What Model Registry Does Not Own

Model Registry does not own:

- provider HTTP request payload construction
- SDK transport code
- provider key loading, scheduling, cooldowns, or quota handling
- exact token-count API calls
- embedding generation
- graph extraction prompts or response parsing
- Qdrant or Neo4j writes
- Retype navigation or documentation grouping

Metadata can say that a model exists. Runtime adapters still decide whether a workflow can actually call that provider/model combination.

## Normal Flow

The shared catalog starts with `models/catalog/providers.json`. That manifest lists each provider and the provider-owned model and settings files.

TypeScript loads the catalog through `models/shared-catalog.ts`, exposes providers through `models/providers/index.ts`, and resolves model settings through `models/registry.ts`. This path keeps the richer setting-control metadata available to UI-facing code.

Python loads the same catalog through `backend/models/registry.py`. The backend view is intentionally smaller: provider id, display name, key-file location, model id, display name, provider call name, surfaces, and numeric limits.

Runtime systems then ask focused questions:

- embedding code asks whether a model has the `embedding` surface and the required embedding limits
- graph extraction asks whether a model has the `chat` surface and a provider call name
- exact token counting resolves the stable model id into the provider call name
- provider key scheduling uses provider/model ids that match the registry contract

## Inputs

Model Registry receives provider manifests, provider setting files, model definition files, shared setting definitions, and model-level setting overrides.

## Outputs

Model Registry produces registered provider/model entries, surface-filtered model lists, resolved setting definitions, backend-readable model records, provider call names, capability metadata, and model limit metadata.

## Failure Behavior

Missing catalog files or malformed required fields should fail during catalog loading or lookup rather than creating partial model support.

If a model is present in metadata but a backend runtime adapter is missing, the runtime system must return an explicit unsupported-provider or unsupported-model error. Metadata must not be treated as proof that embeddings, token counting, graph extraction, or chat transport are wired.

## System Interactions

Model Registry interacts with:

- [Provider Key Scheduler](provider-key-scheduler.md), which uses provider/model ids when selecting eligible credentials
- [Vector Storage And Chunk Embeddings](../world-ingestion-pipeline/vector-storage-and-chunk-embeddings.md), which uses embedding surfaces, dimensions, model ids, provider ids, and max input limits
- [Knowledge Graph Extraction Pipeline](../world-ingestion-pipeline/knowledge-graph-extraction-pipeline.md), which uses chat surfaces, provider/model ids, and max input limits
- token counting adapters, which resolve stable model ids into provider call names before exact input-token checks
- retrieval, which uses the world's locked embedding profile when embedding queries
- future model/settings UI surfaces, which should render controls from resolved setting metadata rather than hardcoded duplicated setting lists

## User-Facing Behavior

Where VySol shows model choices or setting controls, those choices should come from the registry contract. A model should appear only on surfaces it declares, and settings should appear only when they have a clear control shape and purpose.

## Internal Edge Cases

- Unknown model ids return no TypeScript registered model and no Python shared model.
- A model can exist in the catalog but be rejected by a workflow if it does not declare the needed surface.
- A model can declare the correct surface but still be rejected if required limits for that workflow are missing.
- A model can reference a shared setting or provider-owned setting; missing setting definitions are skipped by the TypeScript settings resolver.
- Model-level overrides can narrow defaults or maximums from shared setting definitions.
- The current catalog contains Google AI Studio metadata, but the registry contract must stay provider-neutral.
- TypeScript and Python intentionally read different projections of the same catalog instead of exposing every UI setting to backend runtime code.

## Cross-System Edge Cases

- Frontend and backend model support must not drift into separate allowlists.
- Embedding profiles lock model metadata into world state, so changing embedding dimensions, model ids, provider ids, or max input limits can affect existing worlds.
- Exact token counting depends on registry call names and model limits, but provider-specific token-count adapters own the real counting call.
- Provider key scheduling depends on the same provider/model ids used by embeddings and graph extraction.
- Graph extraction and embeddings can support different surfaces from the same catalog, so adding a chat model does not automatically make it valid for embeddings.
- A provider/model can be valid metadata while still lacking an embedding, graph extraction, chat, or token-counting adapter.

## Implementation Landmarks

Shared catalog files live under `models/catalog`. TypeScript catalog loading and registry helpers live under `models`. Python catalog loading lives under `backend/models`. Embedding runtime projection lives under `backend/embeddings`. Provider-specific runtime helpers live under `backend/models/<provider>` packages.

## What AI/Coders Must Check Before Changing This System

Before changing Model Registry, check:

- the provider manifest and provider-owned model JSON files
- shared and provider-specific setting definitions
- TypeScript registry helpers that resolve models and settings
- Python catalog loading and backend projections
- embedding profile creation and runtime adapter availability
- graph extraction model lookup and adapter availability
- exact token-counting lookup and provider call-name resolution
- provider key eligibility by provider/model id
- docs that describe model support, provider keys, embeddings, graph extraction, or token counting

## Invariants That Must Not Be Broken

- The shared catalog is the source of truth for provider/model metadata.
- `models` remains metadata-focused; runtime request logic belongs in provider adapters.
- TypeScript and Python must derive compatible provider/model identities from the same catalog.
- Model ids must remain stable after they are used by saved worlds or provider-key rules.
- Provider call names must match the provider SDK names used by runtime adapters.
- Unsupported runtime operations must fail explicitly.
- Setting metadata must describe controls, not silently become backend request serialization logic.
