"""Google AI Studio provider error translator tests."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from backend.models.google_ai_studio.errors import parse_google_ai_studio_api_error


@dataclass(slots=True)
class _FakeResponse:
    headers: dict[str, str]


@dataclass(slots=True)
class _FakeGoogleError:
    code: int
    message: str
    response: _FakeResponse | None = None


class GoogleAIStudioErrorTests(unittest.TestCase):
    def test_requests_per_day_maps_to_rpd(self) -> None:
        error = _FakeGoogleError(code=429, message="REQUESTS_PER_DAY quota exhausted")

        parsed = parse_google_ai_studio_api_error(error)

        self.assertEqual(parsed.rate_limit_type, "rpd")
        self.assertEqual(parsed.rate_limit_scope, "model")
        self.assertTrue(parsed.retryable)

    def test_tokens_per_minute_maps_to_tpm(self) -> None:
        error = _FakeGoogleError(code=429, message="TOKENS_PER_MINUTE quota exhausted")

        parsed = parse_google_ai_studio_api_error(error)

        self.assertEqual(parsed.rate_limit_type, "tpm")

    def test_unknown_429_defaults_to_model_rpm_with_retry_after(self) -> None:
        error = _FakeGoogleError(
            code=429,
            message="Too many requests",
            response=_FakeResponse(headers={"Retry-After": "2.5"}),
        )

        parsed = parse_google_ai_studio_api_error(error)

        self.assertEqual(parsed.rate_limit_type, "rpm")
        self.assertEqual(parsed.rate_limit_scope, "model")
        self.assertEqual(parsed.retry_after_seconds, 2)

    def test_project_quota_widens_scope(self) -> None:
        error = _FakeGoogleError(code=429, message="Project quota exceeded")

        parsed = parse_google_ai_studio_api_error(error)

        self.assertEqual(parsed.rate_limit_type, "rpm")
        self.assertEqual(parsed.rate_limit_scope, "project")

    def test_server_error_is_retryable_without_rate_limit(self) -> None:
        error = _FakeGoogleError(code=503, message="Service unavailable")

        parsed = parse_google_ai_studio_api_error(error)

        self.assertTrue(parsed.retryable)
        self.assertIsNone(parsed.rate_limit_type)


if __name__ == "__main__":
    unittest.main()
