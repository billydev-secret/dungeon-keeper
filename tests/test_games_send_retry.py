"""A transient Discord 5xx must not be allowed to end a game.

Clapback game 959cd749 died mid-way through round 5 of 5 on a one-second
edge failure (2026-09-10): ``channel.send`` raised ``DiscordServerError``
(503, "upstream connect error or disconnect/reset before headers"), and
discord.py does not retry 503 — its unconditional-retry set is
``{500, 502, 504, 524}`` (http.py:765), so the error came straight out of the
send with no attempt at all.

These pin the retry ladder and, just as importantly, what it refuses to
retry: a 403 is not going to succeed on the second try, and sleeping on it
would stall a game loop for no reason.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest

from bot_modules.games.utils.send_retry import is_transient, retry_transient


def _http_error(status: int, cls=discord.HTTPException) -> discord.HTTPException:
    """A real discord.py error at *status*, built the way the library does."""
    response = SimpleNamespace(status=status, reason="Test")
    return cls(response, {"code": 0, "message": "boom"})


def _server_error() -> discord.DiscordServerError:
    """The exact shape prod raised."""
    return _http_error(503, discord.DiscordServerError)  # type: ignore[return-value]


class _Sleeper:
    """Stand-in for ``asyncio.sleep`` that records what it was asked to wait."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


# ── Classification ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(_server_error(), True, id="503-server-error"),
        pytest.param(_http_error(500), True, id="500"),
        pytest.param(_http_error(502), True, id="502"),
        pytest.param(_http_error(504), True, id="504"),
        pytest.param(_http_error(403, discord.Forbidden), False, id="403-forbidden"),
        pytest.param(_http_error(404, discord.NotFound), False, id="404-not-found"),
        pytest.param(_http_error(400), False, id="400"),
        pytest.param(_http_error(429), False, id="429-ratelimit"),
        pytest.param(asyncio.TimeoutError(), True, id="timeout"),
        pytest.param(ValueError("a real bug"), False, id="logic-bug"),
        pytest.param(KeyError("scores"), False, id="key-error"),
    ],
)
def test_is_transient_classifies_the_error(exc, expected):
    assert is_transient(exc) is expected


def test_a_connection_reset_is_transient():
    """The edge dropping the connection never reaches an HTTP status."""
    assert is_transient(ConnectionResetError()) is True


# ── The retry ladder ─────────────────────────────────────────────────────


async def test_a_send_that_recovers_on_the_second_try_succeeds():
    calls = []
    sleeper = _Sleeper()

    async def op():
        calls.append(1)
        if len(calls) == 1:
            raise _server_error()
        return "message"

    result = await retry_transient(op, sleep=sleeper)

    assert result == "message"
    assert len(calls) == 2
    # One failure, one nap.
    assert sleeper.delays == [1.0]


async def test_a_send_that_works_first_time_never_sleeps():
    sleeper = _Sleeper()

    async def op():
        return "message"

    assert await retry_transient(op, sleep=sleeper) == "message"
    assert sleeper.delays == []


async def test_the_ladder_backs_off_and_then_gives_up():
    """Three attempts, two naps, and the last error is what the caller sees —
    it carries the status the crash handler classifies on."""
    calls = []
    sleeper = _Sleeper()

    async def op():
        calls.append(1)
        raise _server_error()

    with pytest.raises(discord.DiscordServerError) as caught:
        await retry_transient(op, sleep=sleeper)

    assert len(calls) == 3
    assert sleeper.delays == [1.0, 2.0]
    assert caught.value.status == 503


async def test_attempts_and_base_delay_are_callers_to_set():
    calls = []
    sleeper = _Sleeper()

    async def op():
        calls.append(1)
        raise _server_error()

    with pytest.raises(discord.DiscordServerError):
        await retry_transient(op, attempts=4, base_delay=0.5, sleep=sleeper)

    assert len(calls) == 4
    assert sleeper.delays == [0.5, 1.0, 2.0]


async def test_a_permanent_failure_is_raised_at_once_without_sleeping():
    """A 403 will not come good on the second try, and a game loop must not
    be parked for three seconds discovering that."""
    calls = []
    sleeper = _Sleeper()

    async def op():
        calls.append(1)
        raise _http_error(403, discord.Forbidden)

    with pytest.raises(discord.Forbidden):
        await retry_transient(op, sleep=sleeper)

    assert len(calls) == 1
    assert sleeper.delays == []


async def test_a_logic_bug_is_never_retried():
    """Retrying a KeyError just runs the broken code three times."""
    calls = []
    sleeper = _Sleeper()

    async def op():
        calls.append(1)
        raise KeyError("scores")

    with pytest.raises(KeyError):
        await retry_transient(op, sleep=sleeper)

    assert len(calls) == 1
    assert sleeper.delays == []
