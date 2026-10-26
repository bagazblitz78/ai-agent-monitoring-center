"""HTTP surface of lite project discovery (``/api/projects/discover*``).

Discovery is a read-only scan, batch registration is the only thing that
mutates state — and even that must leave every byte inside a candidate
directory untouched. Both routes sit behind the same session-token and
same-origin guards as the rest of the lite API.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

LITE_PATH = Path(__file__).resolve().parents[2] / "agentscope_lite.py"
_spec = importlib.util.spec_from_file_location("agentscope_lite", LITE_PATH)
lite = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lite)

DISCOVER = "/api/projects/discover"
REGISTER = "/api/projects/discover/register"


# -- fixtures ---------------------------------------------------------------

def _git(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Tester"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"],
                   check=True, capture_output=True)
    (path / "seed.txt").write_bytes(b"seed\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"],
                   check=True, capture_output=True)
    return path


@pytest.fixture
def lite_server(tmp_path):
    """A real lite HTTP server on an ephemeral port, monitors not started."""
    app = lite.LiteApp(tmp_path / "data.json")
    server = lite.LiteServer(("127.0.0.1", 0), lite.ApiHandler)
    server.app = app
    server.token = app.token
    server.port = server.server_address[1]
    server.verbose = False
    server.allow_shutdown = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                              daemon=True)
    thread.start()
    yield SimpleNamespace(app=app, token=app.token, base=f"http://127.0.0.1:{server.port}")
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def _post(base: str, path: str, body=None, token: str | None = None, origin: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-AgentScope-Token"] = token
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(
        base + path, data=json.dumps(body if body is not None else {}).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_dir():
            out[rel] = (path.stat().st_mtime_ns, -1)
        else:
            stat = path.stat()
            out[rel] = (stat.st_mtime_ns, stat.st_size)
    return out


# -- discovery --------------------------------------------------------------

def test_discover_needs_the_session_token(lite_server):
    status, payload = _post(lite_server.base, DISCOVER, {"path": "C:\\"})

    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_discover_rejects_a_foreign_origin(lite_server, tmp_path):
    status, payload = _post(lite_server.base, DISCOVER, {"path": str(tmp_path)},
                            token=lite_server.token, origin="http://evil.example")

    assert status == 403
    assert payload["error"]["code"] == "forbidden"


def test_discover_reports_the_walk(lite_server, tmp_path):
    _git(tmp_path / "alpha")
    (tmp_path / "chat" / ".opencode").mkdir(parents=True)

    status, payload = _post(lite_server.base, DISCOVER, {"path": str(tmp_path)},
                            token=lite_server.token)

    assert status == 200
    data = payload["data"]
    assert data["errors"] == []
    assert data["truncated"] is False
    assert data["scanned"] >= 3
    assert [(Path(c["path"]).name, c["reason"], c["already_registered"])
            for c in data["candidates"]] == [("alpha", "git", False),
                                             ("chat", ".opencode/", False)]


def test_discover_uses_the_same_path_errors_as_registration(lite_server, tmp_path):
    status, payload = _post(lite_server.base, DISCOVER, {"path": str(tmp_path / "nope")},
                            token=lite_server.token)

    assert status == 422
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["errors"] == ["not_found"]


def test_discover_marks_paths_already_registered(lite_server, tmp_path):
    alpha = _git(tmp_path / "alpha")
    _git(tmp_path / "beta")
    first = _post(lite_server.base, "/api/projects", {"path": str(alpha)},
                  token=lite_server.token)
    assert first[0] == 200, first

    status, payload = _post(lite_server.base, DISCOVER, {"path": str(tmp_path)},
                            token=lite_server.token)

    assert status == 200
    assert [c["already_registered"] for c in payload["data"]["candidates"]] == [True, False]


# -- batch registration -----------------------------------------------------

def test_register_batch_is_token_guarded(lite_server):
    status, _ = _post(lite_server.base, REGISTER, {"paths": ["C:\\somewhere"]})

    assert status == 401


def test_register_batch_adds_the_good_and_reports_the_bad(lite_server, tmp_path):
    alpha = _git(tmp_path / "alpha")
    missing = tmp_path / "gone"

    status, payload = _post(
        lite_server.base, REGISTER,
        {"paths": [str(alpha), str(missing), str(alpha)]},
        token=lite_server.token,
    )

    assert status == 200
    data = payload["data"]
    assert [p["name"] for p in data["added"]] == ["alpha"]
    assert data["counts"] == {"added": 1, "skipped": 1, "failed": 1}
    assert data["skipped"] == [{"path": str(alpha), "reason": "already_registered"}]
    assert data["failed"] == [{"path": str(missing), "errors": ["not_found"]}]
    registered = [os.path.realpath(p["path"]) for p in lite_server.app.state.projects]
    assert registered == [os.path.realpath(str(alpha))]
    assert os.path.realpath(str(missing)) not in registered


def test_register_batch_never_touches_the_project_it_registers(lite_server, tmp_path):
    alpha = _git(tmp_path / "alpha")
    before = _snapshot(alpha)

    status, _ = _post(lite_server.base, REGISTER, {"paths": [str(alpha)]},
                      token=lite_server.token)

    assert status == 200
    assert _snapshot(alpha) == before


def test_register_batch_demands_a_non_empty_path_list(lite_server):
    for body in ({}, {"paths": "not-a-list"}, {"paths": []}):
        status, payload = _post(lite_server.base, REGISTER, body, token=lite_server.token)

        assert status == 422, body
        assert payload["error"]["errors"] == ["paths_required"], body
