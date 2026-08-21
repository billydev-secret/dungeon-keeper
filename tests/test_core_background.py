"""The shared background-loop runner.

Four service loops (chat revive, coin drops, greeting watch, intake nudges)
were the same five lines. The lines are worth testing because getting them
wrong is *quiet*: a loop that lets an exception escape simply stops, and
nothing announces that coin drops died three days ago.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from bot_modules.core.background import run_forever


class FakeBot:
    """Closes after ``ticks`` iterations so the loop terminates."""

    def __init__(self, ticks: int):
        self._left = ticks
        self.waited = False

    async def wait_until_ready(self) -> None:
        self.waited = True

    def is_closed(self) -> bool:
        if self._left <= 0:
            return True
        self._left -= 1
        return False


@pytest.fixture
def no_sleep(monkeypatch):
    """Record sleeps instead of taking them."""
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return slept


@pytest.mark.asyncio
async def test_waits_for_the_gateway_before_the_first_tick(no_sleep):
    """The tick reads the member/channel cache, so it can't run cold."""
    order: list[str] = []
    bot = FakeBot(ticks=1)

    async def _ready():
        order.append("ready")

    bot.wait_until_ready = _ready  # type: ignore[method-assign]

    async def tick():
        order.append("tick")

    await run_forever(bot, tick=tick, interval=5, label="x")
    assert order == ["ready", "tick"]


@pytest.mark.asyncio
async def test_ticks_until_the_bot_closes(no_sleep):
    calls = []
    await run_forever(FakeBot(ticks=3), tick=lambda: _count(calls), interval=5, label="x")
    assert len(calls) == 3
    assert no_sleep == [5, 5, 5]


async def _count(calls):
    calls.append(1)


@pytest.mark.asyncio
async def test_a_failing_tick_does_not_end_the_loop(no_sleep, caplog):
    """The whole point. One bad tick must not stop coin drops for good."""
    calls = []

    async def tick():
        calls.append(1)
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        await run_forever(FakeBot(ticks=3), tick=tick, interval=5, label="coin drop")

    assert len(calls) == 3  # kept going
    assert "coin drop tick crashed" in caplog.text
    assert caplog.records[0].exc_info is not None  # with a traceback


@pytest.mark.asyncio
async def test_it_still_sleeps_after_a_failed_tick(no_sleep):
    """Otherwise a tick that throws instantly spins the event loop."""

    async def tick():
        raise RuntimeError("boom")

    await run_forever(FakeBot(ticks=2), tick=tick, interval=30, label="x")
    assert no_sleep == [30, 30]


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed(no_sleep):
    """Shutdown cancels these tasks; eating it would hang the close."""

    async def tick():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_forever(FakeBot(ticks=3), tick=tick, interval=5, label="x")


@pytest.mark.asyncio
async def test_a_closed_bot_never_ticks(no_sleep):
    calls = []
    await run_forever(FakeBot(ticks=0), tick=lambda: _count(calls), interval=5, label="x")
    assert calls == []


@pytest.mark.asyncio
async def test_the_traceback_files_under_the_callers_logger(no_sleep, caplog):
    """Each loop passes its own logger so the crash reads as that service's,
    not as the scaffolding's."""

    async def tick():
        raise RuntimeError("boom")

    mine = logging.getLogger("bot_modules.services.intake_loop")
    with caplog.at_level(logging.ERROR):
        await run_forever(
            FakeBot(ticks=1), tick=tick, interval=5, label="intake nudge", logger=mine
        )
    assert caplog.records[0].name == "bot_modules.services.intake_loop"
