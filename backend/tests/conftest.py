"""Shared fixtures for the backend test suite."""

import warnings

import app.config as config
import pytest

# starlette >= 1.10 prefers the `httpx2` package; falling back to the project's
# `httpx` emits a deprecation warning at import time. Suppress it (before importing
# starlette.testclient below) so the test output stays clean.
warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette\.testclient` is deprecated",
)

from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A freshly built application isolated to ``tmp_path`` as its data home."""
    from app.main import create_app  # local import: keeps collection independent of app.main

    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path))
    monkeypatch.setattr(config, "SETTINGS", None)
    return create_app()


@pytest.fixture
def client(app):
    """A ``TestClient`` for the fixture app; unhandled exceptions become 500 envelopes."""
    return TestClient(app, raise_server_exceptions=False)
