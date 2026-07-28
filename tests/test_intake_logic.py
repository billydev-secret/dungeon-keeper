"""Tests for services/intake_service — intake cards (welcome tracker).

The tested unit is the config parsing, card/step ledger, auto-tick matching,
completion (code wins, skips stamped), stale scan, and watch registry; the
Discord embed/buttons in intake_views are glue exercised via this layer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services import intake_service as svc
from tests.db_template import migrated_db

GUILD = 42
NEWCOMER = 7
GREETER = 501
CHANNEL = 555  # greeter chat / intake channel
MEMBER_ROLE = 901
NSFW_ROLE = 902

CUSTOM_STEPS = [
    {"key": "greeted", "label": "Greeted", "auto": "greeted"},
    {"key": "member_role", "label": "Member role", "auto": "role_gained", "role_id": MEMBER_ROLE},
    {"key": "sfw_questions", "label": "SFW questions asked"},
    {"key": "nsfw_role", "label": "NSFW access", "auto": "role_gained", "role_id": NSFW_ROLE},
]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "intake.db"
    migrated_db(path)
    return path


@pytest.fixture(autouse=True)
def _clean_watch():
    svc._reset_watch_for_tests()
    yield
    svc._reset_watch_for_tests()


def _enable(conn, channel=CHANNEL):
    set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
    set_config_value(conn, svc.CHANNEL_KEY, str(channel), GUILD)


def _use_custom_steps(conn):
    set_config_value(conn, svc.STEPS_KEY, json.dumps(CUSTOM_STEPS), GUILD)


# ── config / dark gate ────────────────────────────────────────────────


def test_ships_dark_until_enabled_and_channel(db_path):
    with open_db(db_path) as conn:
        assert svc.is_enabled(conn, GUILD) is False
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        # Enabled flag alone isn't enough — no channel resolves yet.
        assert svc.is_enabled(conn, GUILD) is False
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        assert svc.is_enabled(conn, GUILD) is True


def test_channel_falls_back_to_greeter_chat(db_path):
    with open_db(db_path) as conn:
        set_config_value(conn, svc.ENABLED_KEY, "1", GUILD)
        set_config_value(conn, svc.FALLBACK_CHANNEL_KEY, "777", GUILD)
        assert svc.intake_channel_id(conn, GUILD) == 777
        assert svc.is_enabled(conn, GUILD) is True
        # An explicit intake channel wins over the fallback.
        set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)
        assert svc.intake_channel_id(conn, GUILD) == CHANNEL


def test_stale_hours_tolerates_garbage(db_path):
    with open_db(db_path) as conn:
        assert svc.stale_hours(conn, GUILD) == svc.DEFAULT_STALE_HOURS
        set_config_value(conn, svc.STALE_HOURS_KEY, "nope", GUILD)
        assert svc.stale_hours(conn, GUILD) == svc.DEFAULT_STALE_HOURS
        set_config_value(conn, svc.STALE_HOURS_KEY, "-3", GUILD)
        assert svc.stale_hours(conn, GUILD) == svc.DEFAULT_STALE_HOURS
        set_config_value(conn, svc.STALE_HOURS_KEY, "6", GUILD)
        assert svc.stale_hours(conn, GUILD) == 6.0


def test_code_matches_case_insensitive_and_empty_never(db_path):
    assert svc.code_matches("Welcome aboard! dk-7734 🎉", "DK-7734") is True
    assert svc.code_matches("no code here", "DK-7734") is False
    # An unset code must never match anything.
    assert svc.code_matches("any message at all", "") is False


# ── step config parsing ───────────────────────────────────────────────


def test_parse_steps_defaults_on_empty_and_garbage():
    assert svc.parse_steps("") == list(svc.DEFAULT_STEPS)
    assert svc.parse_steps("not json {") == list(svc.DEFAULT_STEPS)
    assert svc.parse_steps('{"key": "x"}') == list(svc.DEFAULT_STEPS)  # not a list
    assert svc.parse_steps("[]") == list(svc.DEFAULT_STEPS)


def test_parse_steps_custom_list():
    steps = svc.parse_steps(json.dumps(CUSTOM_STEPS))
    assert [s.key for s in steps] == ["greeted", "member_role", "sfw_questions", "nsfw_role"]
    assert steps[1].auto_kind == svc.AUTO_ROLE_GAINED
    assert steps[1].auto_role_id == MEMBER_ROLE
    assert steps[2].auto_kind == ""  # manual


def test_parse_steps_drops_invalid_entries_individually():
    raw = json.dumps(
        [
            {"key": "ok", "label": "Fine"},
            "not a dict",
            {"key": "", "label": "no key"},
            {"key": "nolabel", "label": ""},
            {"key": "badauto", "label": "Bad", "auto": "telepathy"},
            {"key": "ok", "label": "Duplicate key"},
            {"key": "badrole", "label": "Bad role", "auto": "role_gained", "role_id": "x"},
        ]
    )
    steps = svc.parse_steps(raw)
    assert [s.key for s in steps] == ["ok", "badrole"]
    assert steps[1].auto_role_id == 0  # unparseable role id degrades to 0


def test_parse_steps_code_is_optional_and_stripped():
    # Configs written before step codes existed have no "code" key at all.
    raw = json.dumps(
        [
            {"key": "legacy", "label": "No code key"},
            {"key": "coded", "label": "Coded", "code": "  DK-SFW  "},
            {"key": "blank", "label": "Blank", "code": "   "},
        ]
    )
    assert [s.code for s in svc.parse_steps(raw)] == ["", "DK-SFW", ""]


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        pytest.param([("a", "DK-SFW"), ("b", "DK-NSFW")], None, id="distinct"),
        # Matching is containment, so a code inside another fires both. The
        # codes ride along in the result because labels aren't unique.
        pytest.param(
            [("a", "DK-SFW"), ("b", "DK-SFW-DONE")],
            (("b", "DK-SFW-DONE"), ("a", "DK-SFW")),
            id="substring",
        ),
        pytest.param(
            [("a", "dk-sfw"), ("b", "DK-SFW")],
            (("b", "DK-SFW"), ("a", "dk-sfw")),
            id="case-insensitive-dupe",
        ),
        # Empty codes are "off" and never collide, not even with each other.
        pytest.param([("a", ""), ("b", ""), ("c", "DK")], None, id="empties-ignored"),
    ],
)
def test_code_conflict(codes, expected):
    assert svc.code_conflict(codes) == expected


def test_config_code_conflict_reads_the_side_that_is_not_overridden(db_path):
    # The two sides are writable independently, so a save touching only one
    # of them still has to be checked against what's stored for the other.
    with open_db(db_path) as conn:
        _use_coded_steps(conn)  # DK-SFW / DK-NSFW
        set_config_value(conn, svc.CODE_KEY, "DK-DONE", GUILD)
        assert svc.config_code_conflict(conn, GUILD) is None
        # A completion code the stored step code contains: any message
        # carrying "DK-SFW" carries "DK" too, so completion fires.
        assert svc.config_code_conflict(conn, GUILD, completion="DK") == (
            ("SFW questions", "DK-SFW"),
            (svc.COMPLETION_CODE_LABEL, "DK"),
        )
        # …and the mirror: a step code that swallows the stored completion.
        clash = svc.config_code_conflict(
            conn, GUILD, steps=[svc.StepDef("s", "Wrap", code="say DK-DONE now")]
        )
        assert clash == (
            ("Wrap", "say DK-DONE now"),
            (svc.COMPLETION_CODE_LABEL, "DK-DONE"),
        )


def test_completion_code_containing_a_step_code_closes_the_card(db_path):
    # Why the guard exists, stated as behavior: with the clash in place, the
    # SFW canned message doesn't tick the SFW step — it completes the card and
    # stamps every remaining step skipped.
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        set_config_value(conn, svc.CODE_KEY, "DK", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn, content="all done DK-SFW", channel_id=999) == [
            (svc.ACTION_COMPLETE, NEWCOMER, "")
        ]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param(SimpleNamespace(reference=None), 0, id="not-a-reply"),
        pytest.param(
            SimpleNamespace(reference=SimpleNamespace(resolved=None)), 0, id="unresolved"
        ),
        # DeletedReferencedMessage has no .author at all.
        pytest.param(
            SimpleNamespace(
                reference=SimpleNamespace(resolved=SimpleNamespace(spec=1))
            ),
            0,
            id="deleted-target",
        ),
        pytest.param(
            SimpleNamespace(
                reference=SimpleNamespace(
                    resolved=SimpleNamespace(author=SimpleNamespace(id=NEWCOMER))
                )
            ),
            NEWCOMER,
            id="reply-to-newcomer",
        ),
    ],
)
def test_reply_target_id(message, expected):
    assert svc.reply_target_id(message) == expected


# ── card ledger + dedup ───────────────────────────────────────────────


def test_create_card_snapshots_configured_steps(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert cid is not None
        steps = svc.steps_for(conn, cid)
        assert [s["step_key"] for s in steps] == [s2["key"] for s2 in CUSTOM_STEPS]
        # Snapshot: later config edits must not touch the in-flight card.
        set_config_value(conn, svc.STEPS_KEY, json.dumps([CUSTOM_STEPS[0]]), GUILD)
        assert len(svc.steps_for(conn, cid)) == len(CUSTOM_STEPS)


def test_one_open_card_per_member(db_path):
    with open_db(db_path) as conn:
        first = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert first is not None
        assert svc.create_card(conn, GUILD, NEWCOMER, 101.0) is None
        assert svc.get_open_card(conn, GUILD, NEWCOMER)["id"] == first


def test_resolve_frees_the_slot(db_path):
    with open_db(db_path) as conn:
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert svc.resolve_card(conn, cid, GREETER, 200.0, svc.RESOLUTION_DISMISSED) == 1
        assert svc.get_open_card(conn, GUILD, NEWCOMER) is None
        # Resolving again is a no-op; a rejoin can open a fresh card.
        assert svc.resolve_card(conn, cid, GREETER, 300.0, svc.RESOLUTION_LEFT) == 0
        assert svc.create_card(conn, GUILD, NEWCOMER, 400.0) is not None


def test_delete_card_removes_steps_too(db_path):
    with open_db(db_path) as conn:
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.set_card_message(conn, cid, CHANNEL, 12345)
        assert svc.get_card(conn, cid)["message_id"] == 12345
        svc.delete_card(conn, cid)
        assert svc.get_card(conn, cid) is None
        assert svc.steps_for(conn, cid) == []


# ── manual tick / untick ──────────────────────────────────────────────


def test_tick_records_actor_and_untick_toggles(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert svc.set_step_state(
            conn, cid, "sfw_questions", done=True, actor_id=GREETER, at=200.0
        )
        step = next(s for s in svc.steps_for(conn, cid) if s["step_key"] == "sfw_questions")
        assert step["done_by"] == GREETER
        assert step["done_at"] == 200.0
        # A second tick loses: the original ticker is preserved.
        assert not svc.set_step_state(
            conn, cid, "sfw_questions", done=True, actor_id=999, at=300.0
        )
        step = next(s for s in svc.steps_for(conn, cid) if s["step_key"] == "sfw_questions")
        assert step["done_by"] == GREETER
        # Untick clears; unticking an un-done step is a no-op.
        assert svc.set_step_state(
            conn, cid, "sfw_questions", done=False, actor_id=GREETER, at=400.0
        )
        assert not svc.set_step_state(
            conn, cid, "sfw_questions", done=False, actor_id=GREETER, at=500.0
        )


# ── auto ticks ────────────────────────────────────────────────────────


def test_auto_tick_greeted(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        card, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 200.0, actor_id=GREETER
        )
        assert card is not None
        assert ticked == ["greeted"]
        # Already done → nothing new ticks, no re-render needed.
        _, again = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 300.0, actor_id=GREETER
        )
        assert again == []


def test_auto_tick_role_matches_configured_role_only(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # An unrelated role does nothing.
        _, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 200.0, role_id=12345
        )
        assert ticked == []
        _, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 200.0, role_id=MEMBER_ROLE
        )
        assert ticked == ["member_role"]
        step = next(s for s in svc.steps_for(conn, cid) if s["step_key"] == "member_role")
        assert step["done_by"] == svc.AUTO_ACTOR


def test_auto_tick_role_never_matches_role_id_zero(db_path):
    with open_db(db_path) as conn:
        # Tolerantly-parsed stored config can still carry a role_gained step
        # with role_id 0 — gaining any role (even "role 0") must not tick it.
        set_config_value(
            conn,
            svc.STEPS_KEY,
            json.dumps([{"key": "r", "label": "Role", "auto": "role_gained"}]),
            GUILD,
        )
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        _, ticked = svc.auto_tick(
            conn, GUILD, NEWCOMER, svc.AUTO_ROLE_GAINED, 200.0, role_id=0
        )
        assert ticked == []


def test_default_steps_are_all_tickable():
    # Regression: the default role steps shipped as role_gained with
    # role_id 0 — auto-untickable AND button-less. Every default step must
    # be either manual (gets a button) or an auto kind that needs no role.
    for step in svc.DEFAULT_STEPS:
        if step.auto_kind == svc.AUTO_ROLE_GAINED:
            assert step.auto_role_id > 0, f"{step.key} can never tick"


def test_auto_tick_without_open_card(db_path):
    with open_db(db_path) as conn:
        card, ticked = svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 200.0)
        assert card is None
        assert ticked == []


# ── completion (the code always wins) ─────────────────────────────────


def test_complete_stamps_skips_and_records_poster(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 200.0, actor_id=GREETER)
        result = svc.complete_card(conn, GUILD, NEWCOMER, GREETER, 300.0)
        assert result is not None
        card, skipped = result
        assert card["id"] == cid
        # Everything un-done is skipped, in position order; done steps aren't.
        assert skipped == ["member_role", "sfw_questions", "nsfw_role"]
        closed = svc.get_card(conn, cid)
        assert closed["resolution"] == svc.RESOLUTION_COMPLETED
        assert closed["resolved_by"] == GREETER
        steps = {s["step_key"]: s for s in svc.steps_for(conn, cid)}
        assert steps["greeted"]["skipped"] == 0
        assert steps["sfw_questions"]["skipped"] == 1
        # Progress counts real ticks only, never skips.
        assert svc.count_progress(svc.steps_for(conn, cid)) == (1, 4)


def test_complete_without_open_card(db_path):
    with open_db(db_path) as conn:
        assert svc.complete_card(conn, GUILD, NEWCOMER, GREETER, 300.0) is None


def test_close_for_member_on_leave(db_path):
    with open_db(db_path) as conn:
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.set_card_message(conn, cid, CHANNEL, 12345)
        card = svc.close_for_member(
            conn, GUILD, NEWCOMER, svc.RESOLUTION_LEFT, 0, 200.0
        )
        # Pre-close snapshot keeps the message location for the edit.
        assert card["message_id"] == 12345
        assert svc.get_card(conn, cid)["resolution"] == svc.RESOLUTION_LEFT
        assert svc.close_for_member(
            conn, GUILD, NEWCOMER, svc.RESOLUTION_BANNED, 0, 300.0
        ) is None


# ── message evaluation (greet + step/completion codes) ────────────────


def _eval(conn, **kw):
    defaults = dict(
        channel_id=CHANNEL,
        content="welcome!",
        mentioned_ids=[NEWCOMER],
        author_is_greeter=True,
        author_is_mod=False,
    )
    defaults.update(kw)
    return svc.evaluate_message(conn, GUILD, **defaults)


def test_evaluate_message_dark_or_no_mentions(db_path):
    with open_db(db_path) as conn:
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # Feature dark → nothing, even from a greeter in the right channel.
        assert _eval(conn) == []
        _enable(conn)
        assert _eval(conn, mentioned_ids=[]) == []


def test_evaluate_message_greet_gating(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn) == [(svc.ACTION_GREET, NEWCOMER, "")]
        # Wrong channel → no greet; non-greeter (even a mod) → no greet.
        assert _eval(conn, channel_id=999) == []
        assert _eval(conn, author_is_greeter=False, author_is_mod=True) == []
        # Mentioning someone without an open card → nothing.
        assert _eval(conn, mentioned_ids=[12345]) == []


def test_evaluate_message_completion_code(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        set_config_value(conn, svc.CODE_KEY, "DK-7734", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # Code + mention from a greeter, any channel → complete beats greet.
        assert _eval(conn, content="all set, dk-7734!", channel_id=999) == [
            (svc.ACTION_COMPLETE, NEWCOMER, "")
        ]
        # A mod who isn't a greeter can also complete…
        assert _eval(
            conn,
            content="dk-7734",
            author_is_greeter=False,
            author_is_mod=True,
            channel_id=999,
        ) == [(svc.ACTION_COMPLETE, NEWCOMER, "")]
        # …but a regular member saying the code does nothing.
        assert _eval(
            conn, content="dk-7734", author_is_greeter=False, channel_id=999
        ) == []


def test_evaluate_message_no_code_configured_never_completes(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # Without a configured code, a chatty message still only greets.
        assert _eval(conn, content="dk-7734") == [(svc.ACTION_GREET, NEWCOMER, "")]


def test_evaluate_message_dedupes_mentions_keeps_order(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        set_config_value(conn, svc.CODE_KEY, "DK-7734", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.create_card(conn, GUILD, 8, 100.0)
        actions = _eval(
            conn, content="dk-7734", mentioned_ids=[8, NEWCOMER, 8]
        )
        assert actions == [
            (svc.ACTION_COMPLETE, 8, ""),
            (svc.ACTION_COMPLETE, NEWCOMER, ""),
        ]


def _use_coded_steps(conn):
    """Steps where the two question phases carry their own codes."""
    set_config_value(
        conn,
        svc.STEPS_KEY,
        json.dumps(
            [
                {"key": "greeted", "label": "Greeted", "auto": "greeted"},
                {"key": "sfw_questions", "label": "SFW questions", "code": "DK-SFW"},
                {"key": "nsfw_questions", "label": "NSFW questions", "code": "DK-NSFW"},
            ]
        ),
        GUILD,
    )


def test_evaluate_message_step_code_ticks_one_step_any_channel(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # Any channel, case-insensitive, and the card is NOT closed.
        assert _eval(conn, content="done here -# dk-sfw", channel_id=999) == [
            (svc.ACTION_STEP, NEWCOMER, "sfw_questions")
        ]
        # A mod who isn't a greeter can tick too…
        assert _eval(
            conn,
            content="DK-NSFW",
            channel_id=999,
            author_is_greeter=False,
            author_is_mod=True,
        ) == [(svc.ACTION_STEP, NEWCOMER, "nsfw_questions")]
        # …but a regular member posting the code does nothing.
        assert (
            _eval(
                conn, content="DK-NSFW", channel_id=999, author_is_greeter=False
            )
            == []
        )


def test_evaluate_message_greeting_may_also_carry_a_step_code(db_path):
    # The canned greeting is both a greet and (optionally) a coded message —
    # unlike completion, a step code doesn't suppress the greet.
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn, content="hi! dk-sfw") == [
            (svc.ACTION_GREET, NEWCOMER, ""),
            (svc.ACTION_STEP, NEWCOMER, "sfw_questions"),
        ]


def test_evaluate_message_completion_code_beats_step_codes(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        set_config_value(conn, svc.CODE_KEY, "DK-DONE", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # The card is closing and stamps the rest skipped, so ticking a step
        # first would be noise.
        assert _eval(conn, content="dk-sfw dk-done", channel_id=999) == [
            (svc.ACTION_COMPLETE, NEWCOMER, "")
        ]


def test_evaluate_message_one_message_may_tick_several_steps(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert _eval(conn, content="DK-SFW and DK-NSFW", channel_id=999) == [
            (svc.ACTION_STEP, NEWCOMER, "sfw_questions"),
            (svc.ACTION_STEP, NEWCOMER, "nsfw_questions"),
        ]


def test_evaluate_message_uncoded_steps_never_match(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        # "greeted" has no code; an empty code must never match a message,
        # or every message would tick every uncoded step.
        assert _eval(conn, content="nothing special", channel_id=999) == []


def test_step_code_tick_lands_on_the_card(db_path):
    # The service-level effect the cog wires up: ACTION_STEP -> set_step_state
    # on the open card, and re-posting the code is a no-op.
    with open_db(db_path) as conn:
        _enable(conn)
        _use_coded_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        (_, uid, key), = _eval(conn, content="dk-sfw", channel_id=999)
        assert svc.set_step_state(
            conn, cid, key, done=True, actor_id=GREETER, at=200.0
        )
        row = next(s for s in svc.steps_for(conn, cid) if s["step_key"] == key)
        assert row["done_at"] == 200.0 and row["done_by"] == GREETER
        # Already ticked → no second write, so the original ticker stands.
        assert not svc.set_step_state(
            conn, cid, key, done=True, actor_id=999, at=300.0
        )


def test_step_code_for_a_step_not_on_this_card_ticks_nothing(db_path):
    # Codes match the LIVE config but ticks land on the card's snapshot, so a
    # step added after the card was created has nothing to tick — the match
    # still happens, it just doesn't land.
    with open_db(db_path) as conn:
        _enable(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)  # default steps
        set_config_value(
            conn,
            svc.STEPS_KEY,
            json.dumps(
                [{"key": "brand_new", "label": "Added later", "code": "DK-NEW"}]
            ),
            GUILD,
        )
        assert _eval(conn, content="dk-new", channel_id=999) == [
            (svc.ACTION_STEP, NEWCOMER, "brand_new")
        ]
        assert not svc.set_step_state(
            conn, cid, "brand_new", done=True, actor_id=GREETER, at=200.0
        )


def test_inviter_for(db_path):
    from bot_modules.services.invite_tracker import record_invite

    with open_db(db_path) as conn:
        assert svc.inviter_for(conn, GUILD, NEWCOMER) is None
        record_invite(conn, GUILD, GREETER, NEWCOMER, "abc123", joined_at=100.0)
        assert svc.inviter_for(conn, GUILD, NEWCOMER) == GREETER


def test_greeter_role_id_getter(db_path):
    with open_db(db_path) as conn:
        assert svc.greeter_role_id(conn, GUILD) == 0
        set_config_value(conn, svc.GREETER_ROLE_KEY, "4242", GUILD)
        assert svc.greeter_role_id(conn, GUILD) == 4242


# ── stale scan ────────────────────────────────────────────────────────


def test_stale_after_default_window_and_tick_resets_clock(db_path):
    hour = 3600.0
    with open_db(db_path) as conn:
        cid = svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        assert svc.stale_cards(conn, GUILD, 23 * hour) == []
        assert [c["id"] for c in svc.stale_cards(conn, GUILD, 25 * hour)] == [cid]
        # Any progress resets the clock…
        svc.set_step_state(
            conn, cid, "sfw_questions", done=True, actor_id=GREETER, at=10 * hour
        )
        assert svc.stale_cards(conn, GUILD, 25 * hour) == []
        # …until the window passes again after the last tick.
        assert [c["id"] for c in svc.stale_cards(conn, GUILD, 35 * hour)] == [cid]


def test_stale_skips_nudged_and_resolved(db_path):
    hour = 3600.0
    with open_db(db_path) as conn:
        first = svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        second = svc.create_card(conn, GUILD, 8, 0.0)
        svc.mark_nudged(conn, first, 25 * hour)
        assert [c["id"] for c in svc.stale_cards(conn, GUILD, 26 * hour)] == [second]
        svc.resolve_card(conn, second, GREETER, 26 * hour, svc.RESOLUTION_DISMISSED)
        assert svc.stale_cards(conn, GUILD, 27 * hour) == []


def test_stale_respects_configured_hours(db_path):
    hour = 3600.0
    with open_db(db_path) as conn:
        set_config_value(conn, svc.STALE_HOURS_KEY, "2", GUILD)
        svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        assert svc.stale_cards(conn, GUILD, 1 * hour) == []
        assert len(svc.stale_cards(conn, GUILD, 3 * hour)) == 1


# ── reports ───────────────────────────────────────────────────────────


def test_report_open_cards_progress_and_order(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        second = svc.create_card(conn, GUILD, 8, 50.0)
        first = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        assert first is not None and second is not None
        svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 200.0, actor_id=GREETER)
        svc.mark_nudged(conn, second, 300.0)
        report = svc.report_open_cards(conn, GUILD)
        # Oldest first; progress and pending labels per card.
        assert [r["user_id"] for r in report] == [8, NEWCOMER]
        assert report[0]["nudged"] is True
        assert report[1] == {
            "user_id": NEWCOMER,
            "user_name": str(NEWCOMER),  # no known_users row → id as name
            "created_at": 100.0,
            "nudged": False,
            "done": 1,
            "total": 4,
            "pending": ["Member role", "SFW questions asked", "NSFW access"],
        }


def test_report_outcomes_stats(db_path):
    with open_db(db_path) as conn:
        for uid, created, resolved in [(1, 0.0, 100.0), (2, 0.0, 300.0)]:
            svc.create_card(conn, GUILD, uid, created)
            svc.complete_card(conn, GUILD, uid, GREETER, resolved)
        cid = svc.create_card(conn, GUILD, 3, 0.0)
        svc.resolve_card(conn, cid, GREETER, 50.0, svc.RESOLUTION_DISMISSED)
        svc.create_card(conn, GUILD, 4, 0.0)  # still open — not in outcomes
        out = svc.report_outcomes(conn, GUILD, 0.0)
        assert out["resolved"] == 3
        assert out["counts"] == {"completed": 2, "dismissed": 1}
        assert out["mean_seconds"] == 200.0
        assert out["median_seconds"] == 200.0
        # Window: nothing created since ts 10 → empty.
        assert svc.report_outcomes(conn, GUILD, 10.0)["resolved"] == 0


def test_report_welcomers_counts_ticks_not_auto(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        # A human tick, an auto tick (done_by 0 — never credited), a completion.
        svc.set_step_state(conn, cid, "sfw_questions", done=True, actor_id=GREETER, at=10.0)
        svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_VERIFIED, 20.0)
        svc.complete_card(conn, GUILD, NEWCOMER, GREETER, 30.0)
        report = svc.report_welcomers(conn, GUILD, 0.0)
        assert report == [
            {
                "user_id": GREETER,
                "user_name": str(GREETER),
                "completions": 1,
                "ticks": 1,
            }
        ]


def test_report_skipped_steps(db_path):
    with open_db(db_path) as conn:
        _use_custom_steps(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 0.0)
        svc.auto_tick(conn, GUILD, NEWCOMER, svc.AUTO_GREETED, 10.0, actor_id=GREETER)
        svc.complete_card(conn, GUILD, NEWCOMER, GREETER, 20.0)
        rows = svc.report_skipped_steps(conn, GUILD, 0.0)
        assert [(r["key"], r["appeared"], r["skipped"]) for r in rows] == [
            ("greeted", 1, 0),
            ("member_role", 1, 1),
            ("sfw_questions", 1, 1),
            ("nsfw_role", 1, 1),
        ]


# ── watch registry ────────────────────────────────────────────────────


def test_warm_seeds_open_cards_even_when_disabled(db_path):
    # Regression: warm() used to skip disabled guilds, stranding open cards
    # as zombies across a disable → restart → enable cycle (no auto-ticks,
    # no leave/ban close). Open cards are watched regardless of the flag.
    with open_db(db_path) as conn:
        _enable(conn)
        svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.create_card(conn, 99, 5, 100.0)  # guild 99 is dark but carded
    svc.warm(db_path, [GUILD, 99])
    assert svc.is_watched(GUILD, NEWCOMER) is True
    assert svc.is_watched(99, 5) is True


def test_warm_excludes_resolved_cards(db_path):
    with open_db(db_path) as conn:
        _enable(conn)
        cid = svc.create_card(conn, GUILD, NEWCOMER, 100.0)
        svc.create_card(conn, GUILD, 8, 100.0)
        svc.resolve_card(conn, cid, GREETER, 200.0, svc.RESOLUTION_DISMISSED)
    svc.warm(db_path, [GUILD])
    assert svc.is_watched(GUILD, NEWCOMER) is False
    assert svc.is_watched(GUILD, 8) is True


def test_add_and_discard_watch(db_path):
    svc.add_watched(GUILD, 10)
    assert svc.is_watched(GUILD, 10) is True
    svc.discard(GUILD, 10)
    assert svc.is_watched(GUILD, 10) is False
    svc.discard(GUILD, 999)  # unknown member is harmless
