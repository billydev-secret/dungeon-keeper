"""Voice Master service layer — pure DB CRUD and business helpers.

Schema lives in ``migrations/005_voice_master.sql``. All functions here take a
sqlite3 Connection (matching ``db_utils.open_db``) and are sync. Discord-side
glue lives in ``cogs/voice_master_cog.py``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import discord

log = logging.getLogger("dungeonkeeper.voice_master")


# Discord enforces 2 channel edits per 10 minutes per channel.
EDIT_WINDOW_S: float = 600.0

# Discord caps voice channel names at 100 characters and a category at 50 channels.
MAX_NAME_LEN: int = 100
CATEGORY_CHANNEL_CAP: int = 50

# Default name template — used when no admin override and no saved name.
DEFAULT_NAME_TEMPLATE: str = "{display_name}'s Room"


# ---------------------------------------------------------------------------
# Per-guild config (stored in the existing `config` table under voice_master_*)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceMasterConfig:
    hub_channel_id: int
    category_id: int
    control_channel_id: int
    panel_message_id: int
    default_name_template: str
    default_user_limit: int
    default_bitrate: int
    create_cooldown_s: int
    max_per_member: int
    trust_cap: int
    block_cap: int
    owner_grace_s: int
    empty_grace_s: int
    trusted_prune_days: int
    disable_saves: bool
    saveable_fields: frozenset[str]
    post_inline_panel: bool


_DEFAULT_SAVEABLE = frozenset({"name", "limit", "locked", "hidden", "trusted", "blocked"})


_CONFIG_DEFAULTS: dict[str, str] = {
    "voice_master_hub_channel_id": "0",
    "voice_master_category_id": "0",
    "voice_master_control_channel_id": "0",
    "voice_master_panel_message_id": "0",
    "voice_master_default_name_template": DEFAULT_NAME_TEMPLATE,
    "voice_master_default_user_limit": "0",
    "voice_master_default_bitrate": "0",
    "voice_master_create_cooldown_s": "30",
    "voice_master_max_per_member": "1",
    "voice_master_trust_cap": "25",
    "voice_master_block_cap": "25",
    "voice_master_owner_grace_s": "300",
    "voice_master_empty_grace_s": "15",
    "voice_master_trusted_prune_days": "0",
    "voice_master_disable_saves": "0",
    "voice_master_saveable_fields": "name,limit,locked,hidden,trusted,blocked",
    "voice_master_post_inline_panel": "1",
}


def _parse_int(raw: str, default: int) -> int:
    s = raw.strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        return default


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_voice_master_config(
    conn: sqlite3.Connection, guild_id: int
) -> VoiceMasterConfig:
    """Load the full per-guild Voice Master config in one shot."""
    from db_utils import get_config_value as _get  # avoid cycle at module import

    raw: dict[str, str] = {
        key: _get(conn, key, default, guild_id)
        for key, default in _CONFIG_DEFAULTS.items()
    }
    saveable_csv = raw["voice_master_saveable_fields"]
    saveable = frozenset(
        s.strip() for s in saveable_csv.split(",") if s.strip()
    ) or _DEFAULT_SAVEABLE
    return VoiceMasterConfig(
        hub_channel_id=_parse_int(raw["voice_master_hub_channel_id"], 0),
        category_id=_parse_int(raw["voice_master_category_id"], 0),
        control_channel_id=_parse_int(raw["voice_master_control_channel_id"], 0),
        panel_message_id=_parse_int(raw["voice_master_panel_message_id"], 0),
        default_name_template=raw["voice_master_default_name_template"]
            or DEFAULT_NAME_TEMPLATE,
        default_user_limit=_parse_int(raw["voice_master_default_user_limit"], 0),
        default_bitrate=_parse_int(raw["voice_master_default_bitrate"], 0),
        create_cooldown_s=_parse_int(raw["voice_master_create_cooldown_s"], 30),
        max_per_member=_parse_int(raw["voice_master_max_per_member"], 1),
        trust_cap=_parse_int(raw["voice_master_trust_cap"], 25),
        block_cap=_parse_int(raw["voice_master_block_cap"], 25),
        owner_grace_s=_parse_int(raw["voice_master_owner_grace_s"], 300),
        empty_grace_s=_parse_int(raw["voice_master_empty_grace_s"], 15),
        trusted_prune_days=_parse_int(raw["voice_master_trusted_prune_days"], 0),
        disable_saves=_parse_bool(raw["voice_master_disable_saves"]),
        saveable_fields=saveable,
        post_inline_panel=_parse_bool(raw["voice_master_post_inline_panel"]),
    )


def set_voice_master_config_value(
    conn: sqlite3.Connection, guild_id: int, key: str, value: str
) -> None:
    """Upsert a single voice_master_* config key for the given guild."""
    if key not in _CONFIG_DEFAULTS:
        raise ValueError(f"unknown voice master config key: {key}")
    from db_utils import set_config_value as _set

    _set(conn, key, value, guild_id)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceProfile:
    """Saved per-member channel preferences."""
    saved_name: str | None
    saved_limit: int
    locked: bool
    hidden: bool
    bitrate: int | None


def default_profile() -> VoiceProfile:
    return VoiceProfile(
        saved_name=None, saved_limit=0, locked=False, hidden=False, bitrate=None
    )


@dataclass(frozen=True)
class ActiveChannel:
    channel_id: int
    guild_id: int
    owner_id: int
    created_at: float
    last_edit_at_1: float
    last_edit_at_2: float
    owner_left_at: float | None


# ---------------------------------------------------------------------------
# Active channel CRUD
# ---------------------------------------------------------------------------


def insert_active_channel(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    guild_id: int,
    owner_id: int,
    now: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO voice_master_channels
            (channel_id, guild_id, owner_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (channel_id, guild_id, owner_id, now if now is not None else time.time()),
    )


def get_active_channel(
    conn: sqlite3.Connection, channel_id: int
) -> ActiveChannel | None:
    row = conn.execute(
        "SELECT channel_id, guild_id, owner_id, created_at, "
        "last_edit_at_1, last_edit_at_2, owner_left_at "
        "FROM voice_master_channels WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    if row is None:
        return None
    return ActiveChannel(
        channel_id=row["channel_id"],
        guild_id=row["guild_id"],
        owner_id=row["owner_id"],
        created_at=row["created_at"],
        last_edit_at_1=row["last_edit_at_1"],
        last_edit_at_2=row["last_edit_at_2"],
        owner_left_at=row["owner_left_at"],
    )


def get_owned_channel(
    conn: sqlite3.Connection, guild_id: int, owner_id: int
) -> ActiveChannel | None:
    """Return the (single) active channel owned by this user, if any."""
    row = conn.execute(
        "SELECT channel_id, guild_id, owner_id, created_at, "
        "last_edit_at_1, last_edit_at_2, owner_left_at "
        "FROM voice_master_channels "
        "WHERE guild_id = ? AND owner_id = ? "
        "ORDER BY created_at ASC LIMIT 1",
        (guild_id, owner_id),
    ).fetchone()
    if row is None:
        return None
    return ActiveChannel(
        channel_id=row["channel_id"],
        guild_id=row["guild_id"],
        owner_id=row["owner_id"],
        created_at=row["created_at"],
        last_edit_at_1=row["last_edit_at_1"],
        last_edit_at_2=row["last_edit_at_2"],
        owner_left_at=row["owner_left_at"],
    )


def list_active_channels(
    conn: sqlite3.Connection, guild_id: int
) -> list[ActiveChannel]:
    rows = conn.execute(
        "SELECT channel_id, guild_id, owner_id, created_at, "
        "last_edit_at_1, last_edit_at_2, owner_left_at "
        "FROM voice_master_channels WHERE guild_id = ?",
        (guild_id,),
    ).fetchall()
    return [
        ActiveChannel(
            channel_id=r["channel_id"],
            guild_id=r["guild_id"],
            owner_id=r["owner_id"],
            created_at=r["created_at"],
            last_edit_at_1=r["last_edit_at_1"],
            last_edit_at_2=r["last_edit_at_2"],
            owner_left_at=r["owner_left_at"],
        )
        for r in rows
    ]


def active_channel_count(
    conn: sqlite3.Connection, guild_id: int, owner_id: int
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM voice_master_channels "
        "WHERE guild_id = ? AND owner_id = ?",
        (guild_id, owner_id),
    ).fetchone()
    return int(row["n"])


def delete_active_channel(conn: sqlite3.Connection, channel_id: int) -> None:
    conn.execute(
        "DELETE FROM voice_master_channels WHERE channel_id = ?", (channel_id,)
    )


def set_owner_left_at(
    conn: sqlite3.Connection, channel_id: int, value: float | None
) -> None:
    conn.execute(
        "UPDATE voice_master_channels SET owner_left_at = ? WHERE channel_id = ?",
        (value, channel_id),
    )


def set_owner(
    conn: sqlite3.Connection, channel_id: int, new_owner_id: int
) -> None:
    conn.execute(
        "UPDATE voice_master_channels SET owner_id = ?, owner_left_at = NULL "
        "WHERE channel_id = ?",
        (new_owner_id, channel_id),
    )


def record_edit_in_db(
    conn: sqlite3.Connection, channel_id: int, *, now: float
) -> None:
    """Push ``now`` into the channel's edit-timestamp pair (FIFO)."""
    row = conn.execute(
        "SELECT last_edit_at_1, last_edit_at_2 FROM voice_master_channels "
        "WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    if row is None:
        return
    new1, new2 = record_edit(now, row["last_edit_at_1"], row["last_edit_at_2"])
    conn.execute(
        "UPDATE voice_master_channels "
        "SET last_edit_at_1 = ?, last_edit_at_2 = ? "
        "WHERE channel_id = ?",
        (new1, new2, channel_id),
    )


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------


def load_profile(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> VoiceProfile | None:
    row = conn.execute(
        "SELECT saved_name, saved_limit, locked, hidden, bitrate "
        "FROM voice_master_profiles WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return VoiceProfile(
        saved_name=row["saved_name"],
        saved_limit=int(row["saved_limit"]),
        locked=bool(row["locked"]),
        hidden=bool(row["hidden"]),
        bitrate=int(row["bitrate"]) if row["bitrate"] is not None else None,
    )


def save_profile(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    profile: VoiceProfile,
    *,
    now: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO voice_master_profiles
            (guild_id, user_id, saved_name, saved_limit, locked, hidden, bitrate, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            saved_name=excluded.saved_name,
            saved_limit=excluded.saved_limit,
            locked=excluded.locked,
            hidden=excluded.hidden,
            bitrate=excluded.bitrate,
            updated_at=excluded.updated_at
        """,
        (
            guild_id,
            user_id,
            profile.saved_name,
            profile.saved_limit,
            int(profile.locked),
            int(profile.hidden),
            profile.bitrate,
            now if now is not None else time.time(),
        ),
    )


def update_profile_field(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    field: str,
    value: object,
    now: float | None = None,
) -> None:
    """Patch one field of a member's profile, creating defaults if absent."""
    profile = load_profile(conn, guild_id, user_id) or default_profile()
    kwargs: dict[str, object] = {
        "saved_name": profile.saved_name,
        "saved_limit": profile.saved_limit,
        "locked": profile.locked,
        "hidden": profile.hidden,
        "bitrate": profile.bitrate,
    }
    if field not in kwargs:
        raise ValueError(f"unknown profile field: {field}")
    kwargs[field] = value
    save_profile(conn, guild_id, user_id, VoiceProfile(**kwargs), now=now)  # type: ignore[arg-type]


def delete_profile(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> None:
    conn.execute(
        "DELETE FROM voice_master_profiles WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


# ---------------------------------------------------------------------------
# Trust / block list CRUD (shared shape; thin wrappers below)
# ---------------------------------------------------------------------------


def _list_targets(
    conn: sqlite3.Connection, table: str, guild_id: int, owner_id: int
) -> list[int]:
    rows = conn.execute(
        f"SELECT target_id FROM {table} "
        "WHERE guild_id = ? AND owner_id = ? "
        "ORDER BY added_at ASC",
        (guild_id, owner_id),
    ).fetchall()
    return [int(r["target_id"]) for r in rows]


def _add_target_with_cap(
    conn: sqlite3.Connection,
    table: str,
    *,
    guild_id: int,
    owner_id: int,
    target_id: int,
    cap: int,
    now: float | None = None,
) -> tuple[bool, int | None]:
    """Insert a target; if the list would exceed ``cap``, evict the oldest first.

    Returns ``(added, evicted_target_id)``. ``added`` is False only if
    target was already in the list (idempotent).
    """
    exists = conn.execute(
        f"SELECT 1 FROM {table} WHERE guild_id = ? AND owner_id = ? AND target_id = ?",
        (guild_id, owner_id, target_id),
    ).fetchone()
    if exists:
        return (False, None)
    evicted: int | None = None
    if cap > 0:
        current = _list_targets(conn, table, guild_id, owner_id)
        if len(current) >= cap:
            evicted = current[0]
            conn.execute(
                f"DELETE FROM {table} "
                "WHERE guild_id = ? AND owner_id = ? AND target_id = ?",
                (guild_id, owner_id, evicted),
            )
    conn.execute(
        f"INSERT INTO {table} (guild_id, owner_id, target_id, added_at) "
        "VALUES (?, ?, ?, ?)",
        (guild_id, owner_id, target_id, now if now is not None else time.time()),
    )
    return (True, evicted)


def _remove_target(
    conn: sqlite3.Connection,
    table: str,
    *,
    guild_id: int,
    owner_id: int,
    target_id: int,
) -> bool:
    cur = conn.execute(
        f"DELETE FROM {table} "
        "WHERE guild_id = ? AND owner_id = ? AND target_id = ?",
        (guild_id, owner_id, target_id),
    )
    return cur.rowcount > 0


def list_trusted(
    conn: sqlite3.Connection, guild_id: int, owner_id: int
) -> list[int]:
    return _list_targets(conn, "voice_master_trusted", guild_id, owner_id)


def add_trusted(
    conn: sqlite3.Connection,
    guild_id: int,
    owner_id: int,
    target_id: int,
    *,
    cap: int = 25,
    now: float | None = None,
) -> tuple[bool, int | None]:
    return _add_target_with_cap(
        conn,
        "voice_master_trusted",
        guild_id=guild_id,
        owner_id=owner_id,
        target_id=target_id,
        cap=cap,
        now=now,
    )


def remove_trusted(
    conn: sqlite3.Connection, guild_id: int, owner_id: int, target_id: int
) -> bool:
    return _remove_target(
        conn,
        "voice_master_trusted",
        guild_id=guild_id,
        owner_id=owner_id,
        target_id=target_id,
    )


def list_blocked(
    conn: sqlite3.Connection, guild_id: int, owner_id: int
) -> list[int]:
    return _list_targets(conn, "voice_master_blocked", guild_id, owner_id)


def add_blocked(
    conn: sqlite3.Connection,
    guild_id: int,
    owner_id: int,
    target_id: int,
    *,
    cap: int = 25,
    now: float | None = None,
) -> tuple[bool, int | None]:
    return _add_target_with_cap(
        conn,
        "voice_master_blocked",
        guild_id=guild_id,
        owner_id=owner_id,
        target_id=target_id,
        cap=cap,
        now=now,
    )


def remove_blocked(
    conn: sqlite3.Connection, guild_id: int, owner_id: int, target_id: int
) -> bool:
    return _remove_target(
        conn,
        "voice_master_blocked",
        guild_id=guild_id,
        owner_id=owner_id,
        target_id=target_id,
    )


def remove_member_from_all_lists(
    conn: sqlite3.Connection, guild_id: int, target_id: int
) -> int:
    """Remove a member from every owner's trust + block list in this guild.

    Used when the target leaves or is banned. Returns rows affected.
    """
    n1 = conn.execute(
        "DELETE FROM voice_master_trusted WHERE guild_id = ? AND target_id = ?",
        (guild_id, target_id),
    ).rowcount
    n2 = conn.execute(
        "DELETE FROM voice_master_blocked WHERE guild_id = ? AND target_id = ?",
        (guild_id, target_id),
    ).rowcount
    return n1 + n2


# ---------------------------------------------------------------------------
# Name blocklist
# ---------------------------------------------------------------------------


def list_name_blocklist(
    conn: sqlite3.Connection, guild_id: int
) -> list[str]:
    rows = conn.execute(
        "SELECT pattern FROM voice_master_name_blocklist "
        "WHERE guild_id = ? ORDER BY pattern",
        (guild_id,),
    ).fetchall()
    return [r["pattern"] for r in rows]


def add_name_blocklist(
    conn: sqlite3.Connection,
    guild_id: int,
    pattern: str,
    added_by: int,
    *,
    now: float | None = None,
) -> bool:
    """Returns True on insert, False if pattern already present."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO voice_master_name_blocklist "
        "(guild_id, pattern, added_at, added_by) VALUES (?, ?, ?, ?)",
        (
            guild_id,
            pattern.lower(),
            now if now is not None else time.time(),
            added_by,
        ),
    )
    return cur.rowcount > 0


def remove_name_blocklist(
    conn: sqlite3.Connection, guild_id: int, pattern: str
) -> bool:
    cur = conn.execute(
        "DELETE FROM voice_master_name_blocklist "
        "WHERE guild_id = ? AND pattern = ?",
        (guild_id, pattern.lower()),
    )
    return cur.rowcount > 0


def name_is_blocked(
    name: str, patterns: list[str]
) -> bool:
    """Case-insensitive substring match against any pattern."""
    needle = name.lower()
    return any(p in needle for p in patterns if p)


# ---------------------------------------------------------------------------
# Pure helpers — edit budget
# ---------------------------------------------------------------------------


def can_edit(
    now: float, last1: float, last2: float, *, window: float = EDIT_WINDOW_S
) -> tuple[bool, float]:
    """Whether a channel edit is allowed under Discord's 2-per-window limit.

    Returns ``(allowed, retry_after_seconds)``. ``retry_after_seconds`` is 0
    when allowed.
    """
    if now - last2 >= window:
        return (True, 0.0)
    if now - last1 >= window:
        return (True, 0.0)
    # Both slots within the window — wait until the older one ages out.
    older = min(last1, last2)
    retry = window - (now - older)
    return (False, max(retry, 0.0))


def record_edit(
    now: float, last1: float, last2: float
) -> tuple[float, float]:
    """Push ``now`` into the (last1, last2) FIFO; oldest evicted.

    The returned pair preserves the invariant that the two slots hold the
    two most recent edit timestamps. Slot order itself isn't meaningful.
    """
    return (max(last1, last2), now)


# ---------------------------------------------------------------------------
# Pure helpers — name template
# ---------------------------------------------------------------------------


_TEMPLATE_TOKENS = re.compile(r"\{(display_name|username)\}")


def render_name_template(
    template: str, *, display_name: str, username: str, max_len: int = 100
) -> str:
    """Substitute ``{display_name}`` / ``{username}`` and truncate to Discord's 100-char limit."""
    def sub(m: re.Match[str]) -> str:
        return display_name if m.group(1) == "display_name" else username

    out = _TEMPLATE_TOKENS.sub(sub, template).strip()
    if not out:
        out = f"{display_name}'s Room"
    return out[:max_len]


def resolve_channel_name(
    *,
    saved_name: str | None,
    template: str,
    display_name: str,
    username: str,
    blocklist_patterns: list[str],
) -> tuple[str, bool]:
    """Pick the final channel name, falling back to the template if blocked.

    Returns ``(name, fell_back)``. ``fell_back`` is True when ``saved_name``
    would have been used but matched the blocklist and the template was
    substituted instead.
    """
    if saved_name:
        if not name_is_blocked(saved_name, blocklist_patterns):
            return (saved_name[:MAX_NAME_LEN], False)
        # Saved name is blocked — fall through to template.
        rendered = render_name_template(
            template, display_name=display_name, username=username
        )
        return (rendered, True)
    return (
        render_name_template(template, display_name=display_name, username=username),
        False,
    )


# ---------------------------------------------------------------------------
# Pure helpers — reconciliation planner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationPlan:
    """What to do at startup with each tracked channel."""
    db_to_delete: list[int]            # rows to remove from voice_master_channels
    discord_to_delete: list[int]       # voice channels to delete via API
    orphan_warnings: list[int]         # untracked channels in target category


def compute_reconciliation_actions(
    *,
    tracked_channel_ids: list[int],
    present_channel_ids: set[int],
    channels_with_humans: set[int],
    category_voice_channel_ids: set[int],
    hub_channel_id: int,
) -> ReconciliationPlan:
    """Plan startup cleanup of voice-master-tracked channels.

    Args:
        tracked_channel_ids: channel_ids currently in voice_master_channels.
        present_channel_ids: channel_ids that still exist on Discord.
        channels_with_humans: subset of present channels that have ≥1 non-bot member.
        category_voice_channel_ids: every voice channel in the target category.
        hub_channel_id: the Hub channel ID, never treated as orphan.
    """
    db_to_delete: list[int] = []
    discord_to_delete: list[int] = []
    for cid in tracked_channel_ids:
        if cid not in present_channel_ids:
            db_to_delete.append(cid)
            continue
        if cid not in channels_with_humans:
            discord_to_delete.append(cid)
            db_to_delete.append(cid)

    tracked_set = set(tracked_channel_ids)
    orphan_warnings = sorted(
        cid
        for cid in category_voice_channel_ids
        if cid != hub_channel_id and cid not in tracked_set
    )
    return ReconciliationPlan(
        db_to_delete=sorted(set(db_to_delete)),
        discord_to_delete=sorted(set(discord_to_delete)),
        orphan_warnings=orphan_warnings,
    )


# ---------------------------------------------------------------------------
# DM helper (lifted from services/wellness_enforcement.py:_try_dm)
# ---------------------------------------------------------------------------


async def trusted_prune_loop(
    bot: discord.Client, db_path: Path
) -> None:
    """Once per day, prune trust-list entries for members inactive past the configured threshold.

    Threshold is per-guild via ``voice_master_trusted_prune_days`` (0 = never).
    Activity is sourced from xp_system's member_activity table.
    """
    import asyncio
    from db_utils import open_db
    from xp_system import get_member_last_activity_map

    await bot.wait_until_ready()
    log.info("voice_master: trusted_prune_loop started")
    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                with open_db(db_path) as conn:
                    cfg = load_voice_master_config(conn, guild.id)
                    if cfg.trusted_prune_days <= 0:
                        continue
                    rows = conn.execute(
                        "SELECT DISTINCT target_id FROM voice_master_trusted "
                        "WHERE guild_id = ?",
                        (guild.id,),
                    ).fetchall()
                    target_ids = [int(r["target_id"]) for r in rows]
                    if not target_ids:
                        continue
                    activity = get_member_last_activity_map(conn, guild.id, target_ids)
                cutoff = time.time() - cfg.trusted_prune_days * 86400
                stale = [
                    uid for uid in target_ids
                    if uid not in activity or activity[uid].created_at < cutoff
                ]
                if not stale:
                    continue
                with open_db(db_path) as conn:
                    placeholders = ",".join("?" for _ in stale)
                    conn.execute(
                        f"DELETE FROM voice_master_trusted "
                        f"WHERE guild_id = ? AND target_id IN ({placeholders})",
                        [guild.id, *stale],
                    )
                log.info(
                    "voice_master: pruned %d stale trust entries in guild %d",
                    len(stale), guild.id,
                )
        except Exception:
            log.exception("voice_master: trusted_prune_loop error")
        await asyncio.sleep(86400)  # once a day


async def try_dm(
    user: "discord.User | discord.Member",
    *,
    content: str | None = None,
    embed: "discord.Embed | None" = None,
) -> bool:
    """Send a DM, swallowing Forbidden/HTTPException. Returns True on success."""
    import discord  # local import to keep this module import-light for tests

    try:
        kwargs: dict = {}
        if content:
            kwargs["content"] = content
        if embed:
            kwargs["embed"] = embed
        await user.send(**kwargs)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False
