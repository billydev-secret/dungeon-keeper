"""Economy — the curated rentable colour palette (a currency sink).

DB layer for ``econ_color_catalog``: the per-guild set of admin-curated gradient
colours a member rents from the perk shop via the ``role_preset`` perk. It is
the icon catalog's sibling (see ``economy_icon_catalog_service``) and works the
same way — renting points the member's ``econ_personal_roles.color``/``color2``
at the chosen pair and tags the rental with ``catalog_color_id`` so billing
reads that colour's price (``economy_rentals_service._price_for``).

The palette was the booster cosmetic-role picker until migration 159, where it
stopped being a boost entitlement and became a shop good. Two things survive
from that era and are load-bearing:

* ``key`` is the old ``booster_roles.role_key``. Buttons on already-posted
  panels carry ``custom_id`` ``booster_role:<key>``, so resolving by key keeps
  those panels working with no repost.
* ``legacy_role_id`` records the Discord role each colour used to grant. The 15
  members wearing one keep it permanently and free — nothing here grants or
  revokes it, and ``sync_palette`` refuses to delete a role that still has
  wearers. The colours a member rents now project onto their personal role,
  which outranks the legacy role, so a lapse uncovers the old colour again
  rather than leaving them bare.

A colour is only rentable with both hexes present: the pair is parsed from the
swatch filename, so a row whose filename didn't parse would otherwise offer a
colour that can't be projected.

Every function rides the caller's connection/transaction (no internal commits),
matching the other economy service modules. Discord effects and file I/O are the
caller's job.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

_COLS = (
    "id, guild_id, key, name, hex1, hex2, image_path, price, enabled, "
    "sort_order, legacy_role_id, created_at"
)

#: A colour is rentable only when enabled AND both gradient hexes parsed.
_RENTABLE = "enabled = 1 AND hex1 <> '' AND hex2 <> ''"

_HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


def valid_hex(value: str) -> bool:
    """Whether ``value`` is a bare 6-digit hex colour (no leading ``#``)."""
    return bool(_HEX_RE.match(value or ""))


def color_ints(row: sqlite3.Row) -> tuple[int, int] | None:
    """A catalog row's ``(color, color2)`` as Discord ints, or None if unparsed.

    The projector writes these straight onto ``econ_personal_roles``; None means
    the row is not rentable and callers must refuse rather than project a
    half-set gradient.
    """
    h1, h2 = str(row["hex1"]), str(row["hex2"])
    if not (valid_hex(h1) and valid_hex(h2)):
        return None
    return int(h1, 16), int(h2, 16)


def list_catalog(
    conn: sqlite3.Connection, guild_id: int, *, rentable_only: bool = False
) -> list[sqlite3.Row]:
    """A guild's palette, ordered for display (sort_order, id).

    ``rentable_only`` restricts to colours members may currently rent (the shop
    and picker pass True; the admin dashboard passes False so it can also show
    disabled colours and ones needing a re-sync).
    """
    where = f"WHERE guild_id = ? AND {_RENTABLE}" if rentable_only else "WHERE guild_id = ?"
    return conn.execute(
        f"SELECT {_COLS} FROM econ_color_catalog {where} "
        "ORDER BY sort_order ASC, id ASC",
        (guild_id,),
    ).fetchall()


def get_catalog_color(
    conn: sqlite3.Connection, guild_id: int, color_id: int
) -> sqlite3.Row | None:
    """One palette colour by id, or None if it doesn't exist in this guild."""
    return conn.execute(
        f"SELECT {_COLS} FROM econ_color_catalog WHERE guild_id = ? AND id = ?",
        (guild_id, color_id),
    ).fetchone()


def get_catalog_color_by_key(
    conn: sqlite3.Connection, guild_id: int, key: str
) -> sqlite3.Row | None:
    """One palette colour by its stable key, or None.

    The key route exists for buttons on panels posted before migration 159,
    whose ``custom_id`` is ``booster_role:<key>``.
    """
    return conn.execute(
        f"SELECT {_COLS} FROM econ_color_catalog WHERE guild_id = ? AND key = ?",
        (guild_id, key),
    ).fetchone()


def upsert_catalog_color(
    conn: sqlite3.Connection,
    guild_id: int,
    key: str,
    *,
    name: str,
    hex1: str,
    hex2: str,
    image_path: str,
    sort_order: int,
) -> int:
    """Insert or refresh a palette colour by key; returns its id.

    Used by the swatch sync. On **insert** everything comes from the filename. On
    **update** the sync refreshes only what the file is the truth for — the
    gradient, the art, and the hue ordering that art implies.

    ``name``, ``price`` and ``enabled`` are deliberately NOT touched on an update:
    all three are editable per row on the dashboard, and a re-sync that reset them
    would silently undo an admin's work. (Production's labels arrived lowercase —
    ``dusk ember`` — so renaming is a thing people will actually do.) The filename
    still seeds the name for a brand-new colour.
    """
    conn.execute(
        """
        INSERT INTO econ_color_catalog
            (guild_id, key, name, hex1, hex2, image_path, price, enabled,
             sort_order, legacy_role_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, 0, ?)
        ON CONFLICT(guild_id, key) DO UPDATE SET
            hex1       = excluded.hex1,
            hex2       = excluded.hex2,
            image_path = excluded.image_path,
            sort_order = excluded.sort_order
        """,
        (
            guild_id, key, name, hex1.upper(), hex2.upper(), image_path,
            sort_order, time.time(),
        ),
    )
    row = get_catalog_color_by_key(conn, guild_id, key)
    return int(row["id"]) if row is not None else 0


def update_catalog_color(
    conn: sqlite3.Connection,
    guild_id: int,
    color_id: int,
    *,
    name: str | None = None,
    price: int | None = None,
    enabled: bool | None = None,
    sort_order: int | None = None,
) -> sqlite3.Row | None:
    """Patch a colour's metadata; only non-None fields change. Returns the row.

    A price change takes effect for existing renters at their next renewal (the
    billing engine re-reads the current price), matching the icon catalog and the
    flat perk prices. The hexes are not patchable here: they come from the swatch
    filename, so editing them by hand would desync the art from the colour.
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
        params.extend((guild_id, color_id))
        conn.execute(
            f"UPDATE econ_color_catalog SET {', '.join(sets)} "
            "WHERE guild_id = ? AND id = ?",
            params,
        )
    return get_catalog_color(conn, guild_id, color_id)


def color_in_use(conn: sqlite3.Connection, guild_id: int, color_id: int) -> bool:
    """True when any LIVE rental (active|grace) points at this palette colour.

    An in-use colour must not be hard-deleted — disabling it hides it from new
    renters while current renters keep what they paid for.
    """
    row = conn.execute(
        """
        SELECT 1 FROM econ_rentals
        WHERE guild_id = ? AND catalog_color_id = ? AND state IN ('active', 'grace')
        LIMIT 1
        """,
        (guild_id, color_id),
    ).fetchone()
    return row is not None


def delete_catalog_color(
    conn: sqlite3.Connection, guild_id: int, color_id: int
) -> None:
    """Delete a palette row. Callers MUST check :func:`color_in_use` first.

    Only the catalog row goes; the swatch image on disk and any legacy Discord
    role are left alone (the role may still be worn by a grandfathered member).
    """
    conn.execute(
        "DELETE FROM econ_color_catalog WHERE guild_id = ? AND id = ?",
        (guild_id, color_id),
    )


def palette_size(conn: sqlite3.Connection, guild_id: int) -> int:
    """How many colours the guild currently offers (rentable ones only).

    Zero means the guild has no palette, and the shop hides the row rather than
    advertising a product with nothing behind it.
    """
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM econ_color_catalog WHERE guild_id = ? AND {_RENTABLE}",
        (guild_id,),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def catalog_price_range(
    conn: sqlite3.Connection, guild_id: int, flat_price: int
) -> tuple[int, int, int] | None:
    """(min price, max price, count) over RENTABLE colours, or None if empty.

    Feeds the shop's palette row so it can show a price span when individual
    colours are priced. A colour priced 0 bills the flat perk price, so it counts
    as ``flat_price`` here — otherwise a palette left at the default would
    advertise a span starting at zero.
    """
    rows = conn.execute(
        f"SELECT price FROM econ_color_catalog WHERE guild_id = ? AND {_RENTABLE}",
        (guild_id,),
    ).fetchall()
    if not rows:
        return None
    prices = [int(r["price"]) or flat_price for r in rows]
    return min(prices), max(prices), len(prices)
