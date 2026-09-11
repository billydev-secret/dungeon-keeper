"""Tier 1 unit tests: jail pure logic."""

import time

import pytest
from freezegun import freeze_time

from bot_modules.jail.logic import (
    POLICY_EXPOSURE_COUNT_CAP,
    POLICY_VISIBILITY_MODS,
    POLICY_VISIBILITY_PUBLIC,
    channel_needs_jail_deny,
    channels_needing_jail_deny,
    eligible_voters,
    format_exposure_count,
    is_jail_expired,
    is_policy_public,
    jail_duration_seconds,
    normalize_policy_visibility,
    policy_exposure_warning,
    policy_visibility_label,
    policy_visibility_status,
    resolve_policy_vote,
    restore_roles,
    snapshot_roles,
    tally_votes,
    toggle_policy_visibility,
    vote_outcome,
)
from bot_modules.services.moderation import fmt_duration, parse_duration


# ── Jailed-role channel visibility ────────────────────────────────────

@pytest.mark.parametrize("overwrite,needs", [
    (None, True),    # no overwrite → inherits @everyone → exposed
    (True, True),    # explicitly allowed → must be overridden
    (False, False),  # already denied → leave it
])
def test_channel_needs_jail_deny(overwrite, needs):
    assert channel_needs_jail_deny(overwrite) is needs


def test_channels_needing_jail_deny_filters_and_preserves_order():
    states = [
        (10, None),    # exposed
        (20, False),   # already denied — skip
        (30, True),    # allowed — must override
        (40, None),    # exposed
    ]
    assert channels_needing_jail_deny(states) == [10, 30, 40]


def test_channels_needing_jail_deny_all_denied_is_empty():
    states = [(1, False), (2, False), (3, False)]
    assert channels_needing_jail_deny(states) == []


def test_channels_needing_jail_deny_empty_input():
    assert channels_needing_jail_deny([]) == []


# ── parse_duration ────────────────────────────────────────────────────

@pytest.mark.parametrize("s,expected", [
    ("30m", 1800),
    ("1h", 3600),
    ("2d", 172_800),
    ("1w", 604_800),
    ("1d12h", 129_600),
    ("2h30m", 9000),
    ("", None),
    ("abc", None),
])
def test_parse_duration(s, expected):
    assert parse_duration(s) == expected


# ── fmt_duration ──────────────────────────────────────────────────────

@pytest.mark.parametrize("secs,expected", [
    (3600, "1h"),
    (7200, "2h"),
    (86400, "1d"),
    (90000, "1d 1h"),
    (129600, "1d 12h"),
    (604800, "1w"),
])
def test_fmt_duration(secs, expected):
    assert fmt_duration(secs) == expected


# ── snapshot_roles / restore_roles ────────────────────────────────────

def test_snapshot_roles_returns_copy():
    original = [1, 2, 3]
    snap = snapshot_roles(original)
    assert snap == original
    snap.append(99)
    assert 99 not in original


def test_restore_roles_filters_missing():
    stored = [1, 2, 3, 4]
    available = {1, 3}
    assert restore_roles(stored, available) == [1, 3]


def test_restore_roles_empty_available():
    assert restore_roles([1, 2], set()) == []


# ── is_jail_expired ───────────────────────────────────────────────────

@freeze_time("2026-04-23 12:00:00")
def test_jail_not_yet_expired():
    jail = {"created_at": 0.0, "expires_at": time.time() + 3600}
    assert not is_jail_expired(jail)


@freeze_time("2026-04-23 12:00:00")
def test_jail_exactly_expired():
    jail = {"created_at": 0.0, "expires_at": time.time()}
    assert is_jail_expired(jail)


def test_jail_no_expiry_never_expires():
    jail = {"created_at": 0.0, "expires_at": None}
    assert not is_jail_expired(jail)


@freeze_time("2026-04-23 12:00:00")
def test_jail_duration_seconds():
    now = time.time()
    jail = {"created_at": now - 3600}
    assert jail_duration_seconds(jail) == pytest.approx(3600, abs=1)


# ── eligible_voters ───────────────────────────────────────────────────

def _member(uid, is_bot=False, is_admin=False, role_ids=None):
    return {
        "user_id": uid,
        "is_bot": is_bot,
        "is_administrator": is_admin,
        "role_ids": role_ids or [],
    }


def test_eligible_voters_mod_role():
    members = [_member(1, role_ids=[5001]), _member(2, role_ids=[9999])]
    eligible = eligible_voters(members, mod_role_ids={5001}, admin_role_ids=set())
    assert 1 in eligible
    assert 2 not in eligible


def test_eligible_voters_admin_flag():
    members = [_member(1, is_admin=True), _member(2)]
    eligible = eligible_voters(members, mod_role_ids=set(), admin_role_ids=set())
    assert 1 in eligible
    assert 2 not in eligible


def test_eligible_voters_excludes_bots():
    members = [_member(1, is_bot=True, role_ids=[5001])]
    eligible = eligible_voters(members, mod_role_ids={5001}, admin_role_ids=set())
    assert 1 not in eligible


# ── tally_votes ───────────────────────────────────────────────────────

def test_tally_votes_basic():
    vote_map = {1: "yes", 2: "no", 3: "abstain"}
    eligible = {1, 2, 3, 4}
    tally = tally_votes(vote_map, eligible)
    assert 1 in tally["yes"]
    assert 2 in tally["no"]
    assert 3 in tally["abstain"]
    assert 4 in tally["awaiting"]


def test_tally_ignores_ineligible_votes():
    vote_map = {99: "yes"}  # 99 is not eligible
    eligible = {1, 2}
    tally = tally_votes(vote_map, eligible)
    assert tally["yes"] == []
    assert set(tally["awaiting"]) == {1, 2}


# ── resolve_policy_vote ───────────────────────────────────────────────

def test_resolve_vote_adopted():
    eligible = {1, 2}
    tally = {"yes": [1, 2], "no": [], "abstain": [], "awaiting": []}
    assert resolve_policy_vote(tally, eligible) == "adopted"


def test_resolve_vote_rejected_by_no():
    eligible = {1, 2}
    tally = {"yes": [1], "no": [2], "abstain": [], "awaiting": []}
    assert resolve_policy_vote(tally, eligible) == "rejected"


def test_resolve_vote_pending_missing_votes():
    eligible = {1, 2, 3}
    tally = {"yes": [1], "no": [], "abstain": [], "awaiting": [2, 3]}
    assert resolve_policy_vote(tally, eligible) == "pending"


# ── vote_outcome (timeout-aware) ──────────────────────────────────────

def test_vote_outcome_pre_timeout_pending_with_awaiting():
    eligible = {1, 2, 3}
    tally = {"yes": [1], "no": [], "abstain": [], "awaiting": [2, 3]}
    assert vote_outcome(tally, eligible, expired=False) == "pending"


def test_vote_outcome_pre_timeout_no_with_awaiting_stays_pending():
    # A 'no' alone does not finalize while anyone is still awaiting — the
    # vote waits for full participation (or the timeout sweeper).
    eligible = {1, 2, 3}
    tally = {"yes": [1], "no": [2], "abstain": [], "awaiting": [3]}
    assert vote_outcome(tally, eligible, expired=False) == "pending"


def test_vote_outcome_pre_timeout_all_voted_no_rejects():
    eligible = {1, 2}
    tally = {"yes": [1], "no": [2], "abstain": [], "awaiting": []}
    assert vote_outcome(tally, eligible, expired=False) == "rejected"


def test_vote_outcome_pre_timeout_all_voted_yes_adopts():
    eligible = {1, 2}
    tally = {"yes": [1, 2], "no": [], "abstain": [], "awaiting": []}
    assert vote_outcome(tally, eligible, expired=False) == "adopted"


def test_vote_outcome_expired_drops_absentees_adopts():
    # After timeout, the two absentees stop blocking adoption.
    eligible = {1, 2, 3, 4}
    tally = {"yes": [1, 2], "no": [], "abstain": [], "awaiting": [3, 4]}
    assert vote_outcome(tally, eligible, expired=True) == "adopted"


def test_vote_outcome_expired_abstain_counts_as_participation():
    eligible = {1, 2, 3}
    tally = {"yes": [1], "no": [], "abstain": [2], "awaiting": [3]}
    assert vote_outcome(tally, eligible, expired=True) == "adopted"


def test_vote_outcome_expired_no_still_rejects():
    eligible = {1, 2, 3}
    tally = {"yes": [1], "no": [2], "abstain": [], "awaiting": [3]}
    assert vote_outcome(tally, eligible, expired=True) == "rejected"


def test_vote_outcome_expired_no_quorum():
    eligible = {1, 2, 3}
    tally = {"yes": [], "no": [], "abstain": [], "awaiting": [1, 2, 3]}
    assert vote_outcome(tally, eligible, expired=True) == "rejected_no_quorum"


# ── Policy channel visibility ─────────────────────────────────────────
#
# A proposal starts mods-only and a mod may open it to the general public.
# Opening grants history, so it cannot be taken back in the sense that
# matters — these tests pin the two guards that stand between a press and
# that: an unknown state reading as private, and a warning that names the
# backlog.


@pytest.mark.parametrize("stored,expected", [
    ("public", POLICY_VISIBILITY_PUBLIC),
    ("mods", POLICY_VISIBILITY_MODS),
    # Everything below is an unknown state, and every one reads as PRIVATE.
    # The asymmetry is deliberate: mis-reading private-as-public would have
    # the card claim an openness it doesn't have, while this direction only
    # ever offers to open a channel that is already open.
    (None, POLICY_VISIBILITY_MODS),
    ("", POLICY_VISIBILITY_MODS),
    ("Public", POLICY_VISIBILITY_MODS),
    ("everyone", POLICY_VISIBILITY_MODS),
    (1, POLICY_VISIBILITY_MODS),
])
def test_normalize_policy_visibility(stored, expected):
    assert normalize_policy_visibility(stored) == expected
    assert is_policy_public(stored) is (expected == POLICY_VISIBILITY_PUBLIC)


@pytest.mark.parametrize("stored,nxt", [
    ("mods", POLICY_VISIBILITY_PUBLIC),
    ("public", POLICY_VISIBILITY_MODS),
    (None, POLICY_VISIBILITY_PUBLIC),
])
def test_toggle_policy_visibility(stored, nxt):
    assert toggle_policy_visibility(stored) == nxt


def test_toggle_is_its_own_inverse():
    assert toggle_policy_visibility(toggle_policy_visibility("mods")) == "mods"
    assert toggle_policy_visibility(toggle_policy_visibility("public")) == "public"


@pytest.mark.parametrize("stored,label,status", [
    ("mods", "Open to Members", "🔒 Mods only"),
    ("public", "Make Mods-Only", "🌐 Open to members"),
])
def test_label_names_the_action_and_status_names_the_state(stored, label, status):
    """The two must not drift into saying the same thing.

    A button reading "Mods Only" on a mods-only card is ambiguous — is that
    the situation or the offer? So the button is always the verb and the
    field is always the state.
    """
    assert policy_visibility_label(stored) == label
    assert policy_visibility_status(stored) == status
    assert policy_visibility_label(stored) != policy_visibility_status(stored)


@pytest.mark.parametrize("count,capped,expected", [
    (0, False, "0 messages"),
    (1, False, "1 message"),      # not "1 messages"
    (2, False, "2 messages"),
    (47, False, "47 messages"),
    (POLICY_EXPOSURE_COUNT_CAP, True, "500+ messages"),
])
def test_format_exposure_count(count, capped, expected):
    assert format_exposure_count(count, capped=capped) == expected


def test_exposure_warning_names_the_backlog_and_the_irreversibility():
    """The whole point of the confirm is that it is specific.

    A generic "are you sure?" would not stop the mistake this guards: opening
    a channel while thinking about the proposal and forgetting the candid
    discussion sitting above it.
    """
    text = policy_exposure_warning(47)
    assert "47 messages" in text
    assert "before now" in text          # the backlog, not just from here on
    assert "cannot un-read" in text      # opening is not really reversible
    assert "post here" in text           # members get to talk, not just read


def test_exposure_warning_is_a_floor_when_counting_stopped():
    text = policy_exposure_warning(POLICY_EXPOSURE_COUNT_CAP, capped=True)
    assert "500+ messages" in text


def test_an_uncountable_backlog_never_renders_as_a_number():
    """``None`` means the count failed — usually the bot cannot read history
    in this channel at all.

    Rendering that as "0 messages" would put the most reassuring sentence
    this prompt can produce in front of a mod at the exact moment nobody
    knows how much is about to be exposed. It has to read as unknown.
    """
    assert format_exposure_count(None) == "everything already posted in this channel"
    text = policy_exposure_warning(None)
    assert "0 messages" not in text
    assert "everything already posted in this channel" in text
    # The rest of the warning still has to land.
    assert "before now" in text
    assert "cannot un-read" in text
