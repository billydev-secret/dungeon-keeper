"""Auto React rules, the tipping gate, and placement receipts.

The gate is the interesting part: in a tipping channel a bot-placed emoji is a
live payment button, so it may only land on a post that actually qualified —
and the fail-open direction matters, because the cost of being wrong is a
poster silently losing tips they can neither see nor appeal.
"""
from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.auto_react_service import (
    get_auto_react_rule,
    get_placement,
    list_auto_react_rules_for_guild,
    parse_emojis,
    record_placement,
    remove_auto_react_rule,
    should_place_tip_emoji,
    upsert_auto_react_rule,
)

GUILD = 111
CHANNEL = 222
MESSAGE = 333
AUTHOR = 444


# --------------------------------------------------------------------------
# the tipping gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        pytest.param([True], True, id="explicit-qualifies"),
        pytest.param([False], False, id="not-explicit-withholds"),
        pytest.param([None], True, id="unreadable-fails-open"),
        pytest.param([False, True], True, id="any-explicit-qualifies"),
        pytest.param([False, None], True, id="any-unreadable-fails-open"),
        pytest.param([False, False], False, id="all-clear-withholds"),
        pytest.param([], False, id="nothing-classifiable"),
    ],
)
def test_should_place_tip_emoji_verdicts(verdicts, expected):
    # Only a confident "read it, not explicit" withholds the emoji. A CDN
    # hiccup must not quietly cost a poster their tips.
    assert should_place_tip_emoji(channel_is_nsfw=True, verdicts=verdicts) is expected


@pytest.mark.parametrize(
    "verdicts",
    [
        pytest.param([True], id="explicit"),
        pytest.param([None], id="unreadable"),
        pytest.param([], id="empty"),
    ],
)
def test_tip_emoji_never_placed_outside_an_age_gated_channel(verdicts):
    # Discord's own age gate is the rail; the classifier only narrows within
    # it and never substitutes for it.
    assert should_place_tip_emoji(channel_is_nsfw=False, verdicts=verdicts) is False


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------


def test_rule_defaults_to_tips_disabled(sync_db_path):
    # Existing rules keep behaving as plain emoji decoration.
    upsert_auto_react_rule(sync_db_path, GUILD, CHANNEL, ["🔥"])

    row = get_auto_react_rule(sync_db_path, GUILD, CHANNEL)
    assert row is not None
    assert int(row["tips_enabled"]) == 0
    assert int(row["enabled"]) == 1


def test_rule_round_trips_tips_enabled(sync_db_path):
    upsert_auto_react_rule(
        sync_db_path, GUILD, CHANNEL, ["🔥", "💎"], True, tips_enabled=True
    )

    row = get_auto_react_rule(sync_db_path, GUILD, CHANNEL)
    assert row is not None
    assert int(row["tips_enabled"]) == 1
    assert parse_emojis(row["emojis"]) == ["🔥", "💎"]


def test_upsert_can_turn_tipping_back_off(sync_db_path):
    upsert_auto_react_rule(sync_db_path, GUILD, CHANNEL, ["🔥"], True, tips_enabled=True)
    upsert_auto_react_rule(sync_db_path, GUILD, CHANNEL, ["🔥"], True, tips_enabled=False)

    row = get_auto_react_rule(sync_db_path, GUILD, CHANNEL)
    assert row is not None
    assert int(row["tips_enabled"]) == 0


def test_list_rules_exposes_tips_enabled(sync_db_path):
    upsert_auto_react_rule(sync_db_path, GUILD, CHANNEL, ["🔥"], True, tips_enabled=True)

    rows = list_auto_react_rules_for_guild(sync_db_path, GUILD)
    assert [int(r["tips_enabled"]) for r in rows] == [1]


def test_remove_rule(sync_db_path):
    upsert_auto_react_rule(sync_db_path, GUILD, CHANNEL, ["🔥"])

    assert remove_auto_react_rule(sync_db_path, GUILD, CHANNEL) is True
    assert get_auto_react_rule(sync_db_path, GUILD, CHANNEL) is None
    assert remove_auto_react_rule(sync_db_path, GUILD, CHANNEL) is False


# --------------------------------------------------------------------------
# placement receipts
# --------------------------------------------------------------------------


def test_placement_records_what_was_placed(sync_db_path):
    record_placement(
        sync_db_path,
        guild_id=GUILD,
        channel_id=CHANNEL,
        message_id=MESSAGE,
        author_id=AUTHOR,
        emojis=["🔥", "💎"],
        now=1700000000,
    )

    row = get_placement(sync_db_path, GUILD, MESSAGE)
    assert row is not None
    assert row["author_id"] == AUTHOR
    assert parse_emojis(row["emojis"]) == ["🔥", "💎"]


def test_placement_absent_for_unreacted_messages(sync_db_path):
    # No receipt means nothing on that message is tippable — this is what
    # stops a pasted rung on a text post from becoming a payment target.
    assert get_placement(sync_db_path, GUILD, MESSAGE) is None


def test_placement_is_idempotent(sync_db_path):
    for _ in range(2):
        record_placement(
            sync_db_path,
            guild_id=GUILD,
            channel_id=CHANNEL,
            message_id=MESSAGE,
            author_id=AUTHOR,
            emojis=["🔥"],
        )

    with open_db(sync_db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) c FROM auto_react_placements"
        ).fetchone()["c"]
    assert count == 1


def test_placements_are_scoped_per_guild(sync_db_path):
    record_placement(
        sync_db_path,
        guild_id=GUILD,
        channel_id=CHANNEL,
        message_id=MESSAGE,
        author_id=AUTHOR,
        emojis=["🔥"],
    )

    assert get_placement(sync_db_path, GUILD + 1, MESSAGE) is None
