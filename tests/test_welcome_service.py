"""Tests for services/welcome_service.py — the greeter arrival line.

The arrival line used to be a hard-coded ``@here - {mention} has arrived``
f-string in events_cog with no dashboard control. It is now one per-guild
template: the copy *and* the ping live in the same box, and an empty box means
"post nothing".
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.services.welcome_service import (
    DEFAULT_ARRIVAL_MESSAGE,
    render_arrival_message,
)


def _member(*, mention: str = "<@500>", display_name: str = "Newbie",
            guild_name: str = "The Meadow") -> MagicMock:
    member = MagicMock()
    member.mention = mention
    member.display_name = display_name
    member.guild = MagicMock()
    member.guild.name = guild_name
    return member


def test_default_template_keeps_the_historical_line():
    """Guilds that never touch the dial keep exactly what they had."""
    assert render_arrival_message(DEFAULT_ARRIVAL_MESSAGE, _member()) == (
        "@here - <@500> has arrived"
    )


@pytest.mark.parametrize(
    "template,expected",
    [
        ("{member} is here", "<@500> is here"),
        ("{mention} is here", "<@500> is here"),          # birthday-panel alias
        ("{member_name} joined {server}", "Newbie joined The Meadow"),
        ("Say hi to {member_name}!", "Say hi to Newbie!"),
        ("  padded  ", "padded"),
    ],
)
def test_placeholders_and_trimming(template, expected):
    assert render_arrival_message(template, _member()) == expected


@pytest.mark.parametrize("template", ["", "   ", "\n\t ", None])
def test_blank_template_is_the_off_switch(template):
    """A cleared box posts nothing rather than an empty message."""
    assert render_arrival_message(template, _member()) == ""


@pytest.mark.parametrize(
    "display_name,forbidden",
    [
        ("@everyone", "@everyone"),
        ("@here", "@here"),
        ("<@&123456789012345678>", "<@&123456789012345678>"),
    ],
)
def test_a_hostile_display_name_cannot_ping(display_name, forbidden):
    """The arrival line is plain content, so a name is not a safe splice.

    The line already needs Mention Everyone for its ``@here``; a newcomer who
    renames themselves ``@everyone`` must not ride that permission. Only the
    member-controlled half is escaped — the admin's own ``@here`` survives.
    """
    out = render_arrival_message(
        "@here - {member_name} has arrived", _member(display_name=display_name)
    )
    assert out.startswith("@here - ")          # the admin's own ping survives
    name_part = out[len("@here - "):]
    assert forbidden not in name_part          # the member's does not
    # Neutralised, not deleted — the name still reads in the channel.
    assert discord.utils.escape_mentions(display_name) in name_part


def test_a_guild_can_drop_the_here_ping():
    """The whole point of the dial: no forced @here."""
    out = render_arrival_message("{member_name} has arrived", _member())
    assert "@here" not in out
    assert out == "Newbie has arrived"
