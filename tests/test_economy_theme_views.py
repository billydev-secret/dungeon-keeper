"""Tests for the flash-theme card and receipt copy (economy/theme_views.py).

The scope is one thing, and it is a money claim: ``expired`` is three
different endings — a request nobody reviewed in time (refunded), a theme that
ran its whole window, and one a mod ended early (neither refunded) — so the
state alone cannot say whether coins went back. Both the card and the DM read
``refunded_at`` instead. Branching on the state told a member their coins had
been returned when a mod ended their running theme, which is the worst thing a
bot can be wrong about.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.economy.theme_views import (
    render_theme_review_embed,
    theme_resolution_dm_text,
)
from bot_modules.services.economy_service import EconSettings
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED

_SETTINGS = EconSettings(
    currency_emoji="💎", currency_name="gem", currency_plural="gems", theme_hours=24
)


def _row(state: str, *, refunded_at: float | None) -> dict:
    return {
        "title": "Cursed Cooking",
        "blurb": "Post the worst thing you have ever eaten.",
        "price": 300,
        "state": state,
        "deny_reason": "too close to last week's",
        "refunded_at": refunded_at,
        "user_id": 42,
        "resolver_id": 7,
    }


def _card(state: str, *, refunded: bool) -> discord.Embed:
    return render_theme_review_embed(
        discord.Color.blurple(),
        _SETTINGS,
        sponsor_mention="<@42>",
        title="Cursed Cooking",
        blurb="Post the worst thing you have ever eaten.",
        price=300,
        state=state,
        resolver_id=7,
        deny_reason="too close to last week's",
        refunded=refunded,
    )


def _fields(embed: discord.Embed) -> dict[str, str]:
    return {f.name: f.value for f in embed.fields}


# ── the card ───────────────────────────────────────────────────────────


def test_a_theme_that_ran_is_not_shown_as_declined_and_refunded():
    embed = _card("expired", refunded=False)
    assert embed.title == "🎨 Theme Ended"
    assert embed.color != discord.Color(COLOR_RED)
    fields = _fields(embed)
    assert "↩️ Refund" not in fields
    assert fields["Done"] == "It had its day — no refund."


def test_a_request_nobody_reviewed_is_shown_as_refunded():
    fields = _fields(_card("expired", refunded=True))
    assert fields["↩️ Refund"] == "💎 **300** gems returned"


def test_a_declined_theme_still_shows_the_reason_and_the_refund():
    embed = _card("denied", refunded=True)
    assert embed.title == "❌ Theme Declined"
    fields = _fields(embed)
    assert fields["Reason"] == "too close to last week's"
    assert fields["↩️ Refund"] == "💎 **300** gems returned"


@pytest.mark.parametrize("state", ["approved", "live"])
def test_an_accepted_theme_reads_green_and_owes_nothing_back(state):
    embed = _card(state, refunded=False)
    assert embed.color == discord.Color(COLOR_GREEN)
    assert "↩️ Refund" not in _fields(embed)


def test_a_pending_card_carries_the_theme_and_the_idea():
    fields = _fields(_card("pending", refunded=False))
    assert fields["🎨 Theme"] == "Cursed Cooking"
    assert fields["📝 The idea"].startswith("Post the worst")


# ── the receipt ────────────────────────────────────────────────────────


def test_ending_a_running_theme_never_claims_a_refund():
    """The regression: a mod pressing End Early moved the row to `expired`,
    which the old state-based branch read as 'declined and refunded'."""
    text = theme_resolution_dm_text(_SETTINGS, _row("expired", refunded_at=None))
    assert "refunded" not in text.lower()
    assert "no refund" in text.lower()
    assert "Cursed Cooking" in text


def test_a_theme_nobody_reviewed_does_promise_the_refund():
    text = theme_resolution_dm_text(_SETTINGS, _row("expired", refunded_at=123.0))
    assert "have been refunded" in text


def test_a_declined_theme_promises_the_refund_and_gives_the_reason():
    text = theme_resolution_dm_text(_SETTINGS, _row("denied", refunded_at=123.0))
    assert "have been refunded" in text
    assert "too close to last week's" in text


def test_an_approved_theme_says_it_is_queued_not_live():
    """Approval posts nothing, so the receipt must not imply it is up."""
    text = theme_resolution_dm_text(_SETTINGS, _row("approved", refunded_at=None))
    assert "runs the next time the channel is free" in text


def test_a_live_theme_says_it_is_up_now():
    text = theme_resolution_dm_text(_SETTINGS, _row("live", refunded_at=None))
    assert "live now" in text and "24 hours" in text
