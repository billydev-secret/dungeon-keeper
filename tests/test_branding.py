"""Tests for the shared embed helpers in ``bot_modules.core.branding``.

``apply_section_spacing`` is the Embed-layer equivalent of the trailing
zero-width spacer the login digest and weekly leaderboard apply at their
string layer: it evens out a stacked-field embed's vertical rhythm so a
section heading doesn't hug the section above it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot_modules.core import branding
from bot_modules.core.branding import SECTION_SPACER, apply_section_spacing


def _embed(*values: str) -> discord.Embed:
    embed = discord.Embed(title="t")
    for i, v in enumerate(values):
        embed.add_field(name=f"F{i}", value=v, inline=False)
    return embed


def test_spacer_is_a_zero_width_line():
    # A newline plus U+200B — a printable char Discord won't strip as
    # trailing whitespace, so it renders as one extra empty line.
    assert SECTION_SPACER == "\n​"


def test_appends_spacer_to_every_field_but_the_last():
    embed = apply_section_spacing(_embed("a", "b", "c"))
    assert [f.value.endswith(SECTION_SPACER) for f in embed.fields] == [
        True,
        True,
        False,
    ]


def test_preserves_field_name_and_inline_flag():
    embed = discord.Embed(title="t")
    embed.add_field(name="First", value="a", inline=False)
    embed.add_field(name="Second", value="b", inline=False)
    apply_section_spacing(embed)
    assert embed.fields[0].name == "First"
    assert embed.fields[0].inline is False
    assert embed.fields[0].value == "a" + SECTION_SPACER


def test_single_field_is_untouched():
    embed = apply_section_spacing(_embed("only"))
    assert embed.fields[0].value == "only"


def test_empty_embed_is_a_no_op():
    embed = apply_section_spacing(discord.Embed(title="t"))
    assert len(embed.fields) == 0


def test_is_idempotent():
    embed = _embed("a", "b", "c")
    apply_section_spacing(embed)
    apply_section_spacing(embed)
    # Re-applying must not stack a second spacer on already-spaced fields.
    assert embed.fields[0].value == "a" + SECTION_SPACER
    assert embed.fields[1].value == "b" + SECTION_SPACER
    assert embed.fields[2].value == "c"


def test_returns_the_same_embed_for_chaining():
    embed = _embed("a", "b")
    assert apply_section_spacing(embed) is embed


# ── safe_resolve_accent ───────────────────────────────────────────────
#
# Twelve near-copies of this wrapper lived across the game cogs. They agreed on
# the shape — resolve the guild accent, and never let a branding hiccup crash a
# live game — but disagreed on the fallback, so ``default`` stayed per-caller.


class _Bot:
    def __init__(self, db_path="db.sqlite"):
        self.ctx = SimpleNamespace(db_path=db_path) if db_path else None


@pytest.mark.asyncio
async def test_resolves_through_to_the_real_accent(monkeypatch):
    guild = SimpleNamespace(id=7)
    monkeypatch.setattr(
        branding, "resolve_accent_color", AsyncMock(return_value=discord.Color(0x123456))
    )
    got = await branding.safe_resolve_accent(_Bot(), guild)  # type: ignore[arg-type]
    assert got == discord.Color(0x123456)
    branding.resolve_accent_color.assert_awaited_once_with("db.sqlite", guild)


@pytest.mark.asyncio
async def test_no_guild_returns_the_default_without_touching_the_db(monkeypatch):
    """A DM, or a channel whose .guild is None — nothing to brand."""
    monkeypatch.setattr(branding, "resolve_accent_color", AsyncMock())
    assert await branding.safe_resolve_accent(_Bot(), None) is None
    branding.resolve_accent_color.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_without_ctx_returns_the_default(monkeypatch):
    """Early startup, or a test double — no db_path to read branding from."""
    monkeypatch.setattr(branding, "resolve_accent_color", AsyncMock())
    assert await branding.safe_resolve_accent(_Bot(db_path=None), SimpleNamespace(id=7)) is None
    assert await branding.safe_resolve_accent(object(), SimpleNamespace(id=7)) is None
    branding.resolve_accent_color.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolution_failure_falls_back_rather_than_raising(monkeypatch):
    """The whole reason this wrapper exists: an embed with the wrong color
    still beats a game that crashes building it."""
    monkeypatch.setattr(
        branding, "resolve_accent_color", AsyncMock(side_effect=RuntimeError("db gone"))
    )
    assert await branding.safe_resolve_accent(_Bot(), SimpleNamespace(id=7)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("no_guild", id="no-guild"),
        pytest.param("no_ctx", id="no-ctx"),
        pytest.param("raises", id="resolve-raises"),
    ],
)
async def test_every_failure_path_honours_a_custom_default(monkeypatch, case):
    """chicken, musical chairs and pressure cooker fall back to their own
    yellow rather than letting discord.py pick."""
    YELLOW = 0xF1C40F
    monkeypatch.setattr(
        branding,
        "resolve_accent_color",
        AsyncMock(side_effect=RuntimeError("db gone") if case == "raises" else None),
    )
    bot = _Bot(db_path=None) if case == "no_ctx" else _Bot()
    guild = None if case == "no_guild" else SimpleNamespace(id=7)
    assert await branding.safe_resolve_accent(bot, guild, default=YELLOW) == YELLOW


@pytest.mark.asyncio
async def test_failure_is_logged_at_warning_under_the_callers_label(monkeypatch, caplog):
    """WARNING, not debug: the root logger runs at INFO, so a debug line here
    would be invisible in production and a branding table that has started
    raising would strip every embed's accent with no evidence anywhere."""
    monkeypatch.setattr(
        branding, "resolve_accent_color", AsyncMock(side_effect=RuntimeError("db gone"))
    )
    with caplog.at_level(logging.DEBUG, logger="bot_modules.core.branding"):
        await branding.safe_resolve_accent(
            _Bot(), SimpleNamespace(id=7), log_label="pressure"
        )
    (record,) = [r for r in caplog.records if r.name == "bot_modules.core.branding"]
    assert record.levelno == logging.WARNING
    assert "pressure" in record.getMessage()
    assert record.exc_info is not None  # the traceback rides along


@pytest.mark.asyncio
@pytest.mark.parametrize("guild", [None, SimpleNamespace(id=7)], ids=["no-guild", "no-ctx"])
async def test_guard_returns_stay_silent(monkeypatch, caplog, guild):
    """A DM or a ctx-less bot is ordinary — only a real failure is worth a line."""
    monkeypatch.setattr(branding, "resolve_accent_color", AsyncMock())
    bot = _Bot() if guild is None else _Bot(db_path=None)
    with caplog.at_level(logging.DEBUG, logger="bot_modules.core.branding"):
        await branding.safe_resolve_accent(bot, guild)
    assert [r for r in caplog.records if r.name == "bot_modules.core.branding"] == []


# ── adoption: the game cogs route through the guard ───────────────────

# The six cogs converted in the "guard the last six" commit. Scoped
# deliberately: ~80 direct resolve_accent_color calls remain elsewhere under
# cogs/ (economy, whisper, jail, voice_master, guess, music, ...), and
# converting those is a much larger behaviour change than this list. This
# asserts the six stay converted, not that the repo is finished.
GUARDED_GAME_COGS = [
    "games_ama_cog",
    "games_compliment_cog",
    "games_fantasies_cog",
    "games_mfk_cog",
    "games_story_cog",
    "games_traditional_cog",
]


@pytest.mark.parametrize("module", GUARDED_GAME_COGS)
def test_live_game_embeds_never_call_the_raw_resolver(module):
    """A branding hiccup must not raise into a live game's embed builder.

    These six built embeds with a bare ``await resolve_accent_color(...)``,
    so a raising branding read surfaced to the player as "This interaction
    failed" mid-game. They go through ``safe_resolve_accent`` now; the
    fallback behaviour itself is covered by the tests above.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "src/bot_modules/cogs"
        / f"{module}.py"
    ).read_text(encoding="utf-8")
    unguarded = [
        line.strip()
        for line in source.splitlines()
        if "resolve_accent_color(" in line and "safe_resolve_accent(" not in line
    ]
    assert not unguarded, (
        f"{module} calls resolve_accent_color directly again — use "
        "safe_resolve_accent so a branding failure can't break a live game:\n  "
        + "\n  ".join(unguarded)
    )
