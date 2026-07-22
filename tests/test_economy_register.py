"""Economy register — ledger collector, memo rendering, embeds, drain loop."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.register import (
    CREDIT_COLOUR,
    DEBIT_COLOUR,
    SKIP_KINDS,
    TRANSFER_COLOUR,
    RegisterEntry,
    build_register_embed,
    collect_register_entries,
    render_memo,
)
from bot_modules.services import economy_quests_service as quests_svc
from bot_modules.services.economy_loop import (
    register_tick,
    REGISTER_MAX_PER_TICK,
    REGISTER_STALE_SECONDS,
    run_guild_register,
)
from bot_modules.services.economy_service import (
    DEFAULT_ECON_SETTINGS,
    apply_credit,
    apply_debit,
    load_econ_settings,
    save_econ_settings,
    transfer_currency,
)
from migrations import apply_migrations_sync
from tests.fakes import FakeGuild

GUILD_ID = 9001
CHANNEL_ID = 111
USER_ID = 4242
NOW = 1_700_000_000.0


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


def _row(conn, user_id, amount, *, kind, meta=None, actor_id=None, age=0.0):
    """Insert a ledger row directly, with control over its age."""
    import json

    conn.execute(
        "INSERT INTO econ_ledger "
        "(guild_id, user_id, amount, kind, actor_id, meta, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            GUILD_ID,
            user_id,
            amount,
            kind,
            actor_id,
            json.dumps(meta) if meta else None,
            NOW - age,
        ),
    )
    # Mirror the wallet the same way apply_credit/apply_debit would, in two
    # steps: the table's CHECK (balance >= 0) rejects an opening negative row.
    conn.execute(
        "INSERT OR IGNORE INTO econ_wallets "
        "(guild_id, user_id, balance, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?)",
        (GUILD_ID, user_id, NOW, NOW),
    )
    conn.execute(
        "UPDATE econ_wallets SET balance = balance + ? "
        "WHERE guild_id = ? AND user_id = ?",
        (amount, GUILD_ID, user_id),
    )


def _entry(**overrides) -> RegisterEntry:
    base: dict[str, Any] = dict(
        ledger_id=1,
        user_id=USER_ID,
        amount=50,
        kind="quest",
        actor_id=None,
        meta={},
        created_at=NOW,
        balance_after=100,
    )
    base.update(overrides)
    return RegisterEntry(**base)


def _names(uid: int) -> str:
    return f"User{uid}"


# ── collector ──────────────────────────────────────────────────────────


def test_collect_returns_rows_after_cursor_oldest_first(db):
    with open_db(db) as conn:
        _row(conn, USER_ID, 10, kind="grant")
        _row(conn, USER_ID, 20, kind="quest")
        _row(conn, USER_ID, -5, kind="rental")
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    assert [e.amount for e in entries] == [10, 20, -5]
    assert [e.ledger_id for e in entries] == [1, 2, 3]


def test_collect_skips_rows_at_or_before_cursor(db):
    with open_db(db) as conn:
        _row(conn, USER_ID, 10, kind="grant")
        _row(conn, USER_ID, 20, kind="quest")
        entries = collect_register_entries(conn, GUILD_ID, 1, 10)

    assert [e.ledger_id for e in entries] == [2]


def test_collect_honours_limit(db):
    with open_db(db) as conn:
        for _ in range(5):
            _row(conn, USER_ID, 10, kind="quest")
        entries = collect_register_entries(conn, GUILD_ID, 0, 2)

    assert len(entries) == 2


# ── skipped kinds ──────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", SKIP_KINDS)
def test_skipped_kinds_are_never_collected(db, kind):
    with open_db(db) as conn:
        _row(conn, USER_ID, 10, kind=kind)
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    assert entries == []


def test_auto_faucets_do_not_starve_the_batch(db):
    """A midnight burst of logins must not consume the whole limit.

    Filtering has to happen in SQL, not after the LIMIT — otherwise a day-roll
    flood returns a batch of nothing-to-post and real entries wait for ticks.
    """
    with open_db(db) as conn:
        for _ in range(50):
            _row(conn, USER_ID, 5, kind="login")
        _row(conn, USER_ID, 50, kind="quest")
        entries = collect_register_entries(conn, GUILD_ID, 0, 8)

    assert [e.kind for e in entries] == ["quest"]


def test_balance_accounts_for_skipped_rows_in_between(db):
    """A skipped login still moved the wallet — the maths must include it."""
    with open_db(db) as conn:
        _row(conn, USER_ID, 100, kind="quest")
        _row(conn, USER_ID, 5, kind="login")  # not posted, but real money
        _row(conn, USER_ID, 20, kind="quest")
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    # 100 → (+5 login, hidden) → 125, not 120.
    assert [e.balance_after for e in entries] == [100, 125]


def test_collect_ignores_other_guilds(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (GUILD_ID + 1, USER_ID, 99, "quest", NOW),
        )
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    assert entries == []


def test_balance_after_is_reconstructed_per_row(db):
    """Each entry shows the balance that row produced, not the live balance."""
    with open_db(db) as conn:
        _row(conn, USER_ID, 100, kind="grant")
        _row(conn, USER_ID, 50, kind="quest")
        _row(conn, USER_ID, -30, kind="rental")
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    assert [e.balance_after for e in entries] == [100, 150, 120]


def test_balance_after_excludes_rows_beyond_the_batch(db):
    """A credit landing after the batch must not skew the drained rows."""
    with open_db(db) as conn:
        _row(conn, USER_ID, 100, kind="grant")
        _row(conn, USER_ID, 50, kind="quest")
        _row(conn, USER_ID, 999, kind="grant")  # beyond the limit-2 batch
        entries = collect_register_entries(conn, GUILD_ID, 0, 2)

    assert [e.balance_after for e in entries] == [100, 150]


def test_balance_after_tracks_each_user_separately(db):
    with open_db(db) as conn:
        _row(conn, USER_ID, 100, kind="grant")
        _row(conn, 7777, 40, kind="grant")
        _row(conn, USER_ID, 10, kind="quest")
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    assert [(e.user_id, e.balance_after) for e in entries] == [
        (USER_ID, 100),
        (7777, 40),
        (USER_ID, 110),
    ]


def _make_quest(conn, *, title="Daily Chatterbox", **kw):
    return quests_svc.create_quest(
        conn, GUILD_ID, title=title, description="",
        qtype="daily", reward=50, signoff=0, criteria="",
        starts_at=None, ends_at=None, rotate_tag="",
        community_target=None, created_by=None, **kw,
    )


def _claim(conn, quest_id, user_id, period="2023-11-14"):
    cur = conn.execute(
        "INSERT INTO econ_quest_claims "
        "(quest_id, guild_id, user_id, period, state, created_at) "
        "VALUES (?, ?, ?, ?, 'paid', ?)",
        (quest_id, GUILD_ID, user_id, period, NOW),
    )
    return cur.lastrowid


def test_collect_resolves_quest_title_and_target(db):
    with open_db(db) as conn:
        qid = _make_quest(conn, target_count=5, trigger_kind="message_sent")
        cid = _claim(conn, qid, USER_ID)
        _row(conn, USER_ID, 50, kind="quest", meta={"quest_id": qid, "claim_id": cid})
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    assert entries[0].quest_title == "Daily Chatterbox"
    assert entries[0].quest_target == 5


def test_banded_quest_shows_the_members_own_target(db):
    """A banded quest draws each member a different target — show theirs.

    The library's target_count is meaningless for a band; printing it would
    show a tally the member never actually worked to.
    """
    from bot_modules.economy.quests import effective_target

    period = "2023-11-14"
    with open_db(db) as conn:
        qid = _make_quest(
            conn, target_min=5, target_max=15, trigger_kind="message_sent"
        )
        cid = _claim(conn, qid, USER_ID, period=period)
        _row(conn, USER_ID, 50, kind="quest", meta={"quest_id": qid, "claim_id": cid})
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    expected = effective_target(
        1, 5, 15, user_id=USER_ID, quest_id=qid, period=period
    )
    assert 5 <= expected <= 15
    assert entries[0].quest_target == expected


def test_banded_quest_without_a_claim_shows_no_tally(db):
    """No claim means no period, so no honest per-member target exists."""
    with open_db(db) as conn:
        qid = _make_quest(
            conn, target_min=5, target_max=15, trigger_kind="message_sent"
        )
        _row(conn, USER_ID, 50, kind="quest", meta={"quest_id": qid})
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    assert entries[0].quest_target == 1  # renders as a plain "Quest: **title**"


def test_collect_handles_deleted_quest(db):
    with open_db(db) as conn:
        _row(conn, USER_ID, 50, kind="quest", meta={"quest_id": 12345})
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    assert entries[0].quest_title == ""
    assert render_memo(entries[0], _names) == "Quest: **a quest**"


def test_collect_survives_malformed_meta(db):
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO econ_ledger "
            "(guild_id, user_id, amount, kind, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (GUILD_ID, USER_ID, 10, "quest", "not json{", NOW),
        )
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    assert entries[0].meta == {}


def test_collect_via_real_credit_and_debit_helpers(db):
    """End-to-end against the actual wallet chokepoints, not hand-built rows."""
    with open_db(db) as conn:
        apply_credit(conn, GUILD_ID, USER_ID, 100, "grant", actor_id=1, meta={})
        apply_debit(conn, GUILD_ID, USER_ID, 40, "rental", meta={"perk": "role_color"})
        entries = collect_register_entries(conn, GUILD_ID, 0, 10)

    assert [(e.amount, e.balance_after) for e in entries] == [(100, 100), (-40, 60)]


# ── memo rendering ─────────────────────────────────────────────────────


def test_memo_quest_names_the_quest():
    entry = _entry(kind="quest", quest_title="Daily Chatterbox", quest_target=1)
    assert render_memo(entry, _names) == "Quest: **Daily Chatterbox**"


def test_memo_counted_quest_shows_final_tally():
    entry = _entry(kind="quest", quest_title="Daily Chatterbox", quest_target=5)
    assert render_memo(entry, _names) == "Quest: **Daily Chatterbox** (5/5)"


def test_memo_quest_falls_back_when_quest_deleted():
    entry = _entry(kind="quest", quest_title="")
    assert render_memo(entry, _names) == "Quest: **a quest**"


def test_memo_rental_uses_human_perk_label():
    entry = _entry(kind="rental", amount=-50, meta={"perk": "role_color"})
    assert render_memo(entry, _names) == "Perk rental: **Custom role color**"


def test_memo_rental_renewal_says_renewal():
    entry = _entry(kind="rental", amount=-50, meta={"perk": "role_icon", "renewal": True})
    assert render_memo(entry, _names) == "Perk renewal: **Role icon**"


def test_transfer_is_one_consolidated_entry():
    """Both legs are one event — name both sides, don't post it twice."""
    entry = _entry(kind="transfer_out", amount=-25, meta={"to": 777})
    embed = build_register_embed(entry, DEFAULT_ECON_SETTINGS, _names)

    assert embed.author.name == f"User{USER_ID} → User777"
    assert render_memo(entry, _names) == "Transfer"
    # Sideways movement: unsigned, and neither green nor red.
    assert "25" in (embed.description or "")
    assert "−25" not in (embed.description or "")
    assert embed.colour == TRANSFER_COLOUR
    assert "'s wallet:" in (embed.footer.text or "")


def test_memo_login_includes_source_and_streak():
    entry = _entry(kind="login", meta={"source": "voice", "streak": 4})
    assert render_memo(entry, _names) == "Daily login (voice) — 4-day streak"


def test_memo_login_omits_streak_of_one():
    entry = _entry(kind="login", meta={"source": "text", "streak": 1})
    assert render_memo(entry, _names) == "Daily login (text)"


def test_memo_milestone_and_conversion():
    assert render_memo(_entry(kind="milestone", meta={"streak": 30}), _names) == (
        "Streak milestone — 30 days"
    )
    assert render_memo(_entry(kind="conversion", meta={"xp": 1200.7}), _names) == (
        "XP conversion — 1,201 XP earned"
    )


def test_memo_grant_credits_the_actor_and_reason():
    entry = _entry(kind="grant", actor_id=999, meta={"reason": "event prize"})
    assert render_memo(entry, _names) == "Staff grant by User999 — event prize"


def test_memo_grant_without_reason():
    entry = _entry(kind="grant", actor_id=999, meta={})
    assert render_memo(entry, _names) == "Staff grant by User999"


def test_memo_covers_every_simple_kind():
    for kind, expected in [
        ("quest_community", "Community quest payout"),
        ("qotd", "Answered the question of the day"),
        ("game_participation", "Game participation"),
        ("game_win", "Game win"),
    ]:
        assert render_memo(_entry(kind=kind), _names) == expected


def test_memo_unknown_kind_degrades_gracefully():
    """A future payout kind must never render a blank memo."""
    entry = _entry(kind="mystery_bonus")
    assert render_memo(entry, _names) == "Mystery bonus"


# ── embed ──────────────────────────────────────────────────────────────


def test_embed_credit_is_green_and_signed():
    embed = build_register_embed(_entry(amount=50), DEFAULT_ECON_SETTINGS, _names)
    assert embed.colour == CREDIT_COLOUR
    assert "+50" in (embed.description or "")


def test_embed_debit_is_red_and_shows_minus():
    entry = _entry(amount=-200, kind="rental", meta={"perk": "text_room"})
    embed = build_register_embed(entry, DEFAULT_ECON_SETTINGS, _names)
    assert embed.colour == DEBIT_COLOUR
    assert "−200" in (embed.description or "")


def test_embed_shows_balance_and_author():
    entry = _entry(balance_after=1250)
    embed = build_register_embed(entry, DEFAULT_ECON_SETTINGS, _names)
    assert "1,250" in (embed.footer.text or "")
    assert embed.author.name == f"User{USER_ID}"


def test_embed_memo_is_in_the_description():
    entry = _entry(kind="quest", quest_title="Daily Chatterbox", quest_target=5)
    embed = build_register_embed(entry, DEFAULT_ECON_SETTINGS, _names)
    assert "Daily Chatterbox" in (embed.description or "")
    assert "(5/5)" in (embed.description or "")


# ── drain loop ─────────────────────────────────────────────────────────


def _channel():
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    return channel


def _bot(guild) -> MagicMock:
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    return bot


def _guild_with_channel(channel):
    guild = FakeGuild(id=GUILD_ID)
    guild.channels[CHANNEL_ID] = channel
    return guild


def _enable(conn, *, cursor=0):
    """Turn the feed on. Default cursor 0 = already seeded, drain everything."""
    save_econ_settings(
        conn,
        GUILD_ID,
        {
            "enabled": True,
            "register_channel_id": CHANNEL_ID,
            "register_cursor_id": cursor,
        },
    )


def _cursor(db) -> int:
    with open_db(db) as conn:
        return load_econ_settings(conn, GUILD_ID).register_cursor_id


@pytest.mark.asyncio
async def test_loop_skips_when_no_register_channel(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"enabled": True})
    bot = _bot(FakeGuild(id=GUILD_ID))

    assert await run_guild_register(bot, db, GUILD_ID, NOW) == 0
    bot.get_guild.assert_not_called()  # bails before touching the gateway


@pytest.mark.asyncio
async def test_loop_skips_when_economy_disabled(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"register_channel_id": CHANNEL_ID})
    bot = _bot(FakeGuild(id=GUILD_ID))

    assert await run_guild_register(bot, db, GUILD_ID, NOW) == 0
    bot.get_guild.assert_not_called()


@pytest.mark.asyncio
async def test_first_enable_seeds_cursor_and_posts_nothing(db):
    """Switching the feed on must never replay the guild's history."""
    with open_db(db) as conn:
        for _ in range(3):
            _row(conn, USER_ID, 10, kind="grant")
        _enable(conn, cursor=-1)  # never seeded
    channel = _channel()

    posted = await run_guild_register(_bot(_guild_with_channel(channel)), db, GUILD_ID, NOW)

    assert posted == 0
    channel.send.assert_not_awaited()
    assert _cursor(db) == 3


@pytest.mark.asyncio
async def test_first_transaction_on_an_empty_ledger_is_not_swallowed(db):
    """Seeding an empty ledger must not skip the guild's first-ever payout.

    A 0 cursor is a *seeded* empty ledger, not an unseeded one — re-seeding it
    would jump the cursor past the first real row and lose it silently.
    """
    with open_db(db) as conn:
        _enable(conn, cursor=-1)
    channel = _channel()
    bot = _bot(_guild_with_channel(channel))

    await run_guild_register(bot, db, GUILD_ID, NOW)  # seeds against an empty ledger
    assert _cursor(db) == 0

    with open_db(db) as conn:
        _row(conn, USER_ID, 50, kind="quest")

    assert await run_guild_register(bot, db, GUILD_ID, NOW) == 1


@pytest.mark.asyncio
async def test_drain_posts_new_rows_and_advances_cursor(db):
    with open_db(db) as conn:
        _row(conn, USER_ID, 10, kind="grant")
        _enable(conn, cursor=1)
        _row(conn, USER_ID, 50, kind="quest")
        _row(conn, USER_ID, -20, kind="rental", meta={"perk": "role_color"})
    channel = _channel()

    posted = await run_guild_register(_bot(_guild_with_channel(channel)), db, GUILD_ID, NOW)

    assert posted == 2
    assert channel.send.await_count == 2
    assert _cursor(db) == 3


@pytest.mark.asyncio
async def test_drain_is_not_repeated_on_the_next_tick(db):
    with open_db(db) as conn:
        _enable(conn, cursor=0)
        _row(conn, USER_ID, 50, kind="quest")
    channel = _channel()
    bot = _bot(_guild_with_channel(channel))

    assert await run_guild_register(bot, db, GUILD_ID, NOW) == 1
    assert await run_guild_register(bot, db, GUILD_ID, NOW) == 0
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_stale_rows_are_skipped_but_cursor_advances(db):
    """A backlog after downtime is noise — skip it, don't spam the channel."""
    with open_db(db) as conn:
        _enable(conn, cursor=0)
        _row(conn, USER_ID, 10, kind="grant", age=REGISTER_STALE_SECONDS + 60)
        _row(conn, USER_ID, 50, kind="quest", age=0.0)
    channel = _channel()

    posted = await run_guild_register(_bot(_guild_with_channel(channel)), db, GUILD_ID, NOW)

    assert posted == 1  # only the fresh row
    assert _cursor(db) == 2  # but the stale one is never re-examined


@pytest.mark.asyncio
async def test_drain_caps_rows_per_tick(db):
    with open_db(db) as conn:
        _enable(conn, cursor=0)
        for _ in range(REGISTER_MAX_PER_TICK + 5):
            _row(conn, USER_ID, 5, kind="grant")
    channel = _channel()

    posted = await run_guild_register(_bot(_guild_with_channel(channel)), db, GUILD_ID, NOW)

    assert posted == REGISTER_MAX_PER_TICK
    assert _cursor(db) == REGISTER_MAX_PER_TICK  # the rest spill to next tick


@pytest.mark.asyncio
async def test_forbidden_leaves_cursor_for_retry(db):
    """A permissions problem must not silently burn the entries."""
    with open_db(db) as conn:
        _enable(conn, cursor=0)
        _row(conn, USER_ID, 50, kind="quest")
    channel = _channel()
    channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no"))

    posted = await run_guild_register(_bot(_guild_with_channel(channel)), db, GUILD_ID, NOW)

    assert posted == 0
    assert _cursor(db) == 0


@pytest.mark.asyncio
async def test_missing_channel_posts_nothing(db):
    with open_db(db) as conn:
        _enable(conn, cursor=0)
        _row(conn, USER_ID, 50, kind="quest")

    guild = FakeGuild(id=GUILD_ID)  # channel not in the guild
    assert await run_guild_register(_bot(guild), db, GUILD_ID, NOW) == 0


@pytest.mark.asyncio
async def test_transfer_posts_once_not_twice(db):
    """The two ledger legs of one transfer must produce one entry."""
    with open_db(db) as conn:
        _enable(conn, cursor=0)
        apply_credit(conn, GUILD_ID, USER_ID, 100, "grant", actor_id=9, meta={})
        transfer_currency(conn, GUILD_ID, USER_ID, 7777, 25)
    channel = _channel()

    posted = await run_guild_register(_bot(_guild_with_channel(channel)), db, GUILD_ID, NOW)

    assert posted == 2  # the grant + ONE transfer entry (not two legs)
    headers = [c.kwargs["embed"].author.name for c in channel.send.await_args_list]
    # The loop's own name fallback for a member the gateway doesn't know.
    assert headers[1] == f"User {USER_ID} → User 7777"


@pytest.mark.asyncio
async def test_register_tick_covers_every_guild_and_isolates_failures(db):
    """One bad guild must not stall the rest of the sweep."""
    other = 9002
    with open_db(db) as conn:
        _enable(conn, cursor=0)
        _row(conn, USER_ID, 50, kind="quest")
        save_econ_settings(conn, other, {"enabled": False})

    channel = _channel()
    guild = _guild_with_channel(channel)
    broken = FakeGuild(id=other)
    bot = MagicMock()
    bot.guilds = [broken, guild]
    bot.get_guild = MagicMock(side_effect=lambda gid: {GUILD_ID: guild, other: broken}[gid])

    await register_tick(bot, db, NOW)

    channel.send.assert_awaited_once()  # the enabled guild still drained
