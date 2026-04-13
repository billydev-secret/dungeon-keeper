"""Live log streaming via Server-Sent Events."""

from __future__ import annotations

import asyncio
import collections
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from web.auth import AuthenticatedUser
from web.deps import require_perms

router = APIRouter()

# Ring buffer of recent log lines + set of asyncio queues for live subscribers
_BUFFER_SIZE = 500
_buffer: collections.deque[str] = collections.deque(maxlen=_BUFFER_SIZE)
_subscribers: set[asyncio.Queue[str]] = set()
_formatter = logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)


class _DashboardLogHandler(logging.Handler):
    """Captures log records into the ring buffer and pushes to SSE subscribers."""

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        _buffer.append(line)
        dead: list[asyncio.Queue[str]] = []
        for q in _subscribers:
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)


_handler = _DashboardLogHandler()
_handler.setFormatter(_formatter)
_installed = False


def install_log_handler() -> None:
    """Attach the handler to the root logger. Call once at startup."""
    global _installed
    if _installed:
        return
    root = logging.getLogger()
    root.addHandler(_handler)
    _installed = True


async def _event_stream(request: Request) -> AsyncGenerator[str, None]:
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    _subscribers.add(q)
    try:
        # Send buffered history first
        for line in _buffer:
            yield f"data: {line}\n\n"
        # Then stream live
        while True:
            if await request.is_disconnected():
                break
            try:
                line = await asyncio.wait_for(q.get(), timeout=15)
                yield f"data: {line}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _subscribers.discard(q)


@router.get("/logs/stream")
async def log_stream(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
