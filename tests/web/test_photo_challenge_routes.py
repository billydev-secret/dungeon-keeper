"""Integration tests for /api/photo-challenge/* — the standalone feature that
replaced photo's slot in the shared games menu/scheduler.

The dedicated routes force game_type='photo' and pull the post channel from
config, and the shared /api/games/schedule list must hide photo rows (while the
shared loop still runs them — covered in tests/cogs/test_scheduled_games_loop.py).
"""

from __future__ import annotations

from bot_modules.core.db_utils import open_db
from bot_modules.services.scheduled_games_service import create_scheduled

BASE = "/api/photo-challenge"
GUILD = 123  # fake_ctx default guild


def _insert_row(db_path, game_type):
    with open_db(db_path) as conn:
        create_scheduled(
            conn, guild_id=GUILD, channel_id=42, game_type=game_type, options="{}",
            created_by=1, created_at=0.0, time_of_day=1200, recurrence="daily",
            recur_days=None, start_date=None, next_run_at=1.0, giveup_at=None,
            announce=0, announce_role_id=None,
        )
        conn.commit()


# ── Config ────────────────────────────────────────────────────────────────────

def test_config_default(open_client):
    data = open_client.get(f"{BASE}/config").json()
    assert data == {"enabled": True, "channel_id": "", "ping_role_id": ""}


def test_config_roundtrip(open_client):
    resp = open_client.put(
        f"{BASE}/config",
        json={"channel_id": "555", "ping_role_id": "777", "enabled": False},
    )
    assert resp.status_code == 200
    data = open_client.get(f"{BASE}/config").json()
    assert data == {"enabled": False, "channel_id": "555", "ping_role_id": "777"}


def test_config_clears_ping_role_with_zero(open_client):
    open_client.put(f"{BASE}/config", json={"channel_id": "5", "ping_role_id": "0"})
    assert open_client.get(f"{BASE}/config").json()["ping_role_id"] == ""


def test_config_rejects_non_numeric_channel(open_client):
    resp = open_client.put(f"{BASE}/config", json={"channel_id": "not-a-number"})
    assert resp.status_code == 400


# ── Schedule ──────────────────────────────────────────────────────────────────

def test_schedule_create_requires_a_channel(open_client):
    resp = open_client.post(f"{BASE}/schedule", json={"recurrence": "daily", "time": "20:00"})
    assert resp.status_code == 400


def test_schedule_create_forces_type_and_config_channel(open_client):
    open_client.put(f"{BASE}/config", json={"channel_id": "9999"})
    resp = open_client.post(f"{BASE}/schedule", json={"recurrence": "daily", "time": "20:00"})
    assert resp.status_code == 200
    rows = open_client.get(f"{BASE}/schedule").json()
    assert len(rows) == 1
    assert rows[0]["game_type"] == "photo"
    assert str(rows[0]["channel_id"]) == "9999"


def test_config_channel_change_propagates_to_schedules(open_client):
    open_client.put(f"{BASE}/config", json={"channel_id": "111"})
    open_client.post(f"{BASE}/schedule", json={"recurrence": "daily", "time": "20:00"})
    open_client.put(f"{BASE}/config", json={"channel_id": "222"})
    rows = open_client.get(f"{BASE}/schedule").json()
    assert str(rows[0]["channel_id"]) == "222"


def test_schedule_weekly_requires_days(open_client):
    open_client.put(f"{BASE}/config", json={"channel_id": "9999"})
    resp = open_client.post(f"{BASE}/schedule", json={"recurrence": "weekly", "time": "20:00"})
    assert resp.status_code == 400


def test_schedule_pause_resume_delete(open_client):
    open_client.put(f"{BASE}/config", json={"channel_id": "9999"})
    sid = open_client.post(f"{BASE}/schedule", json={"recurrence": "daily", "time": "20:00"}).json()["id"]
    assert open_client.post(f"{BASE}/schedule/{sid}/pause").status_code == 200
    assert open_client.get(f"{BASE}/schedule").json()[0]["status"] == "paused"
    assert open_client.post(f"{BASE}/schedule/{sid}/resume").status_code == 200
    assert open_client.get(f"{BASE}/schedule").json()[0]["status"] == "active"
    assert open_client.delete(f"{BASE}/schedule/{sid}").status_code == 200
    assert open_client.get(f"{BASE}/schedule").json() == []


# ── Shared-scheduler isolation ────────────────────────────────────────────────

def test_shared_schedule_list_hides_photo(open_client, fake_ctx):
    _insert_row(fake_ctx.db_path, "photo")
    _insert_row(fake_ctx.db_path, "wyr")

    shared = open_client.get("/api/games/schedule").json()
    types = {r["game_type"] for r in shared}
    assert "wyr" in types and "photo" not in types

    # ...but the photo route still sees only the photo row.
    photo = open_client.get(f"{BASE}/schedule").json()
    assert {r["game_type"] for r in photo} == {"photo"}


def test_shared_schedule_rejects_creating_photo(open_client):
    # 'photo' left SCHEDULABLE_GAME_TYPES, so the shared create validates it away.
    resp = open_client.post("/api/games/schedule", json={
        "channel_id": "42", "game_type": "photo", "recurrence": "daily", "time": "20:00",
    })
    assert resp.status_code == 400
