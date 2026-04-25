---
order: 100
---

# Provider Key Scheduler

The Provider Key Scheduler is the shared backend layer that decides which configured provider key should be used for an AI request.

## Why It Exists

VySol needs key handling to work the same way across embeddings, chat, extraction, and future AI systems. Without one shared scheduler, each workflow would eventually grow its own rotation, cooldown, and disabled-key behavior, which would make rate-limit problems hard to debug.

The scheduler keeps that behavior global. A workflow asks for a usable credential for one provider and model. The scheduler loads eligible keys, ignores disabled keys, checks model-aware cooldowns, reserves the request before dispatch, and returns a key that can be used right now without every workflow having to reinvent that logic.

## Key File Contract

Provider keys live under the provider key folder, such as `user/keys/google-ai-studio/` for Google AI Studio.

```json
{
  "name": "Primary Google Project",
  "api_key": "your-provider-key",
  "project_id": "project-one",
  "allowed_models": ["google/gemini-embedding-2-preview"],
  "enabled": true
}
```

`enabled` is optional. Existing key files that do not include it are treated as enabled so older setups keep working. If `enabled` is `false`, the scheduler ignores the key everywhere. If `enabled` is present but is not a true-or-false value, key loading fails with `PROVIDER_KEY_INVALID`.

`allowed_models` is also optional. If it is empty or missing, the key is eligible for every model from that provider. If it lists model ids, the scheduler only uses the key for those exact models.

Older key files may contain a `limits` object. VySol keeps those files compatible, but the scheduler ignores user-entered RPM, TPM, and RPD limits. Provider/model metadata and provider rate-limit responses are the source of truth.

## Selection Behavior

The scheduler intentionally uses cooldown-aware round-robin selection.

Keys are loaded in stable file-name order, but selection does not restart at the first key every time. Instead, VySol keeps a shared cursor for the provider-model pool and rotates the starting point on each selection. For each request, the scheduler tries the next enabled key that:

- supports the requested model
- is not in a persisted cooldown window
- is not blocked for the rest of the current run
- still fits any automatic provider/model quota data VySol knows about

That means requests spread across the eligible pool in round-robin order, but any key that is already known to be cooled down or blocked is skipped immediately.

Before a workflow sends a provider request, the scheduler reserves that request against the selected `provider + quota scope + model` bucket. If VySol knows an automatic quota for that model, later selections see the in-flight request immediately instead of waiting for the first request to finish. If the request fails without a quota signal, the workflow releases the reservation.

The scheduler also keeps one shared in-flight gate per provider quota scope. That means embeddings, graph extraction, and future workflows skip a key while another unconfirmed request is already using the same key or project quota bucket. If another key is free, work moves there; if every key is busy or cooling down, the workflow waits.

## Cooldowns And Recovery

When a provider reports a rate limit, the workflow reports that failure back to the scheduler. The scheduler stores runtime cooldown state beside the key store at `user/keys/.runtime_state.json`.

Temporary per-minute limits use a machine-clock cooldown. If the app restarts, VySol can still see when that key/model should become usable again. Per-day exhaustion blocks that quota bucket for the rest of the current run, leaving pending work resumable later.

Quota scopes are provider-aware. If a key has a `project_id`, the scheduler treats that project as the quota scope. If it does not, the individual credential name becomes the quota scope. This avoids pretending that multiple keys from the same provider project have independent capacity when the provider quota is actually shared.

Quota buckets are also model-aware. A rate limit for one model on a key or project does not block another model on the same key unless the provider reports a credential-level or project-level problem. Unknown HTTP 429 responses fall back to a temporary model-level cooldown using the provider `Retry-After` value when it is available, or 60 seconds when it is not.

## Current Usage

Chunk embeddings and the [Knowledge Graph Extraction Pipeline](../world-ingestion-pipeline/knowledge-graph-extraction-pipeline.md) are both wired into the shared scheduler today. Those workflows still own their own retry state, manifests, and storage writes; the scheduler only owns provider key selection, reservations, cooldown state, and automatic quota-window waiting.

Future chat and later GraphRAG systems should use the same scheduler instead of implementing their own key rotation or cooldown rules.
