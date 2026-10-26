"""Tests for the event persistence helper (M3, Task 5, R-3.5).

``persist_event`` is the single write path for activity events: one row keyed by
a deterministic ``event_uid`` (so a duplicate is a no-op, never a crash) plus a
hub publish so connected SSE clients see it immediately.
"""

from __future__ import annotations

import asyncio
import json

from app import db
from app.engine.events import persist_event
from app.engine.hub import hub
from app.models import ActivityEvent
from sqlalchemy import func, select

NOW = 1_700_000_000.0  # fixed dedupe bucket so the tests are time-independent


def _count(session) -> int:
    return session.execute(select(func.count(ActivityEvent.seq))).scalar_one()


def _rows(session) -> list[ActivityEvent]:
    session.expire_all()
    return list(session.execute(select(ActivityEvent).order_by(ActivityEvent.seq)).scalars())


def test_persist_event_inserts_row_and_returns_seq(session):
    seq = persist_event(
        db.new_session,
        type="file.changed",
        source="watchdog",
        message="created: a.txt",
        payload={"path": "a.txt", "action": "created"},
        now=NOW,
    )

    assert isinstance(seq, int) and seq >= 1
    rows = _rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.seq == seq
    assert row.type == "file.changed"
    assert row.source == "watchdog"
    assert row.message == "created: a.txt"
    assert row.severity == "info"
    assert row.ts.endswith("Z")
    assert json.loads(row.payload_json) == {"path": "a.txt", "action": "created"}


def test_duplicate_event_uid_is_a_noop(session):
    kwargs = {
        "type": "file.changed",
        "source": "watchdog",
        "payload": {"path": "a.txt", "action": "created"},
        "event_uid": "fixed-uid-for-this-test",
    }

    first = persist_event(db.new_session, **kwargs)
    second = persist_event(db.new_session, **kwargs)

    assert second == first
    assert _count(session) == 1


def test_default_uid_dedupes_within_the_same_second(session):
    kwargs = {
        "type": "file.changed",
        "source": "watchdog",
        "payload": {"path": "a.txt", "action": "created"},
        "now": NOW,
    }

    persist_event(db.new_session, **kwargs)
    persist_event(db.new_session, **kwargs)

    assert _count(session) == 1


def test_different_payloads_get_different_rows(session):
    persist_event(
        db.new_session,
        type="file.changed",
        source="watchdog",
        payload={"path": "a.txt", "action": "created"},
        now=NOW,
    )
    persist_event(
        db.new_session,
        type="file.changed",
        source="watchdog",
        payload={"path": "b.txt", "action": "created"},
        now=NOW,
    )

    assert _count(session) == 2


def test_event_uid_is_a_sha1_hex_digest(session):
    seq = persist_event(
        db.new_session, type="git.commit", source="git_poll", payload={"sha": "abc"}, now=NOW
    )

    row = session.get(ActivityEvent, seq)
    assert row is not None
    assert len(row.event_uid) == 40
    assert set(row.event_uid) <= set("0123456789abcdef")


def test_scope_ids_are_persisted(session):
    seq = persist_event(
        db.new_session,
        type="agent.match_ambiguous",
        source="process_monitor",
        project_id=None,
        agent_id=None,
        payload={"state": "needs_confirmation"},
        now=NOW,
    )

    row = session.get(ActivityEvent, seq)
    assert row is not None
    assert row.project_id is None
    assert row.agent_id is None


async def test_persist_event_publishes_to_a_subscribed_client(session):
    queue = hub.subscribe()
    try:
        seq = persist_event(
            db.new_session,
            type="file.changed",
            source="watchdog",
            payload={"path": "live.txt", "action": "created"},
            now=NOW,
        )
        await asyncio.sleep(0.05)

        assert not queue.empty(), "the hub publish never reached the subscriber"
        message = queue.get_nowait()
        assert message["seq"] == seq
        assert message["type"] == "file.changed"
        assert message["payload"] == {"path": "live.txt", "action": "created"}
        assert "hub_seq" in message
    finally:
        hub.unsubscribe(queue)
