"""Tests for the ``/info`` panel's shaping layer.

The panel's whole job is deciding what a member is told and which buttons
they get, so that decision is what is tested: every feature's configured /
unconfigured branch, every opt-in state, the "configured but the cog isn't
loaded" degradation, and the two filters that exist for privacy reasons
(top-channel visibility, and the deliberate absence of a no-contact count).
"""

from __future__ import annotations

import pytest

from bot_modules.member_info.embeds import build_member_info_embed
from bot_modules.member_info.logic import (
    ACTION_JOIN,
    ACTION_LEAVE,
    ACTION_OPEN,
    STATE_IN,
    STATE_OUT,
    STATE_UNSET,
    AccountFacts,
    FeatureState,
    build_optin_rows,
    displayable_roles,
    visible_top_channels,
)

ALL_KEYS = (
    "pen_pals",
    "whispers",
    "guess",
    "dm_mode",
    "wellness",
    "birthday",
    "no_contact",
)


def _rows(states):
    return {row.key: row for row in build_optin_rows(states)}


# ── Unconfigured features never appear ───────────────────────────────────


def test_no_states_means_no_rows():
    assert build_optin_rows({}) == []


@pytest.mark.parametrize("key", ALL_KEYS)
def test_unconfigured_feature_is_dropped_entirely(key):
    """A feature the guild never set up must not be advertised, nor joinable."""
    rows = _rows({key: FeatureState(configured=False, state=STATE_UNSET)})
    assert key not in rows


@pytest.mark.parametrize("key", ALL_KEYS)
def test_configured_feature_appears(key):
    rows = _rows({key: FeatureState(configured=True, state=STATE_UNSET)})
    assert key in rows
    assert rows[key].text


def test_rows_keep_spec_order_regardless_of_input_order():
    states = {key: FeatureState(configured=True) for key in reversed(ALL_KEYS)}
    assert [row.key for row in build_optin_rows(states)] == list(ALL_KEYS)


# ── Per-state actions ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "state", "expected_action"),
    [
        # Joining is offered to someone who never joined *and* to someone who
        # left — leaving is a decision, not a ban, and rejoining is the undo.
        ("pen_pals", STATE_UNSET, ACTION_JOIN),
        ("pen_pals", STATE_OUT, ACTION_JOIN),
        ("pen_pals", STATE_IN, ACTION_LEAVE),
        ("whispers", STATE_UNSET, ACTION_JOIN),
        ("whispers", STATE_IN, ACTION_LEAVE),
        ("guess", STATE_UNSET, ACTION_JOIN),
        ("guess", STATE_IN, ACTION_LEAVE),
        ("birthday", STATE_UNSET, ACTION_JOIN),
        ("birthday", STATE_IN, ACTION_LEAVE),
        # These three always open their own panel — there is no one-click
        # state to flip, which is the point of routing through them.
        ("dm_mode", STATE_IN, ACTION_OPEN),
        ("wellness", STATE_IN, ACTION_OPEN),
        ("wellness", STATE_OUT, ACTION_OPEN),
        ("no_contact", STATE_UNSET, ACTION_OPEN),
    ],
)
def test_action_offered_per_state(key, state, expected_action):
    rows = _rows({key: FeatureState(configured=True, state=state)})
    assert rows[key].action == expected_action
    assert rows[key].action_label


def test_pen_pals_unset_and_out_read_differently():
    """Someone who left is not told "not joined" — the bot doesn't re-pitch."""
    unset = _rows({"pen_pals": FeatureState(configured=True, state=STATE_UNSET)})
    out = _rows({"pen_pals": FeatureState(configured=True, state=STATE_OUT)})
    assert unset["pen_pals"].text != out["pen_pals"].text
    assert "left" in out["pen_pals"].text.lower()


# ── Degradation when a flow can't be reached ─────────────────────────────


@pytest.mark.parametrize("key", ALL_KEYS)
def test_not_actionable_shows_status_without_a_button(key):
    """A missing cog (or an unmet role gate) costs the button, not the row."""
    rows = _rows({key: FeatureState(configured=True, actionable=False)})
    assert key in rows
    assert rows[key].text
    assert rows[key].action is None
    assert rows[key].has_action is False


def test_detail_is_prepended_to_the_row_text():
    rows = _rows(
        {"dm_mode": FeatureState(configured=True, state=STATE_IN, detail="Currently **ask**")}
    )
    assert rows["dm_mode"].text.startswith("Currently **ask**")


# ── The no-contact row must never carry a count ──────────────────────────


def test_no_contact_row_states_are_indistinguishable():
    """Whoever is on the list, the row reads identically.

    `/nocontact list` hides entries the other party created against you. If
    this row's text varied with the member's own state, differencing it
    against that filtered view would leak the hidden ones back.
    """
    texts = {
        _rows({"no_contact": FeatureState(configured=True, state=state)})[
            "no_contact"
        ].text
        for state in (STATE_IN, STATE_OUT, STATE_UNSET)
    }
    assert len(texts) == 1
    assert not any(ch.isdigit() for ch in texts.pop())


# ── Top-channel visibility filter ────────────────────────────────────────


def _chan(channel_id: int, count: int):
    return {"channel_id": channel_id, "cnt": count}


def test_top_channels_drops_channels_the_member_cannot_see():
    rows = [_chan(1, 50), _chan(2, 40), _chan(3, 30)]
    assert visible_top_channels(rows, {1, 3}) == [(1, 50), (3, 30)]


def test_top_channels_backfills_from_lower_ranks_after_filtering():
    """Over-fetching is why the field isn't left empty by the filter."""
    rows = [_chan(1, 90), _chan(2, 80), _chan(3, 70), _chan(4, 60), _chan(5, 50)]
    assert visible_top_channels(rows, {4, 5}) == [(4, 60), (5, 50)]


def test_top_channels_honors_the_limit():
    rows = [_chan(i, 100 - i) for i in range(1, 10)]
    assert len(visible_top_channels(rows, set(range(1, 10)))) == 3


def test_top_channels_with_nothing_visible_is_empty():
    assert visible_top_channels([_chan(1, 5)], set()) == []


# ── Role display ─────────────────────────────────────────────────────────


def test_roles_are_capped_with_an_overflow_count():
    names, overflow = displayable_roles([f"r{i}" for i in range(20)], limit=5)
    assert names == ["r0", "r1", "r2", "r3", "r4"]
    assert overflow == 15


def test_roles_under_the_cap_have_no_overflow():
    names, overflow = displayable_roles(["a", "b"])
    assert names == ["a", "b"]
    assert overflow == 0


# ── Embed assembly ───────────────────────────────────────────────────────


def _facts(**kwargs):
    base = dict(
        account_age_days=400,
        created_ts=1_700_000_000,
        joined_ts=1_710_000_000,
        role_names=["Denizen"],
        level=7,
        total_xp=12_345,
        xp_by_source={"text": 10_000, "voice": 2_345},
        msgs_30d=312,
        top_channels=[(123, 90)],
        last_seen_ts=1_720_000_000,
    )
    base.update(kwargs)
    return AccountFacts(**base)


def test_embed_has_no_moderation_fields():
    """The member-facing card must not grow /modinfo's mod-only sections."""
    embed = build_member_info_embed(
        display_name="Ada",
        avatar_url=None,
        facts=_facts(),
        optin_rows=build_optin_rows({"pen_pals": FeatureState(configured=True)}),
    )
    blob = " ".join(f"{f.name} {f.value}" for f in embed.fields).lower()
    for forbidden in ("watch", "warning", "jail", "ticket"):
        assert forbidden not in blob


def test_wallet_section_absent_when_no_line_given():
    embed = build_member_info_embed(
        display_name="Ada", avatar_url=None, facts=_facts(), optin_rows=[]
    )
    assert not any("wallet" in f.name.lower() for f in embed.fields)


def test_wallet_section_present_when_given():
    embed = build_member_info_embed(
        display_name="Ada",
        avatar_url=None,
        facts=_facts(),
        optin_rows=[],
        wallet_line="🪙 **500** coins",
    )
    assert any("wallet" in f.name.lower() for f in embed.fields)


def test_embed_survives_a_brand_new_member():
    """No XP, no messages, no roles, no opt-ins — still a valid card."""
    embed = build_member_info_embed(
        display_name="New",
        avatar_url=None,
        facts=_facts(
            level=None,
            total_xp=0.0,
            xp_by_source={},
            msgs_30d=0,
            top_channels=[],
            last_seen_ts=None,
            role_names=[],
            joined_ts=None,
        ),
        optin_rows=[],
    )
    assert all(field.value for field in embed.fields)


def test_optin_section_lists_every_row():
    rows = build_optin_rows({key: FeatureState(configured=True) for key in ALL_KEYS})
    embed = build_member_info_embed(
        display_name="Ada", avatar_url=None, facts=_facts(), optin_rows=rows
    )
    section = next(f for f in embed.fields if "Opt-Ins" in f.name)
    for row in rows:
        assert row.label in (section.value or "")


# ── Regression: the roles field must never overrun the embed limit ───────
# Discord allows 100-character role names, so the count cap alone let twelve
# of them build a 1233-character field. Discord rejects the whole embed for
# that — and past the command's defer() a rejected embed is a card that never
# arrives, with no error shown.


def test_roles_field_stays_within_the_embed_limit():
    from bot_modules.services.embeds import EMBED_FIELD_LIMIT

    embed = build_member_info_embed(
        display_name="Ada",
        avatar_url=None,
        facts=_facts(role_names=["R" * 100 for _ in range(40)]),
        optin_rows=[],
    )
    roles = next(f for f in embed.fields if "Roles" in f.name)
    assert len(roles.value or "") <= EMBED_FIELD_LIMIT


def test_roles_field_reports_everything_it_dropped():
    """Names cut for length still get counted, not quietly lost."""
    embed = build_member_info_embed(
        display_name="Ada",
        avatar_url=None,
        facts=_facts(role_names=["R" * 100 for _ in range(40)]),
        optin_rows=[],
    )
    roles = next(f for f in embed.fields if "Roles" in f.name)
    shown = (roles.value or "").count("R" * 100)
    assert f"+{40 - shown} more" in (roles.value or "")


def test_every_variable_length_field_stays_within_the_limit():
    """The whole card, built from worst-case inputs."""
    from bot_modules.services.embeds import EMBED_FIELD_LIMIT

    rows = build_optin_rows({key: FeatureState(configured=True) for key in ALL_KEYS})
    embed = build_member_info_embed(
        display_name="Ada",
        avatar_url=None,
        facts=_facts(
            role_names=["R" * 100 for _ in range(40)],
            xp_by_source={f"source_{i}": 1234 for i in range(30)},
            top_channels=[(10**18 + i, 999) for i in range(25)],
        ),
        optin_rows=rows,
        wallet_line="🪙 **999,999** coins",
        wallet_extra=[f"**Perk {i}** — 🪙 500/wk" for i in range(30)],
    )
    for field in embed.fields:
        assert len(field.value or "") <= EMBED_FIELD_LIMIT, field.name


# ── Regression: the wellness button must not promise a read-only view ────


def test_wellness_opted_in_label_does_not_say_settings():
    """The only entry point re-runs opt-in, which resets notification prefs."""
    rows = _rows({"wellness": FeatureState(configured=True, state=STATE_IN)})
    assert "settings" not in rows["wellness"].action_label.lower()


# ── Regression: the activity field must not contradict its own header ────
# Discord evicts a thread from the guild cache when it archives, and
# processed_messages holds only the thread's own id — so a member whose month
# was spent in now-archived threads has a real count and nothing nameable.


def test_activity_does_not_claim_no_messages_when_there_were_messages():
    embed = build_member_info_embed(
        display_name="Ada",
        avatar_url=None,
        facts=_facts(msgs_30d=150, top_channels=[]),
        optin_rows=[],
    )
    activity = next(f for f in embed.fields if "Activity" in f.name)
    assert "150" in activity.name
    assert "No messages recorded" not in (activity.value or "")


def test_activity_still_says_nothing_recorded_when_there_was_nothing():
    embed = build_member_info_embed(
        display_name="Ada",
        avatar_url=None,
        facts=_facts(msgs_30d=0, top_channels=[]),
        optin_rows=[],
    )
    activity = next(f for f in embed.fields if "Activity" in f.name)
    assert "No messages recorded" in (activity.value or "")


# ── Level progress ───────────────────────────────────────────────────────


def test_level_field_shows_xp_to_the_next_level():
    embed = build_member_info_embed(
        display_name="Ada",
        avatar_url=None,
        facts=_facts(level=7, total_xp=12_345, next_level_xp=13_585),
        optin_rows=[],
    )
    level = next(f for f in embed.fields if "Level" in f.name)
    assert "1,240 XP to Level 8" in (level.value or "")


def test_level_field_omits_progress_when_there_is_no_next_level():
    embed = build_member_info_embed(
        display_name="Ada",
        avatar_url=None,
        facts=_facts(level=None, total_xp=0, next_level_xp=None),
        optin_rows=[],
    )
    level = next(f for f in embed.fields if "Level" in f.name)
    assert "to Level" not in (level.value or "")


@pytest.mark.parametrize(
    ("total_xp", "next_level_xp", "expected"),
    [
        (12_345, 13_585, 1_240),
        (100, 100, 0),
        # The stored level lags the XP that earns it, and the curve factor is a
        # live dial — so "already past the threshold" is reachable and must not
        # render as a negative.
        (14_000, 13_585, 0),
        (0, None, None),
    ],
)
def test_xp_to_next_level_never_goes_negative(total_xp, next_level_xp, expected):
    from bot_modules.member_info.logic import xp_to_next_level

    assert xp_to_next_level(total_xp, next_level_xp) == expected


# ── The "More" field names only what this server runs ────────────────────


def test_help_lines_render_when_given():
    embed = build_member_info_embed(
        display_name="Ada",
        avatar_url=None,
        facts=_facts(),
        optin_rows=[],
        help_lines=["`/ask` — ask Meadow-bot how anything here works."],
    )
    more = next(f for f in embed.fields if "More" in f.name)
    assert "Meadow-bot" in (more.value or "")


def test_no_more_field_without_help_lines():
    """A server with neither cog loaded gets no empty section."""
    embed = build_member_info_embed(
        display_name="Ada", avatar_url=None, facts=_facts(), optin_rows=[]
    )
    assert not any("More" in f.name for f in embed.fields)
