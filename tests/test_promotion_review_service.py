"""Tests for services/promotion_review_service — promotion-review cards.

The tested unit is the gating logic + ledger + in-memory watch registry; the
Discord embed/buttons in promotion_review_views are glue exercised elsewhere.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.inactive.store import create_inactive
from bot_modules.services import promotion_review_service as svc
from bot_modules.services.role_grant_audit_service import record_prune_events
from migrations import apply_migrations_sync

GUILD = 42
ROLE = 900  # a role a sweep pruned
GRANT_ROLE = 901  # the role the Grant button re-adds
CHANNEL = 555  # the Level 5 Log / promotion-reviews channel
SLEEPER_CHANNEL = 777  # inactive_channel_id


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "promo.db"
    apply_migrations_sync(path)
    return path


@pytest.fixture(autouse=True)
def _clean_watch():
    svc._reset_watch_for_tests()
    yield
    svc._reset_watch_for_tests()


def _set_channel(conn):
    set_config_value(conn, svc.CHANNEL_KEY, str(CHANNEL), GUILD)


def _set_grant_role(conn):
    set_config_value(conn, svc.GRANT_ROLE_KEY, str(GRANT_ROLE), GUILD)


def _set_sleeper_channel(conn):
    set_config_value(conn, svc.SLEEPER_CHANNEL_KEY, str(SLEEPER_CHANNEL), GUILD)


def _make_sleeper(conn, user_id):
    create_inactive(
        conn, guild_id=GUILD, user_id=user_id, moderator_id=1,
        reason="", stored_roles=[123], source="auto",
    )


# ── config / dark gate ────────────────────────────────────────────────


def test_ships_dark_until_channel_configured(db_path):
    with open_db(db_path) as conn:
        assert svc.is_enabled(conn, GUILD) is False
        _set_channel(conn)
        assert svc.is_enabled(conn, GUILD) is True
        assert svc.review_channel_id(conn, GUILD) == CHANNEL


def test_config_getters_tolerate_garbage(db_path):
    with open_db(db_path) as conn:
        set_config_value(conn, svc.GRANT_ROLE_KEY, "nope", GUILD)
        assert svc.grant_role_id(conn, GUILD) == 0


# ── card ledger + dedup ───────────────────────────────────────────────


def test_reserve_is_single_open_card_per_member(db_path):
    with open_db(db_path) as conn:
        first = svc.reserve_card(conn, GUILD, 7, svc.KIND_PRUNED_RETURN, 100.0)
        assert first is not None
        # Second reserve for the same open member loses the race → None.
        assert svc.reserve_card(conn, GUILD, 7, svc.KIND_SLEEPER, 101.0) is None
        card = svc.get_open_card(conn, GUILD, 7)
        assert card["id"] == first
        assert card["kind"] == svc.KIND_PRUNED_RETURN


def test_resolve_frees_the_slot(db_path):
    with open_db(db_path) as conn:
        cid = svc.reserve_card(conn, GUILD, 7, svc.KIND_PRUNED_RETURN, 100.0)
        assert svc.resolve_card(conn, cid, 99, 200.0, svc.RESOLUTION_GRANTED) == 1
        assert svc.get_open_card(conn, GUILD, 7) is None
        # Resolving again is a no-op.
        assert svc.resolve_card(conn, cid, 99, 300.0, svc.RESOLUTION_DISMISSED) == 0
        # A brand-new card can now be reserved.
        assert svc.reserve_card(conn, GUILD, 7, svc.KIND_PRUNED_RETURN, 400.0) is not None


def test_set_and_delete_card_message(db_path):
    with open_db(db_path) as conn:
        cid = svc.reserve_card(conn, GUILD, 7, svc.KIND_PRUNED_RETURN, 100.0)
        svc.set_card_message(conn, cid, CHANNEL, 12345)
        assert svc.get_card(conn, cid)["message_id"] == 12345
        svc.delete_card(conn, cid)
        assert svc.get_card(conn, cid) is None
        assert svc.get_open_card(conn, GUILD, 7) is None


# ── trigger evaluation ────────────────────────────────────────────────


def test_evaluate_none_when_dark(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD, [7], ROLE, 100.0)
        _set_grant_role(conn)
        # Channel not set → whole feature dark.
        assert svc.evaluate_trigger(conn, GUILD, 7, 999) is None


def test_evaluate_pruned_return_needs_grant_role(db_path):
    with open_db(db_path) as conn:
        _set_channel(conn)
        record_prune_events(conn, GUILD, [7], ROLE, 100.0)
        # No grant role yet → the pruned-return button would be useless, so no card.
        assert svc.evaluate_trigger(conn, GUILD, 7, 999) is None
        _set_grant_role(conn)
        assert svc.evaluate_trigger(conn, GUILD, 7, 999) == svc.KIND_PRUNED_RETURN


def test_evaluate_skips_when_already_carded(db_path):
    with open_db(db_path) as conn:
        _set_channel(conn)
        _set_grant_role(conn)
        record_prune_events(conn, GUILD, [7], ROLE, 100.0)
        svc.reserve_card(conn, GUILD, 7, svc.KIND_PRUNED_RETURN, 150.0)
        assert svc.evaluate_trigger(conn, GUILD, 7, 999) is None


def test_evaluate_sleeper_only_in_sleeper_channel(db_path):
    with open_db(db_path) as conn:
        _set_channel(conn)
        _set_sleeper_channel(conn)
        _make_sleeper(conn, 7)
        # Posting elsewhere → no card (sleeper only wakes in the sleeper channel).
        assert svc.evaluate_trigger(conn, GUILD, 7, 999) is None
        # Posting in the sleeper channel → sleeper card.
        assert svc.evaluate_trigger(conn, GUILD, 7, SLEEPER_CHANNEL) == svc.KIND_SLEEPER


def test_evaluate_sleeper_ignores_non_held_member(db_path):
    with open_db(db_path) as conn:
        _set_channel(conn)
        _set_sleeper_channel(conn)
        # Never held inactive → posting in the sleeper channel does nothing.
        assert svc.evaluate_trigger(conn, GUILD, 7, SLEEPER_CHANNEL) is None


def test_evaluate_prefers_pruned_return_over_sleeper(db_path):
    with open_db(db_path) as conn:
        _set_channel(conn)
        _set_grant_role(conn)
        _set_sleeper_channel(conn)
        record_prune_events(conn, GUILD, [7], ROLE, 100.0)
        _make_sleeper(conn, 7)
        # Both apply and they posted in the sleeper channel; pruned-return wins.
        assert svc.evaluate_trigger(conn, GUILD, 7, SLEEPER_CHANNEL) == svc.KIND_PRUNED_RETURN


# ── candidate populations ─────────────────────────────────────────────


def test_watch_candidates_union_minus_carded(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD, [1, 2], ROLE, 100.0)
        _make_sleeper(conn, 3)
        svc.reserve_card(conn, GUILD, 2, svc.KIND_PRUNED_RETURN, 150.0)  # already carded
        assert svc.watch_candidates(conn, GUILD) == {1, 3}


def test_still_candidate(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD, [1], ROLE, 100.0)
        _make_sleeper(conn, 2)
        assert svc.still_candidate(conn, GUILD, 1) is True
        assert svc.still_candidate(conn, GUILD, 2) is True
        assert svc.still_candidate(conn, GUILD, 3) is False


# ── card-content helpers ──────────────────────────────────────────────


def test_pruned_roles_for_lists_open_events_recent_first(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD, [7], ROLE, 100.0)
        record_prune_events(conn, GUILD, [7], 902, 300.0)
        assert svc.pruned_roles_for(conn, GUILD, 7) == [(902, 300.0), (ROLE, 100.0)]


def test_mark_prunes_restored_closes_all_for_member(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD, [7], ROLE, 100.0)
        record_prune_events(conn, GUILD, [7], 902, 300.0)
        assert svc.mark_prunes_restored(conn, GUILD, 7, 500.0) == 2
        assert svc.open_prune_user_ids(conn, GUILD) == set()


def test_member_level_defaults_to_zero(db_path):
    with open_db(db_path) as conn:
        assert svc.member_level(conn, GUILD, 7) == 0
        conn.execute(
            "INSERT INTO member_xp (guild_id, user_id, total_xp, level) VALUES (?, ?, ?, ?)",
            (GUILD, 7, 1234, 12),
        )
        assert svc.member_level(conn, GUILD, 7) == 12


# ── in-memory watch registry ──────────────────────────────────────────


def test_warm_only_seeds_enabled_guilds(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD, [1, 2], ROLE, 100.0)
        record_prune_events(conn, 99, [5], ROLE, 100.0)  # guild 99 stays dark
        _set_channel(conn)
    svc.warm(db_path, [GUILD, 99])
    assert svc.is_watched(GUILD, 1) is True
    assert svc.is_watched(GUILD, 2) is True
    assert svc.is_watched(99, 5) is False


def test_warm_seeds_sleepers_and_excludes_carded(db_path):
    with open_db(db_path) as conn:
        _set_channel(conn)
        record_prune_events(conn, GUILD, [1], ROLE, 100.0)
        _make_sleeper(conn, 2)
        svc.reserve_card(conn, GUILD, 1, svc.KIND_PRUNED_RETURN, 150.0)
    svc.warm(db_path, [GUILD])
    assert svc.is_watched(GUILD, 1) is False  # already carded
    assert svc.is_watched(GUILD, 2) is True  # sleeper seeded


def test_note_pruned_and_inactive_add_only_when_enabled(db_path):
    with open_db(db_path) as conn:
        _set_channel(conn)
    svc.note_pruned(db_path, GUILD, [10])
    svc.note_inactive(db_path, GUILD, 11)
    assert svc.is_watched(GUILD, 10) is True
    assert svc.is_watched(GUILD, 11) is True
    # A dark guild ignores the feed.
    svc.note_pruned(db_path, 99, [12])
    assert svc.is_watched(99, 12) is False


def test_discard_drops_from_watch(db_path):
    svc.add_watched(GUILD, 10)
    assert svc.is_watched(GUILD, 10) is True
    svc.discard(GUILD, 10)
    assert svc.is_watched(GUILD, 10) is False
    svc.discard(GUILD, 999)  # unknown member is harmless
