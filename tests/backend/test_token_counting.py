"""Shared exact token counting tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.provider_keys.models import ProviderCredential
from backend.token_counting.errors import (
    MaxInputTokensExceededError,
    TokenCountingConfigurationError,
)
from backend.token_counting.models import TokenCountRequest, TokenCountResult
from backend.token_counting.service import count_input_tokens, ensure_within_max_input_tokens


class TokenCountingTests(unittest.TestCase):
    def test_unsupported_provider_fails_closed(self) -> None:
        request = self._request(provider_id="future-provider")

        with self.assertRaises(TokenCountingConfigurationError):
            count_input_tokens(request=request)

    def test_over_limit_blocks_before_send(self) -> None:
        send_calls = 0

        # BLOCK 1: Stub the exact provider count so the test can verify shared block-before-send behavior without touching a real provider
        # WHY: The shared contract matters here, not provider transport, so the test should isolate whether over-limit requests are stopped before the caller's send path runs
        with patch(
            "backend.token_counting.service.count_input_tokens",
            return_value=TokenCountResult(total_tokens=23),
        ):
            with self.assertRaises(MaxInputTokensExceededError):
                ensure_within_max_input_tokens(
                    request=self._request(contents=["first", "second"]),
                    max_input_tokens=5,
                )
                send_calls += 1

        self.assertEqual(send_calls, 0)

    def test_within_limit_allows_send_with_original_contents_unchanged(self) -> None:
        send_calls = 0
        contents = ["first", "second"]

        # BLOCK 1: Stub an in-range exact count and confirm the caller can proceed without any silent content trimming or mutation
        # WHY: VySol's contract is explicit block-or-send, so the shared layer must leave the request contents untouched when it allows the send path to continue
        with patch(
            "backend.token_counting.service.count_input_tokens",
            return_value=TokenCountResult(total_tokens=23),
        ):
            count_result = ensure_within_max_input_tokens(
                request=self._request(contents=contents),
                max_input_tokens=30,
            )
            send_calls += 1

        self.assertIsNotNone(count_result)
        self.assertEqual(count_result.total_tokens, 23)
        self.assertEqual(send_calls, 1)
        self.assertEqual(contents, ["first", "second"])

    def _request(self, *, provider_id: str = "google", contents: object = "hello world") -> TokenCountRequest:
        return TokenCountRequest(
            provider_id=provider_id,
            model_id="google/gemini-3-flash-preview",
            credential=ProviderCredential(
                provider_id=provider_id,
                credential_name="Primary",
                api_key="fake-api-key",
                project_id="project-one",
                allowed_models=frozenset({"google/gemini-3-flash-preview"}),
            ),
            contents=contents,
        )


if __name__ == "__main__":
    unittest.main()
