"""DM permission service — DB layer and pure helpers ported from accord_bot."""

from __future__ import annotations

import datetime
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import discord

from bot_modules.core.db_utils import open_db

ROLE_DM_OPEN = "DMs: Open"
ROLE_DM_ASK = "DMs: Ask"
ROLE_DM_CLOSED = "DMs: Closed"
DM_ROLE_NAMES = (ROLE_DM_OPEN, ROLE_DM_ASK, ROLE_DM_CLOSED)

# Canonical mode keys and their default (bot-created) role names. Guilds can
# remap any mode to a pre-existing role via ``set_dm_mode_role_ids``; the
# name-based defaults stay as a fallback so unconfigured guilds keep working.
DM_MODE_ROLE_NAMES = {
    "open": ROLE_DM_OPEN,
    "ask": ROLE_DM_ASK,
    "closed": ROLE_DM_CLOSED,
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def normalize_request_type(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in {"friend", "friend_request", "fr", "friendrequest"}:
        return "friend"
    return "dm"


def request_type_label(value: str | None) -> str:
    return "Friend Request" if normalize_request_type(value) == "friend" else "Direct Message"


def relationship_key(a: int, b: int) -> str:
    lo, hi = (a, b) if a < b else (b, a)
    return f"{lo}-{hi}"


def resolve_mode(
    member: discord.Member, role_ids: Optional[dict[str, int]] = None
) -> str:
    """Return a member's DM mode ("open" / "ask" / "closed").

    ``role_ids`` is the guild's configured mode→role-id mapping (from
    ``get_dm_mode_role_ids``). Configured IDs are checked alongside the
    default role names so a guild mid-migration (or with stale default
    roles still assigned) resolves sensibly. Open beats closed; no match
    means "ask".
    """
    ids = {r.id for r in member.roles}
    names = {r.name for r in member.roles}
    role_ids = role_ids or {}
    if role_ids.get("open") in ids or ROLE_DM_OPEN in names:
        return "open"
    if role_ids.get("closed") in ids or ROLE_DM_CLOSED in names:
        return "closed"
    return "ask"


def is_dm_mode_role(
    role: discord.Role, role_ids: Optional[dict[str, int]] = None
) -> bool:
    """True if ``role`` is one of the guild's DM-mode roles.

    Matches either the default bot-created names or the guild's configured
    role IDs (``role_ids`` as returned by ``get_dm_mode_role_ids``).
    """
    if role.name in DM_ROLE_NAMES:
        return True
    if role_ids:
        return role.id in {rid for rid in role_ids.values() if rid}
    return False


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> None:
    with open_db(db_path) as conn:
        _create_tables(conn)


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dm_consent_pairs (
            guild_id INTEGER NOT NULL,
            user_low INTEGER NOT NULL,
            user_high INTEGER NOT NULL,
            rel_type TEXT NOT NULL DEFAULT 'dm',
            reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0,
            source_msg_id INTEGER,
            source_channel_id INTEGER,
            PRIMARY KEY (guild_id, user_low, user_high)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dm_requests (
            guild_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            request_type TEXT NOT NULL DEFAULT 'dm',
            reason TEXT NOT NULL DEFAULT '',
            message_id INTEGER,
            channel_id INTEGER,
            created_at REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            PRIMARY KEY (guild_id, requester_id, target_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dm_request_channels (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dm_audit_channels (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dm_panel_settings (
            guild_id INTEGER PRIMARY KEY,
            panel_channel_id INTEGER,
            panel_message_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dm_mode_roles (
            guild_id INTEGER PRIMARY KEY,
            open_role_id INTEGER NOT NULL DEFAULT 0,
            ask_role_id INTEGER NOT NULL DEFAULT 0,
            closed_role_id INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dm_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            actor_id INTEGER,
            user_a_id INTEGER,
            user_b_id INTEGER,
            action TEXT NOT NULL,
            timestamp REAL NOT NULL,
            notes TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dm_audit_log_guild ON dm_audit_log(guild_id)"
    )


# ---------------------------------------------------------------------------
# Consent pairs
# ---------------------------------------------------------------------------

def load_consent_pairs(db_path: Path) -> dict[int, set[tuple[int, int]]]:
    """Returns {guild_id: {(a,b), (b,a), ...}} for all stored pairs."""
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT guild_id, user_low, user_high FROM dm_consent_pairs"
        ).fetchall()
    out: dict[int, set[tuple[int, int]]] = {}
    for row in rows:
        gid = int(row["guild_id"])
        a, b = int(row["user_low"]), int(row["user_high"])
        out.setdefault(gid, set())
        out[gid].add((a, b))
        out[gid].add((b, a))
    return out


def add_consent_pair(
    db_path: Path,
    guild_id: int,
    user_a: int,
    user_b: int,
    rel_type: str = "dm",
    reason: str = "",
    source_msg_id: Optional[int] = None,
    source_channel_id: Optional[int] = None,
) -> None:
    lo, hi = (user_a, user_b) if user_a < user_b else (user_b, user_a)
    now = time.time()
    with open_db(db_path) as conn:
        conn.execute("""
            INSERT INTO dm_consent_pairs
                (guild_id, user_low, user_high, rel_type, reason, created_at, source_msg_id, source_channel_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_low, user_high) DO UPDATE SET
                rel_type=excluded.rel_type,
                reason=excluded.reason,
                source_msg_id=COALESCE(excluded.source_msg_id, source_msg_id),
                source_channel_id=COALESCE(excluded.source_channel_id, source_channel_id)
        """, (guild_id, lo, hi, normalize_request_type(rel_type), reason, now, source_msg_id, source_channel_id))


def remove_consent_pair(db_path: Path, guild_id: int, user_a: int, user_b: int) -> bool:
    lo, hi = (user_a, user_b) if user_a < user_b else (user_b, user_a)
    with open_db(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM dm_consent_pairs WHERE guild_id = ? AND user_low = ? AND user_high = ?",
            (guild_id, lo, hi),
        )
    return (cur.rowcount or 0) > 0


def get_consent_pair_meta(
    db_path: Path, guild_id: int, user_a: int, user_b: int
) -> Optional[dict[str, Any]]:
    lo, hi = (user_a, user_b) if user_a < user_b else (user_b, user_a)
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT rel_type, reason, created_at, source_msg_id, source_channel_id "
            "FROM dm_consent_pairs WHERE guild_id = ? AND user_low = ? AND user_high = ?",
            (guild_id, lo, hi),
        ).fetchone()
    if not row:
        return None
    return {
        "type": normalize_request_type(row["rel_type"]),
        "reason": row["reason"] or "",
        "created_at": row["created_at"],
        "source_msg_id": row["source_msg_id"],
        "source_channel_id": row["source_channel_id"],
    }


# ---------------------------------------------------------------------------
# DM requests
# ---------------------------------------------------------------------------

def load_requests(db_path: Path) -> dict[int, dict[tuple[int, int], dict[str, Any]]]:
    """Returns {guild_id: {(requester, target): record}} for pending requests."""
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT guild_id, requester_id, target_id, request_type, reason, message_id, channel_id, created_at, status "
            "FROM dm_requests WHERE status = 'pending'"
        ).fetchall()
    out: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
    for row in rows:
        gid = int(row["guild_id"])
        out.setdefault(gid, {})
        out[gid][(int(row["requester_id"]), int(row["target_id"]))] = {
            "request_type": normalize_request_type(row["request_type"]),
            "reason": row["reason"] or "",
            "message_id": row["message_id"],
            "channel_id": row["channel_id"],
            "created_at": row["created_at"],
            "status": row["status"],
        }
    return out


def upsert_request(
    db_path: Path,
    guild_id: int,
    requester_id: int,
    target_id: int,
    request_type: str,
    reason: str,
    message_id: Optional[int],
    channel_id: Optional[int],
) -> None:
    now = time.time()
    with open_db(db_path) as conn:
        conn.execute("""
            INSERT INTO dm_requests
                (guild_id, requester_id, target_id, request_type, reason, message_id, channel_id, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(guild_id, requester_id, target_id) DO UPDATE SET
                request_type=excluded.request_type,
                reason=excluded.reason,
                message_id=excluded.message_id,
                channel_id=excluded.channel_id,
                created_at=excluded.created_at,
                status='pending'
        """, (
            guild_id, requester_id, target_id,
            normalize_request_type(request_type), reason,
            message_id, channel_id, now,
        ))


def remove_request(db_path: Path, guild_id: int, requester_id: int, target_id: int) -> bool:
    with open_db(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM dm_requests WHERE guild_id = ? AND requester_id = ? AND target_id = ?",
            (guild_id, requester_id, target_id),
        )
    return (cur.rowcount or 0) > 0


def update_request_status(
    db_path: Path, guild_id: int, requester_id: int, target_id: int, status: str
) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            "UPDATE dm_requests SET status = ? WHERE guild_id = ? AND requester_id = ? AND target_id = ?",
            (status, guild_id, requester_id, target_id),
        )


def load_request_by_message_id(
    db_path: Path, message_id: int
) -> Optional[dict[str, Any]]:
    """Look up a pending DM request by the DM message that holds its buttons.

    Used by the persistent ``AskConsentView`` to recover state when a button
    is clicked after a bot restart.
    """
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT guild_id, requester_id, target_id, request_type, reason, "
            "message_id, channel_id, created_at, status "
            "FROM dm_requests WHERE message_id = ? AND status = 'pending'",
            (message_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "guild_id": int(row["guild_id"]),
        "requester_id": int(row["requester_id"]),
        "target_id": int(row["target_id"]),
        "request_type": normalize_request_type(row["request_type"]),
        "reason": row["reason"] or "",
        "message_id": row["message_id"],
        "channel_id": row["channel_id"],
        "created_at": row["created_at"],
        "status": row["status"],
    }


def count_pending_for_requester(
    db_path: Path, guild_id: int, requester_id: int
) -> int:
    """Count outstanding pending requests this user has sent."""
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM dm_requests "
            "WHERE guild_id = ? AND requester_id = ? AND status = 'pending'",
            (guild_id, requester_id),
        ).fetchone()
    return int(row["c"]) if row else 0


def expire_stale_pending_requests(
    db_path: Path, *, max_age_seconds: int
) -> list[dict[str, Any]]:
    """Mark pending requests older than ``max_age_seconds`` as expired.

    Returns the rows that were just expired (for audit-log emission).
    """
    cutoff = time.time() - max_age_seconds
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT guild_id, requester_id, target_id, request_type, message_id "
            "FROM dm_requests WHERE status = 'pending' AND created_at < ?",
            (cutoff,),
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE dm_requests SET status = 'expired' "
                "WHERE status = 'pending' AND created_at < ?",
                (cutoff,),
            )
    return [
        {
            "guild_id": int(r["guild_id"]),
            "requester_id": int(r["requester_id"]),
            "target_id": int(r["target_id"]),
            "request_type": normalize_request_type(r["request_type"]),
            "message_id": r["message_id"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Request channels
# ---------------------------------------------------------------------------

def load_request_channels(db_path: Path) -> dict[int, int]:
    with open_db(db_path) as conn:
        rows = conn.execute("SELECT guild_id, channel_id FROM dm_request_channels").fetchall()
    return {int(r["guild_id"]): int(r["channel_id"]) for r in rows}


def set_request_channel(db_path: Path, guild_id: int, channel_id: int) -> None:
    with open_db(db_path) as conn:
        conn.execute("""
            INSERT INTO dm_request_channels (guild_id, channel_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id
        """, (guild_id, channel_id))


# ---------------------------------------------------------------------------
# Audit channels
# ---------------------------------------------------------------------------

def load_audit_channels(db_path: Path) -> dict[int, int]:
    with open_db(db_path) as conn:
        rows = conn.execute("SELECT guild_id, channel_id FROM dm_audit_channels").fetchall()
    return {int(r["guild_id"]): int(r["channel_id"]) for r in rows}


def set_audit_channel(db_path: Path, guild_id: int, channel_id: int) -> None:
    with open_db(db_path) as conn:
        conn.execute("""
            INSERT INTO dm_audit_channels (guild_id, channel_id) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id
        """, (guild_id, channel_id))


# ---------------------------------------------------------------------------
# Panel settings
# ---------------------------------------------------------------------------

def load_panel_settings(db_path: Path) -> dict[int, dict[str, Optional[int]]]:
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT guild_id, panel_channel_id, panel_message_id FROM dm_panel_settings"
        ).fetchall()
    return {
        int(r["guild_id"]): {
            "panel_channel_id": r["panel_channel_id"],
            "panel_message_id": r["panel_message_id"],
        }
        for r in rows
    }


def get_dms_config_with_conn(conn: sqlite3.Connection, guild_id: int) -> dict[str, int]:
    """Return all DM-perms config fields for one guild using an existing connection."""
    req = conn.execute(
        "SELECT channel_id FROM dm_request_channels WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    aud = conn.execute(
        "SELECT channel_id FROM dm_audit_channels WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    pan = conn.execute(
        "SELECT panel_channel_id, panel_message_id FROM dm_panel_settings WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    mode_roles = get_dm_mode_role_ids_with_conn(conn, guild_id)
    return {
        "request_channel_id": int(req["channel_id"]) if req else 0,
        "audit_channel_id": int(aud["channel_id"]) if aud else 0,
        "panel_channel_id": int(pan["panel_channel_id"]) if pan and pan["panel_channel_id"] else 0,
        "panel_message_id": int(pan["panel_message_id"]) if pan and pan["panel_message_id"] else 0,
        "open_role_id": mode_roles["open"],
        "ask_role_id": mode_roles["ask"],
        "closed_role_id": mode_roles["closed"],
    }


def get_dms_config(db_path: Path, guild_id: int) -> dict[str, int]:
    """Return all DM-perms config fields for one guild in a single connection."""
    with open_db(db_path) as conn:
        return get_dms_config_with_conn(conn, guild_id)


def set_panel_settings(
    db_path: Path, guild_id: int, panel_channel_id: Optional[int], panel_message_id: Optional[int]
) -> None:
    with open_db(db_path) as conn:
        conn.execute("""
            INSERT INTO dm_panel_settings (guild_id, panel_channel_id, panel_message_id) VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                panel_channel_id=excluded.panel_channel_id,
                panel_message_id=excluded.panel_message_id
        """, (guild_id, panel_channel_id, panel_message_id))


# ---------------------------------------------------------------------------
# Mode-role overrides
# ---------------------------------------------------------------------------

def get_dm_mode_role_ids_with_conn(
    conn: sqlite3.Connection, guild_id: int
) -> dict[str, int]:
    """Return {"open": id, "ask": id, "closed": id} (0 = use the default role)."""
    try:
        row = conn.execute(
            "SELECT open_role_id, ask_role_id, closed_role_id "
            "FROM dm_mode_roles WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Table not created yet (caller ran before the DM cog's init_db).
        row = None
    if not row:
        return {"open": 0, "ask": 0, "closed": 0}
    return {
        "open": int(row["open_role_id"] or 0),
        "ask": int(row["ask_role_id"] or 0),
        "closed": int(row["closed_role_id"] or 0),
    }


def get_dm_mode_role_ids(db_path: Path, guild_id: int) -> dict[str, int]:
    with open_db(db_path) as conn:
        return get_dm_mode_role_ids_with_conn(conn, guild_id)


def load_dm_mode_roles(db_path: Path) -> dict[int, dict[str, int]]:
    """Returns {guild_id: {"open": id, "ask": id, "closed": id}} for all guilds."""
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT guild_id, open_role_id, ask_role_id, closed_role_id FROM dm_mode_roles"
        ).fetchall()
    return {
        int(r["guild_id"]): {
            "open": int(r["open_role_id"] or 0),
            "ask": int(r["ask_role_id"] or 0),
            "closed": int(r["closed_role_id"] or 0),
        }
        for r in rows
    }


def set_dm_mode_role_ids(
    db_path: Path,
    guild_id: int,
    *,
    open_role_id: int,
    ask_role_id: int,
    closed_role_id: int,
) -> None:
    with open_db(db_path) as conn:
        conn.execute("""
            INSERT INTO dm_mode_roles (guild_id, open_role_id, ask_role_id, closed_role_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                open_role_id=excluded.open_role_id,
                ask_role_id=excluded.ask_role_id,
                closed_role_id=excluded.closed_role_id
        """, (guild_id, open_role_id, ask_role_id, closed_role_id))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def write_audit_log(
    db_path: Path,
    guild_id: int,
    action: str,
    *,
    actor_id: Optional[int] = None,
    user_a_id: Optional[int] = None,
    user_b_id: Optional[int] = None,
    notes: Optional[str] = None,
) -> None:
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO dm_audit_log (guild_id, actor_id, user_a_id, user_b_id, action, timestamp, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, actor_id, user_a_id, user_b_id, action, time.time(), notes),
        )


def get_audit_log(
    db_path: Path,
    guild_id: int,
    user_id: Optional[int] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with open_db(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM dm_audit_log WHERE guild_id = ? AND (user_a_id = ? OR user_b_id = ? OR actor_id = ?) "
                "ORDER BY timestamp DESC LIMIT ?",
                (guild_id, user_id, user_id, user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dm_audit_log WHERE guild_id = ? ORDER BY timestamp DESC LIMIT ?",
                (guild_id, limit),
            ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# DM role management
# ---------------------------------------------------------------------------

async def ensure_dm_roles(
    guild: discord.Guild, role_ids: Optional[dict[str, int]] = None
) -> dict[str, discord.Role]:
    """Return the three DM-mode roles, keyed by mode ("open"/"ask"/"closed").

    A mode with a configured role ID (``role_ids``) that resolves to an
    existing guild role uses that role. Otherwise it falls back to the
    default-named role, creating it if absent.
    """
    role_ids = role_ids or {}
    roles: dict[str, discord.Role] = {}
    for mode, name in DM_MODE_ROLE_NAMES.items():
        role: Optional[discord.Role] = None
        configured_id = role_ids.get(mode) or 0
        if configured_id:
            role = guild.get_role(configured_id)
        if role is None:
            role = discord.utils.get(guild.roles, name=name)
        if role is None:
            role = await guild.create_role(name=name, reason="DM permission system")
        roles[mode] = role
    return roles


async def set_member_dm_mode(
    member: discord.Member, mode: str, role_ids: Optional[dict[str, int]] = None
) -> None:
    """Assign exactly one DM-mode role, removing the others."""
    if mode not in DM_MODE_ROLE_NAMES:
        return
    roles = await ensure_dm_roles(member.guild, role_ids)
    to_remove = [r for m, r in roles.items() if m != mode and r in member.roles]
    to_add = roles[mode]
    if to_remove:
        await member.remove_roles(*to_remove, reason="DM mode change")
    if to_add not in member.roles:
        await member.add_roles(to_add, reason="DM mode change")


# ---------------------------------------------------------------------------
# Panel embed
# ---------------------------------------------------------------------------

def build_panel_embed(
    color: "discord.Color | None" = None,
    role_names: Optional[dict[str, str]] = None,
) -> discord.Embed:
    if color is None:
        color = discord.Color.blurple()
    names = {**DM_MODE_ROLE_NAMES, **(role_names or {})}
    embed = discord.Embed(
        title="📬 DM Request System",
        description=(
            "Want to reach out to someone privately? Use the button below to send them a request first.\n\n"
            "Requests are delivered straight to their DMs — nothing gets posted publicly here."
        ),
        color=color,
    )
    embed.add_field(
        name="👤 DM Status Roles",
        value=(
            "Every member has a status that controls who can reach them. "
            "You can see someone's preference right on their profile as a role:\n\n"
            f"🟢 **{names['open']}** — Anyone can message them freely\n"
            f"🟡 **{names['ask']}** — They want to approve requests first\n"
            f"🔴 **{names['closed']}** — Not accepting requests right now\n\n"
            "Set your own preference with `/dm_set_mode`."
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 How to Send a Request",
        value=(
            "1. Hit **Open DM Request Form** below\n"
            "2. Pick the person you want to reach\n"
            "3. Choose the request type\n"
            "4. Optionally write a short reason\n"
            "5. Submit — they'll get a DM from this bot with Accept / Deny buttons\n\n"
            "You'll be notified in your own DMs when they respond."
        ),
        inline=False,
    )
    embed.add_field(
        name="💬 DM vs Friend Request — what's the difference?",
        value=(
            "**Direct Message** — You just want to chat with them on this server. "
            "This does *not* send a Discord friend request; it only grants permission within this community.\n\n"
            "**Friend Request** — You'd like to add them as a Discord friend, which lets you DM them "
            "outside of this server too. Choose this if you want a longer-term connection beyond just here."
        ),
        inline=False,
    )
    embed.set_footer(text="You can revoke any connection at any time with /dm_revoke.")
    return embed


# ---------------------------------------------------------------------------
# Audit posting helper
# ---------------------------------------------------------------------------

async def post_audit_event(
    guild: discord.Guild,
    audit_channel_id: Optional[int],
    message: str,
    color: "discord.Color | None" = None,
) -> None:
    if not audit_channel_id:
        return
    channel = guild.get_channel(audit_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    if color is None:
        color = discord.Color.blurple()
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    embed = discord.Embed(
        title="📜 DM Permission Audit",
        description=message,
        color=color,
    )
    embed.set_footer(text=timestamp)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass
