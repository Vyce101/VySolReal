"""Shared provider key scheduler tests."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.provider_keys.errors import ProviderKeyConfigurationError
from backend.provider_keys.keys import load_eligible_provider_credentials, load_provider_credentials
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

    def _write_key(
        self,
        filename: str,
        *,
        name: str,
        enabled: bool | str | None = None,
    ) -> None:
        # BLOCK 1: Write a minimal Google key file fixture with optional enabled state
        # WHY: The scheduler tests need real JSON files so they exercise the same sorted loading and compatibility behavior as production key storage
        payload: dict[str, object] = {
            "name": name,
            "api_key": f"fake-{name.lower()}",
            "allowed_models": ["google/gemini-embedding-2-preview"],
        }
        if enabled is not None:
            payload["enabled"] = enabled
        self.provider_dir.joinpath(filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
