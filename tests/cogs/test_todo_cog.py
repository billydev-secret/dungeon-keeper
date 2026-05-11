"""Tests for cogs.todo_cog helpers and the context-menu modal."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cogs.todo_cog import _format_description, _format_task_label, _MAX_CONTENT_LEN  # noqa: E402


# ── _format_task_label ────────────────────────────────────────────────


def test_label_uses_display_name_and_channel_name():
    assert (
        _format_task_label(author_display="Alice", channel_name="general")
        == "Message from @Alice in #general"
    )


# ── _format_description ───────────────────────────────────────────────


def test_description_message_only():
    assert _format_description(message_content="hello world", notes="") == "hello world"


def test_description_notes_only_uses_no_text_marker():
    assert (
        _format_description(message_content="", notes="follow up next week")
        == "[no text content]\n\nfollow up next week"
    )


def test_description_both_joined_with_blank_line():
    assert (
        _format_description(message_content="hello", notes="follow up")
        == "hello\n\nfollow up"
    )


def test_description_neither_uses_no_text_marker():
    assert _format_description(message_content="", notes="") == "[no text content]"


def test_description_whitespace_only_content_uses_no_text_marker():
    assert _format_description(message_content="   \n\t ", notes="") == "[no text content]"
    assert (
        _format_description(message_content="   ", notes="follow up")
        == "[no text content]\n\nfollow up"
    )


def test_description_truncates_long_content():
    long = "x" * (_MAX_CONTENT_LEN + 50)
    out = _format_description(message_content=long, notes="")
    assert out.endswith("…")
    assert len(out) == _MAX_CONTENT_LEN + 1  # MAX chars + the ellipsis


def test_description_truncates_long_content_with_notes():
    long = "x" * (_MAX_CONTENT_LEN + 50)
    out = _format_description(message_content=long, notes="note")
    head, _, tail = out.partition("\n\n")
    assert head.endswith("…")
    assert len(head) == _MAX_CONTENT_LEN + 1
    assert tail == "note"


# ── _TodoFromMessageModal.on_submit ──────────────────────────────────

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from tests.fakes import FakeGuild, FakeRole, FakeUser, fake_interaction  # noqa: E402


def _build_message(content: str, jump_url: str = "https://discord.com/channels/1/2/3"):
    msg = MagicMock()
    msg.content = content
    msg.jump_url = jump_url
    msg.author = MagicMock()
    msg.author.display_name = "Alice"
    msg.channel = MagicMock()
    msg.channel.name = "general"
    return msg


def _build_mod_ctx():
    """A MagicMock AppContext where _is_mod will return True for our user."""
    ctx = MagicMock()
    ctx.mod_role_ids = {5001}
    ctx.admin_role_ids = set()
    return ctx


def _build_mod_user():
    role = FakeRole(id=5001, name="Mod")
    user = FakeUser(id=2001, name="mod_user", roles=[role])
    user.guild_permissions = MagicMock(manage_guild=False, administrator=False)
    return user


@pytest.mark.asyncio
async def test_modal_submit_calls_create_todo_with_formatted_args(monkeypatch):
    from cogs.todo_cog import _TodoFromMessageModal

    captured = {}

    def fake_create_todo(conn, guild_id, added_by, task, **kwargs):
        captured["guild_id"] = guild_id
        captured["added_by"] = added_by
        captured["task"] = task
        captured.update(kwargs)
        return 42

    monkeypatch.setattr("cogs.todo_cog.create_todo", fake_create_todo)

    ctx = _build_mod_ctx()
    ctx.open_db.return_value.__enter__ = lambda s: MagicMock()
    ctx.open_db.return_value.__exit__ = lambda s, a, b, c: False

    msg = _build_message("the original message text")
    modal = _TodoFromMessageModal(message=msg, ctx=ctx)
    modal.notes._value = "follow up tuesday"  # set the TextInput value directly

    user = _build_mod_user()
    guild = FakeGuild(id=9001)
    interaction = fake_interaction(user=user, guild=guild)

    await modal.on_submit(interaction)

    assert captured["guild_id"] == 9001
    assert captured["added_by"] == 2001
    assert captured["task"] == "Message from @Alice in #general"
    assert captured["description"] == "the original message text\n\nfollow up tuesday"
    assert captured["source_message_url"] == "https://discord.com/channels/1/2/3"
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "42" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_modal_submit_blank_notes_only_stores_message(monkeypatch):
    from cogs.todo_cog import _TodoFromMessageModal

    captured = {}

    def fake_create_todo(conn, guild_id, added_by, task, **kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr("cogs.todo_cog.create_todo", fake_create_todo)

    ctx = _build_mod_ctx()
    ctx.open_db.return_value.__enter__ = lambda s: MagicMock()
    ctx.open_db.return_value.__exit__ = lambda s, a, b, c: False

    modal = _TodoFromMessageModal(message=_build_message("just the message"), ctx=ctx)
    modal.notes._value = ""

    interaction = fake_interaction(user=_build_mod_user(), guild=FakeGuild(id=9001))
    await modal.on_submit(interaction)

    assert captured["description"] == "just the message"


@pytest.mark.asyncio
async def test_context_menu_callback_opens_modal_for_any_member(monkeypatch):
    """The context menu is open to everyone — clicking it opens the modal regardless of role.

    Server todos are community-curated; the slash /todo and the context
    menu now share one permission model (open). This test guards against
    accidentally re-locking the context menu to mods.
    """
    from cogs.todo_cog import TodoCog

    bot = MagicMock()
    bot.tree = MagicMock()
    ctx = _build_mod_ctx()
    cog = TodoCog.__new__(TodoCog)
    cog.bot = bot
    cog.ctx = ctx

    # Resolve the registered callback by exercising cog_load to register it,
    # then pull it out of bot.tree.add_command's call args.
    await cog.cog_load()
    add_command_calls = bot.tree.add_command.call_args_list
    ctx_menu = next(
        c.args[0] for c in add_command_calls
        if getattr(c.args[0], "name", None) == "Add to Todo"
    )

    # A non-mod user should still be able to open the modal.
    non_mod = FakeUser(id=3001, name="regular", roles=[])
    non_mod.guild_permissions = MagicMock(manage_guild=False, administrator=False)
    interaction = fake_interaction(user=non_mod, guild=FakeGuild(id=9001))

    await ctx_menu.callback(interaction, _build_message("hi"))

    interaction.response.send_modal.assert_awaited_once()
    interaction.response.send_message.assert_not_called()
