"""Tests for services/voice_master_service.py."""

from __future__ import annotations

import pytest

from db_utils import open_db
from migrations import apply_migrations_sync
from services.voice_master_service import (
    DEFAULT_NAME_TEMPLATE,
    EDIT_WINDOW_S,
    ReconciliationPlan,
    VoiceMasterConfig,
    VoiceProfile,
    active_channel_count,
    add_blocked,
    add_name_blocklist,
    add_trusted,
    can_edit,
    compute_reconciliation_actions,
    default_profile,
    delete_active_channel,
    delete_profile,
    get_active_channel,
    get_owned_channel,
    insert_active_channel,
    list_active_channels,
    list_blocked,
    list_name_blocklist,
    list_trusted,
    load_profile,
    load_voice_master_config,
    name_is_blocked,
    record_edit,
    record_edit_in_db,
    remove_blocked,
    remove_member_from_all_lists,
    remove_name_blocklist,
    remove_trusted,
    render_name_template,
    resolve_channel_name,
    save_profile,
    set_owner,
    set_owner_left_at,
    set_voice_master_config_value,
    update_profile_field,
)

GUILD = 123
OWNER_A = 1001
OWNER_B = 1002
TARGET_X = 2001
TARGET_Y = 2002
TARGET_Z = 2003
ADMIN = 9001
CH1 = 5001
CH2 = 5002
CH3 = 5003


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


# ── Active channel CRUD ──────────────────────────────────────────────


def test_insert_and_get_active_channel(db):
    with open_db(db) as conn:
        insert_active_channel(
            conn, channel_id=CH1, guild_id=GUILD, owner_id=OWNER_A, now=100.0
        )
        ch = get_active_channel(conn, CH1)
    assert ch is not None
    assert ch.channel_id == CH1
    assert ch.guild_id == GUILD
    assert ch.owner_id == OWNER_A
    assert ch.created_at == 100.0
    assert ch.last_edit_at_1 == 0
    assert ch.last_edit_at_2 == 0
    assert ch.owner_left_at is None


def test_get_active_channel_missing(db):
    with open_db(db) as conn:
        assert get_active_channel(conn, 9999) is None


def test_get_owned_channel(db):
    with open_db(db) as conn:
        insert_active_channel(
            conn, channel_id=CH1, guild_id=GUILD, owner_id=OWNER_A, now=100.0
        )
        insert_active_channel(
            conn, channel_id=CH2, guild_id=GUILD, owner_id=OWNER_B, now=200.0
        )
        owned_a = get_owned_channel(conn, GUILD, OWNER_A)
        owned_b = get_owned_channel(conn, GUILD, OWNER_B)
        owned_none = get_owned_channel(conn, GUILD, 99999)
    assert owned_a is not None and owned_a.channel_id == CH1
    assert owned_b is not None and owned_b.channel_id == CH2
    assert owned_none is None


def test_list_active_channels_scoped_to_guild(db):
    with open_db(db) as conn:
        insert_active_channel(conn, channel_id=CH1, guild_id=GUILD, owner_id=OWNER_A)
        insert_active_channel(conn, channel_id=CH2, guild_id=GUILD, owner_id=OWNER_B)
        insert_active_channel(conn, channel_id=CH3, guild_id=999, owner_id=OWNER_A)
        rows = list_active_channels(conn, GUILD)
    assert {r.channel_id for r in rows} == {CH1, CH2}


def test_active_channel_count(db):
    with open_db(db) as conn:
        assert active_channel_count(conn, GUILD, OWNER_A) == 0
        insert_active_channel(conn, channel_id=CH1, guild_id=GUILD, owner_id=OWNER_A)
        assert active_channel_count(conn, GUILD, OWNER_A) == 1
        insert_active_channel(conn, channel_id=CH2, guild_id=GUILD, owner_id=OWNER_A)
        assert active_channel_count(conn, GUILD, OWNER_A) == 2
        # Different owner doesn't affect count.
        insert_active_channel(conn, channel_id=CH3, guild_id=GUILD, owner_id=OWNER_B)
        assert active_channel_count(conn, GUILD, OWNER_A) == 2


def test_delete_active_channel(db):
    with open_db(db) as conn:
        insert_active_channel(conn, channel_id=CH1, guild_id=GUILD, owner_id=OWNER_A)
        delete_active_channel(conn, CH1)
        assert get_active_channel(conn, CH1) is None


def test_set_owner_left_at(db):
    with open_db(db) as conn:
        insert_active_channel(conn, channel_id=CH1, guild_id=GUILD, owner_id=OWNER_A)
        set_owner_left_at(conn, CH1, 500.0)
        ch = get_active_channel(conn, CH1)
        assert ch is not None and ch.owner_left_at == 500.0
        set_owner_left_at(conn, CH1, None)
        ch = get_active_channel(conn, CH1)
        assert ch is not None and ch.owner_left_at is None


def test_set_owner_clears_left_at(db):
    with open_db(db) as conn:
        insert_active_channel(conn, channel_id=CH1, guild_id=GUILD, owner_id=OWNER_A)
        set_owner_left_at(conn, CH1, 500.0)
        set_owner(conn, CH1, OWNER_B)
        ch = get_active_channel(conn, CH1)
        assert ch is not None
        assert ch.owner_id == OWNER_B
        assert ch.owner_left_at is None


def test_record_edit_in_db_pushes_into_pair(db):
    with open_db(db) as conn:
        insert_active_channel(
            conn, channel_id=CH1, guild_id=GUILD, owner_id=OWNER_A, now=0.0
        )
        record_edit_in_db(conn, CH1, now=100.0)
        ch1 = get_active_channel(conn, CH1)
        assert ch1 is not None
        assert sorted([ch1.last_edit_at_1, ch1.last_edit_at_2]) == [0.0, 100.0]
        record_edit_in_db(conn, CH1, now=200.0)
        ch2 = get_active_channel(conn, CH1)
        assert ch2 is not None
        assert sorted([ch2.last_edit_at_1, ch2.last_edit_at_2]) == [100.0, 200.0]
        record_edit_in_db(conn, CH1, now=300.0)
        ch3 = get_active_channel(conn, CH1)
        assert ch3 is not None
        assert sorted([ch3.last_edit_at_1, ch3.last_edit_at_2]) == [200.0, 300.0]


def test_record_edit_in_db_missing_channel_is_noop(db):
    with open_db(db) as conn:
        record_edit_in_db(conn, 99999, now=100.0)  # no exception


# ── Profile CRUD ─────────────────────────────────────────────────────


def test_default_profile_is_empty():
    p = default_profile()
    assert p.saved_name is None
    assert p.saved_limit == 0
    assert p.locked is False
    assert p.hidden is False
    assert p.bitrate is None


def test_load_profile_missing_returns_none(db):
    with open_db(db) as conn:
        assert load_profile(conn, GUILD, OWNER_A) is None


def test_save_and_load_profile_roundtrip(db):
    p = VoiceProfile(
        saved_name="My Room",
        saved_limit=10,
        locked=True,
        hidden=False,
        bitrate=64000,
    )
    with open_db(db) as conn:
        save_profile(conn, GUILD, OWNER_A, p, now=42.0)
        loaded = load_profile(conn, GUILD, OWNER_A)
    assert loaded == p


def test_save_profile_overwrites(db):
    with open_db(db) as conn:
        save_profile(
            conn, GUILD, OWNER_A,
            VoiceProfile(saved_name="A", saved_limit=5, locked=False, hidden=False, bitrate=None),
        )
        save_profile(
            conn, GUILD, OWNER_A,
            VoiceProfile(saved_name="B", saved_limit=10, locked=True, hidden=True, bitrate=96000),
        )
        loaded = load_profile(conn, GUILD, OWNER_A)
    assert loaded is not None
    assert loaded.saved_name == "B"
    assert loaded.saved_limit == 10
    assert loaded.locked is True
    assert loaded.hidden is True
    assert loaded.bitrate == 96000


def test_update_profile_field_creates_default_if_missing(db):
    with open_db(db) as conn:
        update_profile_field(
            conn, GUILD, OWNER_A, field="saved_name", value="New Name"
        )
        p = load_profile(conn, GUILD, OWNER_A)
    assert p is not None
    assert p.saved_name == "New Name"
    assert p.saved_limit == 0
    assert p.locked is False


def test_update_profile_field_patches_existing(db):
    with open_db(db) as conn:
        save_profile(
            conn, GUILD, OWNER_A,
            VoiceProfile(saved_name="Original", saved_limit=5, locked=False, hidden=False, bitrate=None),
        )
        update_profile_field(
            conn, GUILD, OWNER_A, field="locked", value=True
        )
        p = load_profile(conn, GUILD, OWNER_A)
    assert p is not None
    assert p.saved_name == "Original"
    assert p.saved_limit == 5
    assert p.locked is True


def test_update_profile_field_unknown_raises(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError):
            update_profile_field(
                conn, GUILD, OWNER_A, field="nonexistent", value="x"
            )


def test_delete_profile(db):
    with open_db(db) as conn:
        save_profile(
            conn, GUILD, OWNER_A,
            VoiceProfile(saved_name="X", saved_limit=0, locked=False, hidden=False, bitrate=None),
        )
        delete_profile(conn, GUILD, OWNER_A)
        assert load_profile(conn, GUILD, OWNER_A) is None


# ── Trust list ───────────────────────────────────────────────────────


def test_trusted_add_list_remove(db):
    with open_db(db) as conn:
        added, evicted = add_trusted(conn, GUILD, OWNER_A, TARGET_X, now=1.0)
        assert added is True and evicted is None
        added2, _ = add_trusted(conn, GUILD, OWNER_A, TARGET_Y, now=2.0)
        assert added2 is True
        assert list_trusted(conn, GUILD, OWNER_A) == [TARGET_X, TARGET_Y]
        # Idempotent re-add.
        added_again, _ = add_trusted(conn, GUILD, OWNER_A, TARGET_X, now=3.0)
        assert added_again is False
        # Remove.
        assert remove_trusted(conn, GUILD, OWNER_A, TARGET_X) is True
        assert remove_trusted(conn, GUILD, OWNER_A, TARGET_X) is False
        assert list_trusted(conn, GUILD, OWNER_A) == [TARGET_Y]


def test_trusted_fifo_eviction_at_cap(db):
    with open_db(db) as conn:
        # Cap = 2. Insert 3, oldest gets evicted on the third.
        add_trusted(conn, GUILD, OWNER_A, TARGET_X, cap=2, now=1.0)
        add_trusted(conn, GUILD, OWNER_A, TARGET_Y, cap=2, now=2.0)
        added, evicted = add_trusted(conn, GUILD, OWNER_A, TARGET_Z, cap=2, now=3.0)
        assert added is True
        assert evicted == TARGET_X
        assert list_trusted(conn, GUILD, OWNER_A) == [TARGET_Y, TARGET_Z]


def test_trusted_cap_zero_means_unlimited(db):
    with open_db(db) as conn:
        for i, t in enumerate([TARGET_X, TARGET_Y, TARGET_Z], start=1):
            add_trusted(conn, GUILD, OWNER_A, t, cap=0, now=float(i))
        assert list_trusted(conn, GUILD, OWNER_A) == [TARGET_X, TARGET_Y, TARGET_Z]


def test_trusted_per_owner_scoped(db):
    with open_db(db) as conn:
        add_trusted(conn, GUILD, OWNER_A, TARGET_X)
        add_trusted(conn, GUILD, OWNER_B, TARGET_Y)
        assert list_trusted(conn, GUILD, OWNER_A) == [TARGET_X]
        assert list_trusted(conn, GUILD, OWNER_B) == [TARGET_Y]


# ── Block list (mirror of trusted) ───────────────────────────────────


def test_blocked_add_list_remove(db):
    with open_db(db) as conn:
        add_blocked(conn, GUILD, OWNER_A, TARGET_X, now=1.0)
        add_blocked(conn, GUILD, OWNER_A, TARGET_Y, now=2.0)
        assert list_blocked(conn, GUILD, OWNER_A) == [TARGET_X, TARGET_Y]
        assert remove_blocked(conn, GUILD, OWNER_A, TARGET_X) is True
        assert list_blocked(conn, GUILD, OWNER_A) == [TARGET_Y]


def test_blocked_fifo_eviction(db):
    with open_db(db) as conn:
        add_blocked(conn, GUILD, OWNER_A, TARGET_X, cap=2, now=1.0)
        add_blocked(conn, GUILD, OWNER_A, TARGET_Y, cap=2, now=2.0)
        added, evicted = add_blocked(conn, GUILD, OWNER_A, TARGET_Z, cap=2, now=3.0)
        assert added is True
        assert evicted == TARGET_X


# ── Cross-list cleanup ───────────────────────────────────────────────


def test_remove_member_from_all_lists(db):
    with open_db(db) as conn:
        add_trusted(conn, GUILD, OWNER_A, TARGET_X)
        add_trusted(conn, GUILD, OWNER_B, TARGET_X)
        add_blocked(conn, GUILD, OWNER_A, TARGET_X)
        # Different guild — should not be affected.
        add_trusted(conn, 999, OWNER_A, TARGET_X)
        n = remove_member_from_all_lists(conn, GUILD, TARGET_X)
        assert n == 3
        assert list_trusted(conn, GUILD, OWNER_A) == []
        assert list_trusted(conn, GUILD, OWNER_B) == []
        assert list_blocked(conn, GUILD, OWNER_A) == []
        assert list_trusted(conn, 999, OWNER_A) == [TARGET_X]


# ── Name blocklist ───────────────────────────────────────────────────


def test_name_blocklist_add_list_remove(db):
    with open_db(db) as conn:
        assert add_name_blocklist(conn, GUILD, "Slur", ADMIN) is True
        # Stored lowercased.
        assert list_name_blocklist(conn, GUILD) == ["slur"]
        # Idempotent.
        assert add_name_blocklist(conn, GUILD, "slur", ADMIN) is False
        assert add_name_blocklist(conn, GUILD, "another", ADMIN) is True
        assert list_name_blocklist(conn, GUILD) == ["another", "slur"]
        assert remove_name_blocklist(conn, GUILD, "SLUR") is True
        assert list_name_blocklist(conn, GUILD) == ["another"]


def test_name_is_blocked_case_insensitive():
    patterns = ["bad", "ugly"]
    assert name_is_blocked("This is BAD", patterns)
    assert name_is_blocked("ugly room", patterns)
    assert not name_is_blocked("Nice Room", patterns)


def test_name_is_blocked_substring_match():
    assert name_is_blocked("badword", ["bad"])
    assert name_is_blocked("prefixUGLY", ["ugly"])
    assert not name_is_blocked("safe", ["bad", "ugly"])


def test_name_is_blocked_empty_patterns_skipped():
    # Empty pattern would otherwise match everything.
    assert not name_is_blocked("anything", [""])
    assert not name_is_blocked("anything", [])


# ── Edit budget ──────────────────────────────────────────────────────


def test_can_edit_initial_state_allowed():
    allowed, retry = can_edit(now=1000.0, last1=0.0, last2=0.0)
    assert allowed is True
    assert retry == 0.0


def test_can_edit_one_recent_edit_still_allowed():
    # last2 just happened; last1 is empty (=0). Second slot is free.
    allowed, retry = can_edit(now=1000.0, last1=0.0, last2=999.0)
    assert allowed is True
    assert retry == 0.0


def test_can_edit_two_recent_edits_blocked():
    # Both edits within the last 600s window.
    allowed, retry = can_edit(now=1000.0, last1=900.0, last2=950.0)
    assert allowed is False
    # Older edit was at t=900; window opens at 900 + 600 = 1500. retry = 500.
    assert retry == pytest.approx(500.0)


def test_can_edit_oldest_just_aged_out_allowed():
    # Older edit at t=399.999 — exactly at the window boundary (now-600=400).
    allowed, retry = can_edit(now=1000.0, last1=400.0, last2=950.0)
    assert allowed is True
    assert retry == 0.0


def test_can_edit_window_boundary_inclusive():
    # now - older == window exactly → allowed.
    allowed, _ = can_edit(now=1000.0, last1=400.0, last2=999.0)
    assert allowed is True


def test_can_edit_handles_swapped_slot_order():
    # Slots aren't ordered — try with last1 newer than last2.
    allowed, retry = can_edit(now=1000.0, last1=950.0, last2=900.0)
    assert allowed is False
    assert retry == pytest.approx(500.0)


def test_record_edit_initial():
    # Both slots empty → newer becomes (0, now).
    new1, new2 = record_edit(now=100.0, last1=0.0, last2=0.0)
    assert sorted([new1, new2]) == [0.0, 100.0]


def test_record_edit_evicts_oldest():
    new1, new2 = record_edit(now=300.0, last1=100.0, last2=200.0)
    assert sorted([new1, new2]) == [200.0, 300.0]


def test_record_edit_independent_of_slot_order():
    a = record_edit(now=300.0, last1=200.0, last2=100.0)
    b = record_edit(now=300.0, last1=100.0, last2=200.0)
    assert sorted(a) == sorted(b)


def test_edit_window_constant():
    assert EDIT_WINDOW_S == 600.0


# ── Name template ────────────────────────────────────────────────────


def test_render_default_template():
    out = render_name_template(
        "{display_name}'s Room", display_name="Ben", username="ben_smith"
    )
    assert out == "Ben's Room"


def test_render_username_token():
    out = render_name_template(
        "@{username}'s lounge", display_name="Ben", username="ben_smith"
    )
    assert out == "@ben_smith's lounge"


def test_render_both_tokens():
    out = render_name_template(
        "{display_name} ({username})", display_name="Ben", username="ben_smith"
    )
    assert out == "Ben (ben_smith)"


def test_render_no_tokens_passes_through():
    out = render_name_template(
        "Static Name", display_name="Ben", username="ben_smith"
    )
    assert out == "Static Name"


def test_render_empty_template_falls_back():
    out = render_name_template("", display_name="Ben", username="ben_smith")
    assert out == "Ben's Room"


def test_render_truncates_to_100_chars():
    long = "x" * 200
    out = render_name_template(long, display_name="Ben", username="ben_smith")
    assert len(out) == 100


def test_render_truncates_after_substitution():
    template = "{display_name}" + ("y" * 200)
    out = render_name_template(template, display_name="Ben", username="ben_smith")
    assert len(out) == 100
    assert out.startswith("Ben")


# ── resolve_channel_name (saved name + blocklist fallback) ────────────


def test_resolve_uses_saved_when_unblocked():
    name, fell_back = resolve_channel_name(
        saved_name="My Quiet Room",
        template="{display_name}'s Room",
        display_name="Ben",
        username="ben_smith",
        blocklist_patterns=["bad"],
    )
    assert name == "My Quiet Room"
    assert fell_back is False


def test_resolve_falls_back_when_saved_blocked():
    name, fell_back = resolve_channel_name(
        saved_name="badword room",
        template="{display_name}'s Room",
        display_name="Ben",
        username="ben_smith",
        blocklist_patterns=["bad"],
    )
    assert name == "Ben's Room"
    assert fell_back is True


def test_resolve_uses_template_when_no_saved():
    name, fell_back = resolve_channel_name(
        saved_name=None,
        template="@{username}'s lounge",
        display_name="Ben",
        username="ben_smith",
        blocklist_patterns=[],
    )
    assert name == "@ben_smith's lounge"
    assert fell_back is False


def test_resolve_truncates_saved_name_to_100():
    long = "x" * 200
    name, fell_back = resolve_channel_name(
        saved_name=long,
        template="{display_name}'s Room",
        display_name="Ben",
        username="ben_smith",
        blocklist_patterns=[],
    )
    assert len(name) == 100
    assert fell_back is False


# ── Reconciliation planner ────────────────────────────────────────────


def test_reconciliation_empty_state():
    plan = compute_reconciliation_actions(
        tracked_channel_ids=[],
        present_channel_ids=set(),
        channels_with_humans=set(),
        category_voice_channel_ids=set(),
        hub_channel_id=0,
    )
    assert plan == ReconciliationPlan(
        db_to_delete=[], discord_to_delete=[], orphan_warnings=[]
    )


def test_reconciliation_deleted_channel_purged_from_db():
    plan = compute_reconciliation_actions(
        tracked_channel_ids=[CH1],
        present_channel_ids=set(),
        channels_with_humans=set(),
        category_voice_channel_ids=set(),
        hub_channel_id=0,
    )
    assert plan.db_to_delete == [CH1]
    assert plan.discord_to_delete == []
    assert plan.orphan_warnings == []


def test_reconciliation_empty_present_channel_deleted():
    plan = compute_reconciliation_actions(
        tracked_channel_ids=[CH1],
        present_channel_ids={CH1},
        channels_with_humans=set(),
        category_voice_channel_ids={CH1},
        hub_channel_id=0,
    )
    assert plan.db_to_delete == [CH1]
    assert plan.discord_to_delete == [CH1]
    assert plan.orphan_warnings == []


def test_reconciliation_populated_channel_left_alone():
    plan = compute_reconciliation_actions(
        tracked_channel_ids=[CH1],
        present_channel_ids={CH1},
        channels_with_humans={CH1},
        category_voice_channel_ids={CH1},
        hub_channel_id=0,
    )
    assert plan.db_to_delete == []
    assert plan.discord_to_delete == []


def test_reconciliation_orphan_warning_for_untracked_in_category():
    plan = compute_reconciliation_actions(
        tracked_channel_ids=[CH1],
        present_channel_ids={CH1, CH2},
        channels_with_humans={CH1},
        category_voice_channel_ids={CH1, CH2},
        hub_channel_id=0,
    )
    assert plan.orphan_warnings == [CH2]


def test_reconciliation_hub_never_orphan():
    HUB = 7777
    plan = compute_reconciliation_actions(
        tracked_channel_ids=[],
        present_channel_ids={HUB},
        channels_with_humans=set(),
        category_voice_channel_ids={HUB},
        hub_channel_id=HUB,
    )
    assert plan.orphan_warnings == []


def test_reconciliation_mixed_state():
    plan = compute_reconciliation_actions(
        tracked_channel_ids=[CH1, CH2, CH3],
        present_channel_ids={CH2, CH3},        # CH1 was deleted while bot down
        channels_with_humans={CH3},            # CH2 is empty, CH3 has people
        category_voice_channel_ids={CH2, CH3, 8888},  # 8888 is an orphan
        hub_channel_id=0,
    )
    assert plan.db_to_delete == sorted([CH1, CH2])
    assert plan.discord_to_delete == [CH2]
    assert plan.orphan_warnings == [8888]


# ── Per-guild config ─────────────────────────────────────────────────


def test_load_config_returns_defaults_when_unset(db):
    with open_db(db) as conn:
        cfg = load_voice_master_config(conn, GUILD)
    assert isinstance(cfg, VoiceMasterConfig)
    assert cfg.hub_channel_id == 0
    assert cfg.category_id == 0
    assert cfg.control_channel_id == 0
    assert cfg.panel_message_id == 0
    assert cfg.default_name_template == DEFAULT_NAME_TEMPLATE
    assert cfg.default_user_limit == 0
    assert cfg.default_bitrate == 0
    assert cfg.create_cooldown_s == 30
    assert cfg.max_per_member == 1
    assert cfg.trust_cap == 25
    assert cfg.block_cap == 25
    assert cfg.owner_grace_s == 300
    assert cfg.empty_grace_s == 15
    assert cfg.trusted_prune_days == 0
    assert cfg.disable_saves is False
    assert cfg.saveable_fields == frozenset(
        {"name", "limit", "locked", "hidden", "trusted", "blocked"}
    )
    assert cfg.post_inline_panel is True


def test_set_and_load_config_value_roundtrip(db):
    with open_db(db) as conn:
        set_voice_master_config_value(
            conn, GUILD, "voice_master_hub_channel_id", "12345"
        )
        set_voice_master_config_value(
            conn, GUILD, "voice_master_create_cooldown_s", "60"
        )
        set_voice_master_config_value(
            conn, GUILD, "voice_master_disable_saves", "1"
        )
        set_voice_master_config_value(
            conn, GUILD, "voice_master_saveable_fields", "name,limit"
        )
        cfg = load_voice_master_config(conn, GUILD)
    assert cfg.hub_channel_id == 12345
    assert cfg.create_cooldown_s == 60
    assert cfg.disable_saves is True
    assert cfg.saveable_fields == frozenset({"name", "limit"})


def test_set_config_value_rejects_unknown_key(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError):
            set_voice_master_config_value(conn, GUILD, "voice_master_bogus", "x")


def test_load_config_per_guild_scoped(db):
    with open_db(db) as conn:
        set_voice_master_config_value(
            conn, GUILD, "voice_master_hub_channel_id", "111"
        )
        set_voice_master_config_value(
            conn, 999, "voice_master_hub_channel_id", "222"
        )
        cfg_a = load_voice_master_config(conn, GUILD)
        cfg_b = load_voice_master_config(conn, 999)
    assert cfg_a.hub_channel_id == 111
    assert cfg_b.hub_channel_id == 222


def test_load_config_handles_garbage_int_values(db):
    """Garbage in DB → falls back to default rather than crashing."""
    from db_utils import set_config_value

    with open_db(db) as conn:
        set_config_value(conn, "voice_master_create_cooldown_s", "not-a-number", GUILD)
        cfg = load_voice_master_config(conn, GUILD)
    assert cfg.create_cooldown_s == 30  # fell back to default
