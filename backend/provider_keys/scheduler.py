"""Shared provider key scheduler for backend AI requests."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from backend.logger import get_logger

from .keys import load_eligible_provider_credentials
from .models import CredentialModelLimits, ProviderCredential, ProviderRuntimeState
from .storage import load_provider_runtime_states, save_provider_runtime_states, utc_now

logger = get_logger(__name__)


@dataclass(slots=True)
class ProviderRateLimitFailure:
    """Provider rate-limit metadata that is independent of one AI workflow."""

    rate_limit_type: str
    message: str
    retry_after_seconds: int | None = None


@dataclass(slots=True)
class _ScopeWindow:
    requests_in_window: list[float]
    tokens_in_window: list[tuple[float, int]]
    requests_today: int = 0
    request_day: str | None = None
    runtime_blocked_for_run: bool = False


class ProviderKeyScheduler:
    """Pick usable provider credentials and track shared quota cooldowns."""

    def __init__(
        self,
        *,
        provider_id: str,
        model_id: str,
        credentials: list[ProviderCredential],
        runtime_states: dict[str, ProviderRuntimeState],
        provider_keys_root: Path,
    ) -> None:
        # BLOCK 1: Store the model-specific credential pool and runtime state references for this scheduler instance
        # WHY: Future AI workflows need the same scheduling rules without embedding-specific manifest or retry state leaking into this shared layer
        self.provider_id = provider_id
        self.model_id = model_id
        self.credentials = credentials
        self.runtime_states = runtime_states
        self.provider_keys_root = provider_keys_root
        self.scope_windows: dict[str, _ScopeWindow] = {}

    @classmethod
    def for_model(
        cls,
        *,
        provider_id: str,
        model_id: str,
        provider_keys_root: Path,
    ) -> "ProviderKeyScheduler":
        # BLOCK 1: Build a scheduler with enabled credentials that can serve the requested provider model
        # WHY: The scheduler is provider-wide, but each concrete AI call still needs to honor model allow-lists before any key can be selected
        return cls(
            provider_id=provider_id,
            model_id=model_id,
            credentials=load_eligible_provider_credentials(
                provider_id=provider_id,
                model_id=model_id,
                provider_keys_root=provider_keys_root,
            ),
            runtime_states=load_provider_runtime_states(provider_keys_root),
            provider_keys_root=provider_keys_root,
        )

    def select_credential(self, *, token_estimate: int) -> ProviderCredential | None:
        # BLOCK 1: Choose the first usable credential whose persisted cooldown and optional user-entered scheduler limits allow more work right now
        # WHY: VySol intentionally uses failover-style key selection, so a healthy first key should keep being used until cooldowns or limits make the next key necessary
        now = utc_now()
        for credential in self.credentials:
            state = self.runtime_states.get(credential.quota_scope)
            scope_window = self.scope_windows.setdefault(credential.quota_scope, _ScopeWindow(requests_in_window=[], tokens_in_window=[]))
            if scope_window.runtime_blocked_for_run:
                continue
            if state is not None and state.cooldown_until is not None and state.cooldown_until > now:
                continue
            limits = credential.model_limits.get(self.model_id)
            if limits is not None and not self._scope_can_accept_request(
                scope_key=credential.quota_scope,
                limits=limits,
                token_estimate=token_estimate,
            ):
                continue
            logger.info(
                "Selected credential=%s quota_scope=%s for provider=%s model=%s.",
                credential.display_name,
                credential.quota_scope,
                self.provider_id,
                self.model_id,
            )
            return credential
        return None

    def record_success(self, *, scope_key: str, token_estimate: int) -> None:
        # BLOCK 1: Record one completed request against the in-memory scheduler window so future dispatch decisions can honor optional RPM and TPM guidance
        # WHY: Soft limits only influence scheduling if the run remembers its own recent usage, and that per-minute history does not need to persist across restarts because resume intentionally retries again
        window = self.scope_windows.setdefault(scope_key, _ScopeWindow(requests_in_window=[], tokens_in_window=[]))
        now = time.monotonic()
        self._trim_scope_window(window=window, now=now)
        current_day = utc_now().date().isoformat()
        if window.request_day != current_day:
            window.request_day = current_day
            window.requests_today = 0
        window.requests_in_window.append(now)
        window.tokens_in_window.append((now, token_estimate))
        window.requests_today += 1

    def apply_rate_limit_failure(
        self,
        *,
        credential: ProviderCredential,
        failure: ProviderRateLimitFailure,
    ) -> None:
        # BLOCK 1: Persist the provider cooldown using absolute UTC machine time so restarts and resumes can tell when the credential becomes usable again
        # WHY: An app-internal timer would be lost on restart, and rate-limit recovery must still work correctly after the process exits and resumes later
        state = self.runtime_states.get(credential.quota_scope)
        if state is None:
            state = ProviderRuntimeState(
                scope_key=credential.quota_scope,
                provider_id=credential.provider_id,
                credential_name=credential.display_name,
                project_id=credential.project_id,
            )
            self.runtime_states[credential.quota_scope] = state
        state.last_limit_type = failure.rate_limit_type
        state.last_error_message = failure.message
        scope_window = self.scope_windows.setdefault(credential.quota_scope, _ScopeWindow(requests_in_window=[], tokens_in_window=[]))
        if failure.rate_limit_type == "rpd":
            scope_window.runtime_blocked_for_run = True
            state.cooldown_until_utc = None
            self.save_runtime_states()
            logger.warning(
                "Credential=%s quota_scope=%s was blocked for the rest of this run after hitting RPD.",
                credential.display_name,
                credential.quota_scope,
            )
            return
        retry_after_seconds = failure.retry_after_seconds if failure.retry_after_seconds is not None else 60
        state.cooldown_until_utc = (utc_now() + timedelta(seconds=retry_after_seconds)).isoformat()
        self.save_runtime_states()
        logger.warning(
            "Credential=%s quota_scope=%s cooled down until=%s after hitting %s.",
            credential.display_name,
            credential.quota_scope,
            state.cooldown_until_utc,
            failure.rate_limit_type.upper(),
        )

    def save_runtime_states(self) -> None:
        # BLOCK 1: Persist all known runtime cooldown state for this provider key root
        # WHY: Any AI workflow can update shared key cooldowns, so the file must stay current when providers return rate-limit signals
        save_provider_runtime_states(self.runtime_states, self.provider_keys_root)

    def has_future_credential_availability(self) -> bool:
        # BLOCK 1: Detect whether any credential is only temporarily cooled down instead of permanently blocked for the current run
        # WHY: When every remaining credential is unavailable without a future cooldown expiry, callers should stop and leave work pending instead of spinning forever
        return any(
            state.cooldown_until is not None and state.cooldown_until > utc_now()
            for state in self.runtime_states.values()
        )

    def wait_for_next_available_credential(self) -> None:
        # BLOCK 1: Sleep until the nearest persisted cooldown expires when every credential is temporarily unavailable
        # WHY: A short bounded sleep keeps the run from busy-spinning while still letting the machine clock govern when a cooled-down credential becomes usable again
        cooldowns = [
            state.cooldown_until
            for state in self.runtime_states.values()
            if state.cooldown_until is not None and state.cooldown_until > utc_now()
        ]
        if not cooldowns:
            time.sleep(0.1)
            return
        next_cooldown = min(cooldowns)
        sleep_seconds = max(0.1, min(2.0, (next_cooldown - utc_now()).total_seconds()))
        time.sleep(sleep_seconds)

    def _scope_can_accept_request(
        self,
        *,
        scope_key: str,
        limits: CredentialModelLimits,
        token_estimate: int,
    ) -> bool:
        # BLOCK 1: Enforce optional user-entered RPM, TPM, and RPD limits as soft scheduler guidance before dispatching a new request
        # WHY: Users on different billing tiers can know their real limits better than the backend, so honoring those hints reduces needless provider errors even though provider responses still win
        window = self.scope_windows.setdefault(scope_key, _ScopeWindow(requests_in_window=[], tokens_in_window=[]))
        now = time.monotonic()
        self._trim_scope_window(window=window, now=now)
        current_day = utc_now().date().isoformat()
        if window.request_day != current_day:
            window.request_day = current_day
            window.requests_today = 0
        if limits.requests_per_minute is not None and len(window.requests_in_window) >= limits.requests_per_minute:
            return False
        if limits.tokens_per_minute is not None and sum(token_count for _, token_count in window.tokens_in_window) + token_estimate > limits.tokens_per_minute:
            return False
        if limits.requests_per_day is not None and window.requests_today >= limits.requests_per_day:
            return False
        return True

    def _trim_scope_window(self, *, window: _ScopeWindow, now: float) -> None:
        # BLOCK 1: Drop request and token entries older than one minute from the scheduler window
        # WHY: RPM and TPM checks only care about the last rolling minute, so stale entries must be removed before comparing the next request against the configured limits
        cutoff = now - 60
        window.requests_in_window = [timestamp for timestamp in window.requests_in_window if timestamp >= cutoff]
        window.tokens_in_window = [(timestamp, token_count) for timestamp, token_count in window.tokens_in_window if timestamp >= cutoff]
