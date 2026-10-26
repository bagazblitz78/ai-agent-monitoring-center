"""Tests for the file-changes listing API (M3, Task 8).

The rows are seeded directly (no watcher runs here): this is a query-surface
test — pagination maths, filters, project scoping and the envelope shape.
"""

from __future__ import annotations

import pytest
from app.models import FileChange, Project
from app.models.base import new_id
from app.services.paths import path_key_for

PROJECTS = "/api/v1/projects"


def _url(project_id: str) -> str:
    return f"{PROJECTS}/{project_id}/file-changes"


@pytest.fixture
def projects(session, tmp_path) -> tuple[Project, Project]:
    """Two registered projects (neither is watched by these tests)."""
    rows = []
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        rows.append(
            Project(
                name=name,
                path=str(root),
                path_key=path_key_for(root),
                is_git=False,
            )
        )
    session.add_all(rows)
    session.commit()
    return rows[0], rows[1]


def _seed(session, project: Project, *, count: int, start: int = 0, **overrides) -> list[str]:
    """Insert ``count`` rows with strictly increasing timestamps; returns their ids."""
    ids: list[str] = []
    for i in range(count):
        row_id = new_id()
        ids.append(row_id)
        session.add(
            FileChange(
                id=row_id,
                ts=f"2026-10-26T10:00:{start + i:02d}.000Z",
                project_id=project.id,
                path_relative=overrides.get("path_relative", f"file{i}.txt"),
                action=overrides.get("action", "modified"),
                size_bytes=overrides.get("size_bytes", 10),
                git_status=overrides.get("git_status"),
                source=overrides.get("source", "watchdog"),
            )
        )
    session.commit()
    return ids


def _get(client, project_id: str, **params):
    return client.get(_url(project_id), params=params or None)


# --- shape and ordering ---------------------------------------------------


def test_items_are_newest_first_with_pagination_meta(client, session, projects):
    alpha, _beta = projects
    ids = _seed(session, alpha, count=5)

    r = _get(client, alpha.id, page=1, page_size=2)

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"data", "meta"}
    assert set(body["meta"]) == {"page", "page_size", "total"}
    assert body["meta"] == {"page": 1, "page_size": 2, "total": 5}
    items = body["data"]["items"]
    assert len(items) == 2
    # newest first: the last seeded row leads
    assert [item["id"] for item in items] == [ids[4], ids[3]]
    assert items[0]["ts"] == "2026-10-26T10:00:04.000Z"

    page3 = _get(client, alpha.id, page=3, page_size=2).json()
    assert [item["id"] for item in page3["data"]["items"]] == [ids[0]]
    assert page3["meta"]["total"] == 5


def test_item_carries_the_documented_fields(client, session, projects):
    alpha, _beta = projects
    _seed(
        session,
        alpha,
        count=1,
        path_relative="src/app.py",
        action="created",
        size_bytes=42,
        git_status="M",
        source="watchdog",
    )

    item = _get(client, alpha.id).json()["data"]["items"][0]

    assert set(item) == {
        "id",
        "ts",
        "path_relative",
        "action",
        "size_bytes",
        "git_status",
        "source",
    }
    assert item["path_relative"] == "src/app.py"
    assert item["action"] == "created"
    assert item["size_bytes"] == 42
    assert item["git_status"] == "M"
    assert item["source"] == "watchdog"


def test_empty_project_returns_empty_items_not_an_error(client, session, projects):
    alpha, _beta = projects

    r = _get(client, alpha.id)

    assert r.status_code == 200
    assert r.json() == {
        "data": {"items": []},
        "meta": {"page": 1, "page_size": 50, "total": 0},
    }


# --- filters --------------------------------------------------------------


def test_action_filter(client, session, projects):
    alpha, _beta = projects
    _seed(session, alpha, count=2, action="created")
    _seed(session, alpha, count=3, action="deleted", start=2)

    body = _get(client, alpha.id, action="deleted").json()

    assert body["meta"]["total"] == 3
    assert {item["action"] for item in body["data"]["items"]} == {"deleted"}
    assert _get(client, alpha.id, action="renamed").json()["meta"]["total"] == 0


def test_path_prefix_filter(client, session, projects):
    alpha, _beta = projects
    _seed(session, alpha, count=1, path_relative="src/app.py")
    _seed(session, alpha, count=1, path_relative="src/deep/mod.py", start=1)
    _seed(session, alpha, count=1, path_relative="tests/app_test.py", start=2)

    body = _get(client, alpha.id, path_prefix="src/").json()

    assert body["meta"]["total"] == 2
    assert {item["path_relative"] for item in body["data"]["items"]} == {
        "src/app.py",
        "src/deep/mod.py",
    }


def test_free_text_query_matches_anywhere_in_the_path(client, session, projects):
    alpha, _beta = projects
    _seed(session, alpha, count=1, path_relative="src/app.py")
    _seed(session, alpha, count=1, path_relative="docs/README.md", start=1)

    body = _get(client, alpha.id, q="APP").json()  # case-insensitive

    assert body["meta"]["total"] == 1
    assert body["data"]["items"][0]["path_relative"] == "src/app.py"


def test_source_filter(client, session, projects):
    alpha, _beta = projects
    _seed(session, alpha, count=2, source="watchdog")
    _seed(session, alpha, count=1, source="git_poll", start=2)

    body = _get(client, alpha.id, source="git_poll").json()

    assert body["meta"]["total"] == 1
    assert body["data"]["items"][0]["source"] == "git_poll"


def test_filters_combine_with_pagination_counts(client, session, projects):
    alpha, _beta = projects
    _seed(session, alpha, count=3, path_relative="src/a.py", action="modified")
    _seed(session, alpha, count=2, path_relative="src/b.py", action="created", start=3)

    body = _get(client, alpha.id, path_prefix="src/", action="created", page_size=2).json()

    assert body["meta"] == {"page": 1, "page_size": 2, "total": 2}


# --- scoping and errors ---------------------------------------------------


def test_another_projects_rows_never_leak(client, session, projects):
    alpha, beta = projects
    mine = _seed(session, alpha, count=3)
    theirs = _seed(session, beta, count=4, start=3)

    body = _get(client, alpha.id).json()

    assert body["meta"]["total"] == 3
    assert {item["id"] for item in body["data"]["items"]} == set(mine)
    assert not set(theirs) & {item["id"] for item in body["data"]["items"]}

    # and the other project's endpoint shows only its own rows
    other = _get(client, beta.id).json()
    assert other["meta"]["total"] == 4
    assert {item["id"] for item in other["data"]["items"]} == set(theirs)


def test_unknown_project_is_404(client, session, projects):
    r = _get(client, "00000000000000000000000000000000")

    assert r.status_code == 404
    error = r.json()["error"]
    assert error["code"] == "not_found"
    assert "project" in error["message"].lower()


def test_page_size_over_the_cap_is_422(client, session, projects):
    alpha, _beta = projects

    r = _get(client, alpha.id, page_size=201)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_page_size_at_the_cap_is_accepted(client, session, projects):
    alpha, _beta = projects
    _seed(session, alpha, count=1)

    assert _get(client, alpha.id, page_size=200).status_code == 200


def test_unauthenticated_request_is_401(client):
    # ``client`` alone carries no session cookie; auth runs before the handler
    # so even an unknown project id must answer 401, never 404.
    r = client.get(_url("0" * 32))

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_archived_project_is_still_readable(client, session, projects):
    alpha, _beta = projects
    _seed(session, alpha, count=2)
    alpha.status = "archived"
    session.commit()

    r = _get(client, alpha.id)

    assert r.status_code == 200
    assert r.json()["meta"]["total"] == 2
