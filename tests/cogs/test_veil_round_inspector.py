"""Tests for /veil round mod-only inspector."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from services.veil_models import VeilRound
from tests.fakes import FakeGuild, FakeMember, fake_interaction

GUILD_ID = 9001
VEIL_CHANNEL_ID = 8001
ROUND_ID = 42


def _make_round() -> VeilRound:
    return VeilRound(
        id=ROUND_ID, guild_id=GUILD_ID, submitter_id=1001,
        answer_id=1001, channel_id=VEIL_CHANNEL_ID, message_id=12345,
        crop_path="", crop_url="", original_path="",
        difficulty="medium", candidate_count=1, reroll_count=0,
        allow_reuse=False, is_reuse=False, original_round_id=None,
        reuse_blocked=False, created_at=1000.0, solved_at=None, solver_id=None,
        guesses_to_solve=None, unique_guessers_to_solve=None,
        answer_optout=False, deleted_at=None,
    )


def _make_cog():
    from cogs.veil_cog import VeilCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    return VeilCog(bot)


async def _round(cog: Any, interaction: Any, rid: int) -> None:
    await cog.veil_round.callback(cog, interaction, rid)


@pytest.mark.asyncio
async def test_round_inspector_rejects_non_mod():
    user = FakeMember(id=2002)
    user.guild_permissions = MagicMock(manage_guild=False, administrator=False)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[user.id] = user

    interaction = fake_interaction(user=user, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    with patch("cogs.veil_cog._do_load_round", return_value=_make_round()):
        await _round(cog, interaction, ROUND_ID)

    msg = interaction.followup.send.call_args.args[0]
    assert "mod" in msg.lower() or "permission" in msg.lower()


@pytest.mark.asyncio
async def test_round_inspector_returns_embed_for_mod():
    mod = FakeMember(id=3003)
    mod.guild_permissions = MagicMock(manage_guild=True, administrator=False)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[mod.id] = mod

    interaction = fake_interaction(user=mod, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    with patch("cogs.veil_cog._do_load_round", return_value=_make_round()), \
         patch("cogs.veil_cog._do_count_guesses_for_round", return_value=12), \
         patch("cogs.veil_cog._do_count_unique_guessers_for_round", return_value=4):
        await _round(cog, interaction, ROUND_ID)

    call = interaction.followup.send.call_args
    embed = call.kwargs.get("embed")
    assert embed is not None
    desc = (embed.description or "") + " " + (embed.title or "")
    assert "1001" in desc  # submitter / answer mention
    assert call.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_round_inspector_handles_unknown_round():
    mod = FakeMember(id=3003)
    mod.guild_permissions = MagicMock(manage_guild=True, administrator=False)
    guild = FakeGuild(id=GUILD_ID)
    guild.members[mod.id] = mod

    interaction = fake_interaction(user=mod, guild=guild)
    interaction.guild_id = GUILD_ID

    cog = _make_cog()
    with patch("cogs.veil_cog._do_load_round", return_value=None):
        await _round(cog, interaction, ROUND_ID)

    msg = interaction.followup.send.call_args.args[0]
    assert "not found" in msg.lower()
