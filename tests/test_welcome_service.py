"""Tests for services/welcome_service.py — the greeter arrival line.

The arrival line used to be a hard-coded ``@here - {mention} has arrived``
f-string in events_cog with no dashboard control. It is now one per-guild
template: the copy *and* the ping live in the same box, and an empty box means
"post nothing".
"""

from __future__ import annotations

from unittest.mock import MagicMock

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


def test_a_guild_can_drop_the_here_ping():
    """The whole point of the dial: no forced @here."""
    out = render_arrival_message("{member_name} has arrived", _member())
    assert "@here" not in out
    assert out == "Newbie has arrived"
