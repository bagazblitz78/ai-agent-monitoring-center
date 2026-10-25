"""In-process event fan-out for SSE clients (the M2 "engine hub")."""

from __future__ import annotations

from app.engine.hub import EventHub, hub

__all__ = ["EventHub", "hub"]
