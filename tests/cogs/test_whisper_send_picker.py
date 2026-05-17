"""Cog-level: button-driven send flow (picker + compose modal)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.services.whisper_models import WhisperConfig
from tests.fakes import FakeMember, fake_interaction

SENDER, OTHER = 1001, 2001
ROLE = 7001


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=ROLE, channel_id=8001, log_channel_id=8002)


def _role_with_members(ids: list[int]) -> MagicMock:
    role = MagicMock()
    role.id = ROLE
    role.members = [FakeMember(id=i, display_name=f"User{i}") for i in ids]
    return role


# ── _on_send_click ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_click_opens_picker_excluding_self():
    from bot_modules.cogs.whisper_cog import (
        WhisperCog,
        WhisperFeedView,
        WhisperSendTargetSelectView,
    )
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog(bot)
    bot.get_cog = MagicMock(return_value=cog)
    view = WhisperFeedView(bot)

    role = _role_with_members([SENDER, OTHER, 3003])
    sender = FakeMember(id=SENDER)
    sender.roles = [role]
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await view._on_send_click(interaction)

    sent_kwargs = interaction.response.send_message.call_args.kwargs
    picker = sent_kwargs.get("view")
    assert isinstance(picker, WhisperSendTargetSelectView)
    member_ids = {m.id for m in picker._all_members}
    assert SENDER not in member_ids
    assert member_ids == {OTHER, 3003}


@pytest.mark.asyncio
async def test_send_click_rejects_empty_role():
    """No other opted-in members → friendly message, no picker."""
    from bot_modules.cogs.whisper_cog import WhisperFeedView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = WhisperFeedView(bot)

    role = _role_with_members([SENDER])  # only the sender
    sender = FakeMember(id=SENDER)
    sender.roles = [role]
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await view._on_send_click(interaction)

    args = interaction.response.send_message.call_args.args
    assert "no other" in args[0].lower() or "opted-in" in args[0].lower()


# ── WhisperSendTargetSelectView pagination / filter ──────────────────────────


def _make_picker(members: list[FakeMember]):
    from bot_modules.cogs.whisper_cog import WhisperCog, WhisperSendTargetSelectView
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog(bot)
    return WhisperSendTargetSelectView(cog, members, invoker_id=SENDER)  # type: ignore[arg-type]


def test_picker_no_pagination_with_under_25_members():
    import discord as d
    members = [FakeMember(id=4000 + i, display_name=f"u{i}") for i in range(10)]
    picker = _make_picker(members)
    selects = [c for c in picker.children if isinstance(c, d.ui.Select)]
    buttons = [c for c in picker.children if isinstance(c, d.ui.Button)]
    assert len(selects) == 1
    assert len(buttons) == 1  # filter button only — no prev/next


def test_picker_paginates_past_25_members():
    import discord as d
    members = [FakeMember(id=4000 + i, display_name=f"u{i:02d}") for i in range(30)]
    picker = _make_picker(members)
    buttons = [c for c in picker.children if isinstance(c, d.ui.Button)]
    # prev + next + filter = 3 buttons
    assert len(buttons) == 3


# ── _WhisperSendTargetSelect picks → opens compose modal ─────────────────────


@pytest.mark.asyncio
async def test_target_select_opens_compose_modal():
    from bot_modules.cogs.whisper_cog import (
        WhisperCog,
        WhisperSendComposeModal,
        _WhisperSendTargetSelect,
    )
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog(bot)

    members = [FakeMember(id=OTHER, display_name="bob")]
    sel = _WhisperSendTargetSelect(cog, members, 0, placeholder="Pick…")  # type: ignore[arg-type]
    sel._values = [str(OTHER)]  # simulate selection

    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.response.send_modal = AsyncMock()

    await sel.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
    sent_modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(sent_modal, WhisperSendComposeModal)
    assert sent_modal._target_id == OTHER


# ── WhisperSendComposeModal.on_submit → _send_impl ───────────────────────────


@pytest.mark.asyncio
async def test_compose_modal_submit_delegates_to_send_impl():
    from bot_modules.cogs.whisper_cog import WhisperCog, WhisperSendComposeModal
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog(bot)
    cog._send_impl = AsyncMock()  # type: ignore[method-assign]

    modal = WhisperSendComposeModal(cog, OTHER)
    modal.message_input._value = "an anonymous message"  # type: ignore[attr-defined]

    target_member = FakeMember(id=OTHER, display_name="bob")
    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = MagicMock()
    interaction.guild.get_member = MagicMock(return_value=target_member)
    interaction.response.send_message = AsyncMock()

    await modal.on_submit(interaction)

    cog._send_impl.assert_awaited_once()
    call_kwargs = cog._send_impl.call_args.kwargs
    assert call_kwargs["target"] is target_member
    assert call_kwargs["message"] == "an anonymous message"


@pytest.mark.asyncio
async def test_compose_modal_rejects_missing_guild():
    from bot_modules.cogs.whisper_cog import WhisperCog, WhisperSendComposeModal
    bot = MagicMock()
    cog = WhisperCog(bot)
    modal = WhisperSendComposeModal(cog, OTHER)
    modal.message_input._value = "x"  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = None
    interaction.response.send_message = AsyncMock()

    await modal.on_submit(interaction)

    args = interaction.response.send_message.call_args.args
    assert "server" in args[0].lower()


@pytest.mark.asyncio
async def test_compose_modal_rejects_left_target():
    """Target who left the server between picker open and modal submit → error."""
    from bot_modules.cogs.whisper_cog import WhisperCog, WhisperSendComposeModal
    bot = MagicMock()
    cog = WhisperCog(bot)
    modal = WhisperSendComposeModal(cog, OTHER)
    modal.message_input._value = "x"  # type: ignore[attr-defined]

    interaction = fake_interaction(user=FakeMember(id=SENDER))
    interaction.guild = MagicMock()
    interaction.guild.get_member = MagicMock(return_value=None)  # left server
    interaction.response.send_message = AsyncMock()

    await modal.on_submit(interaction)

    args = interaction.response.send_message.call_args.args
    assert "any more" in args[0].lower() or "not in" in args[0].lower()
