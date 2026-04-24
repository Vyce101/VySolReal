# Provider Key Scheduler

The Provider Key Scheduler is the shared backend layer that decides which configured provider key should be used for an AI request.

## Why It Exists

VySol needs key handling to work the same way across embeddings, chat, extraction, and future AI systems. Without one shared scheduler, each workflow would eventually grow its own rotation, cooldown, and disabled-key behavior, which would make rate-limit problems hard to debug.

The scheduler keeps that behavior global. A workflow asks for a usable credential for one provider and model. The scheduler loads eligible keys, ignores disabled keys, checks cooldowns and optional user-entered limits, and returns the first key that can be used right now.

## Key File Contract

Provider keys live under the provider key folder, such as `user/keys/google-ai-studio/` for Google AI Studio.

```json
{
  "name": "Primary Google Project",
  "api_key": "your-provider-key",
  "project_id": "project-one",
  "allowed_models": ["google/gemini-embedding-2-preview"],
  "enabled": true,
  "limits": {
    "google/gemini-embedding-2-preview": {
      "requests_per_minute": 100,
      "tokens_per_minute": 30000,
      "requests_per_day": 1000
    }
  }
}
```

`enabled` is optional. Existing key files that do not include it are treated as enabled so older setups keep working. If `enabled` is `false`, the scheduler ignores the key everywhere. If `enabled` is present but is not a true-or-false value, key loading fails with `PROVIDER_KEY_INVALID`.

`allowed_models` is also optional. If it is empty or missing, the key is eligible for every model from that provider. If it lists model ids, the scheduler only uses the key for those exact models.

`limits` is optional scheduler guidance. Provider responses still win, but these limits help VySol avoid preventable RPM, TPM, or RPD errors when the user knows their real account tier.

## Selection Behavior

The scheduler intentionally uses failover-style selection, not round-robin.

Keys are loaded in stable file-name order. For each request, the scheduler picks the first enabled key that:

- supports the requested model
- is not in a persisted cooldown window
- is not blocked for the rest of the current run
- still fits any configured RPM, TPM, or RPD guidance

That means a healthy first key can receive many requests, including concurrent requests, until it is rate-limited or its configured soft limits say to stop. When that happens, the scheduler skips it and tries the next usable key.

## Cooldowns And Recovery

When a provider reports a rate limit, the workflow reports that failure back to the scheduler. The scheduler stores runtime cooldown state beside the key store at `user/keys/.runtime_state.json`.

Temporary per-minute limits use a machine-clock cooldown. If the app restarts, VySol can still see when that key should become usable again. Per-day exhaustion blocks that quota scope for the rest of the current run, leaving pending work resumable later.

Quota scopes are provider-aware. If a key has a `project_id`, the scheduler treats that project as the quota scope. If it does not, the individual credential name becomes the quota scope. This avoids pretending that multiple keys from the same provider project have independent capacity when the provider quota is actually shared.

## Current Usage

Chunk embeddings are the first workflow wired into the shared scheduler. Embeddings still own chunk retry state, manifests, and Qdrant writes; the scheduler only owns provider key selection and cooldown state.

Future chat, extraction, and GraphRAG systems should use the same scheduler instead of implementing their own key rotation or failover rules.
