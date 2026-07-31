"""No-contact list — DB layer and the cross-feature enforcement points.

Covers the storage contract, then the three places outside the no-contact
module itself where a no-contact pair has to change another feature's
behaviour: Pen Pals matching, Voice Master room permissions, and the DM
consent path.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import no_contact_service as svc
from bot_modules.services.no_contact_logic import (
    KIND_ATTEMPT,
    SURFACE_WHISPER,
    can_remove,
)

GUILD = 42
ALICE = 100
BOB = 200
CAROL = 300
MOD = 999


@pytest.fixture
def db(sync_db_path):
    return sync_db_path


# ── Storage ──────────────────────────────────────────────────────────────


def test_add_and_detect_pair(db):
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE, protected_user_id=ALICE)
    assert svc.is_no_contact(db, GUILD, ALICE, BOB)


def test_detection_is_order_independent(db):
    svc.add_pair(db, GUILD, BOB, ALICE, created_by=ALICE, protected_user_id=ALICE)
    assert svc.is_no_contact(db, GUILD, ALICE, BOB)
    assert svc.is_no_contact(db, GUILD, BOB, ALICE)


def test_real_snowflake_ids_survive_a_round_trip(db):
    """Ids above 2^53 must not be mangled anywhere in the write path.

    The dashboard once sent them through JS ``Number()``, which rounds
    1420895763219492864 to …900 — the row named two members who don't exist,
    so the panel listed an entry that enforced nothing at all.
    """
    big_a = 1420895763219492864
    big_b = 1420895763219492865
    svc.add_pair(db, GUILD, big_a, big_b, created_by=big_a, protected_user_id=big_a)

    assert svc.is_no_contact(db, GUILD, big_a, big_b)
    entry = svc.get_pair(db, GUILD, big_a, big_b)
    assert entry is not None
    assert entry["user_low"] == big_a
    assert entry["user_high"] == big_b
    assert entry["protected_user_id"] == big_a
    assert svc.no_contact_partners(db, GUILD, big_a) == {big_b}


def test_pairs_do_not_leak_across_guilds(db):
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    assert not svc.is_no_contact(db, GUILD + 1, ALICE, BOB)


def test_unrelated_members_are_not_blocked(db):
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    assert not svc.is_no_contact(db, GUILD, ALICE, CAROL)


def test_self_pair_is_rejected(db):
    assert svc.add_pair(db, GUILD, ALICE, ALICE, created_by=ALICE) is False
    assert not svc.is_no_contact(db, GUILD, ALICE, ALICE)


def test_remove_pair(db):
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    assert svc.remove_pair(db, GUILD, BOB, ALICE) is True
    assert not svc.is_no_contact(db, GUILD, ALICE, BOB)


def test_remove_missing_pair_reports_false(db):
    assert svc.remove_pair(db, GUILD, ALICE, BOB) is False


def test_duplicate_add_cannot_hijack_removal_rights(db):
    """Bob re-adding the pair must not make himself the protected member.

    Otherwise the removal rule is trivially defeated: add the same pair,
    take ownership, then lift it. He also must not be able to launder the
    reason or the provenance by re-adding.
    """
    svc.add_pair(
        db, GUILD, ALICE, BOB, created_by=ALICE, protected_user_id=ALICE,
        reason="original",
    )
    svc.add_pair(
        db, GUILD, BOB, ALICE, created_by=BOB, protected_user_id=BOB,
        reason="overwritten",
    )
    entry = svc.get_pair(db, GUILD, ALICE, BOB)
    assert entry is not None
    # He does not take ownership; the entry escalates out of anyone's sole
    # control instead (see test_second_party_add_escalates_to_mutual).
    assert entry["protected_user_id"] is None
    assert entry["reason"] == "original"
    assert entry["created_by"] == ALICE


def test_second_party_add_escalates_to_mutual(db):
    """He adds first, she adds second — he must not keep the only key.

    Before this, her add was a silent no-op: the row still recorded HIM as
    protected, so he could lift her protection at will while she could
    neither remove it nor see that it existed. Both asked for the
    separation, so neither gets to undo it alone.
    """
    svc.add_pair(db, GUILD, BOB, ALICE, created_by=BOB, protected_user_id=BOB)
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE, protected_user_id=ALICE)

    entry = svc.get_pair(db, GUILD, ALICE, BOB)
    assert entry is not None
    assert entry["protected_user_id"] is None
    # Neither party can lift it now; only a moderator can.
    for actor in (ALICE, BOB):
        assert not can_remove(
            protected_user_id=entry["protected_user_id"],
            actor_id=actor,
            actor_is_mod=False,
        )


def test_repeat_add_by_the_same_member_keeps_them_protected(db):
    """Adding twice yourself must not escalate you out of your own entry."""
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE, protected_user_id=ALICE)
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE, protected_user_id=ALICE)
    entry = svc.get_pair(db, GUILD, ALICE, BOB)
    assert entry is not None and entry["protected_user_id"] == ALICE


def test_mutual_entry_never_narrows_to_one_party(db):
    """Escalation to mutual is one-way — a later add can't claim ownership."""
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=MOD, protected_user_id=None)
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=BOB, protected_user_id=BOB)
    entry = svc.get_pair(db, GUILD, ALICE, BOB)
    assert entry is not None and entry["protected_user_id"] is None


def test_no_contact_partners(db):
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    svc.add_pair(db, GUILD, CAROL, ALICE, created_by=ALICE)
    assert svc.no_contact_partners(db, GUILD, ALICE) == {BOB, CAROL}
    assert svc.no_contact_partners(db, GUILD, BOB) == {ALICE}
    assert svc.no_contact_partners(db, GUILD, 12345) == set()


def test_mutual_entry_stores_null_protected(db):
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=MOD, protected_user_id=None)
    entry = svc.get_pair(db, GUILD, ALICE, BOB)
    assert entry is not None and entry["protected_user_id"] is None


def test_list_pairs_for_user_only_returns_their_own(db):
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    svc.add_pair(db, GUILD, CAROL, BOB, created_by=CAROL)
    rows = svc.list_pairs_for_user(db, GUILD, ALICE)
    assert len(rows) == 1
    assert {rows[0]["user_low"], rows[0]["user_high"]} == {ALICE, BOB}


def test_list_pairs_returns_everything_for_the_guild(db):
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    svc.add_pair(db, GUILD, CAROL, BOB, created_by=CAROL)
    assert len(svc.list_pairs(db, GUILD)) == 2


# ── The gate helper ──────────────────────────────────────────────────────


def test_check_and_record_blocks_and_logs(db):
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    assert (
        svc.check_and_record(
            db, GUILD, actor_id=BOB, target_id=ALICE, surface=SURFACE_WHISPER
        )
        is True
    )
    events = svc.list_events(db, GUILD)
    assert len(events) == 1
    assert events[0]["actor_id"] == BOB
    assert events[0]["target_id"] == ALICE
    assert events[0]["kind"] == KIND_ATTEMPT
    assert events[0]["surface"] == SURFACE_WHISPER


def test_check_and_record_allows_and_logs_nothing(db):
    assert (
        svc.check_and_record(
            db, GUILD, actor_id=BOB, target_id=CAROL, surface=SURFACE_WHISPER
        )
        is False
    )
    assert svc.list_events(db, GUILD) == []


def test_check_and_record_is_symmetric(db):
    """A pair blocks both directions — it does not matter who added it."""
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE, protected_user_id=ALICE)
    assert svc.check_and_record(
        db, GUILD, actor_id=ALICE, target_id=BOB, surface=SURFACE_WHISPER
    )


# ── Alert settings ───────────────────────────────────────────────────────


def test_settings_default_to_unconfigured(db):
    assert svc.get_settings(db, GUILD) == {
        "alert_channel_id": 0,
        "alert_role_id": 0,
    }


def test_settings_round_trip(db):
    svc.set_settings(db, GUILD, alert_channel_id=77, alert_role_id=88)
    assert svc.get_settings(db, GUILD) == {
        "alert_channel_id": 77,
        "alert_role_id": 88,
    }
    svc.set_settings(db, GUILD, alert_channel_id=99, alert_role_id=0)
    assert svc.get_settings(db, GUILD) == {
        "alert_channel_id": 99,
        "alert_role_id": 0,
    }


def test_enforcement_does_not_depend_on_alert_config(db):
    """A guild that never configured alerts still gets full enforcement."""
    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    assert svc.get_settings(db, GUILD)["alert_channel_id"] == 0
    assert svc.is_no_contact(db, GUILD, ALICE, BOB)


# ── Cross-feature enforcement: Pen Pals ──────────────────────────────────


def test_pen_pals_never_matches_a_no_contact_pair(db):
    """Pen Pals doesn't relay a message — it puts two people alone in a
    private channel for a day, on the bot's own initiative."""
    from bot_modules.cogs.pen_pals_cog import _is_blocked_pair

    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    with open_db(db) as conn:
        assert _is_blocked_pair(conn, GUILD, ALICE, BOB)
        assert _is_blocked_pair(conn, GUILD, BOB, ALICE)
        assert not _is_blocked_pair(conn, GUILD, ALICE, CAROL)


def test_pen_pals_own_blocklist_still_works(db):
    from bot_modules.cogs.pen_pals_cog import _is_blocked_pair

    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO pen_pals_blocks (guild_id, user_id, blocked_user_id, source, created_at) "
            "VALUES (?, ?, ?, 'member', 0)",
            (GUILD, ALICE, CAROL),
        )
    with open_db(db) as conn:
        assert _is_blocked_pair(conn, GUILD, ALICE, CAROL)


# ── Cross-feature enforcement: Voice Master ──────────────────────────────


def test_voice_effective_blocklist_includes_no_contact(db):
    from bot_modules.services.voice_master_service import effective_blocked

    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    with open_db(db) as conn:
        assert BOB in effective_blocked(conn, GUILD, ALICE)
        # Symmetric without a second pass: his room denies her too.
        assert ALICE in effective_blocked(conn, GUILD, BOB)


def test_voice_visible_blocklist_hides_no_contact(db):
    """The leak this split exists to prevent.

    If a no-contact partner showed up in his *visible* blocklist, he would
    see a block he never set, with her name on it — which tells him exactly
    what the rest of the feature works to conceal.
    """
    from bot_modules.services.voice_master_service import list_blocked

    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    with open_db(db) as conn:
        assert list_blocked(conn, GUILD, BOB) == []
        assert list_blocked(conn, GUILD, ALICE) == []


def test_voice_effective_blocklist_keeps_manual_entries(db):
    from bot_modules.services.voice_master_service import add_blocked, effective_blocked

    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    with open_db(db) as conn:
        add_blocked(conn, GUILD, ALICE, CAROL)
    with open_db(db) as conn:
        effective = effective_blocked(conn, GUILD, ALICE)
        assert set(effective) == {BOB, CAROL}


def test_voice_effective_blocklist_does_not_duplicate(db):
    from bot_modules.services.voice_master_service import add_blocked, effective_blocked

    svc.add_pair(db, GUILD, ALICE, BOB, created_by=ALICE)
    with open_db(db) as conn:
        add_blocked(conn, GUILD, ALICE, BOB)
    with open_db(db) as conn:
        effective = effective_blocked(conn, GUILD, ALICE)
        assert effective.count(BOB) == 1


# ── Cross-feature enforcement: DM consent ────────────────────────────────


def test_dm_request_is_refused_as_though_dms_were_closed(db):
    """The refusal reuses an existing message about HER general setting.

    A refusal that named him — or a new string only he ever sees — would
    single him out. "Isn't accepting DM requests right now" reads the same
    whether or not he is the reason.
    """
    from bot_modules.dm_perms.logic import classify_dm_request

    msg = classify_dm_request(
        target_in_guild=True,
        is_self=False,
        target_is_bot=False,
        target_mode="closed",
        is_mutual=False,
        has_pending=False,
        target_display_name="Alice",
    )
    assert msg is not None
    assert "isn't accepting DM requests" in msg
    assert "no-contact" not in msg.lower()
    assert "block" not in msg.lower()
