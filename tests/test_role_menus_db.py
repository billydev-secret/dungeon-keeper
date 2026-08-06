"""DB round-trips for role menus (migration 073 + role_menus/db.py)."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.role_menus import db as menus_db
from tests.db_template import migrated_db

GUILD = 123


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "role_menus.db"
    migrated_db(db_path)
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


def test_list_options_bulk_matches_the_per_menu_reads(conn):
    """The bulk helper is a drop-in for a ``list_options`` loop.

    Includes a menu with several options, one with a single option, and one
    with none — the empty menu must still appear with ``[]`` (the dashboard
    list view renders it, so dropping the key would hide the menu).
    """
    many = _mk_menu(conn, "Many")
    one = _mk_menu(conn, "One")
    none = _mk_menu(conn, "None")
    menus_db.replace_options(conn, many, [
        {"role_id": 11, "label": "Red", "emoji": "🔴", "button_color": "danger"},
        {"role_id": 22, "label": "Blue", "description": "cool", "elevated": True},
        {"role_id": 33, "label": "Green"},
    ], now=1500.0)
    menus_db.replace_options(conn, one, [{"role_id": 44, "label": "Solo"}], now=1500.0)

    ids = [many, one, none]
    bulk = menus_db.list_options_bulk(conn, ids)

    assert list(bulk) == ids  # every requested menu, in the order asked for
    assert bulk == {mid: menus_db.list_options(conn, mid) for mid in ids}
    assert bulk[none] == []
    # Ordering inside a menu is position order, not insertion/rowid order.
    assert [o["role_id"] for o in bulk[many]] == [11, 22, 33]
    assert [o["position"] for o in bulk[many]] == [0, 1, 2]
    # Row shaping (incl. the bool coercion) comes from the same shaper.
    assert bulk[many][1]["elevated"] is True and bulk[many][0]["elevated"] is False
    # Snowflakes stay ints at the DB layer; the web layer stringifies them.
    assert all(isinstance(o["role_id"], int) for o in bulk[many])


def test_list_options_bulk_issues_a_single_query(conn):
    """The whole point of the helper: one SELECT for N menus, not N."""
    ids = [_mk_menu(conn, f"m{i}") for i in range(4)]
    for mid in ids:
        menus_db.replace_options(conn, mid, [{"role_id": 11, "label": "Red"}], now=1.0)

    sql: list[str] = []
    conn.set_trace_callback(sql.append)
    try:
        menus_db.list_options_bulk(conn, ids)
    finally:
        conn.set_trace_callback(None)

    hits = [s for s in sql if "role_menu_options" in s]
    assert len(hits) == 1, hits


def test_list_options_bulk_with_no_menus_runs_no_query(conn):
    sql: list[str] = []
    conn.set_trace_callback(sql.append)
    try:
        assert menus_db.list_options_bulk(conn, []) == {}
    finally:
        conn.set_trace_callback(None)
    assert sql == []


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
