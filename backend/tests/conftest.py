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


def _configure_home(tmp_path, monkeypatch, *, monitors: bool) -> None:
    """Point the app at this test's temp home and set the engine switch."""
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path / "agentscope-home"))
    # The flag is read when the app starts, so it has to be set before
    # ``create_app`` builds the settings singleton.
    monkeypatch.setenv("AGENTSCOPE_DISABLE_MONITORS", "0" if monitors else "1")
    monkeypatch.setattr(config, "SETTINGS", None)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A freshly built application isolated to ``tmp_path`` as its data home.

    The home sits in a *subdirectory* so the rest of ``tmp_path`` stays free to
    act as monitored project territory — the path policy refuses to watch
    anything inside ``AGENTSCOPE_HOME``, so a project under the home itself
    would (correctly) be rejected.

    The monitoring engine is **off** here (``AGENTSCOPE_DISABLE_MONITORS``):
    unit tests must never spawn watcher threads behind their back. Tests that
    want a live engine opt in through ``monitored_app`` / ``monitored_client``.
    """
    _configure_home(tmp_path, monkeypatch, monitors=False)
    # Imported after the env is patched: ``app.main`` builds a module-level
    # ``app`` on first import, which must land in the temp home — never the
    # real app data directory. Local import: keeps collection independent.
    from app.main import create_app

    return create_app()


@pytest.fixture
def monitored_app(tmp_path, monkeypatch):
    """The same isolated application with the monitoring engine enabled.

    The schema is created up front and the app is *not* entered yet: its
    lifespan — the thing that starts the monitors — only runs inside a
    ``TestClient`` context (see ``monitored_client``).
    """
    _configure_home(tmp_path, monkeypatch, monitors=True)
    from app import db
    from app.models import Base

    db.reset_engine()
    Base.metadata.create_all(db.engine)
    from app.main import create_app

    built = create_app()
    yield built
    db.reset_engine()


@pytest.fixture
def monitored_client(monitored_app):
    """``monitored_app`` with its lifespan running: monitors live, cookie set."""
    with TestClient(monitored_app, raise_server_exceptions=False) as client:
        client.cookies.set(SESSION_COOKIE, monitored_app.state.session_token)
        yield client


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
