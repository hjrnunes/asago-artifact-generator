import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from asago_artifact_generator import llm


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"ok": true}'),
                )
            ]
        )


class _FakeClient:
    def __init__(self):
        self.completions = _FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


class TestLlmJson(unittest.TestCase):
    def test_omits_max_tokens_when_not_configured(self):
        client = _FakeClient()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REDTEAM_MAX_TOKENS", None)
            os.environ.pop("REDTEAM_MAX_COMPLETION_TOKENS", None)
            with patch.object(llm, "get_client", return_value=client):
                result = llm.llm_json("prompt", "system")

        self.assertEqual(result, {"ok": True})
        self.assertNotIn("max_tokens", client.completions.kwargs)
        self.assertNotIn("max_completion_tokens", client.completions.kwargs)

    def test_passes_configured_max_tokens(self):
        client = _FakeClient()
        with patch.dict(os.environ, {"REDTEAM_MAX_TOKENS": "16000"}):
            os.environ.pop("REDTEAM_MAX_COMPLETION_TOKENS", None)
            with patch.object(llm, "get_client", return_value=client):
                result = llm.llm_json("prompt", "system")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.completions.kwargs["max_tokens"], 16000)

    def test_passes_configured_max_completion_tokens(self):
        client = _FakeClient()
        with patch.dict(os.environ, {"REDTEAM_MAX_COMPLETION_TOKENS": "16000"}):
            os.environ.pop("REDTEAM_MAX_TOKENS", None)
            with patch.object(llm, "get_client", return_value=client):
                result = llm.llm_json("prompt", "system")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.completions.kwargs["max_completion_tokens"], 16000)
        self.assertNotIn("max_tokens", client.completions.kwargs)

    def test_rejects_invalid_max_tokens(self):
        client = _FakeClient()
        with patch.dict(os.environ, {"REDTEAM_MAX_TOKENS": "not-a-number"}):
            with patch.object(llm, "get_client", return_value=client):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    llm.llm_json("prompt", "system")

        self.assertIsNone(client.completions.kwargs)

    def test_rejects_both_completion_limit_parameters(self):
        client = _FakeClient()
        env = {
            "REDTEAM_MAX_TOKENS": "16000",
            "REDTEAM_MAX_COMPLETION_TOKENS": "16000",
        }
        with patch.dict(os.environ, env):
            with patch.object(llm, "get_client", return_value=client):
                with self.assertRaisesRegex(ValueError, "only one"):
                    llm.llm_json("prompt", "system")

        self.assertIsNone(client.completions.kwargs)

    def test_rejects_non_positive_max_tokens(self):
        client = _FakeClient()
        with patch.dict(os.environ, {"REDTEAM_MAX_TOKENS": "0"}):
            with patch.object(llm, "get_client", return_value=client):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    llm.llm_json("prompt", "system")

        self.assertIsNone(client.completions.kwargs)

    def test_passes_configured_reasoning_effort(self):
        client = _FakeClient()
        with patch.dict(os.environ, {"REDTEAM_REASONING_EFFORT": "low"}):
            with patch.object(llm, "get_client", return_value=client):
                result = llm.llm_json("prompt", "system")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.completions.kwargs["reasoning_effort"], "low")

    def test_rejects_invalid_reasoning_effort(self):
        client = _FakeClient()
        with patch.dict(os.environ, {"REDTEAM_REASONING_EFFORT": "extreme"}):
            with patch.object(llm, "get_client", return_value=client):
                with self.assertRaisesRegex(ValueError, "one of"):
                    llm.llm_json("prompt", "system")

        self.assertIsNone(client.completions.kwargs)
