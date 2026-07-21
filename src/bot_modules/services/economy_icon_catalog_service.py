"""Economy — the curated rentable role-icon catalog (a currency sink).

DB layer for ``econ_icon_catalog``: the per-guild set of admin-uploaded role
icons a member can rent from the perk shop, each with its own weekly price. The
catalog reuses the existing ``role_icon`` rental perk and the personal-role
projector — renting a catalog icon just points the member's
``econ_personal_roles.icon_path`` at the icon's managed file and tags the rental
with ``catalog_icon_id`` so billing reads that icon's price (see
``economy_rentals_service._price_for``).

Every function rides the caller's connection/transaction (no internal commits),
matching the other economy service modules. Discord effects and file I/O are the
caller's job.

The on-disk image lives under ``<db-parent>/econ_icon_catalog/<guild_id>/<id>.png``
(:func:`icon_catalog_dir` / :func:`icon_catalog_path`) — a stable per-icon path,
so distinct catalog icons never collide and switching icons changes the stored
``icon_path`` (which the projector's icon-switch detection relies on).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

_COLS = "id, guild_id, name, image_path, price, enabled, sort_order, created_at"


def icon_catalog_dir(db_path: Path, guild_id: int) -> Path:
    """The managed directory holding a guild's catalog icon images."""
    directory = db_path.parent / "econ_icon_catalog" / str(guild_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def icon_catalog_path(db_path: Path, guild_id: int, icon_id: int) -> Path:
    """The managed on-disk PNG path for a single catalog icon."""
    return icon_catalog_dir(db_path, guild_id) / f"{icon_id}.png"


def list_catalog(
    conn: sqlite3.Connection, guild_id: int, *, enabled_only: bool = False
) -> list[sqlite3.Row]:
    """All catalog icons for a guild, ordered for display (sort_order, id).

    ``enabled_only`` restricts to icons members may currently rent (the shop
    passes True; the admin dashboard passes False to also show disabled ones).
    """
    if enabled_only:
        return conn.execute(
            f"SELECT {_COLS} FROM econ_icon_catalog "
            "WHERE guild_id = ? AND enabled = 1 "
            "ORDER BY sort_order ASC, id ASC",
            (guild_id,),
        ).fetchall()
    return conn.execute(
        f"SELECT {_COLS} FROM econ_icon_catalog WHERE guild_id = ? "
        "ORDER BY sort_order ASC, id ASC",
        (guild_id,),
    ).fetchall()


def get_catalog_icon(
    conn: sqlite3.Connection, guild_id: int, icon_id: int
) -> sqlite3.Row | None:
    """One catalog icon, or None if it doesn't exist in this guild."""
    return conn.execute(
        f"SELECT {_COLS} FROM econ_icon_catalog WHERE guild_id = ? AND id = ?",
        (guild_id, icon_id),
    ).fetchone()


def add_catalog_icon(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    name: str,
    price: int,
    sort_order: int = 0,
) -> int:
    """Insert a catalog icon (image_path filled in afterwards) and return its id.

    ``image_path`` is left empty here: the caller learns the new id from this
    function, writes the image to :func:`icon_catalog_path`, then calls
    :func:`set_catalog_icon_image` — all in the same transaction.
    """
    cur = conn.execute(
        """
        INSERT INTO econ_icon_catalog
            (guild_id, name, image_path, price, enabled, sort_order, created_at)
        VALUES (?, ?, '', ?, 1, ?, ?)
        """,
        (guild_id, name, price, sort_order, time.time()),
    )
    return int(cur.lastrowid or 0)


def set_catalog_icon_image(
    conn: sqlite3.Connection, guild_id: int, icon_id: int, image_path: str
) -> None:
    """Record the managed image path for an icon (after the file is written)."""
    conn.execute(
        "UPDATE econ_icon_catalog SET image_path = ? WHERE guild_id = ? AND id = ?",
        (image_path, guild_id, icon_id),
    )


def update_catalog_icon(
    conn: sqlite3.Connection,
    guild_id: int,
    icon_id: int,
    *,
    name: str | None = None,
    price: int | None = None,
    enabled: bool | None = None,
    sort_order: int | None = None,
) -> sqlite3.Row | None:
    """Patch an icon's metadata; only non-None fields change. Returns the row.

    A price change takes effect for existing renters at their next renewal (the
    billing engine re-reads the current price) — it is intentionally not charged
    immediately, matching the flat perk-price semantics.
    """
    sets: list[str] = []
    params: list[object] = []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if price is not None:
        sets.append("price = ?")
        params.append(price)
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if sort_order is not None:
        sets.append("sort_order = ?")
        params.append(sort_order)
    if sets:
        params.extend((guild_id, icon_id))
        conn.execute(
            f"UPDATE econ_icon_catalog SET {', '.join(sets)} "
            "WHERE guild_id = ? AND id = ?",
            params,
        )
    return get_catalog_icon(conn, guild_id, icon_id)


def icon_in_use(conn: sqlite3.Connection, guild_id: int, icon_id: int) -> bool:
    """True when any LIVE rental (active|grace) points at this catalog icon.

    An in-use icon must not be hard-deleted — disabling it hides it from new
    renters while current renters keep what they paid for.
    """
    row = conn.execute(
        """
        SELECT 1 FROM econ_rentals
        WHERE guild_id = ? AND catalog_icon_id = ? AND state IN ('active', 'grace')
        LIMIT 1
        """,
        (guild_id, icon_id),
    ).fetchone()
    return row is not None


def delete_catalog_icon(
    conn: sqlite3.Connection, guild_id: int, icon_id: int
) -> None:
    """Delete a catalog icon row. Callers MUST check :func:`icon_in_use` first.

    Deleting only removes the row; the caller unlinks the managed image file
    (the projector self-heals a missing icon file to "no icon" on the next
    reconcile, but an in-use icon should never reach here).
    """
    conn.execute(
        "DELETE FROM econ_icon_catalog WHERE guild_id = ? AND id = ?",
        (guild_id, icon_id),
    )


def catalog_price_range(
    conn: sqlite3.Connection, guild_id: int
) -> tuple[int, int, int] | None:
    """(min price, max price, count) over ENABLED icons, or None if empty.

    Feeds the shop's "role icon" row so it can show a price span and how many
    icons back it, instead of a single flat price, when a catalog is set up.
    """
    row = conn.execute(
        "SELECT MIN(price) AS lo, MAX(price) AS hi, COUNT(*) AS n "
        "FROM econ_icon_catalog WHERE guild_id = ? AND enabled = 1",
        (guild_id,),
    ).fetchone()
    if row is None or row["lo"] is None:
        return None
    return int(row["lo"]), int(row["hi"]), int(row["n"])
