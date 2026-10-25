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

SESSION_COOKIE = "agentscope_session"


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A freshly built application isolated to ``tmp_path`` as its data home."""
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path))
    monkeypatch.setattr(config, "SETTINGS", None)
    # Imported after the env is patched: ``app.main`` builds a module-level
    # ``app`` on first import, which must land in the temp home — never the
    # real app data directory. Local import: keeps collection independent.
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app):
    """A ``TestClient`` for the fixture app; unhandled exceptions become 500 envelopes."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def session(client):
    """A live DB session on a fresh schema in the fixture app's temp home.

    The client is cookie-authenticated so tests can both insert rows and call
    guarded routes. The lazy engine is reset around the test so it can only
    ever point at this test's temporary ``AGENTSCOPE_HOME`` — never the real
    app data directory.
    """
    from app import db
    from app.models import Base

    db.reset_engine()
    Base.metadata.create_all(db.engine)
    client.cookies.set(SESSION_COOKIE, client.app.state.session_token)
    db_session = db.new_session()
    yield db_session
    db_session.close()
    db.reset_engine()
