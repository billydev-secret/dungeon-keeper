"""Cog-level: /whisper send command."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.whisper_models import WhisperConfig
from tests.fakes import FakeMember, FakeRole, fake_interaction

ROLE = 7001
FEED = 8001
LOG = 8002
SENDER_ID = 1001
TARGET_ID = 2001


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=ROLE, channel_id=FEED, log_channel_id=LOG)


def _make_cog():
    from bot_modules.cogs.whisper_cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog.__new__(WhisperCog)
    cog.bot = bot
    cog.ctx = bot.ctx
    cog._last_send_at = {}
    cog._target_sends = {}
    return cog


def _make_target_dmable():
    target = FakeMember(id=TARGET_ID, display_name="Target", roles=[FakeRole(id=ROLE)])
    target.send = AsyncMock(return_value=MagicMock(id=99999))  # type: ignore[attr-defined]
    return target


@pytest.mark.asyncio
async def test_send_happy_path():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, display_name="Sender", roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=88888))
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(side_effect=lambda cid: {FEED: feed_channel, LOG: log_channel}.get(cid))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_insert_whisper", return_value=42), \
         patch("bot_modules.cogs.whisper_cog._do_set_message_ids") as set_ids:
        await cog._send_impl(interaction, target=target, message="hello world")  # type: ignore[arg-type]

    target.send.assert_awaited_once()  # type: ignore[attr-defined]
    feed_channel.send.assert_awaited_once()
    set_ids.assert_called_once_with(":memory:", 42, channel_msg_id=88888, dm_msg_id=99999)
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_send_rejects_when_sender_lacks_role():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[])  # no role
    target = _make_target_dmable()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await cog._send_impl(interaction, target=target, message="hi")  # type: ignore[arg-type]

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "role" in args[0].lower()


@pytest.mark.asyncio
async def test_send_rejects_self_target():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await cog._send_impl(interaction, target=sender, message="hi")  # type: ignore[arg-type]

    args, kwargs = interaction.response.send_message.call_args
    assert "yourself" in args[0].lower()


@pytest.mark.asyncio
async def test_send_picker_lists_only_role_members_excluding_self():
    """/whisper send opens the picker with opt-in members (minus the caller)."""
    from bot_modules.cogs.whisper_cog import WhisperSendTargetSelectView
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    role = MagicMock()
    role.id = ROLE
    role.members = [
        sender,
        FakeMember(id=2001, display_name="Alice", name="alice"),
        FakeMember(id=2002, display_name="Bob", name="bob"),
    ]
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await cog._open_send_picker(interaction)

    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    sent_kwargs = interaction.followup.send.call_args.kwargs
    picker = sent_kwargs.get("view")
    assert isinstance(picker, WhisperSendTargetSelectView)
    member_ids = {m.id for m in picker._all_members}
    assert member_ids == {2001, 2002}


@pytest.mark.asyncio
async def test_send_picker_rejects_when_role_unset():
    """/whisper send errors when whisper role isn't configured."""
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    cfg_no_role = WhisperConfig(guild_id=9001, role_id=0, channel_id=FEED, log_channel_id=LOG)
    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg_no_role):
        await cog._open_send_picker(interaction)

    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    args = interaction.followup.send.call_args.args
    assert "configured" in args[0].lower() or "set up" in args[0].lower()


@pytest.mark.asyncio
async def test_send_picker_rejects_when_caller_lacks_role():
    """/whisper send tells callers without the whisper role to opt in first."""
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[])  # not opted-in
    role = MagicMock()
    role.id = ROLE
    role.members = [FakeMember(id=2001)]
    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()):
        await cog._open_send_picker(interaction)

    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    args = interaction.followup.send.call_args.args
    assert "optin" in args[0].lower() or "role" in args[0].lower()


@pytest.mark.asyncio
async def test_send_slash_command_opens_picker():
    """The /whisper send slash command delegates to _open_send_picker."""
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    interaction = fake_interaction(user=sender)
    interaction.guild = None  # easy short-circuit — just confirm the call path
    interaction.response.send_message = AsyncMock()

    await cog.whisper_send.callback(cog, interaction)

    args = interaction.response.send_message.call_args.args
    assert "server" in args[0].lower()


@pytest.mark.asyncio
async def test_send_dm_forbidden_does_not_persist():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = FakeMember(id=TARGET_ID, roles=[FakeRole(id=ROLE)])
    target.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no dms"))  # type: ignore[attr-defined]

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock()
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(side_effect=lambda cid: {FEED: feed_channel, LOG: log_channel}.get(cid))
    interaction.response.send_message = AsyncMock()

    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("bot_modules.cogs.whisper_cog._do_insert_whisper", return_value=42), \
         patch("bot_modules.cogs.whisper_cog._do_delete_whisper") as mocked_delete:
        await cog._send_impl(interaction, target=target, message="hi")  # type: ignore[arg-type]
        mocked_delete.assert_called_once()

    feed_channel.send.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert "DM" in args[0] or "deliver" in args[0].lower()


# ── B3: rate-limit tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_blocked_by_global_cooldown():
    """A second send within SEND_COOLDOWN_SECONDS should be rejected."""
    from unittest.mock import patch as _patch
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(return_value=None)
    interaction.response.send_message = AsyncMock()

    # Simulate that a send just happened 5 seconds ago
    import time as _t
    cog._last_send_at[SENDER_ID] = _t.time() - 5

    with _patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         _patch("bot_modules.cogs.whisper_cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=target, message="spam")  # type: ignore[arg-type]

    ins.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "wait" in args[0].lower() or "slow" in args[0].lower()


@pytest.mark.asyncio
async def test_send_blocked_by_per_target_hourly_cap():
    """Exceeding SEND_PER_TARGET_HOURLY_CAP whispers to same target in 1h is rejected."""
    from bot_modules.cogs.whisper_cog import SEND_PER_TARGET_HOURLY_CAP
    from unittest.mock import patch as _patch
    import time as _t

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(return_value=None)
    interaction.response.send_message = AsyncMock()

    # Fill up cap for this (guild, sender, target) triple
    now = _t.time()
    rate_key = (9001, SENDER_ID, TARGET_ID)
    cog._target_sends[rate_key] = [now - i * 60 for i in range(SEND_PER_TARGET_HOURLY_CAP)]
    # And reset global cooldown so it doesn't block first
    cog._last_send_at[SENDER_ID] = 0

    with _patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         _patch("bot_modules.cogs.whisper_cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=target, message="spam")  # type: ignore[arg-type]

    ins.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "hour" in args[0].lower() or str(SEND_PER_TARGET_HOURLY_CAP) in args[0]


@pytest.mark.asyncio
async def test_send_allowed_after_cooldown_elapses():
    """After the cooldown window, the send should proceed normally."""
    from unittest.mock import patch as _patch
    import time as _t

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=88888))
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(
        side_effect=lambda cid: {FEED: feed_channel, LOG: log_channel}.get(cid)
    )
    interaction.response.send_message = AsyncMock()

    # Cooldown already elapsed (> 30s ago)
    cog._last_send_at[SENDER_ID] = _t.time() - 60

    with _patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         _patch("bot_modules.cogs.whisper_cog._do_insert_whisper", return_value=42), \
         _patch("bot_modules.cogs.whisper_cog._do_set_message_ids"):
        await cog._send_impl(interaction, target=target, message="hello again")  # type: ignore[arg-type]

    target.send.assert_awaited_once()  # type: ignore[attr-defined]
    interaction.response.send_message.assert_awaited()


# ── S3: block whispers to timed-out members ──────────────────────────────────


@pytest.mark.asyncio
async def test_send_blocked_when_target_is_timed_out():
    """Whisper to a timed-out member should be rejected before insert."""
    from unittest.mock import patch as _patch
    import time as _t

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])

    timed_out_target = FakeMember(id=TARGET_ID, roles=[FakeRole(id=ROLE)])
    timed_out_target.is_timed_out = lambda: True  # type: ignore[attr-defined]
    timed_out_target.send = AsyncMock()  # type: ignore[attr-defined]

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(return_value=None)
    interaction.response.send_message = AsyncMock()

    # Ensure no cooldown
    cog._last_send_at[SENDER_ID] = _t.time() - 60

    with _patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         _patch("bot_modules.cogs.whisper_cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=timed_out_target, message="hi")  # type: ignore[arg-type]

    ins.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "timed out" in args[0].lower()


# ── S8: hard-fail when feed/log channel missing ───────────────────────────────


@pytest.mark.asyncio
async def test_send_fails_when_feed_channel_missing():
    """When feed channel is None/invalid, send should error before DB insert."""
    from unittest.mock import patch as _patch
    import time as _t

    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    # Feed channel returns None; log channel returns a valid TextChannel
    log_channel = MagicMock(spec=discord.TextChannel)
    interaction.guild.get_channel = MagicMock(
        side_effect=lambda cid: None if cid == FEED else log_channel
    )
    interaction.response.send_message = AsyncMock()

    cog._last_send_at[SENDER_ID] = _t.time() - 60  # cooldown elapsed

    with _patch("bot_modules.cogs.whisper_cog._load_config", return_value=_cfg()), \
         _patch("bot_modules.cogs.whisper_cog._do_insert_whisper") as ins:
        await cog._send_impl(interaction, target=target, message="hi")  # type: ignore[arg-type]

    ins.assert_not_called()
    target.send.assert_not_called()  # type: ignore[attr-defined]
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "feed" in args[0].lower() or "channel" in args[0].lower()
