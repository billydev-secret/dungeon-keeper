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
from unittest.mock import AsyncMock

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
    ns = SimpleNamespace(bot=SimpleNamespace(ctx=SimpleNamespace(open_db=lambda: open_db(db_path))))
    ns._top_pct_payout = MethodType(CasinoCog._top_pct_payout, ns)
    ns._bank_announced_win = MethodType(CasinoCog._bank_announced_win, ns)
    ns._ping_enabled = MethodType(CasinoCog._ping_enabled, ns)
    ns.db_path = db_path
    return ns


def _card() -> discord.Embed:
    embed = discord.Embed(
        title="🎰 Golden Meadow Slots",
        description="Triple clover! Nelli collects 🪙 1,200 Coins.",
        color=0x2ECC71,
    )
    return embed


async def _broadcast(
    cog, channel, payout: int, threshold: int = 500, stake: int = 10,
    min_mult: int = 3, embed: discord.Embed | None = None,
):
    await CasinoCog._send_big_win(
        cog, channel, embed or _card(), guild_id=GUILD_ID, payout=payout,
        settings=svc.CasinoSettings(
            broadcast_min_payout=threshold, broadcast_min_mult=min_mult
        ),
        stake=stake, game_label="Slots",
    )


def _banked(cog) -> list[int]:
    with open_db(cog.db_path) as conn:
        return [
            int(r["payout"])
            for r in conn.execute(
                "SELECT payout FROM casino_win_history WHERE guild_id = ? "
                "ORDER BY id", (GUILD_ID,)
            )
        ]


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
async def test_a_push_posts_nothing_and_banks_nothing(cog):
    """A 2,000-coin blackjack push clears a 500 bar four times over on payout
    alone, and used to headline itself as "🔥 Huge Win"."""
    channel = _Channel()
    await _broadcast(cog, channel, 2000, stake=2000)
    assert channel.sends == []
    assert _banked(cog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("min_mult", "posts"),
    [
        pytest.param(3, False, id="default-3x-keeps-a-2x-win-private"),
        pytest.param(2, True, id="a-guild-dialled-to-2x-posts-it"),
    ],
)
async def test_the_minimum_multiple_dial_is_read_from_the_guilds_settings(
    cog, min_mult, posts
):
    """D6: the broadcast needs a real multiple, and the multiple is the
    guild's dial (Economy → Casino → Broadcast minimum multiple), read
    through the same settings loader every table already uses. The same
    2,000-on-1,000 blackjack win stays private under the default and posts
    — and is banked — where an admin turned the dial down."""
    with open_db(cog.db_path) as conn:
        svc.save_casino_settings(
            conn, GUILD_ID,
            {"broadcast_min_payout": 500, "broadcast_min_mult": min_mult},
        )
        settings = svc.load_casino_settings(conn, GUILD_ID)
    channel = _Channel()
    await CasinoCog._send_big_win(
        cog, channel, _card(), guild_id=GUILD_ID, payout=2000,
        settings=settings, stake=1000, game_label="Blackjack",
    )
    assert bool(channel.sends) is posts
    assert (_banked(cog) == [2000]) is posts


@pytest.mark.asyncio
async def test_a_doubled_push_from_the_button_path_posts_nothing_and_banks_nothing(
    cog, monkeypatch
):
    """casino-131: commit 81507b34 fixed "a push is not a big win" for the
    auto-stand path, but the player-pressed path reused ``base_stake`` —
    right for the Play Again button, wrong for the broadcast. A doubled push
    returns 2× base, so the gate saw payout > stake and announced 💰/🔥 for
    a hand that won nothing, then banked it into the Legendary population.
    Two live instances on 2026-09-01."""
    with open_db(cog.db_path) as conn:
        # max_bet high enough that a 2,000 stake is not a "big bet" show —
        # the show path sleeps 1.4s for the hole-card flip.
        svc.save_casino_settings(
            conn, GUILD_ID, {"broadcast_min_payout": 500, "max_bet": 10_000}
        )
    step = svc.BlackjackStep(
        player=["9♠", "2♦", "K♥"], dealer=["K♣", "J♦", "A♠"],
        stake=2000, doubled=True, outcome="push", payout=2000,
    )
    monkeypatch.setattr(
        svc, "resolve_blackjack_action", lambda conn, gid, hid, uid, act: step
    )
    cog._accent = AsyncMock(return_value=None)
    cog._names = AsyncMock(return_value=lambda uid: "Nelli")
    cog._bj_followups = {}
    after = AsyncMock()
    cog._after_instant = after
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID), user=SimpleNamespace(id=31),
        response=SimpleNamespace(edit_message=AsyncMock()),
    )

    assert await CasinoCog._blackjack_step(cog, interaction, 7, "double")

    kwargs = after.await_args.kwargs
    # The broadcast is judged on the TOTAL stake, both halves of the double…
    assert kwargs["stake"] == 2000
    # …while Play Again keeps offering the base stake the player chose.
    view = interaction.response.edit_message.await_args.kwargs["view"]
    assert view.children[0].custom_id == "casino_again:blackjack:x:1000"
    # And judged so, the push stays off the channel and out of the history.
    channel = _Channel()
    await _broadcast(
        cog, channel, kwargs["payout"], stake=kwargs["stake"],
        embed=kwargs["embed"],
    )
    assert channel.sends == []
    assert _banked(cog) == []


@pytest.mark.asyncio
async def test_each_announcement_banks_exactly_one_row(cog):
    """One row per card, not per settled bet — a five-bet roulette round is
    one announcement, and banking it five times would over-weight multi-bet
    rounds in the percentile."""
    channel = _Channel()
    await _broadcast(cog, channel, 1200)
    await _broadcast(cog, channel, 1500)
    assert _banked(cog) == [1200, 1500]


@pytest.mark.asyncio
async def test_the_current_win_is_not_ranked_against_itself(cog):
    """Banking used to happen in the settle transaction, which commits before
    the broadcast reads — so a payout tying the guild's recent maximum always
    cleared its own mark, and a guild whose wins cluster tightly above the
    floor pinged on every broadcast. The read must come first.
    """
    _bank_wins(cog, [2_000] * svc.PING_MIN_SAMPLE)
    channel = _Channel()
    await _broadcast(cog, channel, 2_000)
    assert channel.sends[0]["content"] == "@here"  # ties the existing mark
    # ...but the row it just banked was not part of that decision.
    assert len(_banked(cog)) == svc.PING_MIN_SAMPLE + 1


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
    _bank_wins(cog, [500] * 200)  # every announced win at the bar: a flat top 3%
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
    _bank_wins(cog, [600] * 100 + [3_000] * 4)
    channel = _Channel()
    await _broadcast(cog, channel, 3_000)
    sent = channel.sends[0]
    assert sent["content"] == "@here"
    assert sent["allowed_mentions"].everyone is True
    assert "view" not in sent or sent["view"] is None


@pytest.mark.asyncio
async def test_the_ping_dial_mutes_the_here_but_keeps_the_broadcast(cog):
    """Billy's ask: the loudest wins should stop shouting in this guild. The
    card still posts — only the ping and its mention allowance go away."""
    with open_db(cog.db_path) as conn:
        svc.save_casino_settings(
            conn, GUILD_ID, {"broadcast_ping_enabled": False}
        )
    _bank_wins(cog, [600] * 100 + [3_000] * 4)  # the same history that pings
    channel = _Channel()
    await _broadcast(cog, channel, 3_000)
    assert len(channel.sends) == 1
    sent = channel.sends[0]
    assert sent["content"] is None
    assert sent["allowed_mentions"].everyone is False
    assert sent["embed"].title == "💎 Legendary Win — Slots"


@pytest.mark.asyncio
async def test_a_failed_ping_dial_read_withholds_the_ping(cog):
    """Fail-safe direction: a config read that hiccups must not ping a room
    whose admin unchecked the dial. Silence is the safe way to be wrong."""
    def _boom():
        raise RuntimeError("db gone")

    cog.bot = SimpleNamespace(ctx=SimpleNamespace(open_db=_boom))
    assert await cog._ping_enabled(GUILD_ID) is False


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

    cog.bot = SimpleNamespace(ctx=SimpleNamespace(open_db=_boom))
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
