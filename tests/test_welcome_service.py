"""Tests for the welcome/leave embed builders — color follows the accent."""
from __future__ import annotations

from unittest.mock import MagicMock

import discord

from bot_modules.services.welcome_service import build_leave_embed, build_welcome_embed


def _member() -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.mention = "<@1>"
    member.display_name = "Alex"
    member.id = 1
    guild = MagicMock()
    guild.name = "Test Guild"
    guild.member_count = 42
    guild.icon = None
    member.guild = guild
    member.display_avatar.url = "https://cdn.example/a.png"
    return member


def test_welcome_embed_uses_passed_accent():
    accent = discord.Color(0x123456)
    embed = build_welcome_embed(_member(), "hi {member}", color=accent)
    assert embed.color == accent
    assert embed.color != discord.Color.blurple()


def test_leave_embed_uses_passed_accent():
    accent = discord.Color(0x123456)
    embed = build_leave_embed(_member(), "bye {member_name}", color=accent)
    assert embed.color == accent
    assert embed.color != discord.Color.dark_gray()


def test_welcome_embed_falls_back_when_no_color():
    embed = build_welcome_embed(_member(), "hi")
    assert embed.color == discord.Color.blurple()
