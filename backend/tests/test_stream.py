"""Tests for the SSE event stream and the in-process ``EventHub`` that feeds it."""

from __future__ import annotations

import asyncio
import json

from app.engine.hub import EventHub, hub
from app.models import ActivityEvent

TS = "2026-10-25T09:30:00.000Z"


def _event(event_uid: str, **overrides) -> ActivityEvent:
    """An ``ActivityEvent`` with sane defaults; only ``event_uid`` is mandatory."""
    fields = {"event_uid": event_uid, "ts": TS, "type": "system", "source": "system"}
    fields.update(overrides)
    return ActivityEvent(**fields)


def _lines(body: str, prefix: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith(prefix)]


def _data_objects(body: str) -> list[dict]:
    """Decode every ``data:`` line of an SSE body into a Python object."""
    return [json.loads(line[len("data: ") :]) for line in _lines(body, "data: ")]


def test_stream_replay_returns_persisted_events_in_order(client, session):
    session.add_all(
        [
            _event("e1", message="first"),
            _event("e2", message="second"),
            _event("e3", message="third"),
        ]
    )
    session.commit()

    r = client.get("/api/v1/stream", params={"replay_only": "true", "since_seq": 0})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    # one frame per row, each terminated by a blank line, seqs ascending
    assert body.endswith("\n\n")
    assert _lines(body, "id: ") == ["id: 1", "id: 2", "id: 3"]
    assert _lines(body, "event: activity") == ["event: activity"] * 3
    # exact frame layout of the first row: id / event / data
    first_frame = body.split("\n\n")[0]
    assert first_frame.splitlines()[0] == "id: 1"
    assert first_frame.splitlines()[1] == "event: activity"
    # the data payload is the documented row projection, with payload decoded
    assert _data_objects(first_frame) == [
        {
            "seq": 1,
            "type": "system",
            "source": "system",
            "severity": "info",
            "message": "first",
            "ts": TS,
            "project_id": None,
            "agent_id": None,
            "task_id": None,
            "milestone_id": None,
            "payload": {},
        }
    ]
    assert [obj["message"] for obj in _data_objects(body)] == ["first", "second", "third"]


def test_stream_since_seq_filters(client, session):
    session.add_all([_event(f"e{n}") for n in range(1, 4)])
    session.commit()

    r = client.get("/api/v1/stream", params={"replay_only": "true", "since_seq": 2})

    assert r.status_code == 200
    assert _lines(r.text, "id: ") == ["id: 3"]
    assert len(_lines(r.text, "event: activity")) == 1


def test_stream_replay_overflow_emits_resync(client, session):
    session.add_all([_event(f"e{n}") for n in range(501)])
    session.commit()

    r = client.get("/api/v1/stream", params={"replay_only": "true", "since_seq": 0})

    assert r.status_code == 200
    body = r.text
    assert body.startswith('event: resync\ndata: {"reason":"overflow"}\n\n')
    assert "event: activity" not in body
    assert not _lines(body, "id: ")


def test_stream_requires_auth(client):
    r = client.get("/api/v1/stream", params={"replay_only": "true"})

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_stream_body_forwards_published_hub_messages():
    """After replay the generator subscribes to the hub and forwards its messages."""
    from app.api.stream import _stream_body

    body = _stream_body(["id: 7\nevent: activity\ndata: {}\n\n"], replay_only=False)
    try:
        assert await body.__anext__() == "id: 7\nevent: activity\ndata: {}\n\n"

        # second anext subscribes to the hub and then waits for a message
        pending = asyncio.ensure_future(body.__anext__())
        await asyncio.sleep(0)
        await hub.publish({"seq": 8, "type": "agent.status"})
        frame = await asyncio.wait_for(pending, timeout=5)

        assert frame.startswith("event: activity\ndata: ")
        assert frame.endswith("\n\n")
        data = json.loads(frame[len("event: activity\ndata: ") :])
        assert data["seq"] == 8
        assert data["type"] == "agent.status"
        assert isinstance(data["hub_seq"], int)
        assert hub.subscriber_count() == 1
    finally:
        await body.aclose()
    # the subscription is released when the generator exits
    assert hub.subscriber_count() == 0


async def test_hub_publish_reaches_subscriber():
    local_hub = EventHub()
    queue = local_hub.subscribe()
    assert local_hub.subscriber_count() == 1

    hub_seq = await local_hub.publish({"type": "agent.status", "message": "up"})

    assert hub_seq == 1
    message = queue.get_nowait()
    assert message["hub_seq"] == 1
    assert message["type"] == "agent.status"
    assert message["message"] == "up"

    local_hub.unsubscribe(queue)
    assert local_hub.subscriber_count() == 0


async def test_hub_history_is_bounded():
    local_hub = EventHub(max_history=500)

    for n in range(600):
        await local_hub.publish({"n": n})

    # a bounded deque of 500 keeps only the newest entries, oldest evicted first
    assert len(local_hub._history) == 500
    assert local_hub._history[0]["n"] == 100
    assert local_hub._history[-1]["n"] == 599
