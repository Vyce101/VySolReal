---
order: 100
---

# Provider Key Scheduler

Provider Key Scheduler is the shared backend system that decides which configured provider credential can be used for a provider/model request at a given moment.

It is a credential-availability contract, not a provider client. It loads eligible keys, filters unusable keys, spreads requests across the available pool, reserves in-flight quota scopes, records provider rate-limit feedback, and persists cooldown metadata that later workflows must respect.

## Why Provider Key Scheduler Exists

VySol has multiple backend AI workflows that can call providers during ingestion and graph work. Chunk embeddings, graph extraction, and node embedding during graph manifestation all need the same basic question answered: which credential is currently safe to use for this provider model?

The scheduler exists so those workflows do not each invent their own disabled-key handling, model allow-list filtering, quota-scope grouping, cooldown behavior, or round-robin selection. Provider failures can be interpreted once and shared across workflows instead of being trapped inside one pipeline.

## Who This Page Is For

This page is for contributors, power users, and AI coding agents that need to understand or change provider key loading, provider request scheduling, cooldown state, quota-scope behavior, embedding dispatch, graph extraction dispatch, or graph manifestation node embedding.

## What Provider Key Scheduler Owns

Provider Key Scheduler owns:

- loading provider credential files from the app key store
- validating credential metadata before dispatch
- filtering disabled keys
- filtering credentials by exact allowed model ids
- grouping credentials by provider quota scope
- choosing eligible credentials through shared round-robin selection
- marking a quota scope busy while a provider request is in flight
- reserving known request/token quota windows when provider-owned quota metadata is available
- releasing reservations after non-quota failures, cancellations, or abandoned work
- recording provider rate-limit feedback as model-level or shared cooldown state
- persisting cooldown metadata for later scheduler instances
- keeping per-run request-per-day blocks in scheduler runtime state

## What Provider Key Scheduler Does Not Own

Provider Key Scheduler does not own:

- provider request payloads
- prompt construction
- embedding text construction
- model catalog definitions
- provider SDK clients
- workflow-specific manifests or retry counters
- chunk, graph, or node persistence
- user-facing key management UI
- raw provider response storage

## Normal Flow

A workflow creates or reuses a scheduler for one provider id and one model id. The scheduler loads provider credentials, ignores disabled credentials, rejects malformed enabled credentials, and keeps only credentials that support the requested model.

When the workflow is ready to call a provider, it asks the scheduler to select a credential with a token estimate. The scheduler rotates through the eligible pool with a shared round-robin cursor, skips quota scopes that already have an unconfirmed in-flight request, skips credentials affected by active cooldowns, and checks any known provider-owned quota window before returning a credential.

Before the workflow dispatches the provider request, the scheduler records a reservation for the model quota bucket and marks the credential quota scope busy. The workflow then sends the request through its provider client.

If the provider request succeeds, the workflow confirms success and the scheduler releases the busy marker while keeping the request as quota-window usage. If the request fails without a quota signal, the workflow releases the reservation so the credential does not look artificially exhausted. If the request fails with a provider rate-limit signal, the workflow reports that signal back and the scheduler records the affected cooldown or runtime block.

## Inputs

Provider Key Scheduler receives provider ids, model ids, credential JSON files, enabled/disabled flags, allowed model lists, optional provider project ids, token estimates, provider-owned model quota metadata when supplied by a caller, provider rate-limit metadata, retry-after values, and workflow calls that confirm, release, or abandon reservations.

## Outputs

Provider Key Scheduler produces selected credential objects, `None` when no credential is currently usable, structured invalid-key errors, in-flight reservation state, persisted cooldown metadata, runtime request-per-day blocks, and short waits for callers that choose to sleep until a future credential may become available.

## Saved State And Resume Behavior

Cooldown state is persisted in the provider key runtime state file so time-based cooldowns can survive app restart. The persisted state records the quota bucket, provider, credential name, optional project id, optional model id, limit scope, last limit type, cooldown timestamp, and last error message.

Request-per-day exhaustion currently marks the affected scheduler bucket as blocked for the live run. That block is not a durable day-level lock after a new scheduler runtime starts.

Workflow manifests own workflow progress. The scheduler only owns provider credential availability state.

## Retry, Pause, And Abort Behavior

The scheduler does not retry chunks, extraction passes, or node embeddings by itself. It only returns usable credentials, tracks in-flight reservations, records provider quota feedback, and exposes whether a future credential may become available.

If one credential or quota bucket is cooling down, the workflow can ask again and may receive another eligible credential. If no credential is currently usable but a cooldown or quota window may clear soon, the workflow can call the scheduler's wait helper. If no future availability exists, the workflow decides whether to pause, leave work pending, or fail according to its own manifest contract.

When a workflow pauses or cancels after a credential was selected, it must abandon the in-flight reservation so later work is not blocked by a request whose response will be ignored.

## Failure Behavior

Invalid enabled credential files fail before dispatch with a structured provider-key configuration error. A disabled credential is skipped before API key validation, which lets an intentionally disabled incomplete key remain inert.

Unknown Google AI Studio HTTP 429 responses become model-level rate-limit failures. When the provider exposes `Retry-After`, the scheduler uses that value; otherwise it falls back to a short cooldown. Messages that explicitly mention project, API key, or credential quota widen the cooldown to the shared provider quota bucket.

Non-quota provider failures release the reservation instead of creating a cooldown. Request-per-day failures block the affected model bucket for the live run rather than persisting a timestamped cooldown.

## System Interactions

Provider Key Scheduler interacts with:

- [Vector Storage And Chunk Embeddings](../world-ingestion-pipeline/vector-storage-and-chunk-embeddings.md), which schedules embedding requests
- [Knowledge Graph Extraction Pipeline](../world-ingestion-pipeline/knowledge-graph-extraction-pipeline.md), which schedules extraction requests
- [Graph Manifestation](../world-ingestion-pipeline/graph-manifestation.md), which schedules node embedding requests
- [Model Registry](model-registry.md), which supplies provider/model metadata
- Google AI Studio error translation, which normalizes provider rate-limit scope and type before workflows report failures to the scheduler

Future chat, retrieval, and GraphRAG provider calls should use this scheduler when they are added, but those future workflows are not part of the current scheduler surface.

## User-Facing Behavior

Users do not interact with the scheduler directly. They can still see its effects through workflow status: missing or invalid provider keys, work waiting for usable credentials, provider-limit warnings, paused ingestion or graph work, and disabled keys being ignored. The future UI may expose enabled/disabled controls, but the scheduler does not own that UI.

## Internal Edge Cases

- Missing provider credential directory returns an empty credential pool instead of treating first-run setup as corruption.
- Credential files are loaded in stable filename order so round-robin behavior starts predictably.
- UTF-8 credential files with a byte-order mark are supported.
- A missing `enabled` field defaults to enabled for older credential files.
- A non-boolean `enabled` field fails clearly because future UI toggles need an exact true/false contract.
- A disabled credential is skipped even if its API key is missing.
- An enabled credential with a missing API key fails before dispatch.
- An empty allowed-model list means the credential supports every model for that provider.
- A non-empty allowed-model list must contain the exact requested model id.
- Deprecated user-entered limit hints in key files are ignored during selection.
- A model-level cooldown on one credential does not block a different model on the same credential.
- A project-level or credential-level cooldown blocks the shared provider quota bucket.
- Request-per-day exhaustion blocks only the affected model bucket for the current run.

## Cross-System Edge Cases

- Chunk embeddings, graph extraction, and graph manifestation node embeddings can compete for the same provider quota scope.
- Separate scheduler instances share a process-level busy gate, so two workflows do not dispatch unconfirmed requests through the same quota scope at the same time.
- Separate scheduler instances share a process-level round-robin cursor for the same provider/model/key-root pool, so large batches do not keep restarting at the first credential.
- A provider rate limit learned by one workflow is persisted so later scheduler instances can respect it.
- A workflow that pauses or cancels after selecting a credential must abandon the reservation, or later workflows may wait on a stale in-flight marker.
- Workflows own their own retry and resume manifests, so changing scheduler behavior must not mark chunks, extraction passes, or manifestation nodes complete or failed on its own.

## Implementation Landmarks

Provider key scheduling lives under `backend/provider_keys`. Google AI Studio error classification lives under `backend/models/google_ai_studio`. Current callers are in the embedding service, graph extraction service, and graph manifestation adapters.

## What AI/Coders Must Check Before Changing This System

Before changing Provider Key Scheduler, check credential-file compatibility, disabled-key behavior, allowed-model filtering, quota scope selection, shared busy-gate behavior, round-robin cursor behavior, persisted cooldown format, runtime request-per-day block behavior, reservation cleanup, Google AI Studio rate-limit parsing, and every workflow that calls provider models.

## Invariants That Must Not Be Broken

- Disabled keys must not be selected.
- Invalid key metadata must fail before dispatch.
- Empty allowed-model lists must remain permissive for backward compatibility.
- Non-empty allowed-model lists must stay exact-match.
- Provider/model cooldowns must be shared across scheduler instances and workflows.
- Project-level quota must not be treated as independent key-level quota.
- Non-quota failures must release reservations instead of causing cooldowns.
- Paused or cancelled work must not leave stale in-flight reservations.
- Request-per-day runtime blocks must not be described as durable persisted day locks.
- Workflow manifests own workflow progress; the scheduler owns credential availability.
