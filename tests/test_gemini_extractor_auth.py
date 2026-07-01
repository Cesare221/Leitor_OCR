from __future__ import annotations

import unittest
from unittest import mock
import requests


class GeminiAuthTests(unittest.TestCase):
    def test_uses_api_key_endpoint_when_key_is_configured(self) -> None:
        import gemini_extractor

        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "HEADER|M|C|T|D"}]}}]
        }

        with mock.patch.object(gemini_extractor, "API_KEY", "abc123"), \
            mock.patch.object(gemini_extractor._HTTP, "post", return_value=fake_response) as post_mock:
            gemini_extractor._call_gemini(b"img", "prompt", mime_type="image/jpeg")

        args, kwargs = post_mock.call_args
        self.assertIn("generativelanguage.googleapis.com", args[0])
        self.assertIn("key=abc123", args[0])
        self.assertEqual("user", kwargs["json"]["contents"][0]["role"])
        self.assertEqual("image/jpeg", kwargs["json"]["contents"][0]["parts"][0]["inlineData"]["mimeType"])
        self.assertEqual({"Content-Type": "application/json"}, kwargs["headers"])

    def test_uses_vertex_ai_with_bearer_token_when_api_key_is_missing(self) -> None:
        import gemini_extractor

        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "HEADER|M|C|T|D"}]}}]
        }

        with mock.patch.object(gemini_extractor, "API_KEY", ""), \
            mock.patch.object(gemini_extractor, "_get_access_token", return_value="token-xyz"), \
            mock.patch.object(gemini_extractor._HTTP, "post", return_value=fake_response) as post_mock:
            gemini_extractor._call_gemini(b"img", "prompt", mime_type="image/png")

        args, kwargs = post_mock.call_args
        self.assertIn("aiplatform.googleapis.com", args[0])
        self.assertIn("/projects/listreader/locations/southamerica-east1/publishers/google/models/", args[0])
        self.assertEqual("user", kwargs["json"]["contents"][0]["role"])
        self.assertEqual("Bearer token-xyz", kwargs["headers"]["Authorization"])
        self.assertEqual("image/png", kwargs["json"]["contents"][0]["parts"][0]["inlineData"]["mimeType"])

    def test_warmup_marks_fast_model_unavailable_on_404(self) -> None:
        import gemini_extractor

        fake_probe = mock.Mock()
        fake_probe.status_code = 404
        fake_probe.text = "not found"

        with mock.patch.object(gemini_extractor, "API_KEY", "abc123"), \
            mock.patch.object(gemini_extractor, "FAST_MODEL", "gemini-2.0-flash-lite"), \
            mock.patch.object(gemini_extractor, "STRONG_MODEL", "gemini-2.5-flash"), \
            mock.patch.object(gemini_extractor._HTTP, "post", return_value=fake_probe), \
            mock.patch.object(gemini_extractor, "_FAST_MODEL_AVAILABLE", None):
            warmup = gemini_extractor.warmup_gemini_runtime(timeout_seconds=5)

        self.assertEqual(False, warmup["fast_model_available"])
        self.assertTrue(warmup["token_ready"])

    def test_warmup_keeps_fast_model_on_transient_probe_error(self) -> None:
        import gemini_extractor

        with mock.patch.object(gemini_extractor, "API_KEY", "abc123"), \
            mock.patch.object(gemini_extractor, "FAST_MODEL", "gemini-2.0-flash-lite"), \
            mock.patch.object(gemini_extractor, "STRONG_MODEL", "gemini-2.5-flash"), \
            mock.patch.object(gemini_extractor._HTTP, "post", side_effect=RuntimeError("network down")), \
            mock.patch.object(gemini_extractor, "_FAST_MODEL_AVAILABLE", None):
            warmup = gemini_extractor.warmup_gemini_runtime(timeout_seconds=5)

        self.assertEqual(True, warmup["fast_model_available"])
        self.assertIn("probe:", warmup["error"])

    def test_call_gemini_retries_on_network_timeout(self) -> None:
        import gemini_extractor

        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "HEADER|M|C|T|D"}]}}]
        }

        with mock.patch.object(gemini_extractor, "API_KEY", "abc123"), \
            mock.patch.object(
                gemini_extractor._HTTP,
                "post",
                side_effect=[requests.exceptions.ReadTimeout("timeout"), fake_response],
            ):
            text, _model = gemini_extractor._call_gemini(
                b"img",
                "prompt",
                mime_type="image/jpeg",
                retries=1,
                timeout_seconds=10,
            )

        self.assertIn("HEADER|M|C|T|D", text)


if __name__ == "__main__":
    unittest.main()
