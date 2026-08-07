"""Tests for Mention Awards matching and rule storage.

Covers ``bot_modules/mention_awards/logic.py`` (the pure matcher) and
``store.py`` (rule CRUD + validation). The matcher is the whole safety surface
of the feature — it decides who gets paid from a message the bot neither
posted nor hosts a game for — so every guard gets a row, plus the shape taken
verbatim from the live Hot Seat channel.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.mention_awards.logic import (
    Award,
    Rule,
    first_match,
    match_rule,
    phrase_matches,
)
from bot_modules.mention_awards.store import (
    MAX_AMOUNT,
    MAX_PHRASE_LEN,
    create_rule,
    delete_rule,
    list_rules,
    rules_for_channel,
    update_rule,
    validate,
)

CHANNEL = 1529793620545245194
GUILD = 1476525656115515484
HOST_ROLE = 1529800000000000000
PANDA = 714942612217528402
TURBODOG = 299286553426133012

# The real 2026-08-07 announcement.
CONTENT = "@Hot Seat your turn @turbodog8 ! Let's all find out more about him!"

RULE = Rule(id=1, channel_id=CHANNEL, phrase="your turn", amount=250)


def _match(rule: Rule = RULE, **overrides):
    kwargs = {
        "channel_id": CHANNEL,
        "author_id": PANDA,
        "author_is_bot": False,
        "author_role_ids": [HOST_ROLE],
        "content": CONTENT,
        "mentioned_user_ids": [TURBODOG],
    }
    kwargs.update(overrides)
    return match_rule(rule, **kwargs)


class TestPhraseMatching:
    @pytest.mark.parametrize(
        "phrase,expected",
        [
            ("your turn", True),
            ("YOUR TURN", True),        # case-insensitive
            ("  your turn  ", True),    # edges normalised
            ("Your Turn", True),
            ("hot seat", True),         # matches the role ping text
            ("takes the seat", False),
            ("", False),                # never pays on every message
            ("   ", False),
        ],
    )
    def test_phrase(self, phrase, expected):
        assert phrase_matches(phrase, CONTENT) is expected

    def test_empty_content_never_matches(self):
        assert phrase_matches("your turn", "") is False


def test_real_announcement_matches():
    assert _match() == Award(
        rule_id=1, member_id=TURBODOG, amount=250, announcer_id=PANDA
    )


@pytest.mark.parametrize(
    "label,overrides",
    [
        ("wrong channel", {"channel_id": CHANNEL + 1}),
        ("posted by a bot", {"author_is_bot": True}),
        ("phrase absent", {"content": "good morning everyone"}),
        ("nobody mentioned", {"mentioned_user_ids": []}),
        ("group shout", {"mentioned_user_ids": [TURBODOG, PANDA + 5]}),
        ("self-award", {"mentioned_user_ids": [PANDA]}),
    ],
)
def test_guards_reject(label, overrides):
    assert _match(**overrides) is None, label


def test_zero_amount_never_pays():
    """A parked rule detects nothing rather than crediting nothing."""
    assert _match(Rule(id=1, channel_id=CHANNEL, phrase="your turn", amount=0)) is None


def test_repeated_mention_of_one_member_is_one_mention():
    assert _match(mentioned_user_ids=[TURBODOG, TURBODOG]) is not None


class TestAnnouncerRole:
    """The anti-farm lever: who is allowed to hand out currency."""

    gated = Rule(
        id=1, channel_id=CHANNEL, phrase="your turn", amount=250,
        announcer_role_id=HOST_ROLE,
    )

    def test_announcer_has_the_role(self):
        assert _match(self.gated, author_role_ids=[HOST_ROLE]) is not None

    def test_announcer_lacks_the_role(self):
        assert _match(self.gated, author_role_ids=[HOST_ROLE + 1]) is None

    def test_announcer_has_no_roles(self):
        assert _match(self.gated, author_role_ids=[]) is None

    def test_unset_role_lets_anyone_award(self):
        """The baton pass: the outgoing contestant names the next one, and
        holds no special role."""
        assert _match(RULE, author_role_ids=[]) is not None


class TestFirstMatch:
    def test_first_matching_rule_wins(self):
        rules = [
            Rule(id=1, channel_id=CHANNEL, phrase="nope", amount=10),
            Rule(id=2, channel_id=CHANNEL, phrase="your turn", amount=250),
            Rule(id=3, channel_id=CHANNEL, phrase="turn", amount=999),
        ]
        found = first_match(
            rules,
            channel_id=CHANNEL, author_id=PANDA, author_is_bot=False,
            author_role_ids=[], content=CONTENT, mentioned_user_ids=[TURBODOG],
        )
        assert found is not None
        assert (found.rule_id, found.amount) == (2, 250)

    def test_no_rules_matches_nothing(self):
        assert first_match(
            [], channel_id=CHANNEL, author_id=PANDA, author_is_bot=False,
            author_role_ids=[], content=CONTENT, mentioned_user_ids=[TURBODOG],
        ) is None


class TestValidate:
    @pytest.mark.parametrize(
        "phrase,amount,ok",
        [
            ("your turn", 250, True),
            ("your turn", 0, True),          # parked, but valid
            ("", 250, False),
            ("   ", 250, False),
            ("x" * (MAX_PHRASE_LEN + 1), 250, False),
            ("your turn", -1, False),
            ("your turn", MAX_AMOUNT + 1, False),
        ],
    )
    def test_validate(self, phrase, amount, ok):
        assert (validate(phrase, amount) is None) is ok


class TestStore:
    def test_crud_roundtrip(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            rid = create_rule(
                conn, GUILD, channel_id=CHANNEL, phrase=" your turn ",
                amount=250, announcer_role_id=HOST_ROLE, created_by=PANDA,
            )
            rows = list_rules(conn, GUILD)
            assert len(rows) == 1
            assert rows[0]["phrase"] == "your turn"  # stripped on write
            assert rows[0]["amount"] == 250

            assert update_rule(
                conn, GUILD, rid, channel_id=CHANNEL, phrase="takes the seat",
                amount=100, announcer_role_id=0,
            )
            rules = rules_for_channel(conn, GUILD, CHANNEL)
            assert (rules[0].phrase, rules[0].amount) == ("takes the seat", 100)

            assert delete_rule(conn, GUILD, rid)
            assert list_rules(conn, GUILD) == []

    def test_another_guild_cannot_edit_or_delete(self, sync_db_path):
        """id alone must never be enough — the panel is per-guild."""
        with open_db(sync_db_path) as conn:
            rid = create_rule(
                conn, GUILD, channel_id=CHANNEL, phrase="your turn", amount=250,
            )
            other = GUILD + 1
            assert not update_rule(
                conn, other, rid, channel_id=CHANNEL, phrase="pwned", amount=99999,
            )
            assert not delete_rule(conn, other, rid)
            assert list_rules(conn, GUILD)[0]["phrase"] == "your turn"

    def test_rules_for_channel_scopes_to_the_channel(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            create_rule(conn, GUILD, channel_id=CHANNEL, phrase="a", amount=1)
            create_rule(conn, GUILD, channel_id=CHANNEL + 1, phrase="b", amount=1)
            found = rules_for_channel(conn, GUILD, CHANNEL)
            assert [r.phrase for r in found] == ["a"]
