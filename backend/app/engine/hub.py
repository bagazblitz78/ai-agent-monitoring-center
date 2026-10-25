"""In-process pub/sub hub fanning activity messages out to SSE subscribers.

The hub is deliberately simple: one bounded history deque plus one queue per
subscriber. It is a module-level singleton (``hub``) so any producer can
``await hub.publish(message)`` without wiring, and the stream endpoint
subscribes/unsubscribes around each connected client.

All methods must be called from the event loop that runs the SSE streams —
this is a loop-local broadcast primitive, not a thread-safe broker.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

DEFAULT_MAX_HISTORY = 500


class EventHub:
    """Broadcast dict messages to every subscriber, keeping a bounded history.

    ``publish`` stamps each message with a monotonically increasing
    ``hub_seq`` so consumers can order messages they receive, appends the
    stamped message to a ``deque(maxlen=max_history)`` and fans it out to
    every subscriber queue.
    """

    def __init__(self, max_history: int = DEFAULT_MAX_HISTORY) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=max_history)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._hub_seq = 0

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Return a fresh queue that will receive every subsequent publish."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Stop delivering to ``queue``; safe to call twice for one queue."""
        self._subscribers.discard(queue)

    async def publish(self, message: dict[str, Any]) -> int:
        """Stamp, record and fan out ``message``; return its ``hub_seq``."""
        self._hub_seq += 1
        stamped = {**message, "hub_seq": self._hub_seq}
        self._history.append(stamped)
        for queue in tuple(self._subscribers):
            queue.put_nowait(stamped)
        return self._hub_seq

    def subscriber_count(self) -> int:
        """How many SSE streams are currently subscribed."""
        return len(self._subscribers)


# The singleton every producer publishes to and the stream endpoint reads from.
hub = EventHub()
