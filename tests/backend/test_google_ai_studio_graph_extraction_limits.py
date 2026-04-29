"""Google AI Studio graph extraction token-limit tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.ingestion.graph_extraction.models import GraphExtractionConfig
from backend.ingestion.graph_extraction.providers import GoogleAIStudioGraphExtractionProvider
from backend.provider_keys.models import ProviderCredential
from backend.token_counting import MaxInputTokensExceededError, TokenCountResult, TokenCountingProviderError


class GoogleAIStudioGraphExtractionLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.credential = ProviderCredential(
            provider_id="google",
            credential_name="Primary",
            api_key="fake-api-key",
            project_id="project-one",
            allowed_models=frozenset({"google/gemma-4-31b-it"}),
        )
        self.config = GraphExtractionConfig(
            provider_id="google",
            model_id="google/gemma-4-31b-it",
        )
        self.provider = GoogleAIStudioGraphExtractionProvider()

    def test_oversized_prompt_is_blocked_before_generation_request(self) -> None:
        # BLOCK 1: Make exact token counting report an over-limit prompt and prove generation is not called
        # WHY: Graph extraction must fail locally before sending oversized initial or gleaning prompts to Google
        token_error = MaxInputTokensExceededError(
            code="MAX_INPUT_TOKENS_EXCEEDED",
            message="Input is too large.",
            details={"total_tokens": 300000, "max_input_tokens": 256000},
        )
        with (
            patch("backend.ingestion.graph_extraction.providers.ensure_within_max_input_tokens", side_effect=token_error),
            patch("backend.ingestion.graph_extraction.providers.genai.Client") as client_factory,
        ):
            outcome = self.provider.extract(
                credential=self.credential,
                config=self.config,
                prompt="large prompt",
                log_context={"chunk": 1, "pass_type": "initial"},
            )

        self.assertEqual(outcome.code, "EXTRACTION_PROMPT_TOO_LARGE")
        self.assertFalse(outcome.retryable)
        self.assertEqual(outcome.billable_token_estimate, 300000)
        client_factory.assert_not_called()

    def test_token_count_failure_is_blocked_before_generation_request(self) -> None:
        # BLOCK 1: Make exact token counting fail and prove generation is not called
        # WHY: Falling back to a local estimate would reintroduce the same unsafe preflight gap this guard is meant to close
        token_error = TokenCountingProviderError(
            code="TOKEN_COUNT_FAILED",
            message="Provider token counting failed.",
        )
        with (
            patch("backend.ingestion.graph_extraction.providers.ensure_within_max_input_tokens", side_effect=token_error),
            patch("backend.ingestion.graph_extraction.providers.genai.Client") as client_factory,
        ):
            outcome = self.provider.extract(
                credential=self.credential,
                config=self.config,
                prompt="prompt",
                log_context={"chunk": 1, "pass_type": "glean"},
            )

        self.assertEqual(outcome.code, "EXTRACTION_TOKEN_COUNT_FAILED")
        self.assertFalse(outcome.retryable)
        client_factory.assert_not_called()

    def test_generation_request_runs_after_successful_exact_count(self) -> None:
        # BLOCK 1: Return a successful exact count and a fake model response
        # WHY: The new guard should not block normal extraction calls once the provider confirms the prompt fits
        fake_client = _FakeGoogleClient(response_text='{"nodes": [], "edges": []}\n---COMPLETE---')
        with (
            patch(
                "backend.ingestion.graph_extraction.providers.ensure_within_max_input_tokens",
                return_value=TokenCountResult(total_tokens=123),
            ),
            patch("backend.ingestion.graph_extraction.providers.genai.Client", return_value=fake_client) as client_factory,
        ):
            outcome = self.provider.extract(
                credential=self.credential,
                config=self.config,
                prompt="prompt",
                log_context={"chunk": 1, "pass_type": "initial"},
            )

        self.assertEqual(outcome.response_text, '{"nodes": [], "edges": []}\n---COMPLETE---')
        client_factory.assert_called_once_with(api_key="fake-api-key")
        self.assertEqual(fake_client.models.requested_model, "gemma-4-31b-it")
        self.assertEqual(fake_client.models.requested_contents, "prompt")
        self.assertTrue(fake_client.closed)


class _FakeGoogleClient:
    def __init__(self, *, response_text: str) -> None:
        self.models = _FakeGoogleModels(response_text=response_text)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeGoogleModels:
    def __init__(self, *, response_text: str) -> None:
        self._response_text = response_text
        self.requested_model: str | None = None
        self.requested_contents: object | None = None

    def generate_content(self, *, model: str, contents: object):
        self.requested_model = model
        self.requested_contents = contents
        return _FakeGoogleResponse(text=self._response_text)


class _FakeGoogleResponse:
    def __init__(self, *, text: str) -> None:
        self.text = text


if __name__ == "__main__":
    unittest.main()
