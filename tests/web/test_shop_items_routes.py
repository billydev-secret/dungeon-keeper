"""Tests for /api/economy/shop-items and /shop-orders — custom shop items.

The dashboard half of docs/plans/economy-shop-items.md: the item editor's
validation reaching the client as 400 rather than 500, the delete guard that
protects escrowed money, snowflake precision on the granted role, and the
order queue's refund being exactly-once.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import EconSettings, apply_credit
from bot_modules.services.economy_shop_items_service import create_item, purchase
from web_server.auth import SESSION_COOKIE, DiscordOAuthAuth
from web_server.server import create_app


@pytest.fixture
def non_admin_client(fake_ctx) -> TestClient:
    """A session that is a guild member but holds no admin bit."""
    auth = DiscordOAuthAuth("test-secret", fake_ctx.guild_id)
    client = TestClient(create_app(fake_ctx, auth=auth), raise_server_exceptions=False)
    client.cookies.set(
        SESSION_COOKIE,
        auth.create_session_cookie(
            user_id=42,
            username="rando",
            access_token="token",
            permission_bits=0,
            guild_id=fake_ctx.guild_id,
            guilds=[{"id": fake_ctx.guild_id, "name": "Test Guild", "icon": None}],
        ),
    )
    return client

USER = 4242
# Past 2^53 — a bare number here would arrive at the browser already rounded.
BIG_ROLE = 1531045313807126638


def _item_body(**over) -> dict:
    body = {
        "name": "Shoutout",
        "blurb": "a hello",
        "description": "A shoutout in the announcements channel.",
        "price": 100,
        "kind": "manual",
        "billing": "once",
        "role_id": None,
        "stock": None,
        "per_member_limit": None,
        "available_from": None,
        "available_until": None,
        "ask_note": False,
        "enabled": True,
        "sort_order": 0,
    }
    body.update(over)
    return body


# ── the item editor ────────────────────────────────────────────────


def test_create_then_list(authed_client):
    created = authed_client.post("/api/economy/shop-items", json=_item_body())
    assert created.status_code == 200
    assert created.json()["name"] == "Shoutout"

    listed = authed_client.get("/api/economy/shop-items").json()
    assert [i["name"] for i in listed] == ["Shoutout"]


def test_an_empty_guild_lists_nothing(authed_client):
    assert authed_client.get("/api/economy/shop-items").json() == []


def test_the_granted_role_survives_as_a_string(authed_client):
    """Snowflake precision: JSON numbers past 2^53 lose their tail in JS."""
    resp = authed_client.post(
        "/api/economy/shop-items",
        json=_item_body(kind="role", role_id=str(BIG_ROLE)),
    )
    assert resp.json()["role_id"] == str(BIG_ROLE)
    listed = authed_client.get("/api/economy/shop-items").json()
    assert listed[0]["role_id"] == str(BIG_ROLE)


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        pytest.param({"kind": "role", "role_id": None}, "needs a role", id="role-item-without-role"),
        pytest.param({"kind": "nonsense"}, "unknown kind", id="bad-kind"),
        pytest.param({"billing": "daily"}, "unknown billing", id="bad-billing"),
        pytest.param(
            {"available_from": 200.0, "available_until": 100.0},
            "end after it starts",
            id="backwards-window",
        ),
    ],
)
def test_bad_input_is_a_400_not_a_500(authed_client, over, fragment):
    resp = authed_client.post("/api/economy/shop-items", json=_item_body(**over))
    assert resp.status_code == 400
    assert fragment in resp.json()["detail"]


def test_a_blank_name_is_refused(authed_client):
    resp = authed_client.post("/api/economy/shop-items", json=_item_body(name=""))
    assert resp.status_code == 422


def test_patch_edits_in_place(authed_client):
    item_id = authed_client.post(
        "/api/economy/shop-items", json=_item_body()
    ).json()["id"]
    resp = authed_client.patch(
        f"/api/economy/shop-items/{item_id}",
        json=_item_body(name="Renamed", price=250),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["price"] == 250


def test_patching_a_missing_item_is_a_404(authed_client):
    resp = authed_client.patch("/api/economy/shop-items/999", json=_item_body())
    assert resp.status_code == 404


def test_delete_removes_an_unsold_item(authed_client):
    item_id = authed_client.post(
        "/api/economy/shop-items", json=_item_body()
    ).json()["id"]
    assert authed_client.delete(f"/api/economy/shop-items/{item_id}").status_code == 200
    assert authed_client.get("/api/economy/shop-items").json() == []


def test_delete_is_refused_while_an_order_is_open(authed_client, fake_ctx):
    """Deleting would strand the buyer's escrowed coins."""
    with open_db(fake_ctx.db_path) as conn:
        item_id = create_item(conn, fake_ctx.guild_id, name="Shoutout", price=100)
        apply_credit(conn, fake_ctx.guild_id, USER, 500, "grant")
        purchase(
            conn, EconSettings(enabled=True), fake_ctx.guild_id, USER, item_id
        )
    resp = authed_client.delete(f"/api/economy/shop-items/{item_id}")
    assert resp.status_code == 409
    assert "disable the item instead" in resp.json()["detail"]


def test_editing_requires_admin(non_admin_client):
    resp = non_admin_client.post("/api/economy/shop-items", json=_item_body())
    assert resp.status_code == 403


# ── the order queue ────────────────────────────────────────────────


def _order(fake_ctx, *, price=100) -> int:
    with open_db(fake_ctx.db_path) as conn:
        item_id = create_item(conn, fake_ctx.guild_id, name="Shoutout", price=price)
        apply_credit(conn, fake_ctx.guild_id, USER, 500, "grant")
        out = purchase(
            conn, EconSettings(enabled=True), fake_ctx.guild_id, USER, item_id
        )
    return out.purchase_id


def test_pending_orders_are_listed_with_the_buyer_resolved(authed_client, fake_ctx):
    _order(fake_ctx)
    data = authed_client.get("/api/economy/shop-orders").json()
    assert len(data["orders"]) == 1
    order = data["orders"][0]
    assert order["item_name"] == "Shoutout"
    # A string, and never a raw integer rendered at the browser.
    assert order["user_id"] == str(USER)
    assert order["user_name"]


def test_refund_returns_the_money_and_clears_the_queue(authed_client, fake_ctx):
    order_id = _order(fake_ctx)
    resp = authed_client.post(
        f"/api/economy/shop-orders/{order_id}/refund", json={"reason": "can't do it"}
    )
    assert resp.status_code == 200
    assert resp.json()["refunded"] == 100
    assert authed_client.get("/api/economy/shop-orders").json()["orders"] == []


def test_refunding_twice_is_a_409_not_a_second_payout(authed_client, fake_ctx):
    order_id = _order(fake_ctx)
    assert authed_client.post(
        f"/api/economy/shop-orders/{order_id}/refund", json={}
    ).status_code == 200
    assert authed_client.post(
        f"/api/economy/shop-orders/{order_id}/refund", json={}
    ).status_code == 409


def test_refunding_closes_the_todo_as_missed_not_done(authed_client, fake_ctx):
    """A refunded order must never render as delivered."""
    order_id = _order(fake_ctx)
    authed_client.post(f"/api/economy/shop-orders/{order_id}/refund", json={})
    with open_db(fake_ctx.db_path) as conn:
        row = conn.execute(
            "SELECT completed_at, missed_at FROM todos ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["completed_at"] is None
    assert row["missed_at"] is not None


def test_the_order_queue_requires_admin(non_admin_client):
    assert non_admin_client.get("/api/economy/shop-orders").status_code == 403
