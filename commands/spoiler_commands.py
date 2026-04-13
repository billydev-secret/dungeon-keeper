"""Spoiler guard management commands (now configured via /config spoiler)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app_context import AppContext, Bot


def register_spoiler_commands(bot: Bot, ctx: AppContext) -> None:
    pass
