"""Shared fixtures for the offline tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_transport_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry after a transport error from sleeping in tests."""

    from asago_artifact_generator.authoring import transport

    monkeypatch.setattr(transport, "RETRY_DELAY_SECONDS", 0.0, raising=False)
