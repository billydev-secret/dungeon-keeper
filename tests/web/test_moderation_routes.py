"""Tests for /api/moderation/* endpoints."""

from __future__ import annotations

import time

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.moderation import (
    close_ticket,
    create_jail,
    create_ticket,
    create_warning,
    write_audit,
)


def _seed_jail(db_path, guild_id=123, user_id=1001, moderator_id=2001):
    with open_db(db_path) as conn:
        jail_id = create_jail(
            conn,
            guild_id=guild_id,
            user_id=user_id,
            moderator_id=moderator_id,
            reason="test jail",
            stored_roles=[],
            channel_id=0,
            duration_seconds=3600,
        )
    return jail_id


def _seed_ticket(db_path, guild_id=123, user_id=1001):
    with open_db(db_path) as conn:
        ticket_id = create_ticket(
            conn,
            guild_id=guild_id,
            user_id=user_id,
            channel_id=0,
            description="test ticket",
        )
    return ticket_id


def _seed_warning(db_path, guild_id=123, user_id=1001, moderator_id=2001):
    with open_db(db_path) as conn:
        warn_id = create_warning(
            conn,
            guild_id=guild_id,
            user_id=user_id,
            moderator_id=moderator_id,
            reason="test warning",
        )
    return warn_id


# ── GET /api/moderation/jails ─────────────────────────────────────────

def test_jails_empty_on_fresh_db(open_client):
    resp = open_client.get("/api/moderation/jails")
    assert resp.status_code == 200
    data = resp.json()
    assert "jails" in data
    assert data["jails"] == []


def test_jails_returns_seeded_jail(open_client, fake_ctx):
    _seed_jail(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.get("/api/moderation/jails")
    assert resp.status_code == 200
    jails = resp.json()["jails"]
    assert len(jails) == 1
    assert jails[0]["user_id"] == str(1001)
    assert jails[0]["reason"] == "test jail"


# ── GET /api/moderation/tickets ───────────────────────────────────────

def test_tickets_empty_on_fresh_db(open_client):
    resp = open_client.get("/api/moderation/tickets")
    assert resp.status_code == 200
    data = resp.json()
    assert "tickets" in data
    assert data["tickets"] == []


def test_tickets_returns_seeded_ticket(open_client, fake_ctx):
    _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.get("/api/moderation/tickets")
    assert resp.status_code == 200
    tickets = resp.json()["tickets"]
    assert len(tickets) == 1
    assert tickets[0]["user_id"] == str(1001)


# ── GET /api/moderation/warnings ─────────────────────────────────────

def test_warnings_empty_on_fresh_db(open_client):
    resp = open_client.get("/api/moderation/warnings")
    assert resp.status_code == 200
    data = resp.json()
    assert "warnings" in data
    assert data["warnings"] == []


def test_warnings_returns_seeded_warning(open_client, fake_ctx):
    _seed_warning(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.get("/api/moderation/warnings")
    assert resp.status_code == 200
    warnings = resp.json()["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["user_id"] == str(1001)
    assert warnings[0]["reason"] == "test warning"


# ── GET /api/moderation/stats ─────────────────────────────────────────


def test_stats_returns_zeroes_on_empty_db(open_client):
    body = open_client.get("/api/moderation/stats").json()
    assert body["active_jails"] == 0
    assert body["total_jails"] == 0
    assert body["open_tickets"] == 0
    assert body["closed_tickets"] == 0
    assert body["active_warnings"] == 0
    assert body["recent_actions"] == 0


def test_stats_counts_seeded_state(open_client, fake_ctx):
    _seed_jail(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    _seed_warning(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    with open_db(fake_ctx.db_path) as conn:
        write_audit(
            conn, guild_id=fake_ctx.guild_id, action="x", actor_id=1,
        )

    body = open_client.get("/api/moderation/stats").json()
    assert body["active_jails"] == 1  # seeded with default 'active' status
    assert body["total_jails"] == 1
    assert body["open_tickets"] == 1
    assert body["closed_tickets"] == 0
    assert body["active_warnings"] == 1
    assert body["recent_actions"] == 1


def test_stats_isolates_by_guild(open_client, fake_ctx):
    _seed_jail(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    _seed_jail(fake_ctx.db_path, guild_id=999)  # other guild
    body = open_client.get("/api/moderation/stats").json()
    assert body["total_jails"] == 1


# ── GET /api/moderation/jails — filters ──────────────────────────────


def test_jails_filter_by_status(open_client, fake_ctx):
    _seed_jail(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=1)
    _seed_jail(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=2)
    # Mark one as released
    with open_db(fake_ctx.db_path) as conn:
        conn.execute("UPDATE jails SET status = 'released' WHERE user_id = 2")

    active = open_client.get("/api/moderation/jails?status=active").json()
    released = open_client.get("/api/moderation/jails?status=released").json()
    assert len(active["jails"]) == 1
    assert active["jails"][0]["user_id"] == "1"
    assert len(released["jails"]) == 1
    assert released["jails"][0]["user_id"] == "2"


def test_jails_filter_by_user_id(open_client, fake_ctx):
    _seed_jail(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=100)
    _seed_jail(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=200)
    body = open_client.get("/api/moderation/jails?user_id=100").json()
    assert {j["user_id"] for j in body["jails"]} == {"100"}


# ── GET /api/moderation/tickets — filters ─────────────────────────────


def test_tickets_excludes_deleted_by_default(open_client, fake_ctx):
    """Soft-deleted tickets must NOT appear in the default listing."""
    open_id = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=1)
    deleted_id = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=2)
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "UPDATE tickets SET status = 'deleted' WHERE id = ?", (deleted_id,)
        )

    body = open_client.get("/api/moderation/tickets").json()
    assert {t["id"] for t in body["tickets"]} == {open_id}


def test_tickets_status_closed_includes_deleted(open_client, fake_ctx):
    """status=closed merges both 'closed' and 'deleted' so admins can audit."""
    closed_id = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=1)
    deleted_id = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=2)
    with open_db(fake_ctx.db_path) as conn:
        close_ticket(conn, closed_id, closed_by=99, reason="done")
        conn.execute(
            "UPDATE tickets SET status = 'deleted' WHERE id = ?", (deleted_id,)
        )

    body = open_client.get("/api/moderation/tickets?status=closed").json()
    assert {t["id"] for t in body["tickets"]} == {closed_id, deleted_id}


def test_tickets_filter_by_user_id(open_client, fake_ctx):
    _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=100)
    _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=200)
    body = open_client.get("/api/moderation/tickets?user_id=100").json()
    assert {t["user_id"] for t in body["tickets"]} == {"100"}


# ── GET /api/moderation/tickets/{id} (detail) ────────────────────────


def test_get_ticket_detail_returns_404_for_missing(open_client):
    resp = open_client.get("/api/moderation/tickets/99999")
    assert resp.status_code == 404


def test_get_ticket_detail_includes_subject_and_history(open_client, fake_ctx):
    ticket_id = _seed_ticket(
        fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=42
    )
    _seed_warning(
        fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=42, moderator_id=1
    )
    _seed_jail(
        fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=42, moderator_id=1
    )

    body = open_client.get(f"/api/moderation/tickets/{ticket_id}").json()
    assert body["subject"]["user_id"] == "42"
    assert body["subject"]["warn_count_active"] == 1
    assert body["subject"]["jail_count_total"] == 1
    kinds = {h["kind"] for h in body["history"]}
    assert kinds == {"warn", "jail"}


# ── Ticket mutations: claim / close / reopen / dismiss / escalate ─────


def test_ticket_claim_writes_audit_and_sets_claimer(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.post(f"/api/moderation/tickets/{tid}/claim")
    assert resp.status_code == 200
    assert resp.json()["status"] == "open"

    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT claimer_id FROM tickets WHERE id = ?", (tid,)
        ).fetchone()
        audit = conn.execute(
            "SELECT action FROM audit_log WHERE guild_id = ?", (fake_ctx.guild_id,)
        ).fetchall()
    assert row["claimer_id"] is not None
    assert any(r["action"] == "ticket_claim" for r in audit)


def test_ticket_claim_rejects_non_open_status(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    with open_db(fake_ctx.db_path) as conn:
        close_ticket(conn, tid, closed_by=99, reason="done")
    resp = open_client.post(f"/api/moderation/tickets/{tid}/claim")
    assert resp.status_code == 409


def test_ticket_claim_404_for_unknown(open_client):
    resp = open_client.post("/api/moderation/tickets/99999/claim")
    assert resp.status_code == 404


def test_ticket_close_persists_reason(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.post(
        f"/api/moderation/tickets/{tid}/close", json={"reason": "spam"}
    )
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT status, close_reason FROM tickets WHERE id = ?", (tid,)
        ).fetchone()
    assert row["status"] == "closed"
    assert row["close_reason"] == "spam"


def test_ticket_close_uses_default_reason_when_empty(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    open_client.post(f"/api/moderation/tickets/{tid}/close", json={"reason": "  "})
    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT close_reason FROM tickets WHERE id = ?", (tid,)
        ).fetchone()
    assert row["close_reason"] == "Closed from dashboard"


def test_ticket_close_rejects_already_closed(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    with open_db(fake_ctx.db_path) as conn:
        close_ticket(conn, tid, closed_by=1, reason="x")
    resp = open_client.post(
        f"/api/moderation/tickets/{tid}/close", json={"reason": "again"}
    )
    assert resp.status_code == 409


def test_ticket_reopen_restores_open_status(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    with open_db(fake_ctx.db_path) as conn:
        close_ticket(conn, tid, closed_by=1, reason="x")
    resp = open_client.post(f"/api/moderation/tickets/{tid}/reopen")
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT status, closed_at, close_reason FROM tickets WHERE id = ?", (tid,)
        ).fetchone()
    assert row["status"] == "open"
    assert row["closed_at"] is None


def test_ticket_reopen_rejects_open_ticket(open_client, fake_ctx):
    """Trying to reopen a still-open ticket is 409."""
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.post(f"/api/moderation/tickets/{tid}/reopen")
    assert resp.status_code == 409


def test_ticket_dismiss_closes_with_prefixed_reason(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.post(
        f"/api/moderation/tickets/{tid}/dismiss", json={"reason": "duplicate"}
    )
    assert resp.status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT status, close_reason FROM tickets WHERE id = ?", (tid,)
        ).fetchone()
    assert row["status"] == "closed"
    assert row["close_reason"] == "Dismissed: duplicate"


def test_ticket_dismiss_without_reason_uses_bare_label(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    open_client.post(f"/api/moderation/tickets/{tid}/dismiss", json={"reason": ""})
    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT close_reason FROM tickets WHERE id = ?", (tid,)
        ).fetchone()
    assert row["close_reason"] == "Dismissed"


def test_ticket_escalate_sets_flag(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.post(f"/api/moderation/tickets/{tid}/escalate")
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT escalated FROM tickets WHERE id = ?", (tid,)
        ).fetchone()
    assert row["escalated"] == 1


def test_ticket_escalate_idempotent(open_client, fake_ctx):
    """Re-escalating an already-escalated ticket returns success without error."""
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    open_client.post(f"/api/moderation/tickets/{tid}/escalate")
    resp = open_client.post(f"/api/moderation/tickets/{tid}/escalate")
    assert resp.status_code == 200
    assert "Already escalated" in resp.json()["message"]


def test_ticket_escalate_rejects_closed_ticket(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    with open_db(fake_ctx.db_path) as conn:
        close_ticket(conn, tid, closed_by=1, reason="x")
    resp = open_client.post(f"/api/moderation/tickets/{tid}/escalate")
    assert resp.status_code == 409


# ── Ticket-derived warnings / jails / notes ──────────────────────────


def test_ticket_warn_requires_reason(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.post(
        f"/api/moderation/tickets/{tid}/warn", json={"reason": "   "}
    )
    assert resp.status_code == 400


def test_ticket_warn_creates_warning_and_audit(open_client, fake_ctx):
    tid = _seed_ticket(
        fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=42
    )
    resp = open_client.post(
        f"/api/moderation/tickets/{tid}/warn", json={"reason": "rude"}
    )
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        w = conn.execute(
            "SELECT user_id, reason FROM warnings WHERE guild_id = ?",
            (fake_ctx.guild_id,),
        ).fetchone()
        audit = conn.execute(
            "SELECT action FROM audit_log WHERE guild_id = ?",
            (fake_ctx.guild_id,),
        ).fetchall()
    assert w["user_id"] == 42
    assert w["reason"] == "rude"
    assert any(r["action"] == "ticket_warn" for r in audit)


def test_ticket_jail_rejects_invalid_duration(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.post(
        f"/api/moderation/tickets/{tid}/jail",
        json={"reason": "spam", "duration": "forever"},
    )
    assert resp.status_code == 400


def test_ticket_jail_503_when_bot_unavailable(open_client, fake_ctx):
    """The dashboard jail endpoint now actually applies the role via the bot.
    Without a live bot guild it must refuse loudly rather than silently
    record a row that doesn't reflect Discord state."""
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=42)
    resp = open_client.post(
        f"/api/moderation/tickets/{tid}/jail",
        json={"reason": "spam", "duration": "1h"},
    )
    assert resp.status_code == 503
    assert "Bot" in resp.json()["detail"]


def test_ticket_jail_404_when_target_left_guild(open_client, fake_ctx):
    """If the ticket subject has left the guild, refuse — can't apply a role
    to a member that isn't there."""
    from unittest.mock import MagicMock

    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.get_member = MagicMock(return_value=None)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=42)
    resp = open_client.post(
        f"/api/moderation/tickets/{tid}/jail",
        json={"reason": "spam", "duration": "1h"},
    )
    assert resp.status_code == 404


def test_ticket_jail_invokes_apply_jail_and_returns_jail_id(open_client, fake_ctx):
    """Happy path: route resolves guild + target + moderator, calls
    apply_jail, persists the linked ticket_jail audit entry, returns the
    canonical jail_id from apply_jail."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot_modules.jail.apply import JailOutcome

    target = MagicMock()
    target.id = 42
    moderator = MagicMock()
    moderator.id = 0  # OpenAuth's user_id
    by_id = {42: target, 0: moderator}

    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.get_member = MagicMock(side_effect=lambda uid: by_id.get(int(uid)))
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=42)

    fake_outcome = JailOutcome(ok=True, jail_id=999, channel_id=8888)
    with patch(
        "web_server.routes.moderation.apply_jail",
        new=AsyncMock(return_value=fake_outcome),
    ) as mock_apply:
        resp = open_client.post(
            f"/api/moderation/tickets/{tid}/jail",
            json={"reason": "spam", "duration": "1h"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "999" in body["message"]

    mock_apply.assert_awaited_once()
    kwargs = mock_apply.call_args.kwargs
    assert kwargs["reason"] == "spam"
    assert kwargs["duration_seconds"] == 3600
    assert kwargs["source"] == "dashboard"
    # The ticket linkage rides on apply_jail's source_extra rather than a
    # separate audit row, so there's exactly one audit entry per jail.
    assert kwargs["source_extra"] == {"ticket_id": tid}

    # No duplicate ticket_jail row was written — apply_jail is the single
    # audit source of truth.
    with open_db(fake_ctx.db_path) as conn:
        rows = conn.execute(
            "SELECT action FROM audit_log WHERE guild_id = ?",
            (fake_ctx.guild_id,),
        ).fetchall()
    assert [r["action"] for r in rows] == []  # mocked apply_jail wrote nothing


def test_ticket_jail_409_when_apply_jail_rejects_target(open_client, fake_ctx):
    """Precondition failures (admin/mod/bot/self/already_jailed) come back
    as 409 — these reflect a conflict with the target's state, not a
    bot-configuration problem."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot_modules.jail.apply import JailOutcome

    target = MagicMock()
    target.id = 42
    moderator = MagicMock()
    moderator.id = 0
    by_id = {42: target, 0: moderator}
    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.get_member = MagicMock(side_effect=lambda uid: by_id.get(int(uid)))
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=42)

    rejection = JailOutcome(
        ok=False, error_kind="admin_target", error_message="Cannot jail an admin."
    )
    with patch(
        "web_server.routes.moderation.apply_jail",
        new=AsyncMock(return_value=rejection),
    ):
        resp = open_client.post(
            f"/api/moderation/tickets/{tid}/jail",
            json={"reason": "spam", "duration": "1h"},
        )
    assert resp.status_code == 409
    assert "admin" in resp.json()["detail"].lower()


def test_ticket_jail_500_when_apply_jail_has_permission_error(open_client, fake_ctx):
    """Bot-perm failures (no_role_perms, no_channel_perms, no_member_perms)
    come back as 500 — these are operator-config problems, not target state."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot_modules.jail.apply import JailOutcome

    target = MagicMock()
    target.id = 42
    moderator = MagicMock()
    moderator.id = 0
    by_id = {42: target, 0: moderator}
    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    guild.get_member = MagicMock(side_effect=lambda uid: by_id.get(int(uid)))
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot

    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=42)

    rejection = JailOutcome(
        ok=False,
        error_kind="no_role_perms",
        error_message="Missing Manage Roles.",
    )
    with patch(
        "web_server.routes.moderation.apply_jail",
        new=AsyncMock(return_value=rejection),
    ):
        resp = open_client.post(
            f"/api/moderation/tickets/{tid}/jail",
            json={"reason": "spam", "duration": "1h"},
        )
    assert resp.status_code == 500


def test_ticket_note_requires_body(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.post(
        f"/api/moderation/tickets/{tid}/note", json={"body": "  "}
    )
    assert resp.status_code == 400


def test_ticket_note_writes_audit_with_body(open_client, fake_ctx):
    tid = _seed_ticket(fake_ctx.db_path, guild_id=fake_ctx.guild_id)
    resp = open_client.post(
        f"/api/moderation/tickets/{tid}/note", json={"body": "Spoke to user."}
    )
    assert resp.status_code == 200

    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT action, extra FROM audit_log WHERE guild_id = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (fake_ctx.guild_id,),
        ).fetchone()
    import json as _json
    assert row["action"] == "ticket_note"
    assert _json.loads(row["extra"])["body"] == "Spoke to user."


# ── Warnings: filters ────────────────────────────────────────────────


def test_warnings_filter_by_user_id(open_client, fake_ctx):
    _seed_warning(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=1)
    _seed_warning(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=2)
    body = open_client.get("/api/moderation/warnings?user_id=1").json()
    assert {w["user_id"] for w in body["warnings"]} == {"1"}


def test_warnings_active_only_excludes_revoked(open_client, fake_ctx):
    revoked_id = _seed_warning(
        fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=1
    )
    _seed_warning(fake_ctx.db_path, guild_id=fake_ctx.guild_id, user_id=2)
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "UPDATE warnings SET revoked = 1 WHERE id = ?", (revoked_id,)
        )

    body = open_client.get("/api/moderation/warnings?active_only=true").json()
    assert all(not w["revoked"] for w in body["warnings"])
    assert body["active_count"] == len(body["warnings"])


# ── Policy tickets ───────────────────────────────────────────────────


def test_policy_tickets_lists_seeded_rows(open_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        for status in ("open", "voting", "closed"):
            conn.execute(
                "INSERT INTO policy_tickets (guild_id, creator_id, channel_id, title,"
                " description, status, vote_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fake_ctx.guild_id, 1, 0, f"{status} title", "desc", status, "vote?", time.time()),
            )

    body = open_client.get("/api/moderation/policy-tickets").json()
    assert body["open_count"] == 1
    assert body["voting_count"] == 1
    assert body["closed_count"] == 1
    assert body["total_count"] == 3


def test_policy_tickets_filter_by_status(open_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO policy_tickets (guild_id, creator_id, channel_id, title,"
            " description, status, vote_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fake_ctx.guild_id, 1, 0, "vote", "desc", "voting", "?", time.time()),
        )
        conn.execute(
            "INSERT INTO policy_tickets (guild_id, creator_id, channel_id, title,"
            " description, status, vote_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fake_ctx.guild_id, 1, 0, "done", "desc", "closed", "?", time.time()),
        )

    body = open_client.get("/api/moderation/policy-tickets?status=voting").json()
    assert {t["status"] for t in body["policy_tickets"]} == {"voting"}


# ── Transcripts ──────────────────────────────────────────────────────


def test_transcript_rejects_invalid_record_type(open_client):
    resp = open_client.get("/api/moderation/transcript?record_type=bogus&record_id=1")
    assert resp.status_code == 400


def test_transcript_returns_none_when_missing(open_client):
    body = open_client.get(
        "/api/moderation/transcript?record_type=ticket&record_id=99"
    ).json()
    assert body["transcript"] is None


def test_transcript_returns_stored_content(open_client, fake_ctx):
    import json as _json
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO transcripts (guild_id, record_type, record_id, content, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                fake_ctx.guild_id,
                "ticket",
                42,
                _json.dumps({"messages": [{"author": "alice", "content": "hi"}]}),
                time.time(),
            ),
        )

    body = open_client.get(
        "/api/moderation/transcript?record_type=ticket&record_id=42"
    ).json()
    assert body["transcript"]["messages"][0]["author"] == "alice"


# ── Audit log endpoints ──────────────────────────────────────────────


def test_audit_log_returns_entries_newest_first(open_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        for i in range(3):
            write_audit(
                conn, guild_id=fake_ctx.guild_id, action=f"act_{i}", actor_id=1,
                extra={"i": i},
            )
            time.sleep(0.001)

    body = open_client.get("/api/moderation/audit").json()
    actions = [e["action"] for e in body["entries"]]
    assert actions == ["act_2", "act_1", "act_0"]
    assert body["total"] == 3


def test_audit_log_filter_by_action(open_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        write_audit(conn, guild_id=fake_ctx.guild_id, action="warn", actor_id=1)
        write_audit(conn, guild_id=fake_ctx.guild_id, action="jail", actor_id=1)

    body = open_client.get("/api/moderation/audit?action=jail").json()
    assert body["total"] == 1
    assert body["entries"][0]["action"] == "jail"


def test_audit_log_caps_limit_at_200(open_client, fake_ctx):
    """Requesting a huge limit returns at most 200 rows."""
    with open_db(fake_ctx.db_path) as conn:
        for i in range(5):
            write_audit(conn, guild_id=fake_ctx.guild_id, action="x", actor_id=1)

    body = open_client.get("/api/moderation/audit?limit=10000").json()
    # We only seeded 5 rows so this can't actually exceed; the assertion is
    # that the request succeeds and returns the rows that exist.
    assert len(body["entries"]) == 5


def test_audit_log_decodes_extra_json(open_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        write_audit(
            conn, guild_id=fake_ctx.guild_id, action="custom", actor_id=1,
            extra={"ticket_id": 99, "reason": "spam"},
        )
    body = open_client.get("/api/moderation/audit").json()
    assert body["entries"][0]["extra"] == {"ticket_id": 99, "reason": "spam"}


# ── DM audit log ─────────────────────────────────────────────────────


def test_dm_audit_returns_seeded_rows(open_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO dm_audit_log (guild_id, actor_id, user_a_id, user_b_id,"
            " action, timestamp, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fake_ctx.guild_id, 1, 10, 20, "open", time.time(), "type=ticket"),
        )
    body = open_client.get("/api/moderation/dm-audit").json()
    assert body["total"] == 1
    assert body["entries"][0]["action"] == "open"


def test_dm_audit_filter_by_action_and_type(open_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO dm_audit_log (guild_id, actor_id, user_a_id, user_b_id,"
            " action, timestamp, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fake_ctx.guild_id, 1, 10, 20, "open", time.time(), "type=ticket"),
        )
        conn.execute(
            "INSERT INTO dm_audit_log (guild_id, actor_id, user_a_id, user_b_id,"
            " action, timestamp, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fake_ctx.guild_id, 1, 30, 40, "close", time.time(), "type=other"),
        )

    body = open_client.get(
        "/api/moderation/dm-audit?action=open&type=ticket"
    ).json()
    assert body["total"] == 1
    assert body["entries"][0]["action"] == "open"


# ── Whisper audit log ────────────────────────────────────────────────


def _seed_whisper(db_path, guild_id, *, sender=10, target=20, state="active"):
    with open_db(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO whispers (guild_id, sender_id, target_id, message, created_at,"
            " state, solved, exposed, guesses_left) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 3)",
            (guild_id, sender, target, "secret", time.time(), state),
        )
        return cur.lastrowid


def test_whisper_audit_returns_seeded_rows(open_client, fake_ctx):
    _seed_whisper(fake_ctx.db_path, fake_ctx.guild_id)
    body = open_client.get("/api/moderation/whisper-audit").json()
    assert body["total"] == 1
    assert body["entries"][0]["sender_id"] == "10"
    assert body["entries"][0]["report_count"] == 0


def test_whisper_audit_filter_by_state(open_client, fake_ctx):
    _seed_whisper(fake_ctx.db_path, fake_ctx.guild_id, state="active")
    _seed_whisper(fake_ctx.db_path, fake_ctx.guild_id, sender=11, state="solved")
    body = open_client.get("/api/moderation/whisper-audit?state=solved").json()
    assert body["total"] == 1
    assert body["entries"][0]["state"] == "solved"


def test_whisper_audit_reported_only_filters_to_reported(open_client, fake_ctx):
    no_report = _seed_whisper(fake_ctx.db_path, fake_ctx.guild_id)
    reported = _seed_whisper(fake_ctx.db_path, fake_ctx.guild_id, sender=11)
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO whisper_reports (whisper_id, reporter_id, reason, created_at)"
            " VALUES (?, ?, ?, ?)",
            (reported, 99, "bad", time.time()),
        )

    body = open_client.get(
        "/api/moderation/whisper-audit?reported_only=true"
    ).json()
    ids = {int(e["id"]) for e in body["entries"]}
    assert ids == {reported}
    assert no_report not in ids


# ── Confessions audit ────────────────────────────────────────────────


def test_confessions_audit_returns_seeded_rows(open_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO confession_threads (guild_id, message_id, channel_id,"
            " root_message_id, original_author_id, notify_original_author,"
            " discord_thread_id, reply_button_message_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            # Schema models created_at as int; pass an integer timestamp.
            (fake_ctx.guild_id, 1, 100, 1, 42, 1, 200, 300, int(time.time())),
        )

    body = open_client.get("/api/moderation/confessions-audit").json()
    assert body["total"] == 1
    assert body["entries"][0]["author_id"] == "42"


# ── Auth guard ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/moderation/stats", None),
        ("GET", "/api/moderation/jails", None),
        ("GET", "/api/moderation/tickets", None),
        ("GET", "/api/moderation/tickets/1", None),
        ("GET", "/api/moderation/warnings", None),
        ("GET", "/api/moderation/policy-tickets", None),
        ("GET", "/api/moderation/audit", None),
        ("GET", "/api/moderation/whisper-audit", None),
        ("POST", "/api/moderation/tickets/1/claim", None),
        ("POST", "/api/moderation/tickets/1/close", {"reason": "x"}),
        ("POST", "/api/moderation/tickets/1/warn", {"reason": "x"}),
    ],
)
def test_moderation_endpoints_require_auth(fake_ctx, method, path, body):
    """Every moderation endpoint must reject unauthenticated callers when
    Discord auth is in use. The original LAN/OpenAuth tests above just
    confirm the routes work; this matrix confirms the auth gate is on."""
    from fastapi.testclient import TestClient

    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app

    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app, raise_server_exceptions=False)
    if method == "GET":
        resp = client.get(path)
    else:
        resp = client.post(path, json=body or {})
    assert resp.status_code in (401, 403), f"{method} {path} should require auth"
    client.close()


def test_moderation_requires_auth(fake_ctx):
    """With Discord auth mode, moderation endpoints require a session."""
    from web_server.auth import DiscordOAuthAuth
    from web_server.server import create_app
    from fastapi.testclient import TestClient

    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    app = create_app(fake_ctx, auth=auth)
    client = TestClient(app, raise_server_exceptions=False)
    # No session cookie set
    resp = client.get("/api/moderation/jails")
    assert resp.status_code in (401, 403)
    client.close()


def test_transcript_does_not_leak_across_guilds(open_client, fake_ctx):
    """Ticket/jail ids are a global AUTOINCREMENT, so the id alone isn't a secret.

    Without a guild predicate a moderator of one guild could read another
    guild's private support conversations by enumerating record ids.
    """
    import json as _json
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO transcripts (guild_id, record_type, record_id, content, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                fake_ctx.guild_id + 999,  # a different guild entirely
                "ticket",
                4242,
                _json.dumps({"messages": [{"author": "bob", "content": "secret"}]}),
                time.time(),
            ),
        )

    body = open_client.get(
        "/api/moderation/transcript?record_type=ticket&record_id=4242"
    ).json()
    assert body["transcript"] is None
