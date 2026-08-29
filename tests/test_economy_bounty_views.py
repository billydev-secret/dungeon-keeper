"""Tests for the Bounty Board hub embed (economy/bounty_views.py).

The hub is the board's only entry point since ``/bounty`` was deleted, so its
copy is load-bearing: it is where a member learns what a bounty is, and its
list is the only place an open bounty is visible once chat has buried the card.
Covered here are the branches the copy actually has — the empty board, the rake
and refund lines appearing only when configured, the jump link only when a card
posted, and the "…and N more" tail that keeps a capped list honest.

The list *data* is the service's job (test_economy_bounty_service.py); this is
purely the rendering on top of it.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.economy.bounty_views import build_bounty_hub_embed
from bot_modules.services.economy_bounty_service import BountyBoardEntry
from bot_modules.services.economy_service import EconSettings

GUILD = 700
ACCENT = discord.Color(0x5865F2)


def _settings(**over) -> EconSettings:
    base: dict[str, object] = {
        "enabled": True,
        "bounty_channel_id": 555,
        "bounty_min_stake": 10,
        "bounty_max_open": 3,
        "bounty_expire_days": 14,
        "bounty_rake_pct": 0,
        "currency_plural": "coins",
    }
    base.update(over)
    return EconSettings(**base)  # type: ignore[arg-type]


def _entry(**over) -> BountyBoardEntry:
    base: dict[str, object] = {
        "bounty_id": 1,
        "title": "Draw the mascot",
        "pot": 340,
        "contributors": 4,
        "card_channel_id": 555,
        "card_message_id": 888,
    }
    base.update(over)
    return BountyBoardEntry(**base)  # type: ignore[arg-type]


def _field(embed: discord.Embed, name_contains: str) -> str:
    for field in embed.fields:
        if name_contains in str(field.name):
            return str(field.value)
    raise AssertionError(f"no field ~{name_contains!r} (have {[f.name for f in embed.fields]})")


def test_empty_board_invites_the_first_post():
    embed = build_bounty_hub_embed(ACCENT, _settings(), GUILD, [], open_total=0)
    assert "post the first one" in _field(embed, "Open bounties")


def test_lists_a_bounty_with_its_pot_backers_and_jump_link():
    embed = build_bounty_hub_embed(
        ACCENT, _settings(), GUILD, [_entry()], open_total=1
    )
    listing = _field(embed, "Open bounties")
    assert "Draw the mascot" in listing
    assert "340" in listing
    assert "4 backers" in listing
    assert f"https://discord.com/channels/{GUILD}/555/888" in listing


@pytest.mark.parametrize(
    ("contributors", "expected"),
    [
        pytest.param(0, "no backers yet", id="none"),
        pytest.param(1, "1 backer", id="singular"),
        pytest.param(2, "2 backers", id="plural"),
    ],
)
def test_backer_count_reads_naturally(contributors, expected):
    embed = build_bounty_hub_embed(
        ACCENT, _settings(), GUILD, [_entry(contributors=contributors)], open_total=1
    )
    assert expected in _field(embed, "Open bounties")


def test_a_bounty_whose_card_never_posted_is_listed_without_a_link():
    """post_bounty_card is best-effort — a card that failed to send must not
    take the bounty off the board, and must not render a broken jump link."""
    embed = build_bounty_hub_embed(
        ACCENT,
        _settings(),
        GUILD,
        [_entry(card_channel_id=0, card_message_id=0)],
        open_total=1,
    )
    listing = _field(embed, "Open bounties")
    assert "Draw the mascot" in listing
    assert "jump" not in listing


def test_capped_list_says_how_many_it_is_not_showing():
    embed = build_bounty_hub_embed(
        ACCENT, _settings(), GUILD, [_entry(), _entry(bounty_id=2)], open_total=9
    )
    assert "**7** more" in _field(embed, "Open bounties")


def test_uncapped_list_has_no_tail():
    embed = build_bounty_hub_embed(
        ACCENT, _settings(), GUILD, [_entry()], open_total=1
    )
    assert "more further up" not in _field(embed, "Open bounties")


def test_rake_line_only_when_a_rake_is_configured():
    off = build_bounty_hub_embed(
        ACCENT, _settings(bounty_rake_pct=0), GUILD, [], open_total=0
    )
    assert "house keeps" not in _field(off, "How it works")

    on = build_bounty_hub_embed(
        ACCENT, _settings(bounty_rake_pct=10), GUILD, [], open_total=0
    )
    assert "**10%**" in _field(on, "How it works")


def test_refund_promise_names_the_window_and_vanishes_when_expiry_is_off():
    """bounty_expire_days = 0 disables the sweep (expire_bounties returns []),
    so promising a refund would be a lie."""
    on = build_bounty_hub_embed(
        ACCENT, _settings(bounty_expire_days=14), GUILD, [], open_total=0
    )
    blurb = _field(on, "How it works")
    assert "**14 days**" in blurb
    assert "refunded in full" in blurb

    off = build_bounty_hub_embed(
        ACCENT, _settings(bounty_expire_days=0), GUILD, [], open_total=0
    )
    assert "refunded in full" not in _field(off, "How it works")


def test_blurb_uses_the_guild_currency_name():
    embed = build_bounty_hub_embed(
        ACCENT, _settings(currency_plural="doubloons"), GUILD, [], open_total=0
    )
    assert "doubloons" in _field(embed, "How it works")


# ── the 1024-char embed-field budget ───────────────────────────────────


def _entries(n: int, title_len: int = 30) -> list[BountyBoardEntry]:
    # Real snowflakes: a jump URL is ~96 chars, which is most of a line's cost.
    return [
        BountyBoardEntry(
            bounty_id=i,
            title="T" * title_len,
            pot=123456,
            contributors=4,
            card_channel_id=1532059736038441200,
            card_message_id=1532059736038441201,
        )
        for i in range(n)
    ]


@pytest.mark.parametrize("title_len", [20, 30, 60, 100])
@pytest.mark.parametrize("count", [8, 25])
def test_open_list_never_exceeds_the_embed_field_limit(title_len, count):
    """An over-length field is a 400 on send/edit that core.sticky swallows —
    it would freeze the board's only entry point on a stale render rather than
    failing loudly. Eight full-length titles overflow 1024 on their own."""
    embed = build_bounty_hub_embed(
        ACCENT,
        _settings(bounty_rake_pct=10),
        GUILD,
        _entries(count, title_len),
        open_total=count,
    )
    assert len(_field(embed, "Open bounties")) <= 1024


def test_whole_embed_stays_within_discord_limits():
    embed = build_bounty_hub_embed(
        ACCENT, _settings(bounty_rake_pct=10), GUILD, _entries(25, 100), open_total=25
    )
    assert len(embed) <= 6000
    for field in embed.fields:
        assert len(str(field.value)) <= 1024


def test_budget_dropped_lines_are_counted_in_the_tail():
    """Whatever the budget drops has to show up in "…and N more" — a partial
    board that reads as the whole board is the failure mode worth guarding."""
    entries = _entries(25, 100)
    embed = build_bounty_hub_embed(
        ACCENT, _settings(), GUILD, entries, open_total=25
    )
    listing = _field(embed, "Open bounties")
    shown = listing.count("• ")
    assert shown < len(entries)  # the budget really did bite
    assert f"**{25 - shown}** more" in listing


def test_long_titles_are_clipped_in_the_list_only():
    embed = build_bounty_hub_embed(
        ACCENT, _settings(), GUILD, [_entry(title="X" * 100)], open_total=1
    )
    listing = _field(embed, "Open bounties")
    assert "…" in listing
    assert "X" * 100 not in listing


def test_short_titles_are_left_alone():
    embed = build_bounty_hub_embed(
        ACCENT, _settings(), GUILD, [_entry(title="Draw the mascot")], open_total=1
    )
    assert "Draw the mascot" in _field(embed, "Open bounties")


# ── the award/cancel DMs link the card, and only the card ──────────────


def test_award_and_cancel_buttons_live_only_on_the_card_view():
    """The award/cancel DMs link ``interaction.message`` — that must be the card.

    Both handlers take the message the button was clicked on and hand its
    ``jump_url`` to the member they DM. That is only correct while those
    buttons live on ``BountyBoardView``, which ``_refresh_card`` attaches to
    the bounty's own card. The hub is a ``StickyPanel``: it is deleted and
    reposted as chat moves, so routing either button through it would start
    DMing members a permalink that is dead within minutes — the exact failure
    ``_frozen_card_link`` exists to avoid on the auction side.
    """
    from bot_modules.economy.bounty_views import (
        BountyAwardButton,
        BountyBoardView,
        BountyCancelButton,
        BountyHubView,
    )

    card_items = {type(item) for item in BountyBoardView(1).children}
    assert BountyAwardButton in card_items
    assert BountyCancelButton in card_items

    hub_items = {type(item) for item in BountyHubView().children}
    assert BountyAwardButton not in hub_items
    assert BountyCancelButton not in hub_items

