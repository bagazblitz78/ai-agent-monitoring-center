from pathlib import Path

import platformdirs
from app.config import Settings, assert_bind_allowed, ensure_dirs, resolve_paths


def test_resolve_paths_uses_agent_scope_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path))
    p = resolve_paths(Settings())
    assert p.home == tmp_path
    assert p.db == tmp_path / "data" / "agent_scope.db"
    assert p.token == tmp_path / "session.token"


def test_resolve_paths_default_uses_platformdirs(monkeypatch):
    monkeypatch.delenv("AGENTSCOPE_HOME", raising=False)
    p = resolve_paths(Settings())
    assert p.home == Path(platformdirs.user_data_dir("AgentScope"))


def test_ensure_dirs_creates_only_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path))
    p = resolve_paths(Settings())
    ensure_dirs(p)
    for d in (p.data, p.logs, p.spool, p.backups):
        assert d.is_dir() and tmp_path in d.parents


def test_resolve_paths_rejects_relative_home(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_HOME", "relative/dir")
    import pytest
    with pytest.raises(ValueError):
        resolve_paths(Settings())


def test_default_bind_is_loopback_and_non_loopback_refused():
    s = Settings()
    assert s.host == "127.0.0.1"
    assert_bind_allowed(s)            # must not raise
    import pytest
    with pytest.raises(RuntimeError):
        assert_bind_allowed(Settings(host="0.0.0.0"))
    assert_bind_allowed(Settings(host="0.0.0.0", allow_non_localhost=True))
