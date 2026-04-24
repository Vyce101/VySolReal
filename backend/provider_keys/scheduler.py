"""Shared provider key scheduler for backend AI requests."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from backend.logger import get_logger

from .keys import load_eligible_provider_credentials
from .models import ProviderCredential, ProviderModelQuota, ProviderRuntimeState
from .storage import load_provider_runtime_states, save_provider_runtime_states, utc_now

logger = get_logger(__name__)


@dataclass(slots=True)
class ProviderRateLimitFailure:
    """Provider rate-limit metadata that is independent of one AI workflow."""

    rate_limit_type: str
    message: str
    retry_after_seconds: int | None = None
    limit_scope: str = "model"


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
        model_quota: ProviderModelQuota | None = None,
    ) -> None:
        # BLOCK 1: Store the model-specific credential pool and runtime state references for this scheduler instance
        # WHY: Future AI workflows need the same scheduling rules without embedding-specific manifest or retry state leaking into this shared layer
        self.provider_id = provider_id
        self.model_id = model_id
        self.credentials = credentials
        self.runtime_states = runtime_states
        self.provider_keys_root = provider_keys_root
        self.model_quota = model_quota
        self.scope_windows: dict[str, _ScopeWindow] = {}
        self._lock = Lock()

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
        # BLOCK 1: Choose and reserve the first usable credential for this provider model
        # VARS: token_estimate = estimated token cost reserved before the provider call starts
        # WHY: Reservation happens inside one scheduler lock so concurrent dispatchers cannot all see the same remaining model quota before any request has finished
        with self._lock:
            now = utc_now()
            for credential in self.credentials:
                if self._credential_is_unavailable(credential=credential, now=now):
                    continue
                bucket_key = self._model_bucket_key(credential)
                if self.model_quota is not None and not self._scope_can_accept_request(
                    bucket_key=bucket_key,
                    token_estimate=token_estimate,
                ):
                    continue
                self._reserve_request(bucket_key=bucket_key, token_estimate=token_estimate)
                logger.info(
                    "Selected credential=%s quota_scope=%s quota_bucket=%s for provider=%s model=%s.",
                    credential.display_name,
                    credential.quota_scope,
                    bucket_key,
                    self.provider_id,
                    self.model_id,
                )
                return credential
            return None

    def record_success(self, *, scope_key: str, token_estimate: int) -> None:
        # BLOCK 1: Keep the already-reserved request as confirmed quota usage
        # WHY: The scheduler reserves before dispatch, so a successful response should not double-count the same request in the rolling model window
        with self._lock:
            window = self.scope_windows.setdefault(self._model_bucket_key_from_scope(scope_key), _ScopeWindow(requests_in_window=[], tokens_in_window=[]))
            self._trim_scope_window(window=window, now=time.monotonic())

    def release_reservation(self, *, scope_key: str, token_estimate: int) -> None:
        # BLOCK 1: Remove one pre-dispatch reservation after a request fails without a provider quota signal
        # VARS: token_estimate = estimated token cost that was reserved before dispatch
        # WHY: Non-rate-limit failures should not make a key/model look busier than it is, while true provider quota failures are handled separately as cooldowns
        with self._lock:
            bucket_key = self._model_bucket_key_from_scope(scope_key)
            window = self.scope_windows.setdefault(bucket_key, _ScopeWindow(requests_in_window=[], tokens_in_window=[]))
            self._remove_latest_window_entry(window=window, token_estimate=token_estimate)

    def apply_rate_limit_failure(
        self,
        *,
        credential: ProviderCredential,
        failure: ProviderRateLimitFailure,
    ) -> None:
        # BLOCK 1: Persist the provider cooldown against the exact quota bucket reported by the provider
        # WHY: Model-specific failures should not disable unrelated models on the same key, while explicit project/key failures still need one shared block across models
        with self._lock:
            bucket_key = self._failure_bucket_key(credential=credential, failure=failure)
            state = self.runtime_states.get(bucket_key)
            if state is None:
                state = ProviderRuntimeState(
                    scope_key=bucket_key,
                    provider_id=credential.provider_id,
                    credential_name=credential.display_name,
                    project_id=credential.project_id,
                    quota_scope=credential.quota_scope,
                    model_id=self.model_id if failure.limit_scope == "model" else None,
                    limit_scope=failure.limit_scope,
                )
                self.runtime_states[bucket_key] = state
            state.last_limit_type = failure.rate_limit_type
            state.last_error_message = failure.message
            scope_window = self.scope_windows.setdefault(bucket_key, _ScopeWindow(requests_in_window=[], tokens_in_window=[]))
            if failure.rate_limit_type == "rpd":
                scope_window.runtime_blocked_for_run = True
                state.cooldown_until_utc = None
                self.save_runtime_states()
                logger.warning(
                    "Credential=%s quota_scope=%s quota_bucket=%s was blocked for the rest of this run after hitting RPD.",
                    credential.display_name,
                    credential.quota_scope,
                    bucket_key,
                )
                return
            retry_after_seconds = failure.retry_after_seconds if failure.retry_after_seconds is not None else 60
            state.cooldown_until_utc = (utc_now() + timedelta(seconds=retry_after_seconds)).isoformat()
            self.save_runtime_states()
            logger.warning(
                "Credential=%s quota_scope=%s quota_bucket=%s cooled down until=%s after hitting %s.",
                credential.display_name,
                credential.quota_scope,
                bucket_key,
                state.cooldown_until_utc,
                failure.rate_limit_type.upper(),
            )

    def save_runtime_states(self) -> None:
        # BLOCK 1: Persist all known runtime cooldown state for this provider key root
        # WHY: Any AI workflow can update shared key cooldowns, so the file must stay current when providers return rate-limit signals
        save_provider_runtime_states(self.runtime_states, self.provider_keys_root)

    def has_future_credential_availability(self) -> bool:
        # BLOCK 1: Detect whether this scheduler's model has a future cooldown expiry on any eligible credential
        # WHY: Cooldowns and known quota windows for other models should not keep this workflow sleeping, but active local blockers mean waiting can make progress
        with self._lock:
            now = utc_now()
            if any(
                state.cooldown_until is not None and state.cooldown_until > now
                for credential in self.credentials
                for state in self._states_for_credential(credential)
            ):
                return True
            return self.model_quota is not None and any(
                self._quota_window_wait_seconds(bucket_key=self._model_bucket_key(credential)) is not None
                for credential in self.credentials
            )

    def wait_for_next_available_credential(self) -> None:
        # BLOCK 1: Sleep until the nearest relevant cooldown expires when every credential is temporarily unavailable
        # WHY: A short bounded sleep keeps the run from busy-spinning while ignoring cooldowns and quota windows that belong to unrelated models
        with self._lock:
            now = utc_now()
            cooldowns = [
                state.cooldown_until
                for credential in self.credentials
                for state in self._states_for_credential(credential)
                if state.cooldown_until is not None and state.cooldown_until > now
            ]
            quota_waits = [
                wait_seconds
                for credential in self.credentials
                if (wait_seconds := self._quota_window_wait_seconds(bucket_key=self._model_bucket_key(credential))) is not None
            ]
        if not cooldowns:
            if quota_waits:
                time.sleep(max(0.1, min(2.0, min(quota_waits))))
                return
            time.sleep(0.1)
            return
        next_cooldown = min(cooldowns)
        wait_options = [(next_cooldown - utc_now()).total_seconds()] + quota_waits
        sleep_seconds = max(0.1, min(2.0, min(wait_options)))
        time.sleep(sleep_seconds)

    def _scope_can_accept_request(
        self,
        *,
        bucket_key: str,
        token_estimate: int,
    ) -> bool:
        # BLOCK 1: Check the reserved model window against automatic provider quota metadata when that metadata is available
        # WHY: User-entered limits are ignored, so pre-dispatch throttling should only happen when VySol has provider/model quota data it can own
        if self.model_quota is None:
            return True
        window = self.scope_windows.setdefault(bucket_key, _ScopeWindow(requests_in_window=[], tokens_in_window=[]))
        now = time.monotonic()
        self._trim_scope_window(window=window, now=now)
        current_day = utc_now().date().isoformat()
        if window.request_day != current_day:
            window.request_day = current_day
            window.requests_today = 0
        if self.model_quota.requests_per_minute is not None and len(window.requests_in_window) >= self.model_quota.requests_per_minute:
            return False
        if self.model_quota.tokens_per_minute is not None and sum(token_count for _, token_count in window.tokens_in_window) + token_estimate > self.model_quota.tokens_per_minute:
            return False
        if self.model_quota.requests_per_day is not None and window.requests_today >= self.model_quota.requests_per_day:
            return False
        return True

    def _quota_window_wait_seconds(self, *, bucket_key: str) -> float | None:
        # BLOCK 1: Estimate when a known automatic quota window can accept another request
        # WHY: If future provider metadata adds hard RPM or TPM ceilings, callers should wait for the rolling minute to clear instead of stopping with pending work
        if self.model_quota is None:
            return None
        window = self.scope_windows.setdefault(bucket_key, _ScopeWindow(requests_in_window=[], tokens_in_window=[]))
        now = time.monotonic()
        self._trim_scope_window(window=window, now=now)
        waits: list[float] = []
        if self.model_quota.requests_per_minute is not None and len(window.requests_in_window) >= self.model_quota.requests_per_minute:
            waits.append(60 - (now - min(window.requests_in_window)))
        if self.model_quota.tokens_per_minute is not None and sum(token_count for _, token_count in window.tokens_in_window) >= self.model_quota.tokens_per_minute and window.tokens_in_window:
            waits.append(60 - (now - min(timestamp for timestamp, _ in window.tokens_in_window)))
        return max(0.1, min(waits)) if waits else None

    def _credential_is_unavailable(self, *, credential: ProviderCredential, now: datetime) -> bool:
        # BLOCK 1: Check both shared and model-specific blocks before a credential is selected
        # WHY: A project/key cooldown must stop all model traffic in that scope, but a model cooldown should only skip the matching provider model
        for state in self._states_for_credential(credential):
            if state.cooldown_until is not None and state.cooldown_until > now:
                return True
        return any(
            self.scope_windows.setdefault(bucket_key, _ScopeWindow(requests_in_window=[], tokens_in_window=[])).runtime_blocked_for_run
            for bucket_key in self._bucket_keys_for_credential(credential)
        )

    def _states_for_credential(self, credential: ProviderCredential) -> list[ProviderRuntimeState]:
        # BLOCK 1: Gather cooldown records that can affect this credential for the scheduler's current model
        # WHY: Runtime state is persisted by quota bucket, so selection needs a compact way to check the model bucket and any shared bucket together
        return [
            state
            for bucket_key in self._bucket_keys_for_credential(credential)
            if (state := self.runtime_states.get(bucket_key)) is not None
        ]

    def _bucket_keys_for_credential(self, credential: ProviderCredential) -> list[str]:
        # BLOCK 1: Build the legacy, shared, and model-specific runtime bucket keys for one credential
        # WHY: New writes use explicit bucket suffixes, but old cooldown files used the bare quota scope and should still be respected during upgrade
        return [credential.quota_scope, self._shared_bucket_key(credential), self._model_bucket_key(credential)]

    def _failure_bucket_key(self, *, credential: ProviderCredential, failure: ProviderRateLimitFailure) -> str:
        # BLOCK 1: Convert provider-reported failure scope into the runtime bucket that should be cooled down
        # WHY: Unknown or ordinary 429s should be model-local by default, while explicit credential/project failures need a shared block
        if failure.limit_scope == "model":
            return self._model_bucket_key(credential)
        return self._shared_bucket_key(credential)

    def _model_bucket_key(self, credential: ProviderCredential) -> str:
        return self._model_bucket_key_from_scope(credential.quota_scope)

    def _model_bucket_key_from_scope(self, scope_key: str) -> str:
        return f"{scope_key}:model:{self.model_id}"

    def _shared_bucket_key(self, credential: ProviderCredential) -> str:
        return f"{credential.quota_scope}:all-models"

    def _reserve_request(self, *, bucket_key: str, token_estimate: int) -> None:
        # BLOCK 1: Add one request reservation to the model quota window before the provider call starts
        # WHY: The reservation makes future scheduler selections see in-flight work immediately instead of waiting for provider responses to finish
        window = self.scope_windows.setdefault(bucket_key, _ScopeWindow(requests_in_window=[], tokens_in_window=[]))
        now = time.monotonic()
        self._trim_scope_window(window=window, now=now)
        current_day = utc_now().date().isoformat()
        if window.request_day != current_day:
            window.request_day = current_day
            window.requests_today = 0
        window.requests_in_window.append(now)
        window.tokens_in_window.append((now, token_estimate))
        window.requests_today += 1

    def _remove_latest_window_entry(self, *, window: _ScopeWindow, token_estimate: int) -> None:
        # BLOCK 1: Remove the newest matching request and token reservation from a failed dispatch
        # WHY: Reservations are recorded before provider calls, so a non-quota failure needs one best-effort rollback to avoid underusing the key/model later in the same run
        if window.requests_in_window:
            window.requests_in_window.pop()
        for index in range(len(window.tokens_in_window) - 1, -1, -1):
            if window.tokens_in_window[index][1] == token_estimate:
                window.tokens_in_window.pop(index)
                break
        if window.requests_today > 0:
            window.requests_today -= 1

    def _trim_scope_window(self, *, window: _ScopeWindow, now: float) -> None:
        # BLOCK 1: Drop request and token entries older than one minute from the scheduler window
        # WHY: RPM and TPM checks only care about the last rolling minute, so stale entries must be removed before comparing the next request against known provider quota data
        cutoff = now - 60
        window.requests_in_window = [timestamp for timestamp in window.requests_in_window if timestamp >= cutoff]
        window.tokens_in_window = [(timestamp, token_count) for timestamp, token_count in window.tokens_in_window if timestamp >= cutoff]
