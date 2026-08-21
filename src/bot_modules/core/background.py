"""Scaffolding for the bot's long-running background loops.

A service loop is the same five lines everywhere: wait for the gateway, then
tick until the bot closes, surviving anything a tick throws. Getting those
five lines wrong is quiet and expensive — a loop that lets an exception
escape simply stops, and nothing announces that coin drops or intake nudges
died three days ago.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

DEFAULT_LOG = logging.getLogger(__name__)


async def run_forever(
    bot: Any,
    *,
    tick: Callable[[], Awaitable[Any]],
    interval: float,
    label: str,
    logger: logging.Logger | None = None,
) -> None:
    """Await ``tick()`` every ``interval`` seconds until the bot closes.

    The contract each caller was hand-rolling:

    * wait for the gateway before the first tick, so the tick can rely on
      the cache being warm;
    * re-raise ``CancelledError`` — shutdown cancels these tasks, and
      swallowing it would hang the close;
    * swallow everything else, logged with a traceback, because one bad
      tick must not end the loop for good;
    * sleep *after* the tick, including after a failed one, so a tick that
      throws instantly can't spin the event loop.

    ``logger`` defaults to this module's, but callers pass their own so the
    traceback still files under the service that owns the loop rather than
    under the scaffolding.
    """
    log = logger or DEFAULT_LOG
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s tick crashed", label)
        await asyncio.sleep(interval)
