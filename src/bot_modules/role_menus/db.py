"""CRUD for role menus, options, grant history, and binding picks.

All functions take an open ``sqlite3.Connection`` (via ``ctx.open_db()``) and
are synchronous — callers wrap them in ``asyncio.to_thread`` / ``run_query``.
The schema lives in ``migrations/073_role_menus.sql``.

Options are replaced wholesale on save (positions from array order); the
append-only ``role_menu_grants`` history references role ids and outlives both
options and menus.
"""

from __future__ import annotations

import sqlite3

STYLES = ("buttons", "dropdown")
MODES = ("toggle", "unique", "verify", "drop", "binding")
BUTTON_COLORS = ("secondary", "primary", "success", "danger")

MAX_OPTIONS = 25  # Discord ceiling: 25 buttons (5x5) / 25 select options
TITLE_MAX_LEN = 256
DESCRIPTION_MAX_LEN = 4000
LABEL_MAX_LEN = 80  # button-label limit (select allows 100; keep one number)
OPTION_DESC_MAX_LEN = 100
PLACEHOLDER_MAX_LEN = 150


def _menu_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "guild_id": r["guild_id"],
        "title": r["title"],
        "description": r["description"],
        "accent": r["accent"],
        "thumbnail_url": r["thumbnail_url"],
        "style": r["style"],
        "mode": r["mode"],
        "max_roles": r["max_roles"],
        "required_role_id": r["required_role_id"],
        "cooldown_seconds": r["cooldown_seconds"],
        "placeholder": r["placeholder"],
        "enabled": bool(r["enabled"]),
        "channel_id": r["channel_id"],
        "message_id": r["message_id"],
        "alerted_at": r["alerted_at"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "updated_by": r["updated_by"],
    }


def _option_row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "menu_id": r["menu_id"],
        "role_id": r["role_id"],
        "label": r["label"],
        "emoji": r["emoji"],
        "description": r["description"],
        "button_color": r["button_color"],
        "position": r["position"],
        "elevated": bool(r["elevated"]),
    }


# ── menus ───────────────────────────────────────────────────────────

def list_menus(conn: sqlite3.Connection, guild_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT m.*,"
        " (SELECT COUNT(*) FROM role_menu_options o WHERE o.menu_id = m.id)"
        "   AS option_count"
        " FROM role_menus m WHERE m.guild_id = ? ORDER BY m.created_at",
        (guild_id,),
    ).fetchall()
    out = []
    for r in rows:
        menu = _menu_row(r)
        menu["option_count"] = r["option_count"]
        out.append(menu)
    return out


def get_menu(conn: sqlite3.Connection, menu_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM role_menus WHERE id = ?", (menu_id,)).fetchone()
    return _menu_row(r) if r else None


def create_menu(
    conn: sqlite3.Connection, guild_id: int, title: str, user_id: int, now: float
) -> int:
    cur = conn.execute(
        "INSERT INTO role_menus (guild_id, title, created_at, updated_at, updated_by)"
        " VALUES (?, ?, ?, ?, ?)",
        (guild_id, title, now, now, user_id),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def update_menu(
    conn: sqlite3.Connection,
    menu_id: int,
    *,
    title: str,
    description: str,
    accent: str,
    thumbnail_url: str,
    style: str,
    mode: str,
    max_roles: int,
    required_role_id: int,
    cooldown_seconds: int,
    placeholder: str,
    user_id: int,
    now: float,
) -> None:
    conn.execute(
        "UPDATE role_menus SET title = ?, description = ?, accent = ?,"
        " thumbnail_url = ?, style = ?, mode = ?, max_roles = ?,"
        " required_role_id = ?, cooldown_seconds = ?, placeholder = ?,"
        " updated_at = ?, updated_by = ? WHERE id = ?",
        (
            title, description, accent, thumbnail_url, style, mode, max_roles,
            required_role_id, cooldown_seconds, placeholder, now, user_id, menu_id,
        ),
    )
    conn.commit()


def set_menu_published(
    conn: sqlite3.Connection, menu_id: int, channel_id: int, message_id: int, now: float
) -> None:
    conn.execute(
        "UPDATE role_menus SET channel_id = ?, message_id = ?, enabled = 1,"
        " updated_at = ? WHERE id = ?",
        (channel_id, message_id, now, menu_id),
    )
    conn.commit()


def set_menu_enabled(
    conn: sqlite3.Connection, menu_id: int, enabled: bool, now: float
) -> None:
    conn.execute(
        "UPDATE role_menus SET enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, now, menu_id),
    )
    conn.commit()


def set_menu_alerted(conn: sqlite3.Connection, menu_id: int, ts: float) -> None:
    """Record (ts > 0) or clear (ts == 0) the degradation-alert dedupe stamp."""
    conn.execute(
        "UPDATE role_menus SET alerted_at = ? WHERE id = ?", (ts, menu_id)
    )
    conn.commit()


def delete_menu(conn: sqlite3.Connection, menu_id: int) -> None:
    """Delete a menu, its options, and its binding picks. Grants history stays."""
    conn.execute("DELETE FROM role_menu_options WHERE menu_id = ?", (menu_id,))
    conn.execute("DELETE FROM role_menu_bindings WHERE menu_id = ?", (menu_id,))
    conn.execute("DELETE FROM role_menus WHERE id = ?", (menu_id,))
    conn.commit()


# ── options ─────────────────────────────────────────────────────────

def list_options(conn: sqlite3.Connection, menu_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM role_menu_options WHERE menu_id = ? ORDER BY position",
        (menu_id,),
    ).fetchall()
    return [_option_row(r) for r in rows]


def get_option(conn: sqlite3.Connection, option_id: int) -> dict | None:
    r = conn.execute(
        "SELECT * FROM role_menu_options WHERE id = ?", (option_id,)
    ).fetchone()
    return _option_row(r) if r else None


def replace_options(
    conn: sqlite3.Connection, menu_id: int, options: list[dict], now: float
) -> None:
    """Replace the menu's options wholesale; ``options`` order sets positions."""
    conn.execute("DELETE FROM role_menu_options WHERE menu_id = ?", (menu_id,))
    conn.executemany(
        "INSERT INTO role_menu_options"
        " (menu_id, role_id, label, emoji, description, button_color, position, elevated)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                menu_id,
                int(o["role_id"]),
                o.get("label", ""),
                o.get("emoji", ""),
                o.get("description", ""),
                o.get("button_color", "secondary"),
                pos,
                1 if o.get("elevated") else 0,
            )
            for pos, o in enumerate(options)
        ],
    )
    conn.execute(
        "UPDATE role_menus SET updated_at = ? WHERE id = ?", (now, menu_id)
    )
    conn.commit()


# ── grant history + bindings ────────────────────────────────────────

def record_grants(
    conn: sqlite3.Connection,
    menu_id: int,
    guild_id: int,
    user_id: int,
    changes: list[tuple[int, str]],  # (role_id, "grant" | "remove")
    now: float,
) -> None:
    if not changes:
        return
    conn.executemany(
        "INSERT INTO role_menu_grants (menu_id, guild_id, user_id, role_id, action,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [(menu_id, guild_id, user_id, rid, action, now) for rid, action in changes],
    )
    conn.commit()


def get_binding(conn: sqlite3.Connection, menu_id: int, user_id: int) -> int | None:
    r = conn.execute(
        "SELECT role_id FROM role_menu_bindings WHERE menu_id = ? AND user_id = ?",
        (menu_id, user_id),
    ).fetchone()
    return int(r["role_id"]) if r else None


def set_binding(
    conn: sqlite3.Connection, menu_id: int, user_id: int, role_id: int, now: float
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO role_menu_bindings (menu_id, user_id, role_id,"
        " created_at) VALUES (?, ?, ?, ?)",
        (menu_id, user_id, role_id, now),
    )
    conn.commit()
