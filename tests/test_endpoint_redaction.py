"""Endpoint-free consumer provider logging regressions."""

from __future__ import annotations

import logging
from unittest.mock import patch

from asago_artifact_generator import llm


def test_client_log_keeps_provider_identity_without_base_url(
    caplog,
) -> None:
    """Client construction logs model accounting fields, not its endpoint."""
    previous = (llm.PROVIDER, llm.BASE_URL, llm.MODEL, llm._client)
    try:
        llm.configure_llm(
            provider="openai",
            base_url="https://private.apps.example/v1",
            api_key="offline-test-key",
            model="offline-model",
        )
        with patch("asago_artifact_generator.llm.OpenAI", return_value=object()):
            with caplog.at_level(logging.INFO, logger="asago_artifact_generator.llm"):
                llm.get_client()

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "provider=openai" in messages
        assert "model=offline-model" in messages
        assert "private.apps.example" not in messages
        assert "base_url=" not in messages
    finally:
        llm.PROVIDER, llm.BASE_URL, llm.MODEL, llm._client = previous
