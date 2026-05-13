"""Cog-level tests for the /veil submit command."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.veil_models import VeilConfig
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
    filename: str = "submission.jpg",
) -> MagicMock:
    a = MagicMock()
    a.content_type = content_type
    a.size = size
    a.filename = filename
    a.read = AsyncMock(return_value=read_return)
    return a


def _make_cog(db_path: str = ":memory:"):
    from bot_modules.cogs.veil_cog import VeilCog
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
    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_cfg()):
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
    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_cfg(veil_channel_id=0)):
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
    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_cfg()):
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
    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_cfg(max_image_size_mb=5)):
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
    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_cfg()):
        with patch("bot_modules.cogs.veil_cog._validate_dimensions", return_value=(False, 200, 200)):
            await _submit(cog, interaction, _attachment())

    msg = interaction.followup.send.call_args.args[0]
    assert "too small" in msg.lower() or "minimum" in msg.lower()


@pytest.mark.asyncio
async def test_submit_shows_editor_when_no_pipeline_candidates():
    """No detections → still show the crop editor (centre-crop default, Auto disabled)."""
    import io as _io
    from PIL import Image
    from bot_modules.services.veil_models import PipelineResult
    from bot_modules.cogs.veil_cog import CropEditorView

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

    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_cfg()):
        with patch("bot_modules.cogs.veil_cog.run_pipeline", return_value=empty_result):
            with patch("bot_modules.cogs.veil_cog.render_crop_editor", return_value=b"\xff\xd8fake"):
                await _submit(cog, interaction, _attachment(read_return=img_bytes))

    call_kwargs = interaction.followup.send.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True
    assert isinstance(call_kwargs.kwargs.get("view"), CropEditorView)
    sent_view: CropEditorView = call_kwargs.kwargs["view"]
    assert sent_view._candidate_boxes == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_submit_success_sends_ephemeral_preview(sync_db_path: Path):
    """Full happy-path: pipeline returns a crop, ephemeral preview sent to submitter.

    The veil channel is NOT posted to at submit time — that happens only when
    the submitter clicks the Post button in the preview.
    """
    import io as _io
    from PIL import Image
    from bot_modules.services.veil_models import Detection, BoundingBox, PipelineResult

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
    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_cfg()):
        with patch("bot_modules.cogs.veil_cog.run_pipeline", return_value=fake_result):
            with patch("bot_modules.cogs.veil_cog._do_insert_round", return_value=42):
                with patch("bot_modules.cogs.veil_cog._do_update_round_message"):
                    with patch("bot_modules.cogs.veil_cog._do_set_reroll_count"):
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
    from bot_modules.cogs.veil_cog import GameView
    from bot_modules.services.veil_repo import insert_round
    from bot_modules.core.db_utils import open_db

    with open_db(sync_db_path) as conn:
        insert_round(conn, guild_id=GUILD_ID, submitter_id=1001, answer_id=1001)
        insert_round(conn, guild_id=GUILD_ID, submitter_id=1002, answer_id=1002)

    cog = _make_cog(str(sync_db_path))
    add_view_mock: MagicMock = cog.bot.add_view  # type: ignore[assignment]
    await cog.cog_load()

    gameview_calls = [
        c for c in add_view_mock.call_args_list if isinstance(c.args[0], GameView)
    ]
    assert len(gameview_calls) == 2


# ── Safety patch: fail-closed when Veil role is unconfigured ─────────────────

@pytest.mark.asyncio
async def test_submit_rejects_when_veil_role_not_configured():
    """If veil_role_id is 0 (unconfigured), submit must reject — not fall open.

    Previously _has_veil_role returned True for role_id=0 which let any guild
    member submit on a freshly-installed bot.
    """
    member = _veil_member(has_role=False)  # no veil role at all
    guild = _guild(member)
    interaction = fake_interaction(user=member, guild=guild)
    interaction.guild_id = GUILD_ID
    interaction.guild.get_member = lambda uid: guild.members.get(uid)

    cog = _make_cog()
    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_cfg(veil_role_id=0)):
        await _submit(cog, interaction, _attachment())

    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args.args[0]
    assert "role" in msg.lower() and ("not configured" in msg.lower() or "ask an admin" in msg.lower())


@pytest.mark.asyncio
async def test_on_post_reposts_prompt_after_game_message():
    """After posting a game round, _repost_prompt is called to move the
    sticky status bar below the new round."""
    from bot_modules.cogs.veil_cog import CropEditorView
    from bot_modules.services.veil_models import BoundingBox

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    bot.add_view = MagicMock()

    fake_channel = MagicMock(spec=discord.TextChannel)
    fake_channel.is_nsfw = MagicMock(return_value=True)
    fake_channel.send = AsyncMock(return_value=_fake_game_message())

    guild = FakeGuild(id=GUILD_ID)
    guild.channels[VEIL_CHANNEL_ID] = fake_channel

    interaction = fake_interaction(guild=guild)
    interaction.guild.get_channel = lambda cid: guild.channels.get(cid)

    view = CropEditorView(
        bot,
        b"fake-image",
        100,
        100,
        BoundingBox(0.0, 0.0, 1.0, 1.0),
        GUILD_ID,
        VEIL_CHANNEL_ID,
        submitter_id=1001,
        answer_id=1001,
        difficulty="medium",
        candidate_count=1,
    )

    with patch("bot_modules.cogs.veil_cog.render_crop", return_value=b"fake-crop"), \
         patch("bot_modules.cogs.veil_cog._do_insert_round", return_value=42), \
         patch("bot_modules.cogs.veil_cog._do_update_round_message"), \
         patch("bot_modules.cogs.veil_cog._do_audit"), \
         patch("bot_modules.cogs.veil_cog._repost_prompt", new_callable=AsyncMock) as mock_repost:
        await view._on_post(interaction)

    mock_repost.assert_awaited_once_with(bot, fake_channel, GUILD_ID)
