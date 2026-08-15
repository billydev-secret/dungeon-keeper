"""The casino cog's one seam for the public big-win broadcast.

Everything about *what* the broadcast says is pinned at the logic and builder
layers (``tests/test_casino_logic.py``, ``tests/test_casino_embeds.py``). What
can only be checked here is the wiring: that ``channel.send`` is reached with
no view, and with the mention allowances the ping tier needs.

The view is the whole reason this file exists. The broadcast used to carry the
player's own Play Again / Next Round button — the "me too" invitation for
bystanders — and that is exactly the glue being removed, so it gets a wiring
assertion rather than being trusted to the builder's return type.
"""

from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace

import discord
import pytest

from bot_modules.cogs.casino.cog import CasinoCog
from bot_modules.core.db_utils import open_db
from bot_modules.services import casino_service as svc
from tests.db_template import migrated_db

GUILD_ID = 606060
NOW = 1_800_000_000.0


class _Channel:
    """Records what the cog tried to post, including kwargs it never passes."""

    id = 9001

    def __init__(self):
        self.sends: list[dict] = []

    async def send(self, **kwargs):
        self.sends.append(kwargs)


@pytest.fixture()
def cog(tmp_path: Path):
    """A stand-in carrying only what the broadcast path touches."""
    db_path = tmp_path / "casino.db"
    migrated_db(db_path)
    ns = SimpleNamespace(ctx=SimpleNamespace(open_db=lambda: open_db(db_path)))
    ns._top_pct_payout = MethodType(CasinoCog._top_pct_payout, ns)
    ns.db_path = db_path
    return ns


def _card() -> discord.Embed:
    embed = discord.Embed(
        title="🎰 Golden Meadow Slots",
        description="Triple clover! Nelli collects 🪙 1,200 Coins.",
        color=0x2ECC71,
    )
    return embed


async def _broadcast(cog, channel, payout: int, threshold: int = 500):
    await CasinoCog._send_big_win(
        cog, channel, _card(), guild_id=GUILD_ID, payout=payout,
        threshold=threshold, game_label="Slots",
    )


def _bank_wins(cog, payouts):
    with open_db(cog.db_path) as conn:
        for payout in payouts:
            svc.record_win(conn, GUILD_ID, payout, now=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payout", "threshold"),
    [
        pytest.param(499, 500, id="under-the-bar"),
        pytest.param(50_000, 0, id="bar-switched-off"),
    ],
)
async def test_a_win_below_the_bar_posts_nothing(cog, payout, threshold):
    channel = _Channel()
    await _broadcast(cog, channel, payout, threshold)
    assert channel.sends == []


@pytest.mark.asyncio
async def test_the_broadcast_carries_no_view(cog):
    """Billy's call: the Play Again button on the public recap is gone. It was
    deliberate once — a bystander seeing a big win could play on the spot —
    and the channel copy is a recap now, with the buttons only on the player's
    own card."""
    channel = _Channel()
    await _broadcast(cog, channel, 1200)
    assert len(channel.sends) == 1
    sent = channel.sends[0]
    assert "view" not in sent or sent["view"] is None
    assert sent["embed"].title == "💰 Big Win — Slots"


@pytest.mark.asyncio
async def test_an_ordinary_big_win_pings_nobody(cog):
    """Silent is the default. ``AllowedMentions.none()`` is what makes it hold
    even though the result copy renders player names."""
    _bank_wins(cog, range(1, 201))  # top-3% mark ≈ 195, far under 10× the bar
    channel = _Channel()
    await _broadcast(cog, channel, 1200)
    sent = channel.sends[0]
    assert sent["content"] is None
    assert sent["allowed_mentions"].everyone is False


@pytest.mark.asyncio
async def test_a_top_three_percent_win_pings_here(cog):
    """The @here needs both halves to work: the text, and an allowance that
    lets Discord act on it. Sending "@here" under ``AllowedMentions.none()``
    would render the word and notify no one."""
    _bank_wins(cog, [6_000] * 100 + [20_000] * 4)
    channel = _Channel()
    await _broadcast(cog, channel, 20_000)
    sent = channel.sends[0]
    assert sent["content"] == "@here"
    assert sent["allowed_mentions"].everyone is True
    assert "view" not in sent or sent["view"] is None


@pytest.mark.asyncio
async def test_a_thin_win_history_broadcasts_without_pinging(cog):
    """A guild that hasn't banked PING_MIN_SAMPLE wins yet still gets its
    broadcast — it just can't ping. The refusal must not swallow the post."""
    _bank_wins(cog, [10] * (svc.PING_MIN_SAMPLE - 1))
    channel = _Channel()
    await _broadcast(cog, channel, 50_000)
    assert len(channel.sends) == 1
    assert channel.sends[0]["content"] is None


@pytest.mark.asyncio
async def test_a_failed_percentile_read_still_broadcasts(cog):
    """Losing the ping is the right way to fail; losing the whole
    announcement because a lookup hiccuped is not."""
    def _boom():
        raise RuntimeError("db gone")

    cog.ctx = SimpleNamespace(open_db=_boom)
    channel = _Channel()
    await _broadcast(cog, channel, 50_000)
    assert len(channel.sends) == 1
    assert channel.sends[0]["content"] is None


@pytest.mark.asyncio
async def test_a_send_failure_is_logged_not_raised(cog):
    """The broadcast is a nicety posted after the money already settled — it
    must never propagate into the settle path."""
    class _Broken(_Channel):
        async def send(self, **kwargs):
            raise discord.HTTPException(
                SimpleNamespace(status=403, reason="Forbidden"), "nope"
            )

    await _broadcast(cog, _Broken(), 1200)  # does not raise
