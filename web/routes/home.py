"""Home dashboard endpoint — aggregates live guild + DB stats into a single payload."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from services.message_store import get_known_channels_bulk, get_known_users_bulk
from web.auth import AuthenticatedUser
from web.deps import get_ctx, require_perms, run_query

router = APIRouter()


@router.get("/home")
async def home_data(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms(set())),
):
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None

    now = time.time()
    one_hour = now - 3600
    one_day = now - 86400
    one_week = now - 7 * 86400

    # ── Live guild data ──────────────────────────────────────────────
    guild_info: dict = {}
    voice_channels: list[dict] = []
    presence: dict = {"online": 0, "idle": 0, "dnd": 0, "offline": 0}

    if guild:
        guild_info = {
            "name": guild.name,
            "member_count": guild.member_count,
            "icon_url": str(guild.icon.url) if guild.icon else None,
        }
        for m in guild.members:
            if m.bot:
                continue
            status = str(m.status)
            if status in presence:
                presence[status] += 1

        # Voice channel population
        for vc in guild.voice_channels:
            members_in = [m for m in vc.members if not m.bot]
            if members_in:
                voice_channels.append({
                    "channel_name": vc.name,
                    "channel_id": str(vc.id),
                    "members": [
                        {"user_id": str(m.id), "user_name": m.display_name}
                        for m in members_in
                    ],
                })

    # Collect NSFW channel IDs from live guild cache
    nsfw_channel_ids: list[int] = []
    if guild:
        nsfw_channel_ids = [
            ch.id for ch in guild.channels if getattr(ch, "nsfw", False)
        ]

    # ── DB queries ───────────────────────────────────────────────────
    def _q():
        with ctx.open_db() as conn:
            # Message counts
            msgs_1h = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND ts >= ?",
                (ctx.guild_id, int(one_hour)),
            ).fetchone()[0]

            msgs_24h = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND ts >= ?",
                (ctx.guild_id, int(one_day)),
            ).fetchone()[0]

            # Hourly message counts for last 24h sparkline (24 buckets)
            spark_rows = conn.execute(
                """
                SELECT CAST((ts - ?) / 3600 AS INTEGER) AS bucket, COUNT(*) AS cnt
                FROM messages
                WHERE guild_id = ? AND ts >= ?
                GROUP BY bucket ORDER BY bucket
                """,
                (int(one_day), ctx.guild_id, int(one_day)),
            ).fetchall()
            spark_map = {int(r[0]): int(r[1]) for r in spark_rows}
            msg_sparkline = [spark_map.get(i, 0) for i in range(24)]

            # NSFW channel message counts + sparkline
            nsfw_1h = 0
            nsfw_24h = 0
            nsfw_sparkline = [0] * 24
            nsfw_unique = 0
            if nsfw_channel_ids:
                placeholders = ",".join("?" * len(nsfw_channel_ids))
                nsfw_1h = conn.execute(
                    f"SELECT COUNT(*) FROM messages WHERE guild_id = ? AND ts >= ? AND channel_id IN ({placeholders})",
                    [ctx.guild_id, int(one_hour)] + nsfw_channel_ids,
                ).fetchone()[0]
                nsfw_24h = conn.execute(
                    f"SELECT COUNT(*) FROM messages WHERE guild_id = ? AND ts >= ? AND channel_id IN ({placeholders})",
                    [ctx.guild_id, int(one_day)] + nsfw_channel_ids,
                ).fetchone()[0]
                nsfw_spark_rows = conn.execute(
                    f"""
                    SELECT CAST((ts - ?) / 3600 AS INTEGER) AS bucket, COUNT(*) AS cnt
                    FROM messages
                    WHERE guild_id = ? AND ts >= ? AND channel_id IN ({placeholders})
                    GROUP BY bucket ORDER BY bucket
                    """,
                    [int(one_day), ctx.guild_id, int(one_day)] + nsfw_channel_ids,
                ).fetchall()
                nsfw_spark_map = {int(r[0]): int(r[1]) for r in nsfw_spark_rows}
                nsfw_sparkline = [nsfw_spark_map.get(i, 0) for i in range(24)]
                nsfw_unique = conn.execute(
                    f"SELECT COUNT(DISTINCT author_id) FROM messages WHERE guild_id = ? AND ts >= ? AND channel_id IN ({placeholders})",
                    [ctx.guild_id, int(one_day)] + nsfw_channel_ids,
                ).fetchone()[0]

            # Top 5 channels last hour
            top_channels_rows = conn.execute(
                """
                SELECT channel_id, COUNT(*) AS cnt
                FROM messages
                WHERE guild_id = ? AND ts >= ?
                GROUP BY channel_id ORDER BY cnt DESC LIMIT 5
                """,
                (ctx.guild_id, int(one_hour)),
            ).fetchall()
            top_channels = [
                {"channel_id": str(r[0]), "channel_name": "", "count": int(r[1])}
                for r in top_channels_rows
            ]

            # Top 5 active users last hour
            top_users_rows = conn.execute(
                """
                SELECT author_id, COUNT(*) AS cnt
                FROM messages
                WHERE guild_id = ? AND ts >= ?
                GROUP BY author_id ORDER BY cnt DESC LIMIT 5
                """,
                (ctx.guild_id, int(one_hour)),
            ).fetchall()
            top_users = [
                {"user_id": str(r[0]), "user_name": "", "count": int(r[1])}
                for r in top_users_rows
            ]

            # Recent joins (last 7 days)
            recent_joins = conn.execute(
                "SELECT COUNT(*) FROM invite_edges WHERE guild_id = ? AND joined_at >= ?",
                (ctx.guild_id, one_week),
            ).fetchone()[0]

            # XP earned today
            xp_today = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM xp_events WHERE guild_id = ? AND created_at >= ?",
                (ctx.guild_id, one_day),
            ).fetchone()[0]

            # Unique users who earned XP today
            xp_users_today = conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM xp_events WHERE guild_id = ? AND created_at >= ?",
                (ctx.guild_id, one_day),
            ).fetchone()[0]

            # Moderation snapshot
            active_jails = conn.execute(
                "SELECT COUNT(*) FROM jails WHERE guild_id = ? AND status = 'active'",
                (ctx.guild_id,),
            ).fetchone()[0]

            open_tickets = conn.execute(
                "SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'open'",
                (ctx.guild_id,),
            ).fetchone()[0]

            active_warnings = conn.execute(
                "SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND revoked = 0",
                (ctx.guild_id,),
            ).fetchone()[0]

            # Recent moderation actions (last 5)
            recent_actions = conn.execute(
                """
                SELECT action, actor_id, target_id, created_at
                FROM audit_log
                WHERE guild_id = ?
                ORDER BY created_at DESC LIMIT 5
                """,
                (ctx.guild_id,),
            ).fetchall()
            actions_list = [
                {
                    "action": r["action"],
                    "actor_id": str(r["actor_id"]),
                    "actor_name": "",
                    "target_id": str(r["target_id"]) if r["target_id"] else None,
                    "target_name": "",
                    "created_at": r["created_at"],
                }
                for r in recent_actions
            ]

            # Unique active users today
            unique_today = conn.execute(
                "SELECT COUNT(DISTINCT author_id) FROM messages WHERE guild_id = ? AND ts >= ?",
                (ctx.guild_id, int(one_day)),
            ).fetchone()[0]

            # Resolve names
            all_user_ids: set[int] = set()
            for u in top_users:
                all_user_ids.add(int(u["user_id"]))
            for a in actions_list:
                all_user_ids.add(int(a["actor_id"]))
                if a["target_id"]:
                    all_user_ids.add(int(a["target_id"]))
            all_channel_ids = [int(c["channel_id"]) for c in top_channels]

            # Resolve via known tables
            user_names: dict[int, str] = {}
            channel_names: dict[int, str] = {}
            if all_user_ids:
                user_names = get_known_users_bulk(conn, ctx.guild_id, list(all_user_ids))
            if all_channel_ids:
                channel_names = get_known_channels_bulk(conn, ctx.guild_id, all_channel_ids)

            return {
                "msgs_1h": msgs_1h,
                "msgs_24h": msgs_24h,
                "msg_sparkline": msg_sparkline,
                "nsfw_1h": nsfw_1h,
                "nsfw_24h": nsfw_24h,
                "nsfw_sparkline": nsfw_sparkline,
                "nsfw_unique": nsfw_unique,
                "top_channels": top_channels,
                "top_users": top_users,
                "recent_joins": recent_joins,
                "xp_today": round(xp_today, 1),
                "xp_users_today": xp_users_today,
                "active_jails": active_jails,
                "open_tickets": open_tickets,
                "active_warnings": active_warnings,
                "recent_actions": actions_list,
                "unique_today": unique_today,
                "user_names": {str(k): v for k, v in user_names.items()},
                "channel_names": {str(k): v for k, v in channel_names.items()},
            }

    db_data = await run_query(_q)

    # Overlay live guild names where available
    if guild:
        for u in db_data["top_users"]:
            m = guild.get_member(int(u["user_id"]))
            if m:
                u["user_name"] = m.display_name
            elif u["user_id"] in db_data["user_names"]:
                u["user_name"] = db_data["user_names"][u["user_id"]]

        for c in db_data["top_channels"]:
            ch = guild.get_channel(int(c["channel_id"]))
            if ch:
                c["channel_name"] = ch.name
            elif c["channel_id"] in db_data["channel_names"]:
                c["channel_name"] = db_data["channel_names"][c["channel_id"]]

        for a in db_data["recent_actions"]:
            m = guild.get_member(int(a["actor_id"]))
            a["actor_name"] = m.display_name if m else db_data["user_names"].get(a["actor_id"], "")
            if a["target_id"]:
                t = guild.get_member(int(a["target_id"]))
                a["target_name"] = t.display_name if t else db_data["user_names"].get(a["target_id"], "")
    else:
        for u in db_data["top_users"]:
            u["user_name"] = db_data["user_names"].get(u["user_id"], "")
        for c in db_data["top_channels"]:
            c["channel_name"] = db_data["channel_names"].get(c["channel_id"], "")
        for a in db_data["recent_actions"]:
            a["actor_name"] = db_data["user_names"].get(a["actor_id"], "")
            if a["target_id"]:
                a["target_name"] = db_data["user_names"].get(a["target_id"], "")

    # Remove bulk lookup maps from response
    del db_data["user_names"]
    del db_data["channel_names"]

    return {
        "guild": guild_info,
        "presence": presence,
        "voice_channels": voice_channels,
        **db_data,
    }
