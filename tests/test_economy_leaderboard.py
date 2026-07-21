"""Economy leaderboard panel — collector, embed builder, command, loop refresh."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.economy import live_signal
from bot_modules.economy.leaderboard import (
    CommunityGoal,
    FeedLine,
    LeaderboardData,
    Pulse,
    QuestLine,
    ROLLING_DAYS,
    build_leaderboard_embed,
    collect_leaderboard_data,
    progress_bar,
)
from bot_modules.economy.logic import local_day_bounds, local_day_for
from bot_modules.services import economy_quests_service as quests_svc
from bot_modules.services.economy_loop import (
    run_guild_leaderboard,
    run_live_tick,
)
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


@pytest.fixture(autouse=True)
def _fresh_live_signal():
    live_signal.reset()
    yield
    live_signal.reset()


def _today_bounds(conn) -> tuple[float, float]:
    offset = get_tz_offset_hours(conn, GUILD_ID)
    today = local_day_for(NOW, offset)
    return local_day_bounds(today, offset)


def _paid_claim(conn, quest_id, user_id, *, period, created_at):
    conn.execute(
        "INSERT INTO econ_quest_claims "
        "(quest_id, guild_id, user_id, period, state, created_at) "
        "VALUES (?, ?, ?, ?, 'paid', ?)",
        (quest_id, GUILD_ID, user_id, period, created_at),
    )


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


def test_collect_pulse_and_today_deltas(db):
    with open_db(db) as conn:
        _credit(conn, 1, 100, age_days=2)  # in the 7d window, not today
        _credit(conn, 1, 40)  # written at NOW = inside today
        _credit(conn, 2, 30)
        _credit(conn, 3, 500, kind="transfer_in")  # moved, never earned
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    assert data.pulse.coins_today == 70
    assert data.pulse.earners_today == 2
    assert data.today_by_user == {1: 40, 2: 30}
    assert data.top_earners[0] == (1, 140)  # window total unchanged
    assert data.day_roll_ts is not None and data.day_roll_ts > NOW
    assert data.week_roll_ts is not None and data.week_roll_ts > NOW


def test_collect_feed_aggregates_todays_completions(db):
    with open_db(db) as conn:
        hi = _quest(conn, qtype="daily", title="Say hi")
        bump = _quest(conn, qtype="weekly", title="Bump us")
        day_start, _day_end = _today_bounds(conn)
        _paid_claim(conn, hi, 1, period="p1", created_at=day_start + 10)
        _paid_claim(conn, hi, 2, period="p2", created_at=day_start + 30)
        _paid_claim(conn, bump, 3, period="p3", created_at=day_start + 20)
        _paid_claim(conn, hi, 4, period="p4", created_at=day_start - 10)  # yday
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, "
            "created_at) VALUES (?, ?, ?, 'quest_bonus', ?)",
            (GUILD_ID, 1, 5, day_start + 40),
        )
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    assert data.pulse.quests_today == 3  # yesterday's claim excluded
    assert [(f.title, f.count) for f in data.feed] == [
        ("Say hi", 2),  # newest last-completion first
        ("Bump us", 1),
    ]
    assert data.feed[0].last_ts == day_start + 30
    assert data.set_bonuses_today == 1


def test_collect_auto_goal_live_detail(db):
    with open_db(db) as conn:
        qid = quests_svc.create_quest(
            conn,
            GUILD_ID,
            title="Bump week",
            description="",
            qtype="community",
            reward=25,
            signoff=0,
            criteria="",
            starts_at=None,
            ends_at=None,
            rotate_tag="",
            community_target=70,
            created_by=None,
            trigger_kind="message_sent",
        )
        quests_svc.set_quest_active(conn, GUILD_ID, qid, True)
        quests_svc.set_community_progress(conn, qid, 30, target=70)
        conn.executemany(
            "INSERT INTO econ_community_contrib (quest_id, user_id, count) "
            "VALUES (?, ?, ?)",
            [(qid, 1, 20), (qid, 2, 10)],
        )
        offset = get_tz_offset_hours(conn, GUILD_ID)
        today = local_day_for(NOW, offset)
        quests_svc.record_kind_activity(conn, GUILD_ID, 1, "message_sent", today)
        data = collect_leaderboard_data(conn, GUILD_ID, NOW)

    goal = data.community[0]
    assert goal.auto and goal.tiers == 1  # 30/70 crosses the 40% tier (28)
    assert goal.contributors == 2
    assert goal.today_delta == 1
    assert goal.on_track  # 30 done ≥ 90% of the linear-pace expectation
    assert goal.ends_ts == data.week_roll_ts


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
    top = fields[f"🏆 Top earners (last {ROLLING_DAYS} days)"]
    assert top.index("Alice") < top.index("Bob")
    assert "🥇" in top and "🥈" in top and "💎 `120`" in top
    assert embed.color == discord.Color(0x123456)
    assert embed.footer.text is not None and embed.footer.text.startswith("⚡ Live")
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


def test_embed_sections_stack_full_width():
    # Sections stack as full-width fields; the tables live INSIDE each
    # body (fixed-width code cells), not in Discord's inline-field flow.
    data = LeaderboardData(
        top_earners=[(1, 10)],
        community=[CommunityGoal("Goal", 1, 10, completed=False, settled=False)],
        quests=[QuestLine("daily", "Q", 5, 0)],
    )
    embed = build_leaderboard_embed(
        EconSettings(), data, _names({}), now_ts=NOW
    )
    layout = [(f.name, f.inline) for f in embed.fields]
    assert layout == [
        ("📡 Today's pulse", False),
        (f"🏆 Top earners (last {ROLLING_DAYS} days)", False),
        ("🎯 Community goals — everyone gets paid when we hit them", False),
        ("📋 Quest board", False),
        ("📰 Live feed — today", False),
        ("👤 Your progress", False),
    ]
    # (glyph-led section headings; see build_leaderboard_embed)
    # Breathing room: the description and every field but the last end in a
    # zero-width blank line, so each section heading has space above it.
    assert (embed.description or "").endswith("\n\u200b")
    for f in embed.fields[:-1]:
        assert (f.value or "").endswith("\n\u200b"), f.name
    assert not (embed.fields[-1].value or "").endswith("\u200b")


def test_embed_quest_board_summarizes_per_cadence():
    # Members draw personal boards, so the panel shows one summary line per
    # cadence (draw count vs pool, reward range) — never the full pool.
    quests = [
        QuestLine("daily", f"Quest {i}", reward=40 if i == 0 else 5, reward_xp=0)
        for i in range(14)
    ] + [
        QuestLine("weekly", "Solo weekly", reward=30, reward_xp=0),
        QuestLine("monthly", "Big month", reward=100, reward_xp=0),
    ]
    settings = EconSettings(quest_board_daily=3, quest_board_monthly=0)
    embed = build_leaderboard_embed(
        settings, LeaderboardData([], [], quests), _names({}), now_ts=NOW
    )
    board = next(f.value for f in embed.fields if f.name == "📋 Quest board")
    assert board is not None
    assert "`Daily    3 yours · pool 14` 🪙 5–40 each" in board
    # A pool smaller than the configured size clamps to the pool.
    assert "`Weekly   1 yours · pool 1 ` 🪙 30 each" in board
    # A cadence sized 0 is off for this guild — no line at all.
    assert "Monthly" not in board
    # No individual titles leak into the summary.
    assert "Quest 0" not in board and "Solo weekly" not in board
    assert "reshuffle each reset" in board and "/quests" in board


def test_embed_quest_board_lists_event_quests():
    # "Anytime" (event) quests aren't board-drawn — those stay named.
    quests = [
        QuestLine("daily", "Chatter", reward=5, reward_xp=0),
        QuestLine("event", "Secret Santa", reward=25, reward_xp=10),
    ]
    embed = build_leaderboard_embed(
        EconSettings(), LeaderboardData([], [], quests), _names({}), now_ts=NOW
    )
    board = next(f.value for f in embed.fields if f.name == "📋 Quest board")
    assert board is not None
    assert "`Anytime  Secret Santa    ` 🪙 25 +⭐10xp" in board
    assert "Chatter" not in board


def test_embed_empty_states_and_personal_blurb():
    embed = build_leaderboard_embed(
        EconSettings(), LeaderboardData([], [], []), _names({}), now_ts=NOW
    )
    fields = {f.name: f.value or "" for f in embed.fields}
    assert "be the first" in fields[f"🏆 Top earners (last {ROLLING_DAYS} days)"]
    assert "No quests running" in fields["📋 Quest board"]
    assert not any("Community goals" in (n or "") for n in fields)
    personal = fields["👤 Your progress"]
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

    await cog.bank_post_leaderboard.callback(cog, interaction, None)  # pyright: ignore[reportCallIssue]

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

    await cog.bank_post_leaderboard.callback(cog, interaction, None)  # pyright: ignore[reportCallIssue]

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


def test_embed_auto_goal_tier_marker():
    data = LeaderboardData(
        top_earners=[],
        community=[
            # 75/100 on an auto weekly = tiers 1+2 banked mid-run.
            CommunityGoal(
                "Weekly", 75, 100, completed=False, settled=False,
                auto=True, tiers=2,
            ),
        ],
        quests=[],
    )
    embed = build_leaderboard_embed(EconSettings(), data, _names({}), now_ts=NOW)
    goals = next(f.value for f in embed.fields if "Community goals" in (f.name or ""))
    assert goals is not None
    assert "🏁 tier 2/3 secured · next at 100" in goals


# ── live content: pulse, feed, clocks ───────────────────────────────────────


def _fields(embed) -> dict[str, str]:
    return {f.name: f.value or "" for f in embed.fields}


def test_embed_pulse_deltas_feed_and_clocks():
    data = LeaderboardData(
        top_earners=[(1, 120), (2, 80)],
        community=[],
        quests=[],
        pulse=Pulse(coins_today=70, quests_today=3, earners_today=2),
        today_by_user={1: 40},
        feed=(
            FeedLine("Say hi", 3, NOW - 60),
            FeedLine("Bump us", 1, NOW - 600),
        ),
        set_bonuses_today=1,
        day_roll_ts=NOW + 3600,
        week_roll_ts=NOW + 7200,
    )
    embed = build_leaderboard_embed(
        EconSettings(), data, _names({1: "Alice", 2: "Bob"}), now_ts=NOW
    )
    fields = _fields(embed)

    pulse = fields["📡 Today's pulse"]
    assert "`Paid out today " in pulse and "**70**" in pulse
    assert "`Quests done" in pulse and "**3**" in pulse
    assert "`Members earning`" in pulse and "**2**" in pulse
    assert f"`Dailies reset  ` <t:{int(NOW + 3600)}:R>" in pulse
    assert f"`New weeklies   ` <t:{int(NOW + 7200)}:R>" in pulse

    top = fields[f"🏆 Top earners (last {ROLLING_DAYS} days)"]
    # name and amount are fixed-width cells so the columns align
    assert "🥇 `Alice` 🪙 `120` (+40 today)" in top
    assert top.splitlines()[1] == "🥈 `Bob  ` 🪙 ` 80`"  # padded, no delta

    feed = fields["📰 Live feed — today"]
    assert f"✅ `Say hi ` ×3 · <t:{int(NOW - 60)}:R>" in feed
    assert f"✅ `Bump us` ×1 · <t:{int(NOW - 600)}:R>" in feed
    assert "🎁 Full-board bonus paid ×1 today" in feed


def test_embed_quiet_day_empty_states():
    embed = build_leaderboard_embed(
        EconSettings(),
        LeaderboardData([], [], [], day_roll_ts=NOW + 100),
        _names({}),
        now_ts=NOW,
    )
    fields = _fields(embed)
    assert "day is young" in fields["📡 Today's pulse"]
    assert f"<t:{int(NOW + 100)}:R>" in fields["📡 Today's pulse"]
    assert "Quiet so far today" in fields["📰 Live feed — today"]


def test_embed_community_pace_crowd_and_deadline():
    goal = CommunityGoal(
        "Bump week", 30, 70, completed=False, settled=False,
        auto=True, tiers=1, contributors=4, today_delta=12,
        on_track=False, ends_ts=NOW + 5000,
    )
    embed = build_leaderboard_embed(
        EconSettings(),
        LeaderboardData([], [goal], []),
        _names({}),
        now_ts=NOW,
    )
    goals = _fields(embed)["🎯 Community goals — everyone gets paid when we hit them"]
    # ceil(70×0.7) = 49 — float noise must not round the threshold to 50.
    assert "🏁 tier 1/3 secured · next at 49" in goals
    assert "🐢 needs a push" in goals
    assert "👥 4 contributing" in goals and "+12 today" in goals
    assert f"ends <t:{int(NOW + 5000)}:R>" in goals


def test_embed_spotlight_gets_countdown():
    data = LeaderboardData(
        top_earners=[],
        community=[],
        quests=[QuestLine("daily", "Chatter", 5, 0, spotlight=True)],
        spotlight_kind="message_sent",
        spotlight_label="Send messages",
        week_roll_ts=NOW + 7200,
    )
    embed = build_leaderboard_embed(EconSettings(), data, _names({}), now_ts=NOW)
    board = _fields(embed)["📋 Quest board"]
    assert f"pays **double** — until <t:{int(NOW + 7200)}:R>!" in board
    # The banner names the doubled kind; the summary no longer lists titles.
    assert "Chatter" not in board


# ── live signal + debounced refresh loop ────────────────────────────────────


def test_live_signal_debounce_and_requeue():
    live_signal.mark_dirty(GUILD_ID)
    live_signal.mark_dirty(GUILD_ID)  # coalesces
    assert live_signal.take_ready(NOW, 120.0) == [GUILD_ID]
    assert live_signal.take_ready(NOW + 1, 120.0) == []  # clock restarted

    live_signal.mark_dirty(GUILD_ID)
    assert live_signal.take_ready(NOW + 60, 120.0) == []  # still cooling
    assert live_signal.pending_count() == 1  # kept pending, not dropped
    assert live_signal.take_ready(NOW + 121, 120.0) == [GUILD_ID]


def test_apply_credit_marks_guild_dirty(db):
    from bot_modules.services.economy_service import apply_credit

    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, 1, 10, "quest")
    assert live_signal.take_ready(NOW, 0.0) == [GUILD_ID]


def test_community_bump_marks_guild_dirty(db):
    with open_db(db) as conn:
        qid = quests_svc.create_quest(
            conn,
            GUILD_ID,
            title="Bump week",
            description="",
            qtype="community",
            reward=25,
            signoff=0,
            criteria="",
            starts_at=None,
            ends_at=None,
            rotate_tag="",
            community_target=70,
            created_by=None,
            trigger_kind="message_sent",
        )
        quests_svc.set_quest_active(conn, GUILD_ID, qid, True)
        live_signal.reset()
        quests_svc._bump_community_kind(conn, GUILD_ID, "message_sent", 1, None)
    assert live_signal.pending_count() == 1


def test_dashboard_progress_edit_marks_guild_dirty(db):
    with open_db(db) as conn:
        qid = _quest(conn, qtype="community", title="Manual goal", target=100)
        live_signal.reset()
        quests_svc.set_community_progress(conn, qid, 10, target=100)
    assert live_signal.pending_count() == 1


@pytest.mark.asyncio
async def test_live_tick_refreshes_dirty_guild_with_debounce(db):
    channel = _channel(CHANNEL_ID)
    message = MagicMock()
    message.edit = AsyncMock()
    channel.fetch_message.return_value = message
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    bot = _loop_bot(guild)
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
    live_signal.reset()

    await run_live_tick(bot, db, NOW)  # nothing dirty → no edit
    message.edit.assert_not_awaited()

    live_signal.mark_dirty(GUILD_ID)
    await run_live_tick(bot, db, NOW)
    message.edit.assert_awaited_once()

    live_signal.mark_dirty(GUILD_ID)  # burst right after the repaint
    await run_live_tick(bot, db, NOW + 30)
    message.edit.assert_awaited_once()  # debounce holds it back

    await run_live_tick(bot, db, NOW + 130)
    assert message.edit.await_count == 2  # lands once the cooldown is up


@pytest.mark.asyncio
async def test_live_tick_survives_refresh_failure(db):
    channel = _channel(CHANNEL_ID)
    channel.fetch_message.side_effect = RuntimeError("gateway hiccup")
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
    live_signal.reset()
    live_signal.mark_dirty(GUILD_ID)

    await run_live_tick(_loop_bot(guild), db, NOW)  # must not raise

    assert live_signal.pending_count() == 0  # consumed; hourly tick backstops
