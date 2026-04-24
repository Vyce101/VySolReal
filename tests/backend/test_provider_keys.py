"""Shared provider key scheduler tests."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.provider_keys.errors import ProviderKeyConfigurationError
from backend.provider_keys.keys import load_eligible_provider_credentials, load_provider_credentials
from backend.provider_keys.models import ProviderModelQuota
from backend.provider_keys.scheduler import ProviderKeyScheduler, ProviderRateLimitFailure


class ProviderKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.keys_root = self.temp_dir / "user" / "keys"
        self.provider_dir = self.keys_root / "google-ai-studio"
        self.provider_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_legacy_key_without_enabled_field_loads_as_enabled(self) -> None:
        self._write_key("primary.json", name="Primary")

        credentials = load_provider_credentials(provider_id="google", provider_keys_root=self.keys_root)

        self.assertEqual([credential.display_name for credential in credentials], ["Primary"])
        self.assertTrue(credentials[0].enabled)

    def test_disabled_first_key_is_skipped_for_model_selection(self) -> None:
        self._write_key("a-disabled.json", name="Disabled", enabled=False)
        self._write_key("b-enabled.json", name="Enabled", enabled=True)

        credentials = load_eligible_provider_credentials(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            provider_keys_root=self.keys_root,
        )

        self.assertEqual([credential.display_name for credential in credentials], ["Enabled"])

    def test_disabled_keys_do_not_count_as_eligible(self) -> None:
        self._write_key("primary.json", name="Disabled", enabled=False)

        credentials = load_eligible_provider_credentials(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            provider_keys_root=self.keys_root,
        )

        self.assertEqual(credentials, [])

    def test_disabled_key_with_missing_secret_is_ignored(self) -> None:
        self.provider_dir.joinpath("primary.json").write_text(
            json.dumps(
                {
                    "name": "Disabled",
                    "enabled": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        credentials = load_provider_credentials(provider_id="google", provider_keys_root=self.keys_root)

        self.assertEqual(credentials, [])

    def test_invalid_enabled_value_fails_clearly(self) -> None:
        self._write_key("primary.json", name="Invalid", enabled="yes")

        with self.assertRaises(ProviderKeyConfigurationError) as context:
            load_provider_credentials(provider_id="google", provider_keys_root=self.keys_root)

        self.assertEqual(context.exception.code, "PROVIDER_KEY_INVALID")

    def test_scheduler_uses_first_key_until_cooldown_then_fails_over(self) -> None:
        self._write_key("a-primary.json", name="Primary")
        self._write_key("b-secondary.json", name="Secondary")
        scheduler = ProviderKeyScheduler.for_model(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            provider_keys_root=self.keys_root,
        )

        first = scheduler.select_credential(token_estimate=5)
        second = scheduler.select_credential(token_estimate=5)
        self.assertEqual(first.display_name, "Primary")
        self.assertEqual(second.display_name, "Primary")

        scheduler.apply_rate_limit_failure(
            credential=first,
            failure=ProviderRateLimitFailure(
                rate_limit_type="rpm",
                message="REQUESTS_PER_MINUTE exhausted",
                retry_after_seconds=60,
            ),
        )
        failover = scheduler.select_credential(token_estimate=5)

        self.assertEqual(failover.display_name, "Secondary")

    def test_deprecated_user_limits_are_ignored_for_selection(self) -> None:
        self._write_key(
            "a-primary.json",
            name="Primary",
            limits={
                "google/gemini-embedding-2-preview": {
                    "requests_per_minute": 0,
                    "tokens_per_minute": 0,
                    "requests_per_day": 0,
                }
            },
        )
        self._write_key("b-secondary.json", name="Secondary")
        scheduler = ProviderKeyScheduler.for_model(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            provider_keys_root=self.keys_root,
        )

        selected = scheduler.select_credential(token_estimate=5)

        self.assertEqual(selected.display_name, "Primary")

    def test_model_cooldown_does_not_block_same_key_for_another_model(self) -> None:
        self._write_key("primary.json", name="Primary", allowed_models=[])
        first_model_scheduler = ProviderKeyScheduler.for_model(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            provider_keys_root=self.keys_root,
        )
        first_model_credential = first_model_scheduler.select_credential(token_estimate=5)
        first_model_scheduler.apply_rate_limit_failure(
            credential=first_model_credential,
            failure=ProviderRateLimitFailure(
                rate_limit_type="rpm",
                message="Unknown Google 429",
                retry_after_seconds=60,
            ),
        )
        second_model_scheduler = ProviderKeyScheduler.for_model(
            provider_id="google",
            model_id="google/gemini-3-flash-preview",
            provider_keys_root=self.keys_root,
        )

        selected = second_model_scheduler.select_credential(token_estimate=5)

        self.assertEqual(selected.display_name, "Primary")

    def test_shared_cooldown_blocks_same_key_for_another_model(self) -> None:
        self._write_key("a-primary.json", name="Primary", allowed_models=[])
        self._write_key("b-secondary.json", name="Secondary", allowed_models=[])
        first_model_scheduler = ProviderKeyScheduler.for_model(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            provider_keys_root=self.keys_root,
        )
        first_model_credential = first_model_scheduler.select_credential(token_estimate=5)
        first_model_scheduler.apply_rate_limit_failure(
            credential=first_model_credential,
            failure=ProviderRateLimitFailure(
                rate_limit_type="rpm",
                message="Project quota exhausted",
                retry_after_seconds=60,
                limit_scope="project",
            ),
        )
        second_model_scheduler = ProviderKeyScheduler.for_model(
            provider_id="google",
            model_id="google/gemini-3-flash-preview",
            provider_keys_root=self.keys_root,
        )

        selected = second_model_scheduler.select_credential(token_estimate=5)

        self.assertEqual(selected.display_name, "Secondary")

    def test_unknown_429_defaults_to_model_level_cooldown(self) -> None:
        self._write_key("a-primary.json", name="Primary")
        scheduler = ProviderKeyScheduler.for_model(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            provider_keys_root=self.keys_root,
        )
        primary = scheduler.select_credential(token_estimate=5)

        scheduler.apply_rate_limit_failure(
            credential=primary,
            failure=ProviderRateLimitFailure(
                rate_limit_type="rpm",
                message="Unknown provider 429",
            ),
        )
        runtime_state_payload = json.loads(self.keys_root.joinpath(".runtime_state.json").read_text(encoding="utf-8"))
        state = next(iter(runtime_state_payload.values()))

        self.assertEqual(state["limit_scope"], "model")
        self.assertEqual(state["model_id"], "google/gemini-embedding-2-preview")

    def test_rpd_blocks_only_the_affected_model_bucket_for_the_run(self) -> None:
        self._write_key("a-primary.json", name="Primary", allowed_models=[])
        self._write_key("b-secondary.json", name="Secondary", allowed_models=[])
        scheduler = ProviderKeyScheduler.for_model(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            provider_keys_root=self.keys_root,
        )
        primary = scheduler.select_credential(token_estimate=5)

        scheduler.apply_rate_limit_failure(
            credential=primary,
            failure=ProviderRateLimitFailure(
                rate_limit_type="rpd",
                message="REQUESTS_PER_DAY exhausted",
            ),
        )
        failover = scheduler.select_credential(token_estimate=5)

        self.assertEqual(failover.display_name, "Secondary")

    def test_reservation_before_dispatch_prevents_overbooking_known_quota(self) -> None:
        self._write_key("a-primary.json", name="Primary")
        self._write_key("b-secondary.json", name="Secondary")
        credentials = load_eligible_provider_credentials(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            provider_keys_root=self.keys_root,
        )
        scheduler = ProviderKeyScheduler(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            credentials=credentials,
            runtime_states={},
            provider_keys_root=self.keys_root,
            model_quota=ProviderModelQuota(requests_per_minute=1),
        )

        first = scheduler.select_credential(token_estimate=5)
        second = scheduler.select_credential(token_estimate=5)

        self.assertEqual(first.display_name, "Primary")
        self.assertEqual(second.display_name, "Secondary")

    def _write_key(
        self,
        filename: str,
        *,
        name: str,
        enabled: bool | str | None = None,
        allowed_models: list[str] | None = None,
        limits: dict[str, object] | None = None,
    ) -> None:
        # BLOCK 1: Write a minimal Google key file fixture with optional enabled, allow-list, and deprecated-limit fields
        # WHY: The scheduler tests need real JSON files so they exercise the same sorted loading, compatibility, and selection behavior as production key storage
        payload: dict[str, object] = {
            "name": name,
            "api_key": f"fake-{name.lower()}",
            "allowed_models": ["google/gemini-embedding-2-preview"] if allowed_models is None else allowed_models,
        }
        if enabled is not None:
            payload["enabled"] = enabled
        if limits is not None:
            payload["limits"] = limits
        self.provider_dir.joinpath(filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
