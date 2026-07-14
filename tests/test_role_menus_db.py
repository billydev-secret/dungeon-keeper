"""DB round-trips for role menus (migration 073 + role_menus/db.py)."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.role_menus import db as menus_db
from migrations import apply_migrations_sync

GUILD = 123


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "role_menus.db"
    apply_migrations_sync(db_path)
    with open_db(db_path) as c:
        yield c


def _mk_menu(conn, title="Colors") -> int:
    return menus_db.create_menu(conn, GUILD, title, user_id=42, now=1000.0)


def test_create_and_list_menus(conn):
    mid = _mk_menu(conn)
    menus = menus_db.list_menus(conn, GUILD)
    assert [m["id"] for m in menus] == [mid]
    m = menus[0]
    assert m["title"] == "Colors"
    assert m["style"] == "buttons" and m["mode"] == "toggle"
    assert m["enabled"] and not m["channel_id"] and not m["message_id"]
    assert m["option_count"] == 0
    assert menus_db.list_menus(conn, GUILD + 1) == []


def test_update_menu_fields_roundtrip(conn):
    mid = _mk_menu(conn)
    menus_db.update_menu(
        conn, mid, title="Ping Roles", description="Pick!", accent="#ff0000",
        thumbnail_url="https://x/y.png", style="dropdown", mode="unique",
        max_roles=3, required_role_id=777, cooldown_seconds=10,
        placeholder="Choose…", user_id=43, now=2000.0,
    )
    m = menus_db.get_menu(conn, mid)
    assert m is not None
    assert m["style"] == "dropdown" and m["mode"] == "unique"
    assert m["max_roles"] == 3 and m["required_role_id"] == 777
    assert m["placeholder"] == "Choose…" and m["updated_by"] == 43


def test_replace_options_orders_and_replaces(conn):
    mid = _mk_menu(conn)
    menus_db.replace_options(conn, mid, [
        {"role_id": 11, "label": "Red"},
        {"role_id": 22, "label": "Blue", "emoji": "🔵", "button_color": "primary"},
    ], now=1500.0)
    opts = menus_db.list_options(conn, mid)
    assert [(o["role_id"], o["position"]) for o in opts] == [(11, 0), (22, 1)]
    assert opts[1]["emoji"] == "🔵" and opts[1]["button_color"] == "primary"

    # Wholesale replace: reordered + dropped rows really go away.
    menus_db.replace_options(conn, mid, [{"role_id": 22, "label": "Blue"}], now=1600.0)
    opts = menus_db.list_options(conn, mid)
    assert [o["role_id"] for o in opts] == [22]


def test_grants_history_survives_menu_deletion(conn):
    mid = _mk_menu(conn)
    menus_db.replace_options(conn, mid, [{"role_id": 11, "label": "Red"}], now=1.0)
    menus_db.record_grants(conn, mid, GUILD, 555, [(11, "grant"), (22, "remove")], 2.0)
    menus_db.delete_menu(conn, mid)

    assert menus_db.get_menu(conn, mid) is None
    assert menus_db.list_options(conn, mid) == []
    rows = conn.execute(
        "SELECT role_id, action FROM role_menu_grants WHERE menu_id = ?", (mid,)
    ).fetchall()
    assert [(r["role_id"], r["action"]) for r in rows] == [(11, "grant"), (22, "remove")]


def test_binding_first_pick_wins(conn):
    mid = _mk_menu(conn)
    assert menus_db.get_binding(conn, mid, 555) is None
    menus_db.set_binding(conn, mid, 555, 11, 1.0)
    # A second write must not overwrite the permanent pick.
    menus_db.set_binding(conn, mid, 555, 22, 2.0)
    assert menus_db.get_binding(conn, mid, 555) == 11


def test_bindings_die_with_the_menu(conn):
    mid = _mk_menu(conn)
    menus_db.set_binding(conn, mid, 555, 11, 1.0)
    menus_db.delete_menu(conn, mid)
    assert menus_db.get_binding(conn, mid, 555) is None


def test_publish_and_alert_stamps(conn):
    mid = _mk_menu(conn)
    menus_db.set_menu_enabled(conn, mid, False, 2.0)
    menus_db.set_menu_alerted(conn, mid, 3.0)
    m = menus_db.get_menu(conn, mid)
    assert m is not None and not m["enabled"] and m["alerted_at"] == 3.0

    # Publishing re-enables and the caller clears the alert stamp separately.
    menus_db.set_menu_published(conn, mid, 900, 901, 4.0)
    menus_db.set_menu_alerted(conn, mid, 0)
    m = menus_db.get_menu(conn, mid)
    assert m is not None
    assert m["enabled"] and m["channel_id"] == 900 and m["message_id"] == 901
    assert m["alerted_at"] == 0
