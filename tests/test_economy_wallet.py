"""Wallet embed helpers — memo parsing, memo shortening, rental attribution.

Pure-function tests that moved here with ``build_wallet_embed`` when it left
``economy_cog.py``. The wallet *command* wiring (the ephemeral send, the
leaderboard panel's Wallet button) stays in ``test_economy_cog.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

from bot_modules.economy.view_helpers import fit_lines
from bot_modules.economy.wallet import ellipsis, memo_of, rental_lines


def test_memo_of_tolerates_missing_and_malformed_meta():
    assert memo_of('{"to": 1, "memo": "hi"}') == "hi"
    assert memo_of('{"to": 1}') is None
    assert memo_of(None) is None
    assert memo_of("") is None
    assert memo_of("not json") is None
    # A non-string memo must not crash the render.
    assert memo_of('{"memo": 5}') is None


def test_fit_lines_keeps_newest_rows_under_the_field_cap():
    short = ["a", "b", "c"]
    assert fit_lines(short) == "a\nb\nc"
    # Ten max-length memo rows must not overrun the 1024-char embed field.
    fat = [("x" * 200) for _ in range(10)]
    out = fit_lines(fat)
    assert len(out) <= 1024
    assert out.startswith("x")


def test_ellipsis_trims_trailing_space_before_the_dot():
    assert ellipsis("short memo") == "short memo"
    long_memo = "w" * 60
    assert ellipsis(long_memo).endswith("…")
    assert len(ellipsis(long_memo)) == 40
    # A cut landing on a space must not leave " …".
    assert not ellipsis(("ab " * 30)).endswith(" …")


def _rental(perk, *, owner, beneficiary, state="active"):
    return {
        "perk": perk, "price": 50, "next_bill_at": 1_700_000_000,
        "user_id": owner, "beneficiary_id": beneficiary, "state": state,
    }


def _settings():
    return SimpleNamespace(currency_emoji="🪙")


def test_rental_lines_attribute_gifts_in_both_directions():
    """A gift reads differently to the payer and to the wearer."""
    settings = _settings()

    received = rental_lines(
        settings, [_rental("role_color", owner=1, beneficiary=500)], 500
    )
    assert "(gift received)" in received[0]

    sent = rental_lines(
        settings, [_rental("role_color", owner=500, beneficiary=1)], 500
    )
    assert "(gift to <@1>)" in sent[0]

    # Self-rented: no attribution at all.
    own = rental_lines(
        settings, [_rental("role_color", owner=500, beneficiary=500)], 500
    )
    assert "gift" not in own[0]


def test_rental_lines_mark_grace_state():
    line = rental_lines(
        _settings(),
        [_rental("role_name", owner=500, beneficiary=500, state="grace")],
        500,
    )[0]
    assert "⏳ in grace" in line
    assert "Custom Role Name" in line
