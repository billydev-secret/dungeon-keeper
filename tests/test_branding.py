"""Tests for the shared embed helpers in ``bot_modules.core.branding``.

``apply_section_spacing`` is the Embed-layer equivalent of the trailing
zero-width spacer the login digest and weekly leaderboard apply at their
string layer: it evens out a stacked-field embed's vertical rhythm so a
section heading doesn't hug the section above it.
"""

from __future__ import annotations

import discord

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
