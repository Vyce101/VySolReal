"""Filesystem storage helpers for shared provider key runtime state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import ProviderRuntimeState


def default_provider_keys_root() -> Path:
    """Resolve the default provider key storage root."""
    return Path(__file__).resolve().parents[2] / "user" / "keys"


def provider_runtime_state_file_path(provider_keys_root: Path | None = None) -> Path:
    """Return the runtime state file path for provider cooldown metadata."""
    resolved_root = provider_keys_root if provider_keys_root is not None else default_provider_keys_root()
    return resolved_root / ".runtime_state.json"


def load_provider_runtime_states(provider_keys_root: Path | None = None) -> dict[str, ProviderRuntimeState]:
    """Load persisted provider cooldown states."""
    # BLOCK 1: Load the shared provider runtime state file and default to an empty state map when no cooldown metadata has been saved yet
    # WHY: Runtime limit state is optional support data, so the scheduler must be able to start from a clean slate on first run without treating a missing state file as corruption
    state_path = provider_runtime_state_file_path(provider_keys_root)
    if not state_path.exists():
        return {}
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    return {
        scope_key: ProviderRuntimeState.from_dict(dict(state_payload))
        for scope_key, state_payload in payload.items()
    }


def save_provider_runtime_states(
    states: dict[str, ProviderRuntimeState],
    provider_keys_root: Path | None = None,
) -> None:
    """Persist provider runtime cooldown states."""
    # BLOCK 1: Write the provider runtime state map through an atomic replacement file
    # WHY: Cooldown metadata is shared by all AI workflows, so partial writes could make future scheduling decisions either too aggressive or too conservative
    state_path = provider_runtime_state_file_path(provider_keys_root)
    _atomic_write_json(
        state_path,
        {scope_key: state.to_dict() for scope_key, state in states.items()},
    )


def utc_now() -> datetime:
    """Return the current machine clock in UTC."""
    return datetime.now(timezone.utc)


def _atomic_write_json(target_path: Path, payload: dict[str, object]) -> None:
    # BLOCK 1: Serialize JSON to a sibling temporary file, then replace the target in one filesystem operation
    # WHY: Provider runtime state is small but important for retries, so a crash during write should not leave a truncated JSON file behind
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(target_path)
