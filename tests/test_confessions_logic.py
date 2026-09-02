"""Tests for the extracted confessions pure-logic module.

Covers ``bot_modules/confessions/logic.py``: notify-pref parsing, the
two character-cap formulas, reply cooldown, OP/notification decision
helpers, the tagged ``ButtonAction`` parser, and the component-shape
inspectors. The cog keeps the async Discord glue; this module proves
the helpers behave correctly without a real interaction.
"""

from __future__ import annotations

import pytest

from bot_modules.confessions.logic import (
    HELP_TEXT,
    QUEUED_ACK,
    REJECTION_REASON_MAX,
    ButtonAction,
    build_expiry_dm_text,
    build_rejection_dm_text,
    format_waiting_for,
    pending_option_text,
    ThreadRootInfo,
    audit_channel_id,
    build_dm_notification_text,
    compute_confession_max_chars,
    compute_reply_cooldown,
    compute_reply_max_chars,
    is_op_reply,
    is_stale_interaction_error_code,
    message_exposes_reply_buttons,
    message_has_confess_launcher,
    parse_button_custom_id,
    parse_notify_pref,
    resolve_thread_root_info,
    should_notify_op,
)
from bot_modules.services.confessions_service import (
    CONFESSION_HEADER_LENGTH,
    MAX_DISCORD_MESSAGE_LENGTH,
    MIN_REPLY_COOLDOWN_SECONDS,
)


# ── parse_notify_pref ────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["", "y", "yes", "YES", "Yes", "true", "1", "on", " yes "])
def test_parse_notify_pref_accepts_yes_tokens(raw):
    assert parse_notify_pref(raw) is True


def test_parse_notify_pref_treats_none_input_as_yes_default():
    """An unfilled modal field reaches us as None; default is 'yes'."""
    assert parse_notify_pref(None) is True


@pytest.mark.parametrize("raw", ["n", "no", "NO", "false", "0", "off", " no "])
def test_parse_notify_pref_accepts_no_tokens(raw):
    assert parse_notify_pref(raw) is False


@pytest.mark.parametrize("raw", ["maybe", "sometimes", "yeah", "nope", "2", "asdf"])
def test_parse_notify_pref_returns_none_on_invalid(raw):
    assert parse_notify_pref(raw) is None


# ── compute_confession_max_chars ─────────────────────────────────────


def test_compute_confession_max_chars_respects_cfg_when_below_discord_limit():
    """A conservative guild setting wins over Discord's hard cap."""
    assert compute_confession_max_chars(500) == 500


def test_compute_confession_max_chars_clamps_to_discord_limit_minus_header():
    """The Discord ceiling reserves space for the prefix header."""
    big = MAX_DISCORD_MESSAGE_LENGTH * 2
    assert compute_confession_max_chars(big) == MAX_DISCORD_MESSAGE_LENGTH - CONFESSION_HEADER_LENGTH


def test_compute_confession_max_chars_passes_through_zero_cfg():
    """A 0-config wins via min(); the inner max(1, ...) only protects the Discord
    side. This preserves the original cog behavior exactly."""
    assert compute_confession_max_chars(0) == 0


def test_compute_confession_max_chars_inner_max_protects_discord_floor():
    """If MAX_DISCORD_MESSAGE_LENGTH were ever <= header length the inner
    max(1, ...) keeps the Discord-side floor at 1 rather than going negative.
    With a high cfg, the cfg side won't shrink it."""
    assert compute_confession_max_chars(MAX_DISCORD_MESSAGE_LENGTH) == MAX_DISCORD_MESSAGE_LENGTH - CONFESSION_HEADER_LENGTH


# ── compute_reply_max_chars ──────────────────────────────────────────


def test_compute_reply_max_chars_caps_at_discord_limit():
    assert compute_reply_max_chars(MAX_DISCORD_MESSAGE_LENGTH * 4) == MAX_DISCORD_MESSAGE_LENGTH


def test_compute_reply_max_chars_respects_lower_cfg():
    assert compute_reply_max_chars(500) == 500


# ── compute_reply_cooldown ───────────────────────────────────────────


def test_compute_reply_cooldown_halves_confession_cooldown():
    assert compute_reply_cooldown(120) == 60


def test_compute_reply_cooldown_clamps_to_minimum():
    """A tiny configured cooldown can't drop replies below the floor."""
    assert compute_reply_cooldown(2) == MIN_REPLY_COOLDOWN_SECONDS


def test_compute_reply_cooldown_zero_cfg_uses_minimum():
    """A zero confession cooldown still gets the reply floor."""
    assert compute_reply_cooldown(0) == MIN_REPLY_COOLDOWN_SECONDS


# ── resolve_thread_root_info ─────────────────────────────────────────


def test_resolve_thread_root_info_falls_back_when_no_db_row():
    info = resolve_thread_root_info(
        None,
        fallback_parent_message_id=42,
        fallback_notify_op_on_reply=True,
    )
    assert info == ThreadRootInfo(root_message_id=42, parent_author_id=0, parent_notify_pref=1)


def test_resolve_thread_root_info_uses_zero_default_when_notify_off():
    info = resolve_thread_root_info(
        None,
        fallback_parent_message_id=42,
        fallback_notify_op_on_reply=False,
    )
    assert info.parent_notify_pref == 0


def test_resolve_thread_root_info_carries_stored_values():
    info = resolve_thread_root_info(
        (100, 200, 1),
        fallback_parent_message_id=999,
        fallback_notify_op_on_reply=False,
    )
    assert info.root_message_id == 100
    assert info.parent_author_id == 200
    assert info.parent_notify_pref == 1


def test_resolve_thread_root_info_falls_back_for_legacy_sentinel():
    """Pre-migration rows stored ``-1``; treat as 'use guild default'."""
    info = resolve_thread_root_info(
        (100, 200, -1),
        fallback_parent_message_id=999,
        fallback_notify_op_on_reply=True,
    )
    assert info.parent_notify_pref == 1

    info_off = resolve_thread_root_info(
        (100, 200, -1),
        fallback_parent_message_id=999,
        fallback_notify_op_on_reply=False,
    )
    assert info_off.parent_notify_pref == 0


# ── is_op_reply ──────────────────────────────────────────────────────


def test_is_op_reply_true_when_replier_matches_parent():
    assert is_op_reply(ephemeral=False, parent_author_id=42, replier_id=42) is True


def test_is_op_reply_false_when_ephemeral_even_if_same_user():
    """A 'Reply as Someone New' click never earns the OP badge."""
    assert is_op_reply(ephemeral=True, parent_author_id=42, replier_id=42) is False


def test_is_op_reply_false_when_parent_author_unknown():
    """parent_author_id=0 means legacy/missing row — can't claim OP."""
    assert is_op_reply(ephemeral=False, parent_author_id=0, replier_id=42) is False


def test_is_op_reply_false_when_replier_is_someone_else():
    assert is_op_reply(ephemeral=False, parent_author_id=42, replier_id=99) is False


# ── should_notify_op ─────────────────────────────────────────────────


def test_should_notify_op_true_when_third_party_replies_and_pref_on():
    assert should_notify_op(parent_author_id=42, replier_id=99, parent_notify_pref=1) is True


def test_should_notify_op_false_when_replier_is_op_themselves():
    """No self-DM on your own reply."""
    assert should_notify_op(parent_author_id=42, replier_id=42, parent_notify_pref=1) is False


def test_should_notify_op_false_when_pref_disabled():
    assert should_notify_op(parent_author_id=42, replier_id=99, parent_notify_pref=0) is False


def test_should_notify_op_false_when_no_known_op():
    assert should_notify_op(parent_author_id=0, replier_id=99, parent_notify_pref=1) is False


# ── build_dm_notification_text ───────────────────────────────────────


def test_build_dm_notification_text_includes_guild_name_and_links():
    text = build_dm_notification_text(
        guild_name="My Server",
        guild_id=1,
        reply_channel_id=2,
        reply_message_id=3,
        confession_channel_id=4,
        root_message_id=5,
    )
    assert "My Server" in text
    assert "https://discord.com/channels/1/2/3" in text
    assert "https://discord.com/channels/1/4/5" in text


def test_build_dm_notification_text_two_jump_links():
    text = build_dm_notification_text(
        guild_name="G",
        guild_id=10,
        reply_channel_id=20,
        reply_message_id=30,
        confession_channel_id=40,
        root_message_id=50,
    )
    # Reply link and confession link are different
    assert text.count("https://discord.com/channels/") == 2


# ── parse_button_custom_id ───────────────────────────────────────────


def test_parse_button_custom_id_handles_non_string_input():
    """Discord can technically hand us a dict; we treat it as 'not ours'."""
    assert parse_button_custom_id(None).kind == "ignore"
    assert parse_button_custom_id(123).kind == "ignore"


def test_parse_button_custom_id_ignores_unrelated_ids():
    """Other cogs' buttons share the listener; don't false-trigger on them."""
    assert parse_button_custom_id("some_other_cog|x").kind == "ignore"
    assert parse_button_custom_id("").kind == "ignore"


def test_parse_button_custom_id_new_confession_well_formed():
    action = parse_button_custom_id("nc|7777")
    assert action.kind == "new_confession"
    assert action.guild_id == 7777


def test_parse_button_custom_id_new_confession_malformed():
    action = parse_button_custom_id("nc|notanumber")
    assert action.kind == "invalid"
    assert action.error is not None and "confession" in action.error.lower()


def test_parse_button_custom_id_reply_well_formed():
    action = parse_button_custom_id("cr|123456")
    assert action.kind == "reply"
    assert action.root_id == 123456


def test_parse_button_custom_id_reply_new_well_formed():
    action = parse_button_custom_id("crn|654321")
    assert action.kind == "reply_new"
    assert action.root_id == 654321


def test_parse_button_custom_id_reply_help_well_formed():
    action = parse_button_custom_id("crh|111")
    assert action.kind == "reply_help"
    assert action.root_id == 111


def test_parse_button_custom_id_legacy_bare_cr():
    """Old posts had a bare ``cr`` button before root-id encoding."""
    action = parse_button_custom_id("cr")
    assert action.kind == "legacy_reply"
    assert action.root_id is None


@pytest.mark.parametrize("malformed", [
    "cr|notanumber", "cr|", "cr|123|extra",
    "crn|x", "crn|",
    "crh|x", "crh|",
])
def test_parse_button_custom_id_invalid_reply_prefixes_carry_error(malformed):
    action = parse_button_custom_id(malformed)
    assert action.kind == "invalid"
    assert action.error is not None


def test_parse_button_custom_id_invalid_nc_with_extra_pipe():
    action = parse_button_custom_id("nc|123|extra")
    assert action.kind == "invalid"


# ── message_has_confess_launcher / message_exposes_reply_buttons ──────


class _StubButton:
    def __init__(self, custom_id):
        self.custom_id = custom_id


class _StubRow:
    def __init__(self, children):
        self.children = children


def test_message_has_confess_launcher_matches_by_guild_id():
    components = [_StubRow([_StubButton("nc|99")])]
    assert message_has_confess_launcher(components, guild_id=99) is True


def test_message_has_confess_launcher_rejects_other_guild():
    """Two guilds with launcher buttons must not collide in the cleanup pass."""
    components = [_StubRow([_StubButton("nc|99")])]
    assert message_has_confess_launcher(components, guild_id=42) is False


def test_message_has_confess_launcher_handles_empty_components():
    assert message_has_confess_launcher([], guild_id=1) is False


def test_message_has_confess_launcher_ignores_non_button_children():
    """Children without a custom_id attribute shouldn't crash the check."""
    class _NoCustomId:
        pass
    components = [_StubRow([_NoCustomId(), _StubButton("nc|99")])]
    assert message_has_confess_launcher(components, guild_id=99) is True


def test_message_exposes_reply_buttons_true_for_persistent_reply():
    components = [_StubRow([_StubButton("cr|123")])]
    assert message_exposes_reply_buttons(components) is True


def test_message_exposes_reply_buttons_true_for_ephemeral_reply():
    components = [_StubRow([_StubButton("crn|123")])]
    assert message_exposes_reply_buttons(components) is True


def test_message_exposes_reply_buttons_false_for_help_only():
    """Help button alone doesn't make the message reply-able."""
    components = [_StubRow([_StubButton("crh|123")])]
    assert message_exposes_reply_buttons(components) is False


def test_message_exposes_reply_buttons_false_for_empty():
    assert message_exposes_reply_buttons([]) is False


# ── is_stale_interaction_error_code ──────────────────────────────────


@pytest.mark.parametrize("code", [40060, 10062])
def test_is_stale_interaction_error_code_true_for_known_codes(code):
    assert is_stale_interaction_error_code(code) is True


@pytest.mark.parametrize("code", [50001, 500, 0, None, "40060"])
def test_is_stale_interaction_error_code_false_for_others(code):
    assert is_stale_interaction_error_code(code) is False


# ── HELP_TEXT constant ───────────────────────────────────────────────


def test_help_text_mentions_confess_and_both_reply_buttons():
    """Spot check that the help blurb names confessing and both reply modes."""
    assert "Confess" in HELP_TEXT
    assert "Reply Anonymously" in HELP_TEXT
    assert "Reply as Someone New" in HELP_TEXT


# ── ButtonAction dataclass ───────────────────────────────────────────


def test_button_action_is_frozen():
    """Frozen dataclass prevents accidental mutation by callers."""
    action = ButtonAction(kind="ignore")
    with pytest.raises(Exception):  # noqa: PT011 — dataclasses.FrozenInstanceError
        action.kind = "reply"  # type: ignore[misc]


# ── Audit jump-link location ──────────────────────────────────────────
#
# The audit row records where a confession can be *linked to*, which is not
# where `confession_threads` records it. That row keeps the parent channel
# because it routes replies; a forum confession's starter message lives inside
# its thread, and a forum channel holds no messages of its own, so addressing
# it through the parent produces a link that looks right and goes nowhere.


@pytest.mark.parametrize(
    "is_forum,expected",
    [
        # Forum: the confession IS the thread's starter message.
        (True, 555),
        # Text channel: the message really is in the parent channel.
        (False, 111),
    ],
)
def test_audit_channel_id_picks_the_addressable_location(is_forum, expected):
    assert audit_channel_id(
        dest_channel_id=111, thread_id=555, is_forum=is_forum
    ) == expected


# ── mod-approve mode ──────────────────────────────────────────────────


def test_the_queued_ack_says_it_has_not_posted_yet():
    """A confession that silently fails to appear is what makes people send it
    again, so the ack must not read like a normal successful submission."""
    assert "review" in QUEUED_ACK.lower()


def test_a_rejection_dm_names_no_moderator():
    """Anonymity cuts both ways: pointing a member at the individual who turned
    their confession down is not the mod team answering as a team."""
    text = build_rejection_dm_text(guild_name="The Meadow", reason="Not here, sorry")
    assert "The Meadow" in text
    assert "Not here, sorry" in text
    assert "moderator" not in text.replace("the mods", "")


def test_a_rejection_dm_without_a_reason_says_nothing_extra():
    text = build_rejection_dm_text(guild_name="The Meadow")
    assert ">" not in text


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_a_blank_reason_is_treated_as_no_reason(reason):
    assert ">" not in build_rejection_dm_text(guild_name="G", reason=reason)


def test_a_long_reason_is_cut_to_the_modal_limit():
    text = build_rejection_dm_text(guild_name="G", reason="x" * 900)
    assert "x" * REJECTION_REASON_MAX in text
    assert "x" * (REJECTION_REASON_MAX + 1) not in text


def test_a_multiline_reason_is_flattened_into_the_quote():
    text = build_rejection_dm_text(guild_name="G", reason="one\ntwo")
    assert "> one two" in text


def test_an_expiry_dm_is_not_worded_as_a_rejection():
    """Nobody judged it — saying the mods didn't approve it would be a verdict
    they never reached."""
    text = build_expiry_dm_text(guild_name="The Meadow")
    assert "didn't approve" not in text
    assert "week" in text


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (-500, "just now"),   # a clock that stepped backwards, not a future time
        (0, "just now"),
        (59, "just now"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (86_399, "23h"),
        (86_400, "1d"),
        (8 * 86_400, "8d"),
    ],
)
def test_waiting_time_reads_coarsely(seconds, expected):
    assert format_waiting_for(seconds) == expected


def test_a_picker_option_shows_the_body_and_the_wait():
    label, desc = pending_option_text(
        {"content": "I ate the last biscuit", "created_at": 1000}, now=1000 + 7200
    )
    assert label == "I ate the last biscuit"
    assert "2h" in desc


def test_a_picker_option_fits_discords_limits():
    label, desc = pending_option_text(
        {"content": "x" * 500, "created_at": 0}, now=10**9
    )
    assert len(label) <= 100
    assert len(desc) <= 100


def test_a_picker_option_flattens_a_multiline_confession():
    label, _ = pending_option_text({"content": "a\nb\nc", "created_at": 0}, now=0)
    assert label == "a b c"


def test_an_empty_confession_still_gets_a_pickable_label():
    label, _ = pending_option_text({"content": "   ", "created_at": 0}, now=0)
    assert label.strip()


def test_a_picker_option_cannot_print_an_author():
    label, desc = pending_option_text(
        {"content": "hello", "created_at": 0, "author_id": 424242}, now=0
    )
    assert "424242" not in label + desc
