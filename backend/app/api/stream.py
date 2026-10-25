"""SSE event stream: replay persisted activity events, then follow the hub.

``GET /api/v1/stream`` first replays rows from ``activity_events`` where
``seq > since_seq`` (ascending, capped at 501 probed rows) so a client can
resume from its last cursor, then — unless ``replay_only=true`` — stays open
forwarding messages published to :mod:`app.engine.hub`, with a comment-line
ping after every 15 s of silence. The client disconnect closes the stream:
Starlette cancels the generator, whose ``finally`` releases the subscription.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from app.api.deps import get_db
from app.engine.hub import hub
from app.models import ActivityEvent
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

DbSession = Annotated[Session, Depends(get_db)]

REPLAY_LIMIT = 501  # probe one row past the 500-row replay cap to detect overflow
OVERFLOW_FRAME = 'event: resync\ndata: {"reason":"overflow"}\n\n'
PING_FRAME = ": ping\n\n"
PING_INTERVAL_S = 15.0
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _row_projection(row: ActivityEvent) -> dict[str, Any]:
    """The documented per-row SSE payload, with ``payload_json`` decoded."""
    return {
        "seq": row.seq,
        "type": row.type,
        "source": row.source,
        "severity": row.severity,
        "message": row.message,
        "ts": row.ts,
        "project_id": row.project_id,
        "agent_id": row.agent_id,
        "task_id": row.task_id,
        "milestone_id": row.milestone_id,
        "payload": json.loads(row.payload_json),
    }


def _activity_frame(body: dict[str, Any]) -> str:
    """Render one ``event: activity`` frame terminated by a blank line."""
    return f"event: activity\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"


def _replay_frame(body: dict[str, Any]) -> str:
    """Render one replayed row: ``id: <seq>`` first so the cursor is explicit."""
    return f"id: {body['seq']}\n{_activity_frame(body)}"


async def _stream_body(frames: list[str], replay_only: bool) -> AsyncIterator[str]:
    """Yield the replay frames, then live hub messages until the client leaves.

    The hub subscription is made lazily (after replay) and released in the
    ``finally`` on every exit path — normal return, client disconnect (which
    cancels this generator through Starlette's ``listen_for_disconnect``) or
    an exception.
    """
    for frame in frames:
        yield frame
    if replay_only:
        return
    queue = hub.subscribe()
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL_S)
            except TimeoutError:
                # idle comment line keeps intermediaries and the client honest
                yield PING_FRAME
                continue
            yield _activity_frame(message)
    finally:
        hub.unsubscribe(queue)


async def stream(
    session: DbSession,
    since_seq: int = Query(0, ge=0),
    replay_only: bool = Query(False),
) -> StreamingResponse:
    """``GET /api/v1/stream`` — replay persisted events, then follow the hub.

    More than 500 pending rows is more than the client should replay: it gets
    a single ``event: resync`` frame instead of a potentially huge body.
    """
    rows = session.execute(
        select(ActivityEvent)
        .where(ActivityEvent.seq > since_seq)
        .order_by(ActivityEvent.seq)
        .limit(REPLAY_LIMIT)
    ).scalars().all()
    if len(rows) >= REPLAY_LIMIT:
        frames = [OVERFLOW_FRAME]
    else:
        frames = [_replay_frame(_row_projection(row)) for row in rows]
    return StreamingResponse(
        _stream_body(frames, replay_only),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )


def install_stream(router: APIRouter) -> None:
    """Register the stream route on the guarded API router.

    Added through ``add_api_route`` (like the settings routes) so the router's
    ``require_session`` dependency is baked into the route and the security
    sweep sees it.
    """
    router.add_api_route("/api/v1/stream", stream, methods=["GET"])
