"""Which roles the bot made, and which it adopted.

Before this table existed (migration 203) every state the dashboard showed
about a bot-managed role was an *inference*: the stored id resolves, so it must
be fine; nothing resolves and a role of that name exists, so adopt it; nothing
resolves at all, so somebody must have deleted it. Two live defects came
straight out of that guesswork — a second guild inheriting a ``guild_id = 0``
dial being told a role it never had was deleted, and a dial reading "(none)"
being indistinguishable from one an unrelated whole-form save had cleared.

One row per ``(guild_id, role_key)``, written by
:mod:`bot_modules.core.role_provision` at the moment it creates or adopts, and
by nothing else. A third writer would make this a second source of truth rather
than a record of what the first one did.

**Degrade, never insist.** Every role provisioned before migration 203 has no
row here, and the DM-mode trio can never get one (its call site holds no
database handle at all — see ``ensure_dm_roles``). So a missing row means
"unknown", never "not ours", and the roster falls back to the old inference.
The table holds no personal data: guild, dial key, role id, origin, timestamp.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

#: How the bot came to be pointing at a role.
#:
#: ``created`` — it made the role. ``adopted`` — the guild already had a role
#: of the right name and the bot pointed itself at it. The difference is the
#: whole reason the table exists: it is the only thing that could ever make a
#: "delete this role" button safe to offer, and it is why "stop managing" means
#: *stop pointing at it* and leaves the role in the server.
RoleOrigin = Literal["created", "adopted"]


@dataclass(frozen=True)
class RoleProvenance:
    """One recorded provisioning act."""

    guild_id: int
    role_key: str
    role_id: int
    origin: RoleOrigin
    recorded_at: float


def record_role_provenance(
    conn: sqlite3.Connection,
    guild_id: int,
    role_key: str,
    role_id: int,
    origin: RoleOrigin,
    *,
    now: float | None = None,
) -> None:
    """Record that the bot created or adopted ``role_id`` for ``role_key``.

    Upserts: a dial repointed at a different role keeps one row, describing the
    role it points at *now*. Keeping a history here would make the table a
    second audit log, and ``write_audit`` already is one.
    """
    if not role_key or role_id <= 0:
        return
    conn.execute(
        """
        INSERT INTO bot_managed_roles
            (guild_id, role_key, role_id, origin, recorded_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (guild_id, role_key) DO UPDATE SET
            role_id = excluded.role_id,
            origin = excluded.origin,
            recorded_at = excluded.recorded_at
        """,
        (int(guild_id), role_key, int(role_id), origin,
         float(now if now is not None else time.time())),
    )


def read_role_provenance(
    conn: sqlite3.Connection, guild_id: int
) -> dict[str, RoleProvenance]:
    """Every recorded act for one guild, keyed by dial."""
    rows = conn.execute(
        "SELECT guild_id, role_key, role_id, origin, recorded_at "
        "FROM bot_managed_roles WHERE guild_id = ?",
        (int(guild_id),),
    ).fetchall()
    out: dict[str, RoleProvenance] = {}
    for row in rows:
        origin = str(row["origin"])
        out[str(row["role_key"])] = RoleProvenance(
            guild_id=int(row["guild_id"]),
            role_key=str(row["role_key"]),
            role_id=int(row["role_id"]),
            origin="adopted" if origin == "adopted" else "created",
            recorded_at=float(row["recorded_at"]),
        )
    return out


def forget_role_provenance(
    conn: sqlite3.Connection, guild_id: int, role_key: str
) -> None:
    """Drop the row for one dial — the bot is no longer pointing at anything.

    Called when an admin stops managing a role. The role itself is left in the
    server untouched; this only forgets that the bot was ever pointed at it, so
    a later re-adoption is recorded as the fresh act it is.
    """
    conn.execute(
        "DELETE FROM bot_managed_roles WHERE guild_id = ? AND role_key = ?",
        (int(guild_id), role_key),
    )
