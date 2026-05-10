"""Cog-level tests for the /veil submit command."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from services.veil_models import VeilConfig
from tests.fakes import FakeGuild, FakeMember, FakeRole, fake_interaction

VEIL_ROLE_ID = 7001
VEIL_CHANNEL_ID = 8001
GUILD_ID = 9001


def _cfg(**overrides: Any) -> VeilConfig:
    defaults: dict[str, Any] = dict(
        guild_id=GUILD_ID,
        veil_role_id=VEIL_ROLE_ID,
        veil_channel_id=VEIL_CHANNEL_ID,
        guess_cooldown_seconds=30,
        crop_difficulty="medium",
        min_image_dimension_px=400,
        max_image_size_mb=10,
    )
    defaults.update(overrides)
    return VeilConfig(**defaults)  # type: ignore[arg-type]


def _veil_member(has_role: bool = True) -> FakeMember:
    roles = [FakeRole(id=VEIL_ROLE_ID)] if has_role else []
    return FakeMember(id=1001, roles=roles)


def _guild(member: FakeMember | None = None) -> FakeGuild:
    m = member or _veil_member()
    g = FakeGuild(id=GUILD_ID)
    g.members[m.id] = m
    return g


def _attachment(
    content_type: str = "image/jpeg",
    size: int = 1_000_000,
    read_return: bytes = b"fake-bytes",
) -> MagicMock:
    a = MagicMock()
    a.content_type = content_type
    a.size = size
    a.read = AsyncMock(return_value=read_return)
    return a


def _make_cog(db_path: str = ":memory:"):
    from cogs.veil_cog import VeilCog
    bot = MagicMock()
    bot.ctx.db_path = db_path
    bot.add_view = MagicMock()
    return VeilCog(bot)


async def _submit(cog: Any, interaction: Any, image: Any) -> None:
    """Invoke veil_submit's underlying coroutine, bypassing the app_commands.Command wrapper."""
    await cog.veil_submit.callback(cog, interaction, image)


# ── Validation rejection tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_rejects_no_veil_role():
    member = _veil_member(has_role=False)
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("cogs.veil_cog._load_config", return_value=_cfg()):
        await _submit(cog, interaction, _attachment())

    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args.args[0]
    assert "Veil role" in msg


@pytest.mark.asyncio
async def test_submit_rejects_unconfigured_channel():
    member = _veil_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("cogs.veil_cog._load_config", return_value=_cfg(veil_channel_id=0)):
        await _submit(cog, interaction, _attachment())

    msg = interaction.followup.send.call_args.args[0]
    assert "not configured" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_non_image_mime():
    member = _veil_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("cogs.veil_cog._load_config", return_value=_cfg()):
        await _submit(cog, interaction, _attachment(content_type="video/mp4"))

    msg = interaction.followup.send.call_args.args[0]
    assert "image" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_oversized_file():
    member = _veil_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("cogs.veil_cog._load_config", return_value=_cfg(max_image_size_mb=5)):
        await _submit(cog, interaction, _attachment(size=6 * 1024 * 1024))

    msg = interaction.followup.send.call_args.args[0]
    assert "too large" in msg.lower() or "maximum" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_small_dimensions():
    member = _veil_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("cogs.veil_cog._load_config", return_value=_cfg()):
        with patch("cogs.veil_cog._validate_dimensions", return_value=(False, 200, 200)):
            await _submit(cog, interaction, _attachment())

    msg = interaction.followup.send.call_args.args[0]
    assert "too small" in msg.lower() or "minimum" in msg.lower()


@pytest.mark.asyncio
async def test_submit_rejects_no_pipeline_candidates():
    import io as _io
    from PIL import Image
    from services.veil_models import PipelineResult

    buf = _io.BytesIO()
    Image.new("RGB", (500, 500)).save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    member = _veil_member()
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    empty_result = PipelineResult(candidates=[], crops=[])

    with patch("cogs.veil_cog._load_config", return_value=_cfg()):
        with patch("cogs.veil_cog.run_pipeline", return_value=empty_result):
            await _submit(cog, interaction, _attachment(read_return=img_bytes))

    msg = interaction.followup.send.call_args.args[0]
    assert "crop region" in msg.lower() or "viable" in msg.lower()


@pytest.mark.asyncio
async def test_submit_success_sends_ephemeral_preview(sync_db_path: Path):
    """Full happy-path: pipeline returns a crop, ephemeral preview sent to submitter.

    The veil channel is NOT posted to at submit time — that happens only when
    the submitter clicks the Post button in the preview.
    """
    import io as _io
    from PIL import Image
    from services.veil_models import Detection, BoundingBox, PipelineResult

    buf = _io.BytesIO()
    Image.new("RGB", (500, 500)).save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    det = Detection(label="BREAST", score=0.9, box=BoundingBox(10, 10, 100, 100))
    fake_result = PipelineResult(candidates=[det], crops=[b"fake-crop-jpeg"])

    member = _veil_member()
    guild = _guild(member)

    fake_channel = MagicMock(spec=discord.TextChannel)
    fake_channel.send = AsyncMock(return_value=_fake_game_message())
    guild.channels[VEIL_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)
    interaction.guild.get_channel = lambda cid: guild.channels.get(cid)
    interaction.user.id = member.id

    cog = _make_cog(str(sync_db_path))
    with patch("cogs.veil_cog._load_config", return_value=_cfg()):
        with patch("cogs.veil_cog.run_pipeline", return_value=fake_result):
            with patch("cogs.veil_cog._do_insert_round", return_value=42):
                with patch("cogs.veil_cog._do_update_round_message"):
                    with patch("cogs.veil_cog._do_set_reroll_count"):
                        await _submit(cog, interaction, _attachment(read_return=img_bytes))

    # Channel post deferred until the user clicks Post — not called here.
    fake_channel.send.assert_not_called()

    # Ephemeral preview sent to submitter
    interaction.followup.send.assert_called_once()
    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True


def _fake_game_message() -> MagicMock:
    """Minimal discord.Message fake for testing."""
    msg = MagicMock()
    msg.id = 12345
    msg.attachments = [MagicMock(url="https://cdn.discord.com/fake/crop.jpg")]
    msg.edit = AsyncMock()
    msg.guild = MagicMock()
    return msg


@pytest.mark.asyncio
async def test_cog_load_registers_game_views_from_db(sync_db_path: Path):
    """cog_load queries active rounds and calls bot.add_view for each."""
    from services.veil_repo import insert_round
    from db_utils import open_db

    with open_db(sync_db_path) as conn:
        insert_round(conn, guild_id=GUILD_ID, submitter_id=1001, answer_id=1001)
        insert_round(conn, guild_id=GUILD_ID, submitter_id=1002, answer_id=1002)

    cog = _make_cog(str(sync_db_path))
    add_view_mock: MagicMock = cog.bot.add_view  # type: ignore[assignment]
    await cog.cog_load()

    assert add_view_mock.call_count == 2
