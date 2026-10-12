"""In-process event bus for /desk/events SSE stream.

Thread-safe (sources run under APScheduler threads AND under FastAPI async
tasks). Each subscriber gets a bounded queue; emit() fans out non-blocking.
A subscriber that falls behind drops events (full queue) rather than slowing
emission.
"""
from __future__ import annotations

import json
import queue
import time
from threading import Lock
from typing import Any

_LOCK = Lock()
_SUBSCRIBERS: list[queue.Queue] = []
_RUN_ACTIVE: dict[str, Any] = {"active": False, "started_at": None, "label": None}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def emit(event_type: str, **data) -> None:
    """Push an event onto every subscriber's queue. Non-blocking."""
    event = {"type": event_type, "at": _now(), **data}
    with _LOCK:
        subs = list(_SUBSCRIBERS)
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=2000)
    with _LOCK:
        _SUBSCRIBERS.append(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _LOCK:
        if q in _SUBSCRIBERS:
            _SUBSCRIBERS.remove(q)


def try_start(label: str) -> bool:
    """Claim the one-run-at-a-time slot. Returns True if the caller got it."""
    with _LOCK:
        if _RUN_ACTIVE["active"]:
            return False
        _RUN_ACTIVE["active"] = True
        _RUN_ACTIVE["started_at"] = _now()
        _RUN_ACTIVE["label"] = label
    return True


def release() -> None:
    with _LOCK:
        _RUN_ACTIVE["active"] = False
        _RUN_ACTIVE["started_at"] = None
        _RUN_ACTIVE["label"] = None


def is_active() -> dict:
    with _LOCK:
        return dict(_RUN_ACTIVE)


def sse_format(event: dict) -> str:
    etype = event.get("type", "message")
    return f"event: {etype}\ndata: {json.dumps(event, default=str)}\n\n"
