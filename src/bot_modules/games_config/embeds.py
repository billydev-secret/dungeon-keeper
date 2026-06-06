"""Embed builders for the games-config admin cog.

These functions return ``discord.Embed`` objects ready to send. They
delegate body-text decisions to :mod:`bot_modules.games_config.logic`
so the cog itself becomes a thin one-liner per command.

All embeds use the cluster's shared palette from
:mod:`bot_modules.games.constants` so admin output matches gameplay
output at a glance.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import discord

from bot_modules.games.constants import (
    ERROR_COLOR,
    BRAND_COLOR,
    SUCCESS_COLOR,
)

from .logic import (
    ChannelResolver,
    audit_channel_change,
    describe_active_game,
    describe_force_end,
    format_allowed_channels,
)


def build_channel_allowed_embed(channel_mention: str) -> discord.Embed:
    """Embed shown after ``/games allow-channel`` succeeds."""
    return discord.Embed(
        title="✅ Channel Allowed",
        description=f"{channel_mention} is now a game channel.",
        color=SUCCESS_COLOR,
    )


def build_channel_disallowed_embed(channel_mention: str) -> discord.Embed:
    """Embed shown after ``/games disallow-channel`` succeeds."""
    return discord.Embed(
        title="✅ Channel Removed",
        description=f"{channel_mention} is no longer a game channel.",
        color=SUCCESS_COLOR,
    )


def build_channel_list_embed(
    rows: Sequence[Sequence[Any]],
    resolver: ChannelResolver,
) -> discord.Embed:
    """Embed shown for ``/games list-channels``."""
    return discord.Embed(
        title="Game Channels",
        description=format_allowed_channels(rows, resolver),
        color=BRAND_COLOR,
    )


def build_game_status_embed(row: Any) -> discord.Embed:
    """Embed shown for ``/games game-status``.

    ``row`` is the ``games_active_games`` row (or None when no game is
    running in the channel). Color is the neutral meadow gold in both
    branches — the title carries the state.
    """
    title, description = describe_active_game(row)
    return discord.Embed(
        title=title,
        description=description,
        color=BRAND_COLOR,
    )


def build_force_end_embed(game_type: str) -> discord.Embed:
    """Embed shown after ``/games game-end`` force-closes a game."""
    return discord.Embed(
        title="🛑 Game Force-Closed",
        description=describe_force_end(game_type),
        color=ERROR_COLOR,
    )


def build_audit_channel_embed(channel_id: int | None) -> discord.Embed:
    """Embed shown after ``/games audit-channel`` sets or clears the channel."""
    title, description = audit_channel_change(channel_id)
    return discord.Embed(
        title=title,
        description=description,
        color=SUCCESS_COLOR,
    )
