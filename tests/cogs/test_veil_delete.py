"""Cog-level tests for /veil delete + Post-time safety checks."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.veil_models import BoundingBox, VeilRound
from tests.fakes import FakeGuild, FakeMember, fake_interaction

GUILD_ID = 9001
VEIL_CHANNEL_ID = 8001
VEIL_ROLE_ID = 7001
SUBMITTER_ID = 1001
OTHER_USER_ID = 1002
MOD_ROLE_ID = 7777
ROUND_ID = 42


def _make_round(*, submitter_id: int = SUBMITTER_ID, original_path: str = "") -> VeilRound:
    return VeilRound(
        id=ROUND_ID, guild_id=GUILD_ID, submitter_id=submitter_id,
        answer_id=submitter_id, channel_id=VEIL_CHANNEL_ID, message_id=12345,
        crop_path="", crop_url="", original_path=original_path,
        difficulty="medium", candidate_count=1, reroll_count=0,
        allow_reuse=False, is_reuse=False, original_round_id=None,
        reuse_blocked=False, created_at=1000.0, solved_at=None, solver_id=None,
        guesses_to_solve=None, unique_guessers_to_solve=None,
        answer_optout=False, deleted_at=None,
    )


def _make_cog(db_path: str = ":memory:"):
    from cogs.veil_cog import VeilCog
    bot = MagicMock()
    bot.ctx.db_path = db_path
    return VeilCog(bot)


async def _delete(cog: Any, interaction: Any, round_id: int) -> None:
    await cog.veil_delete.callback(cog, interaction, round_id)


# ── /veil delete authorization ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_allowed_for_submitter():
    member = FakeMember(id=SUBMITTER_ID)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[member.id] = member
    fake_msg = MagicMock()
    fake_msg.delete = AsyncMock()
    import discord as _discord
    fake_channel = MagicMock(spec=_discord.TextChannel)
    fake_channel.fetch_message = AsyncMock(return_value=fake_msg)
    guild.channels[VEIL_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    with patch("cogs.veil_cog._do_load_round", return_value=_make_round()), \
         patch("cogs.veil_cog._do_soft_delete_round") as soft_del:
        await _delete(cog, interaction, ROUND_ID)

    soft_del.assert_called_once()
    fake_msg.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_allowed_for_mod():
    """A user with manage_guild permission can delete any round."""
    mod = FakeMember(id=OTHER_USER_ID)
    mod.guild_permissions = MagicMock(manage_guild=True, administrator=False)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[mod.id] = mod
    fake_msg = MagicMock()
    fake_msg.delete = AsyncMock()
    import discord as _discord
    fake_channel = MagicMock(spec=_discord.TextChannel)
    fake_channel.fetch_message = AsyncMock(return_value=fake_msg)
    guild.channels[VEIL_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(user=mod, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    with patch("cogs.veil_cog._do_load_round", return_value=_make_round()), \
         patch("cogs.veil_cog._do_soft_delete_round") as soft_del:
        await _delete(cog, interaction, ROUND_ID)

    soft_del.assert_called_once()


@pytest.mark.asyncio
async def test_delete_rejects_unrelated_user():
    """Non-submitter, non-mod must be rejected."""
    user = FakeMember(id=OTHER_USER_ID)
    user.guild_permissions = MagicMock(manage_guild=False, administrator=False)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[user.id] = user

    interaction = fake_interaction(user=user, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    with patch("cogs.veil_cog._do_load_round", return_value=_make_round()), \
         patch("cogs.veil_cog._do_soft_delete_round") as soft_del:
        await _delete(cog, interaction, ROUND_ID)

    soft_del.assert_not_called()


@pytest.mark.asyncio
async def test_delete_rejects_unknown_round():
    user = FakeMember(id=SUBMITTER_ID)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[user.id] = user

    interaction = fake_interaction(user=user, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    with patch("cogs.veil_cog._do_load_round", return_value=None), \
         patch("cogs.veil_cog._do_soft_delete_round") as soft_del:
        await _delete(cog, interaction, ROUND_ID)

    soft_del.assert_not_called()


@pytest.mark.asyncio
async def test_delete_unlinks_original_file_when_present(tmp_path: Path):
    orig_file = tmp_path / "42.png"
    orig_file.write_bytes(b"original-bytes")

    member = FakeMember(id=SUBMITTER_ID)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[member.id] = member
    fake_msg = MagicMock()
    fake_msg.delete = AsyncMock()
    import discord as _discord
    fake_channel = MagicMock(spec=_discord.TextChannel)
    fake_channel.fetch_message = AsyncMock(return_value=fake_msg)
    guild.channels[VEIL_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    rnd = _make_round(original_path=str(orig_file))
    with patch("cogs.veil_cog._do_load_round", return_value=rnd), \
         patch("cogs.veil_cog._do_soft_delete_round"):
        await _delete(cog, interaction, ROUND_ID)

    assert not orig_file.exists()


# ── _on_post NSFW recheck ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_rejects_non_nsfw_channel():
    """Defense in depth: if the configured channel is no longer NSFW at post
    time (or was never), refuse to post."""
    from cogs.veil_cog import CropEditorView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()

    fake_channel = MagicMock()
    fake_channel.is_nsfw = lambda: False
    fake_channel.send = AsyncMock()
    fake_channel.mention = "#general"

    interaction = fake_interaction()
    interaction.guild.get_channel = lambda cid: fake_channel
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    view = CropEditorView(
        bot,
        image_bytes=b"",
        img_w=500,
        img_h=500,
        crop_box=BoundingBox(0.0, 0.0, 500.0, 500.0),
        guild_id=GUILD_ID,
        veil_channel_id=VEIL_CHANNEL_ID,
        submitter_id=SUBMITTER_ID,
        answer_id=SUBMITTER_ID,
        difficulty="medium",
        candidate_count=1,
    )

    with patch("cogs.veil_cog._do_insert_round") as insert_mock:
        with patch("cogs.veil_cog.isinstance", lambda obj, types: True):
            await view._on_post(interaction)

    insert_mock.assert_not_called()
    fake_channel.send.assert_not_called()


# ── _on_post double-click guard ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_post_double_click_inserts_only_once(tmp_path, monkeypatch):
    """Two near-simultaneous Post clicks must produce only one round / one message."""
    import cogs.veil_cog as veil_cog
    from cogs.veil_cog import CropEditorView

    monkeypatch.setattr(veil_cog, "_VEIL_ORIG_DIR", tmp_path / "orig")

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()

    fake_channel = MagicMock()
    fake_channel.is_nsfw = lambda: True
    fake_msg = MagicMock()
    fake_msg.id = 9999
    attached = MagicMock()
    attached.url = "https://cdn.example/c.jpg"
    fake_msg.attachments = [attached]
    fake_channel.send = AsyncMock(return_value=fake_msg)
    fake_channel.mention = "#veil"

    interaction = fake_interaction()
    interaction.guild.get_channel = lambda cid: fake_channel
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    view = CropEditorView(
        bot,
        image_bytes=b"",
        img_w=500,
        img_h=500,
        crop_box=BoundingBox(0.0, 0.0, 500.0, 500.0),
        guild_id=GUILD_ID,
        veil_channel_id=VEIL_CHANNEL_ID,
        submitter_id=SUBMITTER_ID,
        answer_id=SUBMITTER_ID,
        difficulty="medium",
        candidate_count=1,
        original_bytes=b"orig",
        original_ext=".png",
    )

    with patch("cogs.veil_cog._do_insert_round", return_value=77) as insert_mock, \
         patch("cogs.veil_cog._do_update_round_message"), \
         patch("cogs.veil_cog._do_set_original_path"), \
         patch("cogs.veil_cog._do_audit"), \
         patch("cogs.veil_cog.render_crop", return_value=b"\xff\xd8fake"), \
         patch("cogs.veil_cog._repost_prompt", new_callable=AsyncMock):
        with patch("cogs.veil_cog.isinstance", lambda obj, types: True):
            import asyncio as _aio
            await _aio.gather(view._on_post(interaction), view._on_post(interaction))

    assert insert_mock.call_count == 1
    assert fake_channel.send.call_count == 1
