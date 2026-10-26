"""Persist activity events and fan them out to the SSE hub.

M3 owns only this helper (R-3.5): monitors call :func:`persist_event`, which
writes **one** ``ActivityEvent`` row keyed by a deterministic ``event_uid`` and
then publishes the same payload on the hub so connected clients see it live.
M4 replaces the internals (ingest bus, coalescing) without touching callers.

Deduplication is by ``event_uid``: identical event, identical scope, same
one-second bucket → the second call is a no-op that returns the existing ``seq``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from app.engine.hub import hub
from app.models import ActivityEvent
from app.models.base import utc_now
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

#: The event loop the watchers publish on. Set once at application startup so
#: watchdog threads (which have no running loop of their own) can hand the hub
#: a message safely; without it the row is still written and SSE clients pick
#: the event up on their next replay.
_PUBLISH_LOOP: asyncio.AbstractEventLoop | None = None


def set_publish_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Register the loop watcher threads should publish through."""
    global _PUBLISH_LOOP
    _PUBLISH_LOOP = loop


def default_event_uid(
    *,
    type: str,
    payload: dict[str, Any],
    project_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    milestone_id: str | None = None,
    now: float | None = None,
) -> str:
    """sha1 of type + scope + payload key + one-second bucket.

    For file events the payload key is ``path|action`` (the plan's formula); for
    everything else the whole payload, canonically encoded, takes its place so
    two distinct events in the same second never collide.
    """
    epoch = int(time.time() if now is None else now)
    if "path" in payload or "action" in payload:
        key = f"{payload.get('path')}|{payload.get('action')}"
    else:
        key = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    material = "|".join(
        (
            type,
            project_id or "",
            agent_id or "",
            task_id or "",
            milestone_id or "",
            key,
            str(epoch),
        )
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def _publish(message: dict[str, Any]) -> None:
    """Hand ``message`` to the hub from any thread, never blocking the caller."""
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = _PUBLISH_LOOP
    if loop is None or loop.is_closed():
        logger.debug("hub publish skipped: no event loop available for %s", message.get("type"))
        return
    try:
        # Thread-safe from both the loop's own thread and a watcher thread.
        loop.call_soon_threadsafe(lambda: loop.create_task(hub.publish(message)))
    except RuntimeError:  # loop shut down between the check and the call
        logger.debug("hub publish skipped: event loop closed")


def persist_event(
    session_factory: SessionFactory,
    *,
    type: str,
    source: str,
    severity: str = "info",
    message: str = "",
    payload: dict[str, Any] | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    milestone_id: str | None = None,
    event_uid: str | None = None,
    now: float | None = None,
) -> int:
    """Insert one activity event and publish it; return its ``seq``.

    Duplicates (same ``event_uid``) return the ``seq`` of the row that won the
    race and publish nothing. A duplicate primary key is only ever treated as a
    duplicate when the uid really exists — any other integrity failure
    (e.g. a dangling foreign key) still raises.
    """
    body = dict(payload or {})
    uid = event_uid or default_event_uid(
        type=type,
        payload=body,
        project_id=project_id,
        agent_id=agent_id,
        task_id=task_id,
        milestone_id=milestone_id,
        now=now,
    )
    row = ActivityEvent(
        event_uid=uid,
        ts=utc_now(),
        type=type,
        source=source,
        severity=severity,
        message=message,
        payload_json=json.dumps(body, ensure_ascii=False, sort_keys=True, default=str),
        project_id=project_id,
        agent_id=agent_id,
        task_id=task_id,
        milestone_id=milestone_id,
    )

    session = session_factory()
    try:
        session.add(row)
        session.commit()
        # Read the generated values while the session is still open: a factory
        # that expires on commit would leave the detached instance unusable.
        seq = int(row.seq)
        ts = row.ts
    except IntegrityError:
        session.rollback()
        existing = session.execute(
            select(ActivityEvent.seq).where(ActivityEvent.event_uid == uid)
        ).scalar_one_or_none()
        if existing is None:
            raise  # not a duplicate: a real integrity failure, surface it
        return int(existing)
    finally:
        session.close()

    _publish(
        {
            "seq": seq,
            "type": type,
            "source": source,
            "severity": severity,
            "message": message,
            "ts": ts,
            "project_id": project_id,
            "agent_id": agent_id,
            "task_id": task_id,
            "milestone_id": milestone_id,
            "payload": body,
        }
    )
    return seq
