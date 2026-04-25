"""Google AI Studio exact token counter tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.models.google_ai_studio.token_counting import GoogleAIStudioTokenCounter
from backend.provider_keys.models import ProviderCredential
from backend.token_counting.errors import TokenCountingProviderError
from backend.token_counting.models import TokenCountRequest


class _FakeCountResponse:
    total_tokens = 23


class _FakeModelsResource:
    last_model = None
    last_contents = None
    last_config = None

    def count_tokens(self, *, model, contents, config=None):
        # BLOCK 1: Capture the Google countTokens request shape so the test can verify the selected model and contents exactly
        # WHY: The counter must use the same credential and model selection path as production, or max-input enforcement could drift away from the real provider request
        type(self).last_model = model
        type(self).last_contents = contents
        type(self).last_config = config
        return _FakeCountResponse()


class _FakeClient:
    last_api_key = None
    closed_count = 0

    def __init__(self, *, api_key):
        type(self).last_api_key = api_key
        self.models = _FakeModelsResource()

    def close(self):
        type(self).closed_count += 1


class GoogleAIStudioTokenCountingTests(unittest.TestCase):
    def test_counter_uses_selected_credential_and_model(self) -> None:
        _FakeClient.closed_count = 0
        request = self._request()

        with patch("backend.models.google_ai_studio.token_counting.genai.Client", _FakeClient):
            result = GoogleAIStudioTokenCounter().count_input_tokens(request=request)

        self.assertEqual(result.total_tokens, 23)
        self.assertEqual(_FakeClient.last_api_key, "fake-api-key")
        self.assertEqual(_FakeModelsResource.last_model, "gemini-embedding-2-preview")
        self.assertEqual(_FakeModelsResource.last_contents, "hello world")
        self.assertIsNone(_FakeModelsResource.last_config)
        self.assertEqual(_FakeClient.closed_count, 1)

    def test_client_creation_failure_returns_structured_token_count_error(self) -> None:
        # BLOCK 1: Make Google client creation fail before any countTokens request exists
        # WHY: Client construction failures must still use the shared fail-closed token counting error path so callers can block with structured app errors
        with patch(
            "backend.models.google_ai_studio.token_counting.genai.Client",
            side_effect=RuntimeError("bad client"),
        ):
            with self.assertRaises(TokenCountingProviderError) as raised:
                GoogleAIStudioTokenCounter().count_input_tokens(request=self._request())

        self.assertEqual(raised.exception.code, "GOOGLE_TOKEN_COUNT_FAILED")
        self.assertEqual(raised.exception.details["reason"], "bad client")

    def _request(self) -> TokenCountRequest:
        return TokenCountRequest(
            provider_id="google",
            model_id="google/gemini-embedding-2-preview",
            credential=ProviderCredential(
                provider_id="google",
                credential_name="Primary",
                api_key="fake-api-key",
                project_id="project-one",
                allowed_models=frozenset({"google/gemini-embedding-2-preview"}),
            ),
            contents="hello world",
        )


if __name__ == "__main__":
    unittest.main()
