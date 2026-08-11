"""Tests for the intake stale-nudge sweep — who gets pinged, and who doesn't.

The nudge is a greeter-role ping, so the bar for sending one is that a greeter
could actually act on it. These cover the states where they can't: a member
still sitting in Discord's membership screening, and one who left unnoticed.

Chat-revive-loop style: hand-rolled bot/guild stubs, a real migrated DB,
explicit ``now``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services import intake_service as svc
from bot_modules.services.intake_loop import run_tick
from tests.db_template import migrated_db

GUILD = 42
NEWCOMER = 7
CHANNEL = 555
HOUR = 3600.0
NOW = 30 * HOUR  # well past the 24h default window


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "intake.db"
    migrated_db(path)
    with open_db(path) as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
    return path


class FakeChannel:
    """Just enough discord.TextChannel for the nudge; isinstance-compatible."""

    __class__ = discord.TextChannel  # type: ignore[assignment]

    def __init__(self) -> None:
        self.id = CHANNEL
        self.name = "greeter-chat"
        self.send = AsyncMock(return_value=SimpleNamespace(id=777))


class FakeGuild:
    def __init__(self, channel: FakeChannel, member: object | None) -> None:
        self.id = GUILD
        self._channel = channel
        self._member = member

    def get_channel(self, cid: int):
        return self._channel if cid == self._channel.id else None

    def get_member(self, uid: int):
        return self._member

    async def fetch_member(self, uid: int):
        if self._member is None:
            raise discord.NotFound(_response(404), "unknown member")
        return self._member


def _member(*, pending: bool) -> SimpleNamespace:
    return SimpleNamespace(id=NEWCOMER, pending=pending)


def _bot(db_path, guild: FakeGuild) -> SimpleNamespace:
    return SimpleNamespace(
        guilds=[guild],
        get_guild=lambda gid: guild if gid == guild.id else None,
        ctx=SimpleNamespace(db_path=db_path, open_db=lambda: open_db(db_path)),
    )


def _response(status: int) -> SimpleNamespace:
    return SimpleNamespace(status=status, reason="stub")


def _stale_card(db_path, user_id: int = NEWCOMER) -> int:
    """A card created at t=0, so any NOW past the window makes it stale.

    ``message_id`` stays 0: the nudge then posts standalone instead of as a
    reply, which keeps the fakes free of message fetching — the reply
    plumbing isn't what these tests are about.
    """
    with open_db(db_path) as conn:
        card_id = svc.create_card(conn, GUILD, user_id, 0.0)
        svc.set_card_message(conn, card_id, CHANNEL, 0)
        return card_id


def _card(db_path, card_id: int):
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT * FROM intake_cards WHERE id = ?", (card_id,)
        ).fetchone()


@pytest.mark.parametrize(
    ("presence", "expected"),
    [
        (svc.PRESENCE_IN, svc.NUDGE_SEND),
        (svc.PRESENCE_SCREENING, svc.NUDGE_SKIP),
        (svc.PRESENCE_GONE, svc.NUDGE_CLOSE_LEFT),
        (svc.PRESENCE_UNKNOWN, svc.NUDGE_SKIP),
    ],
)
def test_nudge_action_per_presence(presence, expected):
    assert svc.nudge_action(presence) == expected


async def test_nudges_a_member_who_is_actually_in(db_path):
    card_id = _stale_card(db_path)
    channel = FakeChannel()
    await run_tick(_bot(db_path, FakeGuild(channel, _member(pending=False))), db_path, NOW)
    channel.send.assert_awaited_once()
    assert _card(db_path, card_id)["nudged_at"] == NOW


async def test_screening_member_is_not_nudged_and_stays_eligible(db_path):
    """Still in membership screening = no roles, no channels, nothing a
    greeter can do — so no ping, and crucially no ``nudged_at`` stamp: the
    card must still be able to ping once accepting makes greeting possible."""
    card_id = _stale_card(db_path)
    channel = FakeChannel()
    guild = FakeGuild(channel, _member(pending=True))
    await run_tick(_bot(db_path, guild), db_path, NOW)
    channel.send.assert_not_awaited()
    row = _card(db_path, card_id)
    assert row["nudged_at"] is None
    assert row["resolved_at"] is None

    # …and once they accept, the same card nudges on a later tick.
    guild._member = _member(pending=False)
    await run_tick(_bot(db_path, guild), db_path, NOW + HOUR)
    channel.send.assert_awaited_once()
    assert _card(db_path, card_id)["nudged_at"] == NOW + HOUR


async def test_departed_member_closes_the_card_instead_of_pinging(db_path):
    """A leave the bot missed (downtime) leaves an orphan card; the sweep
    resolves it as 'left' rather than asking greeters to chase a ghost."""
    card_id = _stale_card(db_path)
    channel = FakeChannel()
    await run_tick(_bot(db_path, FakeGuild(channel, None)), db_path, NOW)
    channel.send.assert_not_awaited()
    row = _card(db_path, card_id)
    assert row["resolution"] == svc.RESOLUTION_LEFT
    # Stamped by close_member_card's own clock, the shared leave path.
    assert row["resolved_at"] is not None
    assert row["nudged_at"] is None


async def test_unresolvable_member_is_left_alone(db_path):
    """Discord wouldn't say (rate limit / outage): don't ping, don't close,
    don't stamp — just re-decide on the next tick."""
    card_id = _stale_card(db_path)
    channel = FakeChannel()
    guild = FakeGuild(channel, None)

    async def _boom(uid: int):
        raise discord.HTTPException(_response(503), "unavailable")

    guild.fetch_member = _boom
    await run_tick(_bot(db_path, guild), db_path, NOW)
    channel.send.assert_not_awaited()
    row = _card(db_path, card_id)
    assert row["nudged_at"] is None
    assert row["resolved_at"] is None
