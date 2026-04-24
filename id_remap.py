"""ID remapping for dev/prod environment separation (spec §4.3).

In production, stored IDs are used directly. In dev, every channel/category/role
ID stored in the database refers to a prod guild entity that doesn't exist in the
test guild. This module builds a name-based mapping from prod IDs to dev IDs on
dev startup and provides a resolve_id() helper that cogs call instead of using
stored IDs directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from config import Config

log = logging.getLogger("dungeonkeeper.id_remap")

_SNAPSHOT_PATH = Path(__file__).parent / "prod_snapshot.json"


@dataclass
class RemapStats:
    matched: dict[str, int] = field(default_factory=dict)
    total: dict[str, int] = field(default_factory=dict)

    def count(self, kind: str, *, matched: bool) -> None:
        self.total[kind] = self.total.get(kind, 0) + 1
        if matched:
            self.matched[kind] = self.matched.get(kind, 0) + 1

    def report(self) -> str:
        lines = ["ID remap summary:"]
        for kind in sorted(self.total):
            m = self.matched.get(kind, 0)
            t = self.total[kind]
            flag = "" if m == t else f"  ⚠ {t - m} unmatched"
            lines.append(f"  {kind:12s} {m}/{t}{flag}")
        return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _channel_to_dict(ch: discord.abc.GuildChannel) -> dict:
    return {
        "id": ch.id,
        "name": ch.name,
        "type": "category" if isinstance(ch, discord.CategoryChannel) else "text",
        "parent_name": ch.category.name if ch.category else None,
    }


def match_channel(prod: dict, dev_channels: list[dict]) -> int | None:
    """Return the dev channel ID that best matches a prod channel descriptor."""
    # Exact: name + type + parent_name
    candidates = [
        c for c in dev_channels
        if c["name"] == prod["name"]
        and c["type"] == prod["type"]
        and c.get("parent_name") == prod.get("parent_name")
    ]
    if len(candidates) == 1:
        return candidates[0]["id"]
    if len(candidates) > 1:
        log.warning(
            "Ambiguous channel match for prod name=%r (%d candidates in dev) — skipping",
            prod["name"], len(candidates),
        )
        return None

    # Loose fallback: name + type only (parent may have been reorganised)
    fallback = [
        c for c in dev_channels
        if c["name"] == prod["name"] and c["type"] == prod["type"]
    ]
    if len(fallback) == 1:
        log.info("Loose channel match for prod name=%r (parent differs)", prod["name"])
        return fallback[0]["id"]
    return None


def match_role(prod: dict, dev_roles: list[dict]) -> int | None:
    """Return the dev role ID that matches a prod role by name."""
    candidates = [r for r in dev_roles if r["name"] == prod["name"]]
    if len(candidates) == 1:
        return candidates[0]["id"]
    if len(candidates) > 1:
        log.warning(
            "Ambiguous role match for prod name=%r (%d candidates in dev) — skipping",
            prod["name"], len(candidates),
        )
    return None


async def build_remap(
    db,
    dev_guild: discord.Guild,
    snapshot_path: str | Path = _SNAPSHOT_PATH,
) -> RemapStats:
    """Rebuild the id_remap table from prod_snapshot.json + live dev guild state."""
    path = Path(snapshot_path)
    if not path.exists():
        log.warning("prod_snapshot.json not found at %s; skipping ID remap", path)
        return RemapStats()

    snapshot = json.loads(path.read_text(encoding="utf-8"))
    exported_at = snapshot.get("exported_at", "unknown")
    log.info("Building ID remap from prod_snapshot.json (exported %s)", exported_at)

    dev_channels = [_channel_to_dict(c) for c in dev_guild.channels]
    dev_roles = [{"id": r.id, "name": r.name} for r in dev_guild.roles]

    stats = RemapStats()
    await db.execute("DELETE FROM id_remap")

    for prod_ch in snapshot.get("channels", []):
        dev_id = match_channel(prod_ch, dev_channels)
        await db.execute(
            "INSERT INTO id_remap (kind, prod_id, dev_id, name, parent_name, matched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("channel", prod_ch["id"], dev_id, prod_ch["name"], prod_ch.get("parent_name"), _now_iso()),
        )
        stats.count("channel", matched=dev_id is not None)
        if dev_id is None:
            log.warning("No dev match for prod channel %r (id=%d)", prod_ch["name"], prod_ch["id"])

    for prod_cat in snapshot.get("categories", []):
        dev_id = match_channel(prod_cat, dev_channels)
        await db.execute(
            "INSERT INTO id_remap (kind, prod_id, dev_id, name, parent_name, matched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("category", prod_cat["id"], dev_id, prod_cat["name"], None, _now_iso()),
        )
        stats.count("category", matched=dev_id is not None)
        if dev_id is None:
            log.warning("No dev match for prod category %r (id=%d)", prod_cat["name"], prod_cat["id"])

    for prod_role in snapshot.get("roles", []):
        dev_id = match_role(prod_role, dev_roles)
        await db.execute(
            "INSERT INTO id_remap (kind, prod_id, dev_id, name, parent_name, matched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("role", prod_role["id"], dev_id, prod_role["name"], None, _now_iso()),
        )
        stats.count("role", matched=dev_id is not None)
        if dev_id is None:
            log.warning("No dev match for prod role %r (id=%d)", prod_role["name"], prod_role["id"])

    # Bot user: always remap prod bot ID → live dev bot user ID
    prod_bot_id = snapshot.get("bot_user_id")
    if prod_bot_id:
        await db.execute(
            "INSERT INTO id_remap (kind, prod_id, dev_id, name, parent_name, matched_at) "
            "VALUES ('bot_user', ?, ?, 'bot', NULL, ?)",
            (prod_bot_id, dev_guild.me.id, _now_iso()),
        )
        stats.count("bot_user", matched=True)

    await db.commit()
    log.info("%s", stats.report())
    return stats


async def resolve_id(db, kind: str, stored_id: int, cfg: "Config") -> int | None:
    """Resolve a stored prod ID to a dev ID, or return it unchanged in prod.

    Returns None if no mapping exists (feature should degrade gracefully).
    """
    if cfg.is_prod:
        return stored_id
    row = await db.fetchone(
        "SELECT dev_id FROM id_remap WHERE kind=? AND prod_id=?",
        (kind, stored_id),
    )
    if row is None or row["dev_id"] is None:
        log.warning("No dev remap for %s id=%s", kind, stored_id)
        return None
    return row["dev_id"]
