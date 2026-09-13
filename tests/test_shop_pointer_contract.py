"""Contract: every public paid-perk card advertises the shop, and truthfully.

Two members ever see a themed day or a Pin of the Day: the one who bought it,
and everyone else. The card in the channel is the only place the second group
learns the thing was purchasable at all, so it carries a pointer back to the
shop (todo #181).

The pointer is the fragile half. ``/bank theme``, ``/bank pin`` and
``/bank sponsor`` were **deleted** when the shop was reorganised into sections
(2026-08-29, commit 093b118f) — a card still naming one would be advertising a
command Discord answers with "unknown integration", and nothing else in the
suite would notice, because a string in an embed compiles fine. So the command
in the pointer is checked against the cog that actually defines it, and the
section caption against the table the shop renders from.
"""

from __future__ import annotations

import re
from pathlib import Path

import discord
import pytest

from bot_modules.economy.pin_views import render_pin_live_embed
from bot_modules.economy.shop import (
    SECTION_CAPTIONS,
    SECTION_SERVER,
    SHOP_POINTER,
    SHOP_POINTER_FIELD_NAME,
)
from bot_modules.economy.theme_views import render_theme_live_embed
from bot_modules.services.economy_service import EconSettings

_SETTINGS = EconSettings(
    currency_emoji="💎", currency_name="gem", currency_plural="gems", theme_hours=24
)

_COG = Path(__file__).resolve().parents[1] / "src/bot_modules/cogs/economy_cog.py"


def _name_fn(user_id: int) -> str:
    return f"Member{user_id}"


def _theme_card() -> discord.Embed:
    return render_theme_live_embed(
        discord.Color.blurple(),
        _SETTINGS,
        sponsor_id=42,
        name_fn=_name_fn,
        title="Cursed Cooking",
        blurb="Post the worst thing you have ever eaten.",
    )


def _pin_card() -> discord.Embed:
    return render_pin_live_embed(
        discord.Color.blurple(),
        sponsor_id=42,
        name_fn=_name_fn,
        message="Remember the movie night is Friday.",
    )


@pytest.mark.parametrize(
    "build, offer",
    [
        pytest.param(_theme_card, "themed day", id="flash-theme"),
        pytest.param(_pin_card, "Pin a message", id="pin-of-the-day"),
    ],
)
def test_public_paid_card_points_at_the_shop(build, offer: str) -> None:
    """Both cards carry the pointer field, naming their own product."""
    embed = build()
    pointers = [f for f in embed.fields if f.name == SHOP_POINTER_FIELD_NAME]
    assert len(pointers) == 1, "exactly one shop pointer per card"
    value = pointers[0].value or ""
    assert offer in value
    assert SHOP_POINTER in value


@pytest.mark.parametrize(
    "build",
    [pytest.param(_theme_card, id="flash-theme"), pytest.param(_pin_card, id="pin-of-the-day")],
)
def test_pointer_is_the_last_field(build) -> None:
    """The advertisement sits under the card's own content, never above it.

    The reader came for the theme or the pin; the offer is the footnote.
    """
    embed = build()
    assert embed.fields[-1].name == SHOP_POINTER_FIELD_NAME


def test_pointer_names_a_command_that_still_exists() -> None:
    """The trap this file exists for: a pointer outliving its command.

    Read from the cog source rather than by importing it — a cog import drags
    in the whole bot, and what is being asserted is a fact about the declared
    command surface, which the source states directly.
    """
    source = _COG.read_text(encoding="utf-8")
    cited = re.findall(r"/bank ([a-z_]+)", SHOP_POINTER)
    assert cited, "the pointer should name a command at all"
    for sub in cited:
        assert f'@bank.command(name="{sub}"' in source, (
            f"the shop pointer cites /bank {sub}, which economy_cog no longer "
            "defines — /bank theme, /bank pin and /bank sponsor were deleted "
            "in 093b118f; point members at a command that exists"
        )


def test_pointer_section_matches_the_shop_it_describes() -> None:
    """Rename the aisle and the signpost moves with it, or this fails."""
    assert SECTION_CAPTIONS[SECTION_SERVER] in SHOP_POINTER
