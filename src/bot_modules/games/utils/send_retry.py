"""Retry a Discord call that failed for a reason likely to be gone in a second.

Discord's edge occasionally returns a 5xx that is over almost as soon as it
happens. discord.py retries some of those for us, but its unconditional-retry
set is ``{500, 502, 504, 524}`` (``discord/http.py:765``) — **503 is not in
it**. So a 503 comes straight out of ``channel.send`` with no attempt at all,
and a game loop that was only sending its next phase card dies of it. That is
what killed Clapback game 959cd749 four rounds into a five-round game on
2026-09-10, with this from the edge:

    503 Service Unavailable (error code: 0): upstream connect error or
    disconnect/reset before headers.

Two things live here, and the split matters:

* :func:`is_transient` — the *classification*. It is the same question a
  caller's crash handler asks after the retries are spent ("was that Discord
  having a bad second, or is my game broken?"), so both sides read it from
  one place rather than keeping two lists of status codes that drift.
* :func:`retry_transient` — a small bounded ladder around one awaitable.

**On duplicates.** Retrying a send can post the message twice if Discord
accepted the first attempt but lost the response on the way back. discord.py
already takes that bet unconditionally for the four statuses above, and the
observed failure here ("reset before headers") is one where the request never
reached Discord at all. A duplicated vote card is the worst case; a game
destroyed mid-round is the thing we are trading it against.

Deliberately *not* retried: anything under 500. A 403 means the bot cannot
post in that channel and a 404 means the thing is gone — neither improves on
the second try, and sleeping on them parks a game loop for seconds to learn
nothing. 429 is a rate limit, which discord.py handles inside the HTTP layer
before it ever reaches us.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiohttp
import discord

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Attempts in total, not retries after the first — 3 means try, wait 1s, try,
#: wait 2s, try. Three seconds of patience against losing a whole game.
DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 1.0

SleepFn = Callable[[float], Awaitable[None]]

#: Failures that never carry an HTTP status because the connection itself went
#: away. ``ConnectionError`` covers the builtin reset/abort family.
_TRANSPORT_ERRORS = (
    aiohttp.ClientConnectionError,
    asyncio.TimeoutError,
    ConnectionError,
)


def is_transient(exc: BaseException) -> bool:
    """Is *exc* worth trying again, or is it telling us something real?

    True for a 5xx from Discord and for a connection that died before it got
    a status; False for every 4xx (permissions, deleted messages, rate
    limits) and for anything that isn't an HTTP failure at all — a
    ``KeyError`` in game logic is not fixed by running the same code again.
    """
    if isinstance(exc, discord.DiscordServerError):
        return True
    if isinstance(exc, discord.HTTPException):
        return exc.status >= 500
    return isinstance(exc, _TRANSPORT_ERRORS)


async def retry_transient(
    op: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    sleep: SleepFn = asyncio.sleep,
    label: str = "discord call",
) -> T:
    """Await ``op()``, retrying a transient failure with a doubling backoff.

    Re-raises the last failure once *attempts* are spent, so the caller still
    sees the real error (and can classify it with :func:`is_transient`) rather
    than a wrapper that has thrown the status away. A non-transient failure is
    re-raised immediately, without sleeping.

    *op* is a zero-argument coroutine factory — ``lambda: channel.send(...)``
    — so the call is rebuilt on each attempt; an already-awaited coroutine
    could not be retried. *sleep* is injected so tests can assert the backoff
    without waiting for it.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except Exception as exc:
            if not is_transient(exc) or attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            log.warning(
                "%s failed transiently (%s); retrying in %ss (attempt %d/%d)",
                label, exc, delay, attempt, attempts,
            )
            await sleep(delay)
    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover
