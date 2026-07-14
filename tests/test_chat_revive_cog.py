"""Smoke tests for the /revive cog — command callbacks over a real schema."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot_modules.cogs.chat_revive_cog as cog_mod
from bot_modules.chat_revive.starter_pack import STARTER_QUESTIONS
from bot_modules.cogs.chat_revive_cog import ChatReviveCog
from bot_modules.core.db_utils import open_db
from bot_modules.services.chat_revive_service import (
    add_question,
    get_channel_config,
    get_guild_config,
    list_questions,
    save_guild_config,
    GuildConfig,
)
from migrations import apply_migrations_sync
from tests.fakes import fake_interaction

GID, CID = 100, 200


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


@pytest.fixture
def cog(db, monkeypatch):
    monkeypatch.setattr(
        cog_mod,
        "resolve_accent_color",
        AsyncMock(return_value=discord.Colour.blurple()),
    )
    ctx = SimpleNamespace(db_path=db)
    bot = SimpleNamespace(ctx=ctx, games_db=None, game_busy_checks={})
    return ChatReviveCog(bot, ctx)  # type: ignore[arg-type]


@pytest.fixture
def interaction():
    i = fake_interaction()
    i.guild.id = GID
    i.guild_id = GID
    i.user.id = 42
    return i


def _channel(channel_id: int = CID) -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.name = "general"
    ch.mention = f"<#{channel_id}>"
    ch.slowmode_delay = 0
    ch.is_nsfw.return_value = False
    ch.send = AsyncMock(return_value=SimpleNamespace(id=777))
    return ch


def _role(role_id: int = 555) -> MagicMock:
    r = MagicMock(spec=discord.Role)
    r.id = role_id
    r.mention = f"<@&{role_id}>"
    return r


async def test_setup_enables_and_seeds(cog, interaction, db):
    await cog.setup_cmd.callback(cog, interaction, role=_role(), daily_budget=2)
    interaction.followup.send.assert_awaited_once()
    with open_db(db) as conn:
        cfg = get_guild_config(conn, GID)
        qs = list_questions(conn, GID)
    assert cfg.enabled and cfg.role_id == 555 and cfg.daily_budget == 2
    assert len(qs) == len(STARTER_QUESTIONS)


async def test_channel_configures_categories(cog, interaction, db):
    await cog.channel_cmd.callback(
        cog, interaction, _channel(), categories="Deep, music, deep", ping=True
    )
    with open_db(db) as conn:
        cfg = get_channel_config(conn, GID, CID)
    assert cfg is not None
    assert cfg.categories == ("deep", "music")
    assert cfg.ping_enabled
    msg = interaction.response.send_message.await_args.args[0]
    assert "deep, music" in msg and "ping on" in msg


async def test_channel_rejects_bad_categories(cog, interaction, db):
    await cog.channel_cmd.callback(cog, interaction, _channel(), categories="no good!")
    msg = interaction.response.send_message.await_args.args[0]
    assert "didn't understand" in msg
    with open_db(db) as conn:
        assert get_channel_config(conn, GID, CID) is None


async def test_question_add_and_duplicate(cog, interaction, db):
    await cog.question_add.callback(cog, interaction, "Fresh one?", category="deep")
    first = interaction.response.send_message.await_args.args[0]
    await cog.question_add.callback(cog, interaction, "fresh one?")
    second = interaction.response.send_message.await_args.args[0]
    assert "Added" in first and "deep" in first
    assert "already in the bank" in second


async def test_question_bulk_from_attachment(cog, interaction):
    file = MagicMock(spec=discord.Attachment)
    file.size = 100
    file.read = AsyncMock(return_value=b"One?\ndeep: Two?\nOne?\n")
    await cog.question_bulk.callback(cog, interaction, file)
    msg = interaction.followup.send.await_args.args[0]
    assert "**2**" in msg and "**1**" in msg


async def test_question_bulk_rejects_huge_file(cog, interaction):
    file = MagicMock(spec=discord.Attachment)
    file.size = 10_000_000
    await cog.question_bulk.callback(cog, interaction, file)
    assert "too large" in interaction.response.send_message.await_args.args[0]


async def test_question_list_summary_and_detail(cog, interaction, db):
    with open_db(db) as conn:
        for i in range(30):
            add_question(
                conn, GID, f"Q{i}?", category="silly", created_by=1, now_ts=time.time()
            )
    await cog.question_list.callback(cog, interaction)
    summary = interaction.response.send_message.await_args.args[0]
    assert "30 questions" in summary and "**silly** 30" in summary
    await cog.question_list.callback(cog, interaction, category="silly")
    detail = interaction.response.send_message.await_args.args[0]
    assert "`#1`" in detail and "…and 5 more." in detail


async def test_question_retire(cog, interaction, db):
    with open_db(db) as conn:
        qid = add_question(conn, GID, "Old?", created_by=1, now_ts=time.time())
    await cog.question_retire.callback(cog, interaction, qid)
    assert "Retired" in interaction.response.send_message.await_args.args[0]
    await cog.question_retire.callback(cog, interaction, 9999)
    assert "No question" in interaction.response.send_message.await_args.args[0]
    with open_db(db) as conn:
        assert list_questions(conn, GID) == []


async def test_fire_requires_setup(cog, interaction):
    await cog.fire_cmd.callback(cog, interaction, _channel())
    assert "isn't set up yet" in interaction.followup.send.await_args.args[0]


async def test_fire_posts_and_records(cog, interaction, db):
    ch = _channel()
    with open_db(db) as conn:
        save_guild_config(conn, GuildConfig(guild_id=GID, enabled=True, role_id=555))
        add_question(conn, GID, "Manual spark?", created_by=1, now_ts=time.time())
    await cog.fire_cmd.callback(cog, interaction, ch)
    ch.send.assert_awaited_once()
    sent_text = ch.send.await_args.args[0]
    assert "Manual spark?" in sent_text
    assert "<@&555>" not in sent_text  # pings default off per channel
    with open_db(db) as conn:
        row = conn.execute("SELECT * FROM revive_events").fetchone()
    assert row["trigger_kind"] == "manual"
    assert row["message_id"] == 777
    assert row["pinged"] == 0
    assert "Revived" in interaction.followup.send.await_args.args[0]


async def test_fire_reports_empty_bank(cog, interaction, db):
    with open_db(db) as conn:
        save_guild_config(conn, GuildConfig(guild_id=GID, enabled=True))
    await cog.fire_cmd.callback(cog, interaction, _channel())
    assert "No eligible question" in interaction.followup.send.await_args.args[0]


async def test_optin_post_requires_setup(cog, interaction):
    await cog.optin_post.callback(cog, interaction, _channel())
    assert "run `/revive setup`" in interaction.response.send_message.await_args.args[0]


async def test_optin_post_sends_button(cog, interaction, db):
    ch = _channel()
    with open_db(db) as conn:
        save_guild_config(conn, GuildConfig(guild_id=GID, enabled=True, role_id=555))
    await cog.optin_post.callback(cog, interaction, ch)
    ch.send.assert_awaited_once()
    view = ch.send.await_args.kwargs["view"]
    custom_ids = [item.custom_id for item in view.children]
    assert custom_ids == ["chat_revive_optin:555"]


async def test_optin_button_toggles_role():
    from bot_modules.chat_revive.actions import ReviveOptInButton

    role = MagicMock(spec=discord.Role)
    role.id = 555
    member = MagicMock(spec=discord.Member)
    member.roles = []
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    i = fake_interaction()
    i.user = member
    i.guild.get_role = MagicMock(return_value=role)

    button = ReviveOptInButton(555)
    await button.callback(i)
    member.add_roles.assert_awaited_once()
    assert "summon list" in i.response.send_message.await_args.args[0]

    member.roles = [role]
    await button.callback(i)
    member.remove_roles.assert_awaited_once()
    assert "Rest easy" in i.response.send_message.await_args.args[0]


async def test_stats_empty_then_populated(cog, interaction, db):
    interaction.guild.get_role = MagicMock(return_value=None)
    await cog.stats_cmd.callback(cog, interaction)
    assert "No revives yet" in interaction.followup.send.await_args.args[0]

    now = time.time()
    with open_db(db) as conn:
        save_guild_config(conn, GuildConfig(guild_id=GID, enabled=True))
        qid = add_question(conn, GID, "Winner?", created_by=1, now_ts=now)
        conn.execute(
            "INSERT INTO revive_events (guild_id, channel_id, question_id, "
            "trigger_kind, pinged, local_day, created_at, measured_at, success, "
            "follow_msgs, follow_authors) "
            "VALUES (?, ?, ?, 'auto', 0, '2026-07-14', ?, ?, 1, 5, 3)",
            (GID, CID, qid, now - 600, now),
        )
        conn.execute("UPDATE revive_questions SET use_count = 1")
    await cog.stats_cmd.callback(cog, interaction)
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "**1** revives all-time" in embed.description
    assert "1/1" in embed.description
    assert "Winner?" in embed.description


async def test_flourish_toggle(cog, interaction, db):
    await cog.flourish_cmd.callback(cog, interaction, False)
    with open_db(db) as conn:
        assert get_guild_config(conn, GID).flourish_enabled is False
    await cog.flourish_cmd.callback(cog, interaction, True)
    with open_db(db) as conn:
        assert get_guild_config(conn, GID).flourish_enabled is True


async def test_check_explains_no_history(cog, interaction, db):
    with open_db(db) as conn:
        save_guild_config(conn, GuildConfig(guild_id=GID, enabled=True))
        add_question(conn, GID, "Preview?", created_by=1, now_ts=time.time())
    await cog.check_cmd.callback(cog, interaction, _channel())
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "Holding back" in embed.description
    assert "not enabled for revives" in embed.description
