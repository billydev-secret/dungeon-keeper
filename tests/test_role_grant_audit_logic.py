"""Tests for services/role_grant_audit_service — the prune ledger + audit buckets."""

from __future__ import annotations

import time
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.role_grant_audit_service import (
    GrantAuditSnapshot,
    backfill_prune_events_from_role_events,
    build_grant_audit_embed,
    clear_card_ref,
    compute_recent_inactive,
    compute_stripped_returned,
    compute_waiting_for_first_grant,
    gather_grant_audit,
    get_ever_pruned_ids,
    get_hold_excluded_ids,
    get_open_prune_events,
    guilds_with_card,
    load_card_ref,
    mark_restored,
    record_prune_events,
    refresh_grant_audit_card,
    resolve_grant_audit_buckets,
    save_card_ref,
)
from migrations import apply_migrations_sync

GUILD_ID = 12345
ROLE_ID = 555
CUTOFF = 1_000_000.0  # activity at/after this counts as "active again"


@dataclass
class FakeActivity:
    created_at: float


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "audit.db"
    apply_migrations_sync(path)
    return path


# ── ledger roundtrip ──────────────────────────────────────────────────


def test_record_and_open_events_roundtrip(db_path):
    with open_db(db_path) as conn:
        assert record_prune_events(conn, GUILD_ID, [201, 202], ROLE_ID, 100.0) == 2
        events = get_open_prune_events(conn, GUILD_ID, ROLE_ID)
        assert {int(e["user_id"]) for e in events} == {201, 202}
        assert all(float(e["pruned_at"]) == 100.0 for e in events)


def test_record_empty_user_list_is_noop(db_path):
    with open_db(db_path) as conn:
        assert record_prune_events(conn, GUILD_ID, [], ROLE_ID, 100.0) == 0
        assert get_open_prune_events(conn, GUILD_ID, ROLE_ID) == []


def test_mark_restored_closes_only_that_user(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD_ID, [201, 202], ROLE_ID, 100.0)
        assert mark_restored(conn, GUILD_ID, 201, ROLE_ID, 200.0) == 1
        remaining = get_open_prune_events(conn, GUILD_ID, ROLE_ID)
        assert [int(e["user_id"]) for e in remaining] == [202]
        # Restored users still count as ever-pruned (never "waiting for first grant").
        assert get_ever_pruned_ids(conn, GUILD_ID, ROLE_ID) == {201, 202}


def test_mark_restored_without_open_event_is_noop(db_path):
    with open_db(db_path) as conn:
        assert mark_restored(conn, GUILD_ID, 999, ROLE_ID, 200.0) == 0


def test_mark_restored_does_not_rewrite_closed_events(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD_ID, [201], ROLE_ID, 100.0)
        mark_restored(conn, GUILD_ID, 201, ROLE_ID, 200.0)
        assert mark_restored(conn, GUILD_ID, 201, ROLE_ID, 300.0) == 0
        row = conn.execute(
            "SELECT restored_at FROM role_prune_events WHERE user_id = 201"
        ).fetchone()
        assert float(row["restored_at"]) == 200.0


def test_open_events_scoped_to_role_and_guild(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD_ID, [201], ROLE_ID, 100.0)
        record_prune_events(conn, GUILD_ID, [202], ROLE_ID + 1, 100.0)
        record_prune_events(conn, GUILD_ID + 1, [203], ROLE_ID, 100.0)
        events = get_open_prune_events(conn, GUILD_ID, ROLE_ID)
        assert [int(e["user_id"]) for e in events] == [201]


def test_open_events_report_latest_prune_per_user(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD_ID, [201], ROLE_ID, 100.0)
        record_prune_events(conn, GUILD_ID, [201], ROLE_ID, 500.0)
        events = get_open_prune_events(conn, GUILD_ID, ROLE_ID)
        assert len(events) == 1
        assert float(events[0]["pruned_at"]) == 500.0


# ── hold exclusions ───────────────────────────────────────────────────


def test_hold_excluded_ids_covers_inactive_jail_and_config(db_path):
    from bot_modules.core.db_utils import set_config_value

    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO inactive_members (guild_id, user_id, stored_roles, created_at, status) "
            "VALUES (?, 301, '[]', 0, 'active')",
            (GUILD_ID,),
        )
        conn.execute(
            "INSERT INTO jails (guild_id, user_id, moderator_id, stored_roles, created_at, status) "
            "VALUES (?, 302, 0, '[]', 0, 'active')",
            (GUILD_ID,),
        )
        set_config_value(conn, "inactive_role_id", "777", GUILD_ID)
        held_ids, hold_role_ids = get_hold_excluded_ids(conn, GUILD_ID)
        assert held_ids == {301, 302}
        assert hold_role_ids == {777}


def test_hold_excluded_ids_empty_when_nothing_configured(db_path):
    with open_db(db_path) as conn:
        held_ids, hold_role_ids = get_hold_excluded_ids(conn, GUILD_ID)
        assert held_ids == set()
        assert hold_role_ids == set()


# ── bucketing (pure) ──────────────────────────────────────────────────


def test_waiting_excludes_granted_and_ever_pruned_sorts_desc():
    levels = {1: 5, 2: 9, 3: 7, 4: 6}
    out = compute_waiting_for_first_grant(levels, granted_ids={1}, ever_pruned_ids={3})
    assert out == [(2, 9), (4, 6)]


def test_waiting_empty_inputs():
    assert compute_waiting_for_first_grant({}, set(), set()) == []


def _events(*pairs):
    return [{"user_id": uid, "pruned_at": ts} for uid, ts in pairs]


def test_stripped_returned_requires_resumed_activity():
    events = _events((10, 100.0), (11, 200.0), (12, 300.0), (13, 400.0))
    activity = {
        10: FakeActivity(CUTOFF + 5),  # resumed → in
        11: FakeActivity(CUTOFF - 5),  # still inactive → out
        # 12: no activity → out
        13: FakeActivity(CUTOFF),  # boundary: at cutoff counts as resumed
    }
    out = compute_stripped_returned(events, set(), activity, CUTOFF)
    assert [r["user_id"] for r in out] == [13, 10]  # newest prune first


def test_stripped_returned_excludes_regranted():
    events = _events((10, 100.0))
    activity = {10: FakeActivity(CUTOFF + 5)}
    assert compute_stripped_returned(events, {10}, activity, CUTOFF) == []


def test_recent_inactive_includes_stale_and_untracked():
    events = _events((10, 100.0), (11, 200.0), (12, 300.0))
    activity = {
        10: FakeActivity(CUTOFF + 5),  # resumed → out
        11: FakeActivity(CUTOFF - 5),  # stale → in
        # 12: no record → in
    }
    out = compute_recent_inactive(events, set(), activity, CUTOFF)
    assert [r["user_id"] for r in out] == [12, 11]


def test_recent_inactive_excludes_regranted():
    events = _events((11, 200.0))
    assert compute_recent_inactive(events, {11}, {}, CUTOFF) == []


def test_recent_inactive_caps_at_limit_newest_first():
    events = _events(*[(100 + i, float(i)) for i in range(15)])
    out = compute_recent_inactive(events, set(), {}, CUTOFF, limit=10)
    assert len(out) == 10
    assert [r["pruned_at"] for r in out] == [float(i) for i in range(14, 4, -1)]


# ── backfill from role_events ─────────────────────────────────────────

INACTIVITY_DAYS = 30
WINDOW = INACTIVITY_DAYS * 86400


def _seed_remove_event(conn, user_id, removed_at, role_name="NSFW"):
    conn.execute(
        "INSERT INTO role_events (guild_id, user_id, role_name, action, granted_at) "
        "VALUES (?, ?, ?, 'remove', ?)",
        (GUILD_ID, user_id, role_name, removed_at),
    )


def _seed_activity(conn, user_id, last_message_at):
    conn.execute(
        "INSERT INTO member_activity (guild_id, user_id, last_channel_id, last_message_id, last_message_at) "
        "VALUES (?, ?, 1, 1, ?)",
        (GUILD_ID, user_id, last_message_at),
    )


def _fake_role(holder_ids=()):
    role = MagicMock()
    role.id = ROLE_ID
    role.name = "NSFW"
    role.members = [MagicMock(id=uid) for uid in holder_ids]
    return role


def _fake_guild():
    guild = MagicMock()
    guild.id = GUILD_ID
    return guild


def test_backfill_inserts_prune_like_removals(db_path):
    removed_at = time.time() - 10 * 86400
    with open_db(db_path) as conn:
        _seed_remove_event(conn, 401, removed_at)
        # Last activity long before removal — inactive well past the window.
        _seed_activity(conn, 401, removed_at - WINDOW - 86400)
        inserted = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        assert inserted == 1
        events = get_open_prune_events(conn, GUILD_ID, ROLE_ID)
        assert [int(e["user_id"]) for e in events] == [401]
        assert float(events[0]["pruned_at"]) == pytest.approx(removed_at)


def test_backfill_skips_removals_that_cannot_be_prunes(db_path):
    removed_at = time.time() - 10 * 86400
    with open_db(db_path) as conn:
        # Active the day before removal — a mod removal, not the prune loop.
        _seed_remove_event(conn, 402, removed_at)
        _seed_activity(conn, 402, removed_at - 86400)
        # No activity record at all — the prune loop never strips those.
        _seed_remove_event(conn, 403, removed_at)
        inserted = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        assert inserted == 0


def test_backfill_includes_members_who_resumed_after_removal(db_path):
    # The known live cases (Phantom, Nate): pruned, then came back. Their
    # current last-activity is *after* the removal, which can't disprove a
    # prune — they must land in the ledger as open events.
    removed_at = time.time() - 20 * 86400
    with open_db(db_path) as conn:
        _seed_remove_event(conn, 404, removed_at)
        _seed_activity(conn, 404, removed_at + 5 * 86400)
        inserted = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        assert inserted == 1
        assert [int(e["user_id"]) for e in get_open_prune_events(conn, GUILD_ID, ROLE_ID)] == [404]


def test_backfill_marks_current_holders_restored(db_path):
    removed_at = time.time() - 20 * 86400
    now = time.time()
    with open_db(db_path) as conn:
        _seed_remove_event(conn, 405, removed_at)
        _seed_activity(conn, 405, removed_at - WINDOW - 86400)
        inserted = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(holder_ids=[405]), INACTIVITY_DAYS, now=now
        )
        assert inserted == 1
        assert get_open_prune_events(conn, GUILD_ID, ROLE_ID) == []
        row = conn.execute(
            "SELECT restored_at FROM role_prune_events WHERE user_id = 405"
        ).fetchone()
        assert float(row["restored_at"]) == pytest.approx(now)


def test_backfill_is_idempotent(db_path):
    removed_at = time.time() - 10 * 86400
    with open_db(db_path) as conn:
        _seed_remove_event(conn, 406, removed_at)
        _seed_activity(conn, 406, removed_at - WINDOW - 86400)
        first = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        second = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        assert (first, second) == (1, 0)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM role_prune_events WHERE user_id = 406"
        ).fetchone()
        assert int(count["n"]) == 1


def test_backfill_ignores_other_role_names(db_path):
    removed_at = time.time() - 10 * 86400
    with open_db(db_path) as conn:
        _seed_remove_event(conn, 407, removed_at, role_name="Veteran")
        _seed_activity(conn, 407, removed_at - WINDOW - 86400)
        inserted = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        assert inserted == 0


# ── snapshot assembly (gather + live resolution) ──────────────────────


class _FakeRole:
    def __init__(self, role_id=ROLE_ID, members=()):
        self.id = role_id
        self.name = "NSFW"
        self.members = list(members)


class _FakeMember:
    def __init__(self, user_id, display_name="", bot=False, roles=()):
        self.id = user_id
        self.display_name = display_name or f"member-{user_id}"
        self.bot = bot
        self.roles = list(roles)


class _FakeGuild:
    me = None

    def __init__(self, role, members):
        self.id = GUILD_ID
        self._role = role
        self._members = {m.id: m for m in members}

    def get_role(self, role_id):
        return self._role if role_id == self._role.id else None

    def get_member(self, user_id):
        return self._members.get(user_id)

    def get_channel(self, channel_id):
        return None


def _seed_snapshot_fixture(db_path, now):
    """3001 waiting · 3002 stripped-returned · 3003 stripped-still-inactive."""
    with open_db(db_path) as conn:
        for uid, level in ((3001, 6), (3002, 7)):
            conn.execute(
                "INSERT INTO member_xp (guild_id, user_id, total_xp, level) "
                "VALUES (?, ?, 0, ?)",
                (GUILD_ID, uid, level),
            )
        record_prune_events(conn, GUILD_ID, [3002], ROLE_ID, now - 5 * 86400)
        record_prune_events(conn, GUILD_ID, [3003], ROLE_ID, now - 3 * 86400)
        for uid, days_ago in ((3001, 1), (3002, 1), (3003, 60)):
            _seed_activity(conn, uid, now - days_ago * 86400)


def test_gather_and_resolve_buckets(db_path):
    now = time.time()
    _seed_snapshot_fixture(db_path, now)
    guild = _FakeGuild(_FakeRole(), [_FakeMember(u) for u in (3001, 3002, 3003)])
    with open_db(db_path) as conn:
        gathered = gather_grant_audit(conn, GUILD_ID, ROLE_ID, 5)
    snap = resolve_grant_audit_buckets(guild, guild._role, gathered, 5, now)  # type: ignore[arg-type]
    assert [r["user_id"] for r in snap.waiting] == [3001]
    assert [r["user_id"] for r in snap.returned] == [3002]
    assert snap.returned[0]["level"] == 7
    assert [r["user_id"] for r in snap.inactive] == [3003]
    assert snap.inactivity_days == 30  # no prune rule → default window


def test_resolve_buckets_excludes_departed_and_held(db_path):
    now = time.time()
    _seed_snapshot_fixture(db_path, now)
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO inactive_members (guild_id, user_id, stored_roles, created_at, status) "
            "VALUES (?, 3002, '[]', 0, 'active')",
            (GUILD_ID,),
        )
        gathered = gather_grant_audit(conn, GUILD_ID, ROLE_ID, 5)
    # 3001 left the server (not in guild cache); 3002 sits on an inactive hold.
    guild = _FakeGuild(_FakeRole(), [_FakeMember(3002), _FakeMember(3003)])
    snap = resolve_grant_audit_buckets(guild, guild._role, gathered, 5, now)  # type: ignore[arg-type]
    assert snap.waiting == []
    assert snap.returned == []
    assert [r["user_id"] for r in snap.inactive] == [3003]


# ── card embed builder ────────────────────────────────────────────────


def _snapshot(waiting=(), returned=(), inactive=(), min_level=5, days=30):
    return GrantAuditSnapshot(
        min_level=min_level,
        inactivity_days=days,
        waiting=list(waiting),
        returned=list(returned),
        inactive=list(inactive),
    )


def test_embed_has_three_buckets_with_counts():
    snap = _snapshot(
        waiting=[{"user_id": 1, "display_name": "Nismo", "level": 7}],
        returned=[
            {"user_id": 2, "display_name": "Phantom", "level": 6, "pruned_at": 100.0}
        ],
        inactive=[
            {"user_id": 3, "display_name": "Ghost", "level": None, "pruned_at": 200.0}
        ],
    )
    embed = build_grant_audit_embed("NSFW", snap, now_ts=1000.0)
    assert embed.title == "📋 Grant audit — NSFW"
    names = [f.name for f in embed.fields]
    assert names[0] == "🕐 Waiting for first grant (1)"
    assert names[1] == "↩️ Stripped but came back (1)"
    assert names[2] == "💤 Recently stripped, still inactive"
    assert "Nismo" in str(embed.fields[0].value)
    assert "Phantom" in str(embed.fields[1].value) and "<t:100:R>" in str(embed.fields[1].value)
    assert "Ghost" in str(embed.fields[2].value)


def test_embed_empty_buckets_show_all_clear():
    embed = build_grant_audit_embed("NSFW", _snapshot(), now_ts=1000.0)
    assert embed.fields[0].value == "Nobody — all clear."
    assert embed.fields[1].value == "Nobody — all clear."
    assert "30d inactivity prune" in str(embed.fields[2].value)


def test_embed_caps_waiting_with_overflow_line():
    waiting = [
        {"user_id": i, "display_name": f"m{i}", "level": 5} for i in range(20)
    ]
    embed = build_grant_audit_embed("NSFW", _snapshot(waiting=waiting), now_ts=0.0)
    assert "…and 5 more on the dashboard." in str(embed.fields[0].value)
    assert embed.fields[0].name == "🕐 Waiting for first grant (20)"


# ── card ref storage ──────────────────────────────────────────────────


def test_card_ref_roundtrip(db_path):
    with open_db(db_path) as conn:
        ref = load_card_ref(conn, GUILD_ID)
        assert ref.message_id == 0 and ref.grant_name == ""
        save_card_ref(conn, GUILD_ID, 111, 2**60 + 3, "nsfw", 7)
        ref = load_card_ref(conn, GUILD_ID)
        assert (ref.channel_id, ref.message_id) == (111, 2**60 + 3)
        assert (ref.grant_name, ref.min_level) == ("nsfw", 7)
        assert guilds_with_card(conn) == [GUILD_ID]
        clear_card_ref(conn, GUILD_ID)
        assert load_card_ref(conn, GUILD_ID).message_id == 0
        assert guilds_with_card(conn) == []


# ── card refresh (Discord I/O mocked) ─────────────────────────────────


def _seed_card_guild(db_path, now):
    _seed_snapshot_fixture(db_path, now)
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO grant_roles (guild_id, grant_name, label, role_id) "
            "VALUES (?, 'nsfw', 'NSFW', ?)",
            (GUILD_ID, ROLE_ID),
        )
        save_card_ref(conn, GUILD_ID, 555000, 666000, "nsfw", 5)


def _card_bot(guild):
    import discord
    from unittest.mock import AsyncMock

    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock()
    message.edit = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=message)
    guild.get_channel = lambda cid: channel if cid == 555000 else None
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    return bot, channel, message


async def test_refresh_card_edits_message_in_place(db_path):
    now = time.time()
    _seed_card_guild(db_path, now)
    guild = _FakeGuild(_FakeRole(), [_FakeMember(u) for u in (3001, 3002, 3003)])
    bot, channel, message = _card_bot(guild)

    await refresh_grant_audit_card(bot, db_path, GUILD_ID, now_ts=now)

    channel.fetch_message.assert_awaited_once_with(666000)
    message.edit.assert_awaited_once()
    embed = message.edit.call_args.kwargs["embed"]
    assert embed.title == "📋 Grant audit — NSFW"
    assert "member-3001" in embed.fields[0].value


async def test_refresh_card_retires_ref_when_message_deleted(db_path):
    import discord
    from unittest.mock import AsyncMock

    now = time.time()
    _seed_card_guild(db_path, now)
    guild = _FakeGuild(_FakeRole(), [])
    bot, channel, _ = _card_bot(guild)
    not_found = discord.NotFound(MagicMock(status=404, reason="Not Found"), "gone")
    channel.fetch_message = AsyncMock(side_effect=not_found)

    await refresh_grant_audit_card(bot, db_path, GUILD_ID, now_ts=now)

    with open_db(db_path) as conn:
        assert load_card_ref(conn, GUILD_ID).message_id == 0
        assert guilds_with_card(conn) == []


async def test_refresh_card_noop_without_stored_ref(db_path):
    guild = _FakeGuild(_FakeRole(), [])
    bot, channel, message = _card_bot(guild)
    await refresh_grant_audit_card(bot, db_path, GUILD_ID)
    channel.fetch_message.assert_not_awaited()


# ── implicit strips (grant recorded, removal never was — the Maju case) ──


def _seed_grant_event(conn, user_id, granted_at, role_name="NSFW"):
    conn.execute(
        "INSERT INTO role_events (guild_id, user_id, role_name, action, granted_at) "
        "VALUES (?, ?, ?, 'grant', ?)",
        (GUILD_ID, user_id, role_name, granted_at),
    )


def test_unrecorded_strip_never_shows_as_waiting(db_path):
    """A member with a grant row, no remove row, no ledger row, and no role
    was stripped without a record (e.g. during bot downtime) — they must land
    in a stripped bucket with an unknown date, not in "waiting for first
    grant" (the live false positive: Maju)."""
    now = time.time()
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO member_xp (guild_id, user_id, total_xp, level) "
            "VALUES (?, 4001, 0, 15)",
            (GUILD_ID,),
        )
        _seed_grant_event(conn, 4001, now - 120 * 86400)
        _seed_activity(conn, 4001, now - 74 * 86400)  # inactive past the window
        gathered = gather_grant_audit(conn, GUILD_ID, ROLE_ID, 5, "NSFW")
    guild = _FakeGuild(_FakeRole(), [_FakeMember(4001, "maju")])
    snap = resolve_grant_audit_buckets(guild, guild._role, gathered, 5, now)  # type: ignore[arg-type]
    assert snap.waiting == []
    assert [r["user_id"] for r in snap.inactive] == [4001]
    assert snap.inactive[0]["pruned_at"] is None


def test_unrecorded_strip_active_member_lands_in_returned(db_path):
    now = time.time()
    with open_db(db_path) as conn:
        _seed_grant_event(conn, 4002, now - 120 * 86400)
        _seed_activity(conn, 4002, now - 86400)  # active yesterday
        gathered = gather_grant_audit(conn, GUILD_ID, ROLE_ID, 5, "NSFW")
    guild = _FakeGuild(_FakeRole(), [_FakeMember(4002)])
    snap = resolve_grant_audit_buckets(guild, guild._role, gathered, 5, now)  # type: ignore[arg-type]
    assert [r["user_id"] for r in snap.returned] == [4002]
    assert snap.returned[0]["pruned_at"] is None


def test_current_holder_with_grant_row_is_not_an_implicit_strip(db_path):
    now = time.time()
    with open_db(db_path) as conn:
        _seed_grant_event(conn, 4003, now - 10 * 86400)
        _seed_activity(conn, 4003, now - 86400)
        gathered = gather_grant_audit(conn, GUILD_ID, ROLE_ID, 5, "NSFW")
    holder = _FakeMember(4003)
    guild = _FakeGuild(_FakeRole(members=[holder]), [holder])
    snap = resolve_grant_audit_buckets(guild, guild._role, gathered, 5, now)  # type: ignore[arg-type]
    assert snap.waiting == snap.returned == snap.inactive == []


def test_ledgered_member_not_duplicated_as_implicit(db_path):
    now = time.time()
    with open_db(db_path) as conn:
        _seed_grant_event(conn, 4004, now - 120 * 86400)
        record_prune_events(conn, GUILD_ID, [4004], ROLE_ID, now - 5 * 86400)
        _seed_activity(conn, 4004, now - 86400)
        gathered = gather_grant_audit(conn, GUILD_ID, ROLE_ID, 5, "NSFW")
    guild = _FakeGuild(_FakeRole(), [_FakeMember(4004)])
    snap = resolve_grant_audit_buckets(guild, guild._role, gathered, 5, now)  # type: ignore[arg-type]
    assert len(snap.returned) == 1
    assert snap.returned[0]["pruned_at"] is not None  # real event wins


def test_gather_without_role_name_skips_grant_history(db_path):
    now = time.time()
    with open_db(db_path) as conn:
        _seed_grant_event(conn, 4005, now - 120 * 86400)
        gathered = gather_grant_audit(conn, GUILD_ID, ROLE_ID, 5)
    assert gathered.ever_granted_ids == set()


def test_known_dates_sort_before_unknown_in_inactive_bucket():
    events = [
        {"user_id": 1, "pruned_at": None},
        {"user_id": 2, "pruned_at": 100.0},
        {"user_id": 3, "pruned_at": 200.0},
    ]
    out = compute_recent_inactive(events, set(), {}, CUTOFF)
    assert [r["user_id"] for r in out] == [3, 2, 1]


def test_embed_renders_unrecorded_strip_date():
    snap = _snapshot(
        returned=[{"user_id": 1, "display_name": "maju", "level": 15, "pruned_at": None}],
        inactive=[{"user_id": 2, "display_name": "ghost", "level": None, "pruned_at": None}],
    )
    embed = build_grant_audit_embed("NSFW", snap, now_ts=1000.0)
    assert "stripped (date unrecorded)" in str(embed.fields[1].value)
    assert "stripped (date unrecorded)" in str(embed.fields[2].value)
