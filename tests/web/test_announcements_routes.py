"""Integration tests for /api/announcements/* — queue, schedule, clone, history."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db, set_config_value
from web_server.auth import DiscordOAuthAuth, SESSION_COOKIE
from web_server.server import create_app

BASE = "/api/announcements"
GUILD = 123  # fake_ctx default guild


def _body(**over):
    body = {"channel_id": "42", "title": "Big news", "body": "Details"}
    body.update(over)
    return body


def _items(client):
    return client.get(BASE).json()["items"]


def _mark_sent(fake_ctx, ann_id, *, channel_id=42, message_id=777):
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "UPDATE announcements SET status='sent', sent_at=1.0, "
            "sent_channel_id=?, sent_message_id=? WHERE id=?",
            (channel_id, message_id, ann_id),
        )


# ── create ────────────────────────────────────────────────────────────────────

def test_create_draft_without_time(open_client):
    resp = open_client.post(BASE, json=_body())
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"
    items = _items(open_client)
    assert len(items) == 1
    assert items[0]["status"] == "draft"
    assert items[0]["post_at"] is None
    assert items[0]["channel_id"] == "42"  # snowflakes are strings


def test_create_scheduled_uses_guild_offset(open_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "tz_offset_hours", "-7", GUILD)
    resp = open_client.post(
        BASE, json=_body(post_date="2030-01-01", post_time="18:00")
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "scheduled"
    # 18:00 local at UTC-7 = 01:00 UTC next day.
    expected = datetime(2030, 1, 2, 1, tzinfo=timezone.utc).timestamp()
    assert data["post_at"] == expected


def test_create_past_time_rejected(open_client):
    resp = open_client.post(BASE, json=_body(post_date="2000-01-01", post_time="12:00"))
    assert resp.status_code == 400


def test_create_validation_errors(open_client):
    checks = [
        _body(post_date="2030-01-01"),                          # date without time
        _body(post_time="12:00"),                               # time without date
        _body(post_date="2030-01-01", post_time="25:99"),       # bad time
        _body(post_date="Jan 1", post_time="12:00"),            # bad date
        _body(mention_kind="here"),                             # bad kind
        _body(mention_kind="role"),                             # role without id
        _body(channel_id="not-a-number"),
        _body(title="", body=""),
        _body(title="", body="   "),
        _body(accent_hex="xyzxyz"),
        _body(accent_hex="#FFF"),
        _body(image_url="ftp://example.com/x.png"),
    ]
    for payload in checks:
        assert open_client.post(BASE, json=payload).status_code == 400, payload


def test_create_unknown_field_rejected(open_client):
    assert open_client.post(BASE, json=_body(surprise=1)).status_code == 422


def test_accent_hex_normalized(open_client):
    open_client.post(BASE, json=_body(accent_hex="#ab12cd"))
    assert _items(open_client)[0]["accent_hex"] == "AB12CD"


# ── update ────────────────────────────────────────────────────────────────────

def test_put_edits_draft(open_client):
    ann_id = open_client.post(BASE, json=_body()).json()["id"]
    resp = open_client.put(f"{BASE}/{ann_id}", json=_body(title="Updated"))
    assert resp.status_code == 200
    assert _items(open_client)[0]["title"] == "Updated"


def test_put_setting_time_schedules_and_clearing_reverts_to_draft(open_client):
    ann_id = open_client.post(BASE, json=_body()).json()["id"]
    resp = open_client.put(
        f"{BASE}/{ann_id}", json=_body(post_date="2030-01-01", post_time="18:00")
    )
    assert resp.json()["status"] == "scheduled"
    resp = open_client.put(f"{BASE}/{ann_id}", json=_body())
    assert resp.json()["status"] == "draft"
    item = _items(open_client)[0]
    assert item["post_at"] is None and item["post_date"] is None


def test_put_on_sent_row_conflicts(open_client, fake_ctx):
    ann_id = open_client.post(BASE, json=_body()).json()["id"]
    _mark_sent(fake_ctx, ann_id)
    assert open_client.put(f"{BASE}/{ann_id}", json=_body()).status_code == 409


def test_put_missing_404(open_client):
    assert open_client.put(f"{BASE}/9999", json=_body()).status_code == 404


# ── post-now / delete / clone ────────────────────────────────────────────────

def test_post_now_arms_a_draft(open_client):
    ann_id = open_client.post(BASE, json=_body()).json()["id"]
    before = time.time()
    assert open_client.post(f"{BASE}/{ann_id}/post-now").status_code == 200
    item = _items(open_client)[0]
    assert item["status"] == "scheduled"
    assert item["post_at"] >= before - 1


def test_post_now_on_sent_row_conflicts(open_client, fake_ctx):
    ann_id = open_client.post(BASE, json=_body()).json()["id"]
    _mark_sent(fake_ctx, ann_id)
    assert open_client.post(f"{BASE}/{ann_id}/post-now").status_code == 409


def test_delete(open_client):
    ann_id = open_client.post(BASE, json=_body()).json()["id"]
    assert open_client.delete(f"{BASE}/{ann_id}").status_code == 200
    assert _items(open_client) == []
    assert open_client.delete(f"{BASE}/{ann_id}").status_code == 404


def test_clone_sent_row_resets_to_draft(open_client, fake_ctx):
    ann_id = open_client.post(
        BASE,
        json=_body(
            plain_text="Hey", mention_kind="everyone", accent_hex="FF0000",
            post_date="2030-01-01", post_time="18:00",
        ),
    ).json()["id"]
    _mark_sent(fake_ctx, ann_id)

    resp = open_client.post(f"{BASE}/{ann_id}/clone")
    assert resp.status_code == 200
    new_id = resp.json()["id"]
    assert new_id != ann_id

    clone = next(i for i in _items(open_client) if i["id"] == new_id)
    assert clone["status"] == "draft"
    assert clone["title"] == "Big news"
    assert clone["plain_text"] == "Hey"
    assert clone["mention_kind"] == "everyone"
    assert clone["accent_hex"] == "FF0000"
    assert clone["post_at"] is None and clone["post_date"] is None
    assert clone["sent_at"] is None and clone["jump_url"] is None


def test_clone_missing_404(open_client):
    assert open_client.post(f"{BASE}/9999/clone").status_code == 404


# ── list shape ────────────────────────────────────────────────────────────────

def test_list_carries_tz_accent_and_jump_url(open_client, fake_ctx):
    with open_db(fake_ctx.db_path) as conn:
        set_config_value(conn, "tz_offset_hours", "-7", GUILD)
    ann_id = open_client.post(BASE, json=_body()).json()["id"]
    _mark_sent(fake_ctx, ann_id, channel_id=42, message_id=777)

    data = open_client.get(BASE).json()
    assert data["tz_offset_hours"] == -7.0
    assert data["guild_id"] == str(GUILD)
    assert len(data["default_accent_hex"]) == 6
    sent = data["items"][0]
    assert sent["jump_url"] == f"https://discord.com/channels/{GUILD}/42/777"
    assert sent["created_by"] == "0"  # OpenAuth anonymous admin, stringified


# ── auth gating ───────────────────────────────────────────────────────────────

def test_non_admin_forbidden(fake_ctx):
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = TestClient(create_app(fake_ctx, auth=auth))
    cookie = auth.create_session_cookie(
        user_id=7,
        username="mod",
        access_token="token",
        permission_bits=0x2000,  # MANAGE_MESSAGES → moderator, not admin
        guild_id=fake_ctx.guild_id,
        guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
    )
    client.cookies.set(SESSION_COOKIE, cookie)
    try:
        assert client.get(BASE).status_code == 403
        assert client.post(BASE, json=_body()).status_code == 403
    finally:
        client.close()
