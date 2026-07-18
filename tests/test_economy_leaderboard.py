"""Economy leaderboard panel — collector, embed builder, command, loop refresh."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.leaderboard import (
    LeaderboardData,
    QuestLine,
    ROLLING_DAYS,
    build_leaderboard_embed,
    collect_leaderboard_data,
    progress_bar,
)
from bot_modules.services import economy_quests_service as quests_svc
from bot_modules.services.economy_loop import run_guild_leaderboard
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    save_econ_settings,
)
from migrations import apply_migrations_sync
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
CHANNEL_ID = 111
NOW = 1_700_000_000.0


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


def _credit(conn, user_id, amount, *, kind="quest", age_days=0.0):
    conn.execute(
        "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (GUILD_ID, user_id, amount, kind, NOW - age_days * 86400),
    )


def _quest(conn, *, qtype, title, reward=10, reward_xp=0, target=None, active=True):
    qid = quests_svc.create_quest(
        conn,
        GUILD_ID,
        title=title,
        description="",
        qtype=qtype,
        reward=reward,
        signoff=0,
        criteria="",
        starts_at=None,
        ends_at=None,
        rotate_tag="",
        community_target=target,
        created_by=None,
        reward_xp=reward_xp,
    )
    if active:
        quests_svc.set_quest_active(conn, GUILD_ID, qid, True)
    return qid


# ── collector ───────────────────────────────────────────────────────────────


def test_collect_top_earners_window_and_exclusions(db):
    with open_db(db) as conn:
        for uid in range(1, 7):  # six earners, uid == relative rank seed
            _credit(conn, uid, uid * 10)
        _credit(conn, 1, 500, age_days=ROLLING_DAYS + 1)  # outside the window
        _credit(conn, 2, 500, kind="transfer_in")  # moved, not earned
        _credit(conn, 3, -25)  # spending never counts
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    assert len(data.top_earners) == 5  # six candidates, top five kept
    assert data.top_earners[0] == (6, 60)
    assert data.top_earners[-1] == (2, 20)  # transfer_in ignored
    assert all(uid != 1 or amt == 10 for uid, amt in data.top_earners)


def test_collect_quests_and_community_goals(db):
    with open_db(db) as conn:
        _quest(conn, qtype="weekly", title="Weekly one", reward=30, reward_xp=15)
        _quest(conn, qtype="daily", title="Daily one")
        _quest(conn, qtype="monthly", title="Benched", active=False)
        cq = _quest(conn, qtype="community", title="Group goal", target=100)
        quests_svc.set_community_progress(conn, cq, 40, target=100)
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    assert [(q.qtype, q.title) for q in data.quests] == [
        ("daily", "Daily one"),  # cadence order, not insertion order
        ("weekly", "Weekly one"),
    ]
    assert data.quests[1].reward_xp == 15
    goal = data.community[0]
    assert (goal.title, goal.current, goal.target) == ("Group goal", 40, 100)
    assert not goal.completed and not goal.settled


# ── builder ─────────────────────────────────────────────────────────────────


def _names(mapping):
    return lambda uid: mapping.get(uid, f"User {uid}")


def test_embed_ranks_earners_with_names_and_branding():
    settings = EconSettings(currency_plural="Gems", currency_emoji="💎")
    data = LeaderboardData(
        top_earners=[(1, 120), (2, 80)], community=[], quests=[]
    )
    embed = build_leaderboard_embed(
        settings, data, _names({1: "Alice", 2: "Bob"}), now_ts=NOW,
        color=discord.Color(0x123456),
    )

    assert "Gems" in (embed.title or "")
    fields = {f.name: f.value or "" for f in embed.fields}
    top = fields[f"Top earners (last {ROLLING_DAYS} days)"]
    assert top.index("Alice") < top.index("Bob")
    assert "🥇" in top and "🥈" in top and "💎 120" in top
    assert embed.color == discord.Color(0x123456)
    assert embed.footer.text == "Updates hourly"
    assert embed.timestamp is not None


def test_embed_community_bar_and_states():
    from bot_modules.economy.leaderboard import CommunityGoal

    data = LeaderboardData(
        top_earners=[],
        community=[
            CommunityGoal("Half there", 50, 100, completed=False, settled=False),
            CommunityGoal("Done", 10, 10, completed=True, settled=True),
        ],
        quests=[],
    )
    embed = build_leaderboard_embed(EconSettings(), data, _names({}), now_ts=NOW)
    goals = next(f.value for f in embed.fields if "Community goals" in (f.name or ""))
    assert goals is not None
    assert progress_bar(50, 100) in goals
    assert "✅ paid out" in goals


def test_embed_quest_board_lines_and_overflow():
    quests = [
        QuestLine("daily", f"Quest {i}", reward=5, reward_xp=10 if i == 0 else 0)
        for i in range(14)
    ]
    embed = build_leaderboard_embed(
        EconSettings(), LeaderboardData([], [], quests), _names({}), now_ts=NOW
    )
    board = next(f.value for f in embed.fields if f.name == "Quest board")
    assert board is not None
    assert "`Daily` **Quest 0**" in board and "+⭐10xp" in board
    assert "…and 2 more" in board


def test_embed_empty_states_and_personal_blurb():
    embed = build_leaderboard_embed(
        EconSettings(), LeaderboardData([], [], []), _names({}), now_ts=NOW
    )
    fields = {f.name: f.value or "" for f in embed.fields}
    assert "be the first" in fields[f"Top earners (last {ROLLING_DAYS} days)"]
    assert "No quests running" in fields["Quest board"]
    assert not any("Community goals" in (n or "") for n in fields)
    personal = fields["Your progress"]
    assert "/quests" in personal and "/bank wallet" in personal
    assert "only you" in personal


# ── settings round-trip ─────────────────────────────────────────────────────


def test_leaderboard_ids_round_trip(db):
    with open_db(db) as conn:
        save_econ_settings(
            conn,
            GUILD_ID,
            {"leaderboard_channel_id": 123, "leaderboard_message_id": 456},
        )
        settings = load_econ_settings(conn, GUILD_ID)
    assert settings.leaderboard_channel_id == 123
    assert settings.leaderboard_message_id == 456


# ── /bank post-leaderboard ──────────────────────────────────────────────────


@pytest.fixture
def ctx(db):
    return SimpleNamespace(db_path=db, open_db=lambda: open_db(db))


@pytest.fixture(autouse=True)
def _patch_accents():
    with (
        patch(
            "bot_modules.cogs.economy_cog.resolve_accent_color",
            new=AsyncMock(return_value=discord.Color(0x123456)),
        ),
        patch(
            "bot_modules.services.economy_loop.resolve_accent_color",
            new=AsyncMock(return_value=discord.Color(0x123456)),
        ),
    ):
        yield


def _make_cog(ctx):
    from bot_modules.cogs.economy_cog import EconomyCog

    return EconomyCog(MagicMock(), ctx)


def _channel(channel_id: int) -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.mention = f"<#{channel_id}>"
    ch.send = AsyncMock(return_value=MagicMock(id=8888))
    ch.fetch_message = AsyncMock()
    return ch


def _admin() -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = 500
    m.guild_permissions = MagicMock(administrator=True)
    m.roles = []
    return m


def _stored(db) -> tuple[int, int]:
    with open_db(db) as conn:
        s = load_econ_settings(conn, GUILD_ID)
    return s.leaderboard_channel_id, s.leaderboard_message_id


@pytest.mark.asyncio
async def test_post_leaderboard_posts_and_saves_ids(ctx, db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"enabled": True})
        # The command windows on wall-clock time, so seed at real now.
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (GUILD_ID, 42, 99, "quest", time.time()),
        )
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    interaction = fake_interaction(guild=FakeGuild(id=GUILD_ID))
    interaction.user = _admin()
    interaction.channel = channel

    await cog.bank_post_leaderboard.callback(cog, interaction, None)

    embed = channel.send.await_args.kwargs["embed"]
    top = next(f.value for f in embed.fields if "Top earners" in (f.name or ""))
    assert top is not None and "99" in top
    assert "User 42" in top  # not a guild member, known_users empty → fallback
    assert _stored(db) == (CHANNEL_ID, 8888)
    msg = interaction.response.send_message.await_args.args[0]
    assert "refreshes" in msg


@pytest.mark.asyncio
async def test_post_leaderboard_refreshes_in_place(ctx, db):
    with open_db(db) as conn:
        save_econ_settings(
            conn,
            GUILD_ID,
            {
                "enabled": True,
                "leaderboard_channel_id": CHANNEL_ID,
                "leaderboard_message_id": 4444,
            },
        )
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    old = MagicMock()
    old.edit = AsyncMock()
    channel.fetch_message.return_value = old
    interaction = fake_interaction(guild=FakeGuild(id=GUILD_ID))
    interaction.user = _admin()
    interaction.channel = channel

    await cog.bank_post_leaderboard.callback(cog, interaction, None)

    channel.fetch_message.assert_awaited_once_with(4444)
    old.edit.assert_awaited_once()
    channel.send.assert_not_awaited()
    assert _stored(db) == (CHANNEL_ID, 4444)


# ── hourly loop refresh ─────────────────────────────────────────────────────


def _loop_bot(guild) -> MagicMock:
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    return bot


@pytest.mark.asyncio
async def test_loop_refresh_edits_panel_in_place(db):
    channel = _channel(CHANNEL_ID)
    message = MagicMock()
    message.edit = AsyncMock()
    channel.fetch_message.return_value = message
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    with open_db(db) as conn:
        save_econ_settings(
            conn,
            GUILD_ID,
            {
                "enabled": True,
                "leaderboard_channel_id": CHANNEL_ID,
                "leaderboard_message_id": 4444,
            },
        )
        _credit(conn, 42, 30)

    await run_guild_leaderboard(_loop_bot(guild), db, GUILD_ID, NOW)

    channel.fetch_message.assert_awaited_once_with(4444)
    embed = message.edit.await_args.kwargs["embed"]
    top = next(f.value for f in embed.fields if "Top earners" in (f.name or ""))
    assert top is not None and "30" in top


@pytest.mark.asyncio
async def test_loop_refresh_skips_without_panel(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"enabled": True})
    bot = _loop_bot(FakeGuild(id=GUILD_ID))

    await run_guild_leaderboard(bot, db, GUILD_ID, NOW)

    bot.get_guild.assert_not_called()  # bails before touching the gateway


@pytest.mark.asyncio
async def test_loop_refresh_clears_ids_when_panel_deleted(db):
    channel = _channel(CHANNEL_ID)
    channel.fetch_message.side_effect = discord.NotFound(
        MagicMock(status=404), "gone"
    )
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    with open_db(db) as conn:
        save_econ_settings(
            conn,
            GUILD_ID,
            {
                "enabled": True,
                "leaderboard_channel_id": CHANNEL_ID,
                "leaderboard_message_id": 4444,
            },
        )

    await run_guild_leaderboard(_loop_bot(guild), db, GUILD_ID, NOW)

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
    assert settings.leaderboard_channel_id == 0
    assert settings.leaderboard_message_id == 0


@pytest.mark.asyncio
async def test_loop_refresh_keeps_ids_on_transient_error(db):
    channel = _channel(CHANNEL_ID)
    channel.fetch_message.side_effect = discord.HTTPException(
        MagicMock(status=500), "boom"
    )
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    with open_db(db) as conn:
        save_econ_settings(
            conn,
            GUILD_ID,
            {
                "enabled": True,
                "leaderboard_channel_id": CHANNEL_ID,
                "leaderboard_message_id": 4444,
            },
        )

    await run_guild_leaderboard(_loop_bot(guild), db, GUILD_ID, NOW)

    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD_ID)
    assert settings.leaderboard_message_id == 4444  # untouched, retried next tick
