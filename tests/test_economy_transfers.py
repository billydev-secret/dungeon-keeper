"""Pay-receipt rendering and the public/private downgrade rule.

The tests that matter here are the negative ones: the public receipt must not
carry the sender's balance, and a public receipt must be withheld from a
no-contact pair. Both are silent failures in production — nobody notices a
leak or a disclosed block by looking at a happy path.
"""
from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from bot_modules.economy.transfers import (
    build_payment_receipt,
    pay_disclosure,
    receipt_is_public,
)

ACCENT = discord.Color(0x5865F2)


def _settings(**over):
    base = dict(
        currency_emoji="🪙",
        currency_name="coin",
        currency_plural="coins",
        currency_icon_url=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _user(uid: int):
    return SimpleNamespace(id=uid, mention=f"<@{uid}>")


# ── the balance footer ───────────────────────────────────────────────────


def test_private_receipt_footers_the_senders_balance():
    embed = build_payment_receipt(
        _settings(), ACCENT, _user(500), _user(900), 40, balance=1234
    )
    assert embed.footer.text == "Your balance: 1,234"
    # The sender is reading their own ephemeral receipt; naming them is noise.
    assert "<@500>" not in (embed.description or "")
    assert "<@900>" in embed.description


def test_public_receipt_never_footers_a_balance_even_when_handed_one():
    """The leak this whole module exists to prevent.

    A caller passing ``balance`` alongside ``public=True`` is the realistic
    mistake — the builder must drop it rather than trust the caller.
    """
    embed = build_payment_receipt(
        _settings(), ACCENT, _user(500), _user(900), 40, balance=1234, public=True
    )
    assert embed.footer.text is None
    assert "1,234" not in str(embed.to_dict())


def test_public_receipt_names_both_members():
    """No interaction attribution rides a followup, so the embed carries it."""
    embed = build_payment_receipt(
        _settings(), ACCENT, _user(500), _user(900), 40, public=True
    )
    assert "<@500>" in embed.description
    assert "<@900>" in embed.description


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        pytest.param(1, "coin", id="singular"),
        pytest.param(2, "coins", id="plural"),
    ],
)
@pytest.mark.parametrize("public", [False, True], ids=["private", "public"])
def test_receipt_pluralises_the_unit_in_both_variants(amount, expected, public):
    embed = build_payment_receipt(
        _settings(), ACCENT, _user(500), _user(900), amount, public=public
    )
    assert f"**{amount:,}** {expected}" in embed.description


@pytest.mark.parametrize("public", [False, True], ids=["private", "public"])
def test_memo_renders_escaped_in_both_variants(public):
    """Billy's call: ticking public publishes the note, so it shows either way."""
    embed = build_payment_receipt(
        _settings(), ACCENT, _user(500), _user(900), 40,
        memo="for the *art*", public=public,
    )
    assert "for the \\*art\\*" in embed.description


def test_icon_url_thumbnails_when_configured():
    embed = build_payment_receipt(
        _settings(currency_icon_url="https://example.invalid/c.png"),
        ACCENT, _user(500), _user(900), 40,
    )
    assert embed.thumbnail.url == "https://example.invalid/c.png"


# ── the downgrade rule ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("requested", "blocked", "can_post", "expected"),
    [
        pytest.param(True, False, True, True, id="asked-for-and-allowed"),
        pytest.param(False, False, True, False, id="not-asked-for"),
        pytest.param(True, True, True, False, id="no-contact-pair"),
        pytest.param(True, False, False, False, id="cannot-post-here"),
        # Both reasons at once must not somehow re-enable it.
        pytest.param(True, True, False, False, id="blocked-and-cannot-post"),
    ],
)
def test_receipt_is_public_downgrades_on_either_reason(
    requested, blocked, can_post, expected
):
    assert receipt_is_public(requested, blocked=blocked, can_post=can_post) is expected


# ── the recipient's notification ─────────────────────────────────────────


@pytest.mark.parametrize(
    "requested,blocked,can_post,public,notify",
    [
        pytest.param(False, False, True, False, True, id="ordinary-private-pay"),
        pytest.param(True, False, True, True, True, id="ordinary-public-pay"),
        # The defect: a blocked pair used to be gated on the receipt only, and
        # the recipient's DM went out carrying the sender's name and memo.
        pytest.param(False, True, True, False, False, id="blocked-private-pay"),
        pytest.param(True, True, True, False, False, id="blocked-public-pay"),
        # A channel the bot can't speak in withholds the receipt and nothing
        # else — the recipient is still told about their own money.
        pytest.param(True, False, False, False, True, id="cannot-post-here"),
    ],
)
def test_pay_disclosure_gates_the_notification_on_no_contact_alone(
    requested, blocked, can_post, public, notify
):
    d = pay_disclosure(requested, blocked=blocked, can_post=can_post)
    assert d.public_receipt is public
    assert d.notify_recipient is notify
