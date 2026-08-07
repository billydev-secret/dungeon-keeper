"""Tests for Mention Awards condition matching and rule storage.

Covers ``bot_modules/mention_awards/logic.py`` (the pure chip matcher) and
``store.py`` (rule CRUD + validation + conditions JSON). The matcher is the
whole safety surface of the feature — it decides who gets paid from a message
the bot neither posted nor hosts a game for — so every guard and every chip
kind gets a row, plus the shapes taken verbatim from the live Hot Seat
channel.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.mention_awards.logic import (
    Award,
    Condition,
    Rule,
    condition_matches,
    first_match,
    match_rule,
    phrase_matches,
    regex_matches,
)
from bot_modules.mention_awards.store import (
    MAX_AMOUNT,
    MAX_CONDITIONS,
    MAX_TEXT_LEN,
    conditions_from_json,
    conditions_to_json,
    create_rule,
    delete_rule,
    list_rules,
    rules_for_channel,
    update_rule,
    validate,
)

CHANNEL = 1529793620545245194
GUILD = 1476525656115515484
HOT_SEAT_ROLE = 1529800000000000000
HOST_ROLE = 1529800000000000001
PANDA = 714942612217528402
TURBODOG = 299286553426133012

# The real 2026-08-07 announcement, in raw gateway form: the role ping is
# <@&id> markup, not the rendered "@Hot Seat".
CONTENT = (
    f"<@&{HOT_SEAT_ROLE}> your turn <@{TURBODOG}> ! "
    "Let's all find out more about him!"
)

PHRASE_CHIP = Condition(kind="contains_text", value="your turn")
RULE = Rule(id=1, channel_id=CHANNEL, amount=250, conditions=(PHRASE_CHIP,))


def _match(rule: Rule = RULE, **overrides):
    kwargs = {
        "channel_id": CHANNEL,
        "author_id": PANDA,
        "author_is_bot": False,
        "author_role_ids": [HOST_ROLE],
        "content": CONTENT,
        "mentioned_user_ids": [TURBODOG],
        "mentioned_role_ids": [HOT_SEAT_ROLE],
    }
    kwargs.update(overrides)
    return match_rule(rule, **kwargs)


def _chip(cond: Condition, **overrides) -> bool:
    kwargs = {
        "author_id": PANDA,
        "author_role_ids": frozenset({HOST_ROLE}),
        "content": CONTENT,
        "mentioned_role_ids": frozenset({HOT_SEAT_ROLE}),
    }
    kwargs.update(overrides)
    return condition_matches(cond, **kwargs)


class TestTextMatching:
    @pytest.mark.parametrize(
        "phrase,expected",
        [
            ("your turn", True),
            ("YOUR TURN", True),        # case-insensitive
            ("  your turn  ", True),    # edges normalised
            ("takes the seat", False),
            ("", False),                # never pays on every message
            ("   ", False),
        ],
    )
    def test_phrase(self, phrase, expected):
        assert phrase_matches(phrase, CONTENT) is expected

    def test_role_ping_is_markup_not_text(self):
        """'hot seat' as TEXT can never match a role ping — it's <@&id> raw.

        This is the whole reason mentions_role exists as its own chip kind.
        """
        assert phrase_matches("hot seat", CONTENT) is False

    @pytest.mark.parametrize(
        "pattern,expected",
        [
            (r"your\s+turn", True),
            (r"^<@&\d+> your turn", True),
            (r"YOUR TURN", True),        # IGNORECASE
            (r"\bseat\b", False),
            (r"", False),
            (r"([unclosed", False),      # broken pattern fails closed
        ],
    )
    def test_regex(self, pattern, expected):
        assert regex_matches(pattern, CONTENT) is expected


class TestConditionKinds:
    @pytest.mark.parametrize(
        "cond,expected",
        [
            (Condition("contains_text", "your turn"), True),
            (Condition("contains_text", r"your\s+turn", regex=True), True),
            (Condition("contains_text", r"\bnope\b", regex=True), False),
            (Condition("mentions_role", str(HOT_SEAT_ROLE)), True),
            (Condition("mentions_role", str(HOT_SEAT_ROLE + 1)), False),
            (Condition("mentions_role", "not-a-number"), False),
            (Condition("from_user", str(PANDA)), True),
            (Condition("from_user", str(TURBODOG)), False),
            (Condition("author_has_role", str(HOST_ROLE)), True),
            (Condition("author_has_role", str(HOST_ROLE + 99)), False),
            (Condition("no_such_kind", "x"), False),  # unknown fails closed
        ],
    )
    def test_kinds(self, cond, expected):
        assert _chip(cond) is expected


def test_real_announcement_matches():
    assert _match() == Award(
        rule_id=1, member_id=TURBODOG, amount=250, announcer_id=PANDA
    )


@pytest.mark.parametrize(
    "label,overrides",
    [
        ("wrong channel", {"channel_id": CHANNEL + 1}),
        ("posted by a bot", {"author_is_bot": True}),
        ("trigger text absent", {"content": "good morning everyone"}),
        ("nobody mentioned", {"mentioned_user_ids": []}),
        ("group shout", {"mentioned_user_ids": [TURBODOG, PANDA + 5]}),
        ("self-award", {"mentioned_user_ids": [PANDA]}),
    ],
)
def test_guards_reject(label, overrides):
    assert _match(**overrides) is None, label


def test_zero_amount_never_pays():
    assert _match(Rule(id=1, channel_id=CHANNEL, amount=0,
                       conditions=(PHRASE_CHIP,))) is None


def test_no_conditions_matches_nothing():
    """An empty chip list fails closed — it must never mean 'always pay'."""
    assert _match(Rule(id=1, channel_id=CHANNEL, amount=250)) is None


def test_conditions_are_anded():
    """Every chip must hold; one miss kills the rule."""
    both = Rule(id=1, channel_id=CHANNEL, amount=250, conditions=(
        PHRASE_CHIP, Condition("mentions_role", str(HOT_SEAT_ROLE)),
    ))
    assert _match(both) is not None
    assert _match(both, mentioned_role_ids=[]) is None
    assert _match(both, content=f"<@{TURBODOG}> hi") is None


def test_role_ping_does_not_count_as_a_user_mention():
    """@Hot Seat + @turbodog = ONE user mention; the ping rides separately."""
    assert _match(mentioned_user_ids=[TURBODOG],
                  mentioned_role_ids=[HOT_SEAT_ROLE]) is not None


def test_from_user_still_cannot_self_award():
    """A from_user chip naming the author doesn't bypass the self-award guard."""
    pinned = Rule(id=1, channel_id=CHANNEL, amount=250, conditions=(
        Condition("from_user", str(PANDA)),
    ))
    assert _match(pinned, mentioned_user_ids=[PANDA]) is None
    assert _match(pinned, mentioned_user_ids=[TURBODOG]) is not None


def test_baton_pass_without_author_chips_is_open():
    """No author chip → anyone announces: Hav0c → juliet, 2026-07-28."""
    hav0c, juliet = 1487183422307832011, 1469366943634034820
    assert _match(
        author_id=hav0c, author_role_ids=[],
        content=f"<@&{HOT_SEAT_ROLE}> your turn <@{juliet}>!",
        mentioned_user_ids=[juliet],
    ) == Award(rule_id=1, member_id=juliet, amount=250, announcer_id=hav0c)


class TestFirstMatch:
    def test_first_matching_rule_wins(self):
        rules = [
            Rule(id=1, channel_id=CHANNEL, amount=10,
                 conditions=(Condition("contains_text", "nope"),)),
            Rule(id=2, channel_id=CHANNEL, amount=250, conditions=(PHRASE_CHIP,)),
            Rule(id=3, channel_id=CHANNEL, amount=999,
                 conditions=(Condition("contains_text", "turn"),)),
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
        "amount,conds,ok",
        [
            (250, [PHRASE_CHIP], True),
            (0, [PHRASE_CHIP], True),                       # parked, but valid
            (250, [], False),                               # no chips
            (-1, [PHRASE_CHIP], False),
            (MAX_AMOUNT + 1, [PHRASE_CHIP], False),
            (250, [Condition("contains_text", "")], False),
            (250, [Condition("contains_text", "x" * (MAX_TEXT_LEN + 1))], False),
            (250, [Condition("contains_text", r"([bad", regex=True)], False),
            (250, [Condition("contains_text", r"your\s+turn", regex=True)], True),
            (250, [Condition("mentions_role", str(HOT_SEAT_ROLE))], True),
            (250, [Condition("mentions_role", "0")], False),
            (250, [Condition("mentions_role", "abc")], False),
            (250, [Condition("from_user", str(PANDA))], True),
            (250, [Condition("no_such_kind", "x")], False),
            (250, [PHRASE_CHIP] * (MAX_CONDITIONS + 1), False),
        ],
    )
    def test_validate(self, amount, conds, ok):
        assert (validate(amount, conds) is None) is ok


class TestConditionsJson:
    def test_roundtrip(self):
        chips = (
            Condition("contains_text", r"your\s+turn", regex=True),
            Condition("mentions_role", str(HOT_SEAT_ROLE)),
        )
        assert conditions_from_json(conditions_to_json(chips)) == chips

    @pytest.mark.parametrize(
        "raw", [None, "", "not json", "42", '{"kind":"x"}', '[42, "x"]'],
    )
    def test_malformed_json_yields_no_chips(self, raw):
        """A corrupted row parks its rule (no chips = never matches)."""
        assert conditions_from_json(raw) == ()


class TestStore:
    def test_crud_roundtrip(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            rid = create_rule(
                conn, GUILD, channel_id=CHANNEL, amount=250,
                conditions=[PHRASE_CHIP,
                            Condition("mentions_role", str(HOT_SEAT_ROLE))],
                created_by=PANDA,
            )
            rows = list_rules(conn, GUILD)
            assert len(rows) == 1
            assert conditions_from_json(rows[0]["conditions"]) == (
                PHRASE_CHIP, Condition("mentions_role", str(HOT_SEAT_ROLE)),
            )

            assert update_rule(
                conn, GUILD, rid, channel_id=CHANNEL, amount=100,
                conditions=[Condition("contains_text", "takes the seat")],
            )
            rules = rules_for_channel(conn, GUILD, CHANNEL)
            assert rules[0].amount == 100
            assert rules[0].conditions[0].value == "takes the seat"

            assert delete_rule(conn, GUILD, rid)
            assert list_rules(conn, GUILD) == []

    def test_another_guild_cannot_edit_or_delete(self, sync_db_path):
        """id alone must never be enough — the panel is per-guild."""
        with open_db(sync_db_path) as conn:
            rid = create_rule(
                conn, GUILD, channel_id=CHANNEL, amount=250,
                conditions=[PHRASE_CHIP],
            )
            other = GUILD + 1
            assert not update_rule(
                conn, other, rid, channel_id=CHANNEL, amount=99999,
                conditions=[Condition("contains_text", "pwned")],
            )
            assert not delete_rule(conn, other, rid)
            kept = rules_for_channel(conn, GUILD, CHANNEL)[0]
            assert (kept.amount, kept.conditions[0].value) == (250, "your turn")

    def test_rules_for_channel_scopes_to_the_channel(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            create_rule(conn, GUILD, channel_id=CHANNEL, amount=1,
                        conditions=[Condition("contains_text", "a")])
            create_rule(conn, GUILD, channel_id=CHANNEL + 1, amount=1,
                        conditions=[Condition("contains_text", "b")])
            found = rules_for_channel(conn, GUILD, CHANNEL)
            assert [r.conditions[0].value for r in found] == ["a"]
