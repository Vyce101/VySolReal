"""Google AI Studio embedding max-input enforcement tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path

from backend.embeddings.catalog import create_embedding_profile
from backend.embeddings.models import EmbeddingWorkItem
from backend.models.google_ai_studio.embedding_client import GoogleEmbeddingError, GoogleEmbeddingResponse
from backend.models.google_ai_studio.gemini_embedding_2_preview import GoogleAIStudioEmbeddingProvider
from backend.provider_keys.models import ProviderCredential
from backend.token_counting.errors import MaxInputTokensExceededError, TokenCountingProviderError
from backend.token_counting.models import TokenCountResult


class GoogleAIStudioEmbeddingLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = GoogleAIStudioEmbeddingProvider()
        self.profile = create_embedding_profile(model_id="google/gemini-embedding-2-preview")
        self.credential = ProviderCredential(
            provider_id="google",
            credential_name="Primary",
            api_key="fake-api-key",
            project_id="project-one",
            allowed_models=frozenset({"google/gemini-embedding-2-preview"}),
        )
        self.work_item = EmbeddingWorkItem(
            book_number=1,
            chunk_number=1,
            point_id="point-1",
            chunk_text="tiny",
            text_hash="hash-1",
            source_filename="book.txt",
            chunk_path=Path("book.txt"),
            chunk_position="1/1",
        )

    def test_chunk_over_limit_blocks_without_calling_embed_content(self) -> None:
        over_limit_error = MaxInputTokensExceededError(
            code="MAX_INPUT_TOKENS_EXCEEDED",
            message="too large",
            details={"total_tokens": 9000},
        )

        # BLOCK 1: Force the shared exact token path to report an oversized request for a tiny chunk to prove the old /4 heuristic is no longer the enforcement source
        # WHY: If the provider still used the old estimate, this tiny chunk would pass, so the only way this block can trigger is through the new exact-count path
        with patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.ensure_within_max_input_tokens",
            side_effect=over_limit_error,
        ), patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.embed_content",
            side_effect=AssertionError("embed_content should not run when token enforcement blocks first."),
        ):
            result = self.provider.embed_text(
                credential=self.credential,
                profile=self.profile,
                work_item=self.work_item,
            )

        self.assertEqual(result.code, "EMBEDDING_CHUNK_TOO_LARGE")
        self.assertEqual(result.billable_token_estimate, 9000)

    def test_query_over_limit_blocks_without_calling_embed_content(self) -> None:
        over_limit_error = MaxInputTokensExceededError(
            code="MAX_INPUT_TOKENS_EXCEEDED",
            message="too large",
            details={"total_tokens": 9000},
        )

        with patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.ensure_within_max_input_tokens",
            side_effect=over_limit_error,
        ), patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.embed_content",
            side_effect=AssertionError("embed_content should not run when token enforcement blocks first."),
        ):
            result = self.provider.embed_query(
                credential=self.credential,
                profile=self.profile,
                query="tiny",
            )

        self.assertEqual(result.code, "RETRIEVAL_QUERY_TOO_LARGE")
        self.assertEqual(result.billable_token_estimate, 9000)

    def test_chunk_token_count_failure_blocks_without_fallback(self) -> None:
        with patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.ensure_within_max_input_tokens",
            side_effect=TokenCountingProviderError(code="GOOGLE_TOKEN_COUNT_FAILED", message="blocked"),
        ), patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.embed_content",
            side_effect=AssertionError("embed_content should not run when token counting fails."),
        ):
            result = self.provider.embed_text(
                credential=self.credential,
                profile=self.profile,
                work_item=self.work_item,
            )

        self.assertEqual(result.code, "EMBEDDING_TOKEN_COUNT_FAILED")
        self.assertFalse(result.retryable)

    def test_query_token_count_failure_blocks_without_fallback(self) -> None:
        with patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.ensure_within_max_input_tokens",
            side_effect=TokenCountingProviderError(code="GOOGLE_TOKEN_COUNT_FAILED", message="blocked"),
        ), patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.embed_content",
            side_effect=AssertionError("embed_content should not run when token counting fails."),
        ):
            result = self.provider.embed_query(
                credential=self.credential,
                profile=self.profile,
                query="tiny",
            )

        self.assertEqual(result.code, "RETRIEVAL_QUERY_TOKEN_COUNT_FAILED")
        self.assertFalse(result.retryable)

    def test_provider_failures_reuse_exact_counted_tokens(self) -> None:
        with patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.ensure_within_max_input_tokens",
            return_value=TokenCountResult(total_tokens=23),
        ), patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.embed_content",
            return_value=GoogleEmbeddingError(
                code_suffix="429",
                message="Too many requests",
                retryable=True,
            ),
        ):
            result = self.provider.embed_text(
                credential=self.credential,
                profile=self.profile,
                work_item=self.work_item,
            )

        self.assertEqual(result.code, "EMBEDDING_PROVIDER_429")
        self.assertEqual(result.billable_token_estimate, 23)

    def test_exact_count_within_limit_allows_provider_call(self) -> None:
        with patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.ensure_within_max_input_tokens",
            return_value=TokenCountResult(total_tokens=23),
        ), patch(
            "backend.models.google_ai_studio.gemini_embedding_2_preview.embed_content",
            return_value=GoogleEmbeddingResponse(vector=[1.0] * self.profile.dimensions, billable_character_count=4),
        ) as embed_content_mock:
            result = self.provider.embed_query(
                credential=self.credential,
                profile=self.profile,
                query="tiny",
            )

        self.assertEqual(result.vector, [1.0] * self.profile.dimensions)
        self.assertEqual(embed_content_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
