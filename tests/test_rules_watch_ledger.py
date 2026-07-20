"""Tests for rules_watch/ledger.py — the two concrete-act detectors.

These are ledgers, not classifiers (see the module docstring and §7 of
docs/reviews/2026-07-20-rules-watch-tuning.md). What they must get right is
therefore lopsided: a miss costs a line in a review that a human writes by hand
instead, while a false hit puts an innocent member's name on a list. So most of
the cases below are negatives.

Every string marked "real" is taken verbatim from the guild corpus. The two
must-fire cases are the actual actioned incidents:
  - Ciccio's bot-disclaimer, 2026-04-30, 2.5 months before his ban
  - Whoami23 naming lily's Reddit post, 2026-07-11, access revoked
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.rules_watch.ledger import (
    demonstrates_observation,
    detect_cross_platform,
    detect_dm_consent,
    get_repeat_authors,
    has_bot_disclaimer,
    has_dm_intent,
    is_intake_ritual,
    names_platform,
    record_hit,
)
from migrations import apply_migrations_sync

GUILD = 123
CHANNEL = 456
MOD_CHANNEL = 999
AUTHOR = 1001
TARGET = 1002
OTHER_TARGET = 1003

BASE_TS = 1_700_000_000
DAY = 86400


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO known_channels (guild_id, channel_id, channel_name) VALUES (?,?,?)",
            (GUILD, CHANNEL, "💛│the-meadow"),
        )
        conn.execute(
            "INSERT INTO known_channels (guild_id, channel_id, channel_name) VALUES (?,?,?)",
            (GUILD, MOD_CHANNEL, "🏢│mod-chat"),
        )
    return db_path


def _msg(conn, message_id, author_id, content, ts, channel=CHANNEL):
    conn.execute(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id, "
        "content, ts) VALUES (?,?,?,?,?,?)",
        (message_id, GUILD, channel, author_id, content, ts),
    )


# ── DM-consent: pure text ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        # real — Ciccio, 2026-04-30
        "I don't know how to use the ask bot, or have access to it.",
        # real — Ciccio, 2026-04-25
        "I don't know how to use the bot to ask.",
        # real — bigoryx's reusable script, per Little Loaf's quote
        "I hate the bot",
        "the request bot wasn't working",
        "I couldn't figure out the bot",
        "idk how to work that stupid bot",
    ],
)
def test_bot_disclaimers_are_recognised(text):
    assert has_bot_disclaimer(text)


@pytest.mark.parametrize(
    "text",
    [
        # The etiquette guide teaches exactly this. It must never fire.
        "Hey, is it cool if I DM you about your latest post?",
        # real — Cami, labelled `ok` in the eval set
        "May I DM you? I know your DMs are closed",
        "can I dm you?",
        "I used the bot to ask, hope that's ok!",
        "the bot said you're open to DMs",
    ],
)
def test_endorsed_public_dm_asking_is_not_a_disclaimer(text):
    assert not has_bot_disclaimer(text)


@pytest.mark.parametrize(
    "text",
    [
        # real — all ordinary bot-outage chatter about other bots
        "CAT BOT IS BROKEN AGAIN",
        "Oh nooo cat bot is acting up again??",
        "Jail bot is broken but I can talk to her and time her out for now",
        "Did you do the verification bot thing there? Mines not working",
        "yeah the dm bot is broken atm lol just gotta do it manually",
    ],
)
def test_bot_outage_chatter_has_no_dm_intent(text):
    # These DO read as disclaimers; what keeps them out of the ledger is the
    # absence of intent to DM a specific person.
    assert not has_dm_intent(text)


def test_dm_intent_requires_a_second_person():
    assert has_dm_intent("if I did I'd ask it if it was ok to dm you")
    assert has_dm_intent("I'm also up for DMs if you feel like it")
    assert not has_dm_intent("my dms are open")
    assert not has_dm_intent("the bot is broken")


# ── DM-consent: end to end ────────────────────────────────────────────


def test_ciccio_disclaimer_is_recorded(db):
    """The real 2026-04-30 message, 2.5 months before the ban."""
    content = (
        "I don't know how to use the ask bot, or have access to it. "
        "But if I did I'd ask it if it was ok to dm you. Totally cool either way"
    )
    with open_db(db) as conn:
        hit = detect_dm_consent(conn, GUILD, CHANNEL, AUTHOR, content, BASE_TS)
    assert hit is not None
    assert hit.kind == "dm_consent"
    assert "know how to use" in hit.matched_phrase


def test_disclaimer_without_dm_intent_does_not_fire(db):
    with open_db(db) as conn:
        hit = detect_dm_consent(
            conn, GUILD, CHANNEL, AUTHOR, "CAT BOT IS BROKEN AGAIN", BASE_TS
        )
    assert hit is None


def test_dm_intent_without_disclaimer_does_not_fire(db):
    """Public asking is endorsed behaviour — the whole point of §8.1."""
    with open_db(db) as conn:
        hit = detect_dm_consent(
            conn, GUILD, CHANNEL, AUTHOR,
            "Hey, is it cool if I DM you about your latest post?", BASE_TS,
        )
    assert hit is None


def test_disclaimer_in_mod_channel_does_not_fire(db):
    """Little Loaf quoting bigoryx in mod-chat is analysis, not an act."""
    content = 'He knows how to use the bot. "I hate the bot" means he hates a trail, and he DMed you anyway'
    with open_db(db) as conn:
        hit = detect_dm_consent(conn, GUILD, MOD_CHANNEL, AUTHOR, content, BASE_TS)
    assert hit is None


def test_dm_intent_in_a_nearby_message_still_fires(db):
    """bigoryx's script split the disclaimer from the ask across messages."""
    with open_db(db) as conn:
        _msg(conn, 1, AUTHOR, "can I dm you about it?", BASE_TS - 60)
        hit = detect_dm_consent(
            conn, GUILD, CHANNEL, AUTHOR, "I hate the bot", BASE_TS
        )
    assert hit is not None


def test_dm_intent_outside_the_window_does_not_fire(db):
    with open_db(db) as conn:
        _msg(conn, 1, AUTHOR, "can I dm you about it?", BASE_TS - 3600)
        hit = detect_dm_consent(
            conn, GUILD, CHANNEL, AUTHOR, "I hate the bot", BASE_TS
        )
    assert hit is None


def test_nearby_dm_intent_from_someone_else_does_not_count(db):
    with open_db(db) as conn:
        _msg(conn, 1, TARGET, "can I dm you about it?", BASE_TS - 60)
        hit = detect_dm_consent(
            conn, GUILD, CHANNEL, AUTHOR, "I hate the bot", BASE_TS
        )
    assert hit is None


# ── Cross-platform: pure text ─────────────────────────────────────────


def test_platform_names_are_detected():
    assert names_platform("saw your reddit post") == "reddit"
    assert names_platform("check my insta") == "insta"
    assert names_platform("nothing here") is None


@pytest.mark.parametrize(
    "text",
    [
        # real — the welcome ritual, performed by greeters and mods ~70× in the
        # corpus. §7.4 as written would have fired on all of these.
        "Are you coming from the den? What's your Reddit name, love connecting the faces 🙂",
        "Samesies! What's your reddit name? We usually like to ask a few ice breaker questions",
        "Hi Mimi! I know you from Reddit! Haha lovely seeing you here 😘💚",
        "Welcome! I recognize you from Reddit!",
        "Hi new friend! Are you from Reddit??",
        "Drop your reddit name when you get back if you don't mind",
    ],
)
def test_intake_ritual_is_exempt(text):
    assert is_intake_ritual(text)


@pytest.mark.parametrize(
    "text",
    [
        # real — Whoami23, 2026-07-11, the actioned case
        "I'm talking about your Reddit post",
        "I saw your post on reddit",
        "I used to comment on your reddit posts",
    ],
)
def test_demonstrated_observation_is_recognised(text):
    assert demonstrates_observation(text)
    assert not is_intake_ritual(text)


def test_in_server_bio_and_post_are_not_cross_platform():
    """§7.4 lists `your bio` / `your post`. Both are IN-server features here."""
    assert names_platform("I loved your bio, the icebreakers are great") is None
    assert names_platform("your post in the flash channel is stunning") is None


def test_bare_of_is_not_treated_as_onlyfans():
    """`OF` in §7.4's list is unusable — it matches the word "of"."""
    assert names_platform("a picture of you") is None
    assert names_platform("check out my onlyfans") == "onlyfans"


# ── Cross-platform: end to end ────────────────────────────────────────


def test_whoami23_reddit_reference_is_recorded(db):
    with open_db(db) as conn:
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, TARGET,
            "I'm talking about your Reddit post", BASE_TS,
        )
    assert hit is not None
    assert hit.kind == "cross_platform"
    assert hit.platform == "reddit"


def test_handle_request_does_not_fire(db):
    with open_db(db) as conn:
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, TARGET,
            "What's your Reddit name, love connecting the faces 🙂", BASE_TS,
        )
    assert hit is None


def test_undirected_reference_does_not_fire(db):
    """The directedness filter is what §11 calls load-bearing for low risk."""
    with open_db(db) as conn:
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, None,
            "I'm talking about your Reddit post", BASE_TS,
        )
    assert hit is None


def test_target_raising_it_here_and_now_exempts(db):
    """She just brought it up in this channel — referencing it is responsive."""
    with open_db(db) as conn:
        _msg(conn, 1, TARGET, "just posted a new set on my reddit btw!", BASE_TS - 600)
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, TARGET,
            "I saw your reddit post, gorgeous", BASE_TS,
        )
    assert hit is None


def test_target_having_an_internet_presence_does_not_exempt(db):
    """Regression: a broad exemption suppressed the real Whoami23 case.

    A 30-day guild-wide version of the "she mentioned it herself" check gave
    blanket immunity to anyone who talks about their own Reddit — which is the
    exact population this protects. In the live corpus one of the mentions
    granting immunity was lily *reporting* velocibaker for finding her Reddit.
    Fails before the window was tightened to same-channel/6h.
    """
    with open_db(db) as conn:
        # Days earlier, and in another channel: an internet presence, not an
        # invitation.
        _msg(conn, 1, TARGET, "I usually hide them on reddit lol",
             BASE_TS - 5 * DAY, channel=MOD_CHANNEL)
        _msg(conn, 2, TARGET, "small flag — he found my Reddit profile",
             BASE_TS - 5 * DAY, channel=MOD_CHANNEL)
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, TARGET,
            "I'm talking about your Reddit post", BASE_TS,
        )
    assert hit is not None


def test_target_mention_in_another_channel_does_not_exempt(db):
    with open_db(db) as conn:
        _msg(conn, 1, TARGET, "just posted on my reddit!", BASE_TS - 600,
             channel=MOD_CHANNEL)
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, TARGET,
            "I saw your reddit post, gorgeous", BASE_TS,
        )
    assert hit is not None


def test_stale_target_mention_does_not_exempt(db):
    with open_db(db) as conn:
        _msg(conn, 1, TARGET, "my reddit is linked in my profile", BASE_TS - 2 * DAY)
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, TARGET,
            "I saw your reddit post, gorgeous", BASE_TS,
        )
    assert hit is not None


def test_a_different_platform_does_not_exempt(db):
    with open_db(db) as conn:
        _msg(conn, 1, TARGET, "follow my insta!", BASE_TS - 600)
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, TARGET,
            "I saw your reddit post", BASE_TS,
        )
    assert hit is not None


def test_self_reference_does_not_fire(db):
    with open_db(db) as conn:
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, AUTHOR, "I saw your reddit post", BASE_TS
        )
    assert hit is None


# ── Persistence and escalation ────────────────────────────────────────


def test_record_hit_is_idempotent_per_message(db):
    with open_db(db) as conn:
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, TARGET, "I'm talking about your Reddit post", BASE_TS
        )
        assert hit is not None
        first = record_hit(
            conn, guild_id=GUILD, hit=hit, message_id=77,
            channel_id=CHANNEL, author_id=AUTHOR, target_id=TARGET,
        )
        second = record_hit(
            conn, guild_id=GUILD, hit=hit, message_id=77,
            channel_id=CHANNEL, author_id=AUTHOR, target_id=TARGET,
        )
    assert first is not None
    assert second is None


def test_repeat_authors_needs_two_distinct_targets(db):
    """§7.4: ≥2 distinct targets is what separated whoami23 from a warning."""
    with open_db(db) as conn:
        hit = detect_cross_platform(
            conn, GUILD, CHANNEL, AUTHOR, TARGET, "I saw your reddit post", BASE_TS
        )
        assert hit is not None
        record_hit(
            conn, guild_id=GUILD, hit=hit, message_id=1,
            channel_id=CHANNEL, author_id=AUTHOR, target_id=TARGET,
        )
        record_hit(
            conn, guild_id=GUILD, hit=hit, message_id=2,
            channel_id=CHANNEL, author_id=AUTHOR, target_id=TARGET,
        )
        assert get_repeat_authors(conn, GUILD) == []

        record_hit(
            conn, guild_id=GUILD, hit=hit, message_id=3,
            channel_id=CHANNEL, author_id=AUTHOR, target_id=OTHER_TARGET,
        )
        repeats = get_repeat_authors(conn, GUILD)

    assert len(repeats) == 1
    assert repeats[0]["author_id"] == AUTHOR
    assert repeats[0]["distinct_targets"] == 2
