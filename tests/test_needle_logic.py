"""Mention-injection guard for the Needle auto-thread welcome message.

``_apply_variables`` substitutes a member-controlled display name into the
welcome template that the bot then *sends and pins*. A nickname like
``<@&roleId>`` or ``@everyone`` must not survive as a live ping. These tests
exercise the substitution helper directly (logic layer) — no Discord needed.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from bot_modules.cogs.needle_cog import _apply_variables


def _msg(display: str, channel_id: int = 555) -> Any:
    author = SimpleNamespace(display_name=display, name=display)
    return cast(Any, SimpleNamespace(author=author, channel=SimpleNamespace(id=channel_id)))


def _thread(mention: str = "<#999>") -> Any:
    return cast(Any, SimpleNamespace(mention=mention))


def test_apply_variables_neutralizes_role_mention_in_nickname():
    out = _apply_variables(
        "Thread created by $USER in $CHANNEL",
        message=_msg("<@&123456789012345678>"),
        thread=_thread(),
    )
    assert "<@&123456789012345678>" not in out
    assert "​" in out  # zero-width break inserted by escape_mentions


def test_apply_variables_neutralizes_everyone_in_nickname():
    out = _apply_variables(
        "Welcome $USER",
        message=_msg("@everyone"),
        thread=_thread(),
    )
    assert "@everyone" not in out


def test_apply_variables_keeps_channel_and_thread_refs():
    out = _apply_variables(
        "$USER in $CHANNEL — $THREAD",
        message=_msg("Alice", channel_id=42),
        thread=_thread("<#777>"),
    )
    assert "Alice" in out
    assert "<#42>" in out
    assert "<#777>" in out
