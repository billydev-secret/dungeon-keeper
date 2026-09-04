"""The nine states of a bot-managed role, and the sentence each one gets.

The Bot-Managed Roles page is an audit surface — its whole value is that an
admin can trust what it says — so the state machine behind it is a table test
with one row per state, plus the four judgement calls that are easy to get
subtly wrong: which "(none)" is which, deleted-versus-inherited, when hierarchy
is even relevant, and which cards may carry a write button at all.
"""

from __future__ import annotations

import pytest

from bot_modules.services import feature_roles as fr
from bot_modules.services.role_provenance import RoleProvenance
from bot_modules.services.role_roster_service import (
    ADOPTABLE,
    DELETED,
    IN_USE,
    INHERITED,
    NOT_MADE,
    OFFER_FIRST,
    OUT_OF_REACH,
    TURNED_OFF,
    DialReading,
    LiveRole,
    describe_role,
    summary_line,
)

WELCOME = fr.WELCOME_PING            # mention-only, config KV
JAILED = fr.JAILED_ROLE              # handed out, no coherent "off"
GUESS = fr.GUESS_ROLE                # create-on-offer
WELLNESS = fr.WELLNESS_ROLE          # owned by another feature's store


def _role(rid=500, name="Welcome Ping", position=3, members=0, managed=False):
    return LiveRole(
        id=rid, name=name, position=position, managed=managed, member_count=members
    )


@pytest.mark.parametrize(
    "entry, reading, expected",
    [
        # Nothing stored, nothing named: the ordinary fresh-server case.
        pytest.param(WELCOME, DialReading(), NOT_MADE, id="not-made"),
        # Stored id resolves — the steady state.
        pytest.param(
            WELCOME, DialReading(stored_id=500, live_role=_role()), IN_USE,
            id="in-use",
        ),
        # An explicit "(none)" the dial honours.
        pytest.param(WELCOME, DialReading(opted_out=True), TURNED_OFF, id="off"),
        # Stored, this guild's own row, resolves to nothing: a real deletion.
        pytest.param(
            WELCOME, DialReading(stored_id=500, stored_is_own=True), DELETED,
            id="deleted",
        ),
        # Stored via the legacy guild_id=0 fallback: NOT a deletion. This is
        # the distinction that stopped the bot accusing a second guild of
        # deleting a role it never had.
        pytest.param(
            WELCOME, DialReading(stored_id=500, stored_is_own=False), INHERITED,
            id="inherited",
        ),
        # Nothing stored, but a role of the right name is sitting there — the
        # provisioner would adopt it rather than making a twin.
        pytest.param(
            WELCOME, DialReading(named_matches=(_role(),)), ADOPTABLE,
            id="adoptable",
        ),
        # A role the bot hands out, sitting above the bot's own top role.
        pytest.param(
            JAILED,
            DialReading(
                stored_id=9, live_role=_role(9, "Jailed", position=40),
                bot_top_position=10,
            ),
            OUT_OF_REACH,
            id="out-of-reach",
        ),
        # A create-on-offer dial with nothing yet: it is NOT "not made yet",
        # because nothing will ever make it except an offer.
        pytest.param(GUESS, DialReading(), OFFER_FIRST, id="offer-first"),
    ],
)
def test_state_table(entry, reading, expected):
    assert describe_role(entry, reading).state == expected


def test_hierarchy_is_only_judged_for_roles_the_bot_hands_out():
    """Mentioning a role needs no hierarchy at all.

    Four of the five pings are never granted to anybody, so flagging one for
    sitting above the bot would be crying wolf on a page whose entire value is
    that its warnings are real.
    """
    high = DialReading(
        stored_id=1, live_role=_role(1, "Welcome Ping", position=99),
        bot_top_position=5,
    )
    assert describe_role(WELCOME, high).state == IN_USE


def test_a_role_the_bot_hands_out_above_it_is_out_of_reach():
    reading = DialReading(
        stored_id=1, live_role=_role(1, "Jailed", position=99), bot_top_position=5,
    )
    card = describe_role(JAILED, reading)
    assert card.state == OUT_OF_REACH
    # The fix is in Discord, so the sentence has to say where.
    assert "Server Settings" in card.headline


def test_renamed_is_a_note_not_a_state():
    """A renamed role still works — the bot goes by id — so it must not look
    like a problem, but the admin should still be told."""
    reading = DialReading(
        stored_id=1, live_role=_role(1, "Announcements", members=12),
    )
    card = describe_role(WELCOME, reading)
    assert card.state == IN_USE
    assert any("Announcements" in n for n in card.notes)


def test_two_roles_of_the_same_name_are_reported():
    reading = DialReading(
        stored_id=1,
        live_role=_role(1, "Welcome Ping"),
        named_matches=(_role(1, "Welcome Ping"), _role(2, "Welcome Ping")),
    )
    card = describe_role(WELCOME, reading)
    assert any("2 roles called" in n for n in card.notes)


def test_provenance_turns_an_inference_into_a_fact():
    made = RoleProvenance(1, "welcome_ping_role_id", 1, "adopted", 0.0)
    card = describe_role(
        WELCOME, DialReading(stored_id=1, live_role=_role(1), provenance=made)
    )
    assert card.origin == "adopted"
    assert any("adopted it" in n for n in card.notes)


def test_a_role_with_no_provenance_row_says_it_does_not_know():
    """Everything provisioned before migration 203 — and the DM trio forever,
    since its call site holds no database handle. Guessing confidently there
    would be exactly the failure the table was built to end.
    """
    card = describe_role(WELCOME, DialReading(stored_id=1, live_role=_role(1)))
    assert card.origin == ""
    assert any("can't say" in n for n in card.notes)


def test_deleted_says_so_more_strongly_when_the_bot_made_it():
    prov = RoleProvenance(1, "welcome_ping_role_id", 1, "created", 0.0)
    card = describe_role(
        WELCOME, DialReading(stored_id=1, stored_is_own=True, provenance=prov)
    )
    assert card.state == DELETED
    assert "The role I made is gone" in card.headline


# ── which cards may carry a write button ──────────────────────────────


def test_a_create_on_offer_role_is_never_creatable_from_the_roster():
    """The condition Billy set for reopening these two dials. A "Make it now"
    button here would restore the exact empty-role failure they were excluded
    for, from the one page that looks like it should have one."""
    for reading in (
        DialReading(),
        DialReading(stored_id=5, stored_is_own=True),
        DialReading(opted_out=True),
    ):
        assert describe_role(GUESS, reading).can_create is False


def test_a_role_owned_by_another_feature_gets_no_write_buttons():
    """Wellness, Survivor and the DM trio store their ids themselves. A second
    writer reaching into those is how a repoint gets silently undone by the
    owning page's next whole-form save."""
    card = describe_role(WELLNESS, DialReading())
    assert (card.can_create, card.can_adopt, card.can_stop) == (False, False, False)


def test_a_dial_with_no_coherent_off_cannot_be_stopped():
    """A jail with no role is not a jail. Offering "stop managing" there would
    be offering to break the feature with no way to tell that had happened."""
    card = describe_role(JAILED, DialReading(stored_id=1, live_role=_role(1, "Jailed")))
    assert card.can_stop is False
    assert describe_role(
        WELCOME, DialReading(stored_id=1, live_role=_role(1))
    ).can_stop is True


def test_an_ordinary_unset_ping_dial_can_be_made_now():
    assert describe_role(WELCOME, DialReading()).can_create is True


# ── adoption candidates the provisioner would actually take ───────────
#
# `core.role_provision.adoptable_role_ids` skips an integration-managed role
# and — for a role the bot hands out — one at or above its own top role, then
# creates a working twin lower down. A page that judged adoptability by name
# alone promised "I'll use that one rather than making a second" about exactly
# those roles, and the second one appeared anyway.


def test_a_role_above_the_bot_is_not_an_adoption_candidate():
    """@Jailed sitting above Dungeon Keeper can never be handed out."""
    card = describe_role(
        JAILED,
        DialReading(
            named_matches=(_role(9, "Jailed", position=20),),
            bot_top_position=5,
        ),
    )
    assert card.state == NOT_MADE
    assert any("can't use it" in n for n in card.notes)


def test_an_integration_managed_role_is_not_an_adoption_candidate():
    card = describe_role(
        WELCOME,
        DialReading(named_matches=(_role(9, "Welcome Ping", managed=True),)),
    )
    assert card.state == NOT_MADE
    assert any("can't use it" in n for n in card.notes)


def test_a_usable_twin_is_still_adopted_over_an_unusable_one():
    """Order follows guild.roles (lowest first); the first *usable* one wins."""
    card = describe_role(
        JAILED,
        DialReading(
            named_matches=(
                _role(9, "Jailed", position=20),
                _role(10, "Jailed", position=2, members=4),
            ),
            bot_top_position=5,
        ),
    )
    assert card.state == ADOPTABLE
    assert card.member_count == 4


def test_a_mention_only_dial_ignores_hierarchy_when_adopting():
    """The bot never hands @Welcome Ping out, so position is irrelevant."""
    card = describe_role(
        WELCOME,
        DialReading(
            named_matches=(_role(9, "Welcome Ping", position=20),),
            bot_top_position=5,
        ),
    )
    assert card.state == ADOPTABLE


# ── the opening sentence ──────────────────────────────────────────────


def test_summary_says_nothing_has_been_made_on_a_fresh_server():
    cards = [describe_role(e, DialReading()) for e in fr.MANAGED_ROLES]
    assert summary_line(cards) == (
        "Dungeon Keeper hasn't made any roles in this server yet."
    )


def test_summary_names_what_needs_attention():
    healthy = describe_role(WELCOME, DialReading(stored_id=1, live_role=_role(1)))
    broken = describe_role(
        fr.RISKY_PING, DialReading(stored_id=2, stored_is_own=True)
    )
    line = summary_line([healthy, broken])
    assert "1 role" in line
    assert "@Risky Rolls" in line


def test_summary_stays_quiet_when_everything_works():
    cards = [
        describe_role(WELCOME, DialReading(stored_id=1, live_role=_role(1))),
        describe_role(
            fr.RISKY_PING,
            DialReading(stored_id=2, live_role=_role(2, "Risky Rolls")),
        ),
    ]
    assert "working order" in summary_line(cards)
