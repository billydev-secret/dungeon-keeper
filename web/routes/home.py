"""Home dashboard endpoint — aggregates live guild + DB stats into a single payload."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from db_utils import get_config_value
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

    # ── Recent joins from guild cache (more reliable than invite_edges) ──
    one_month = now - 30 * 86400
    joins_1d = 0
    joins_7d = 0
    joins_30d = 0
    if guild:
        for m in guild.members:
            if not m.joined_at:
                continue
            jts = m.joined_at.timestamp()
            if jts >= one_day:
                joins_1d += 1
            if jts >= one_week:
                joins_7d += 1
            if jts >= one_month:
                joins_30d += 1
    recent_joins = joins_7d

    # ── Mod member IDs for "Returned After Break" acknowledgment check ──
    # A returning user stays on the card until any mod has replied to or
    # mentioned them after the return event. We need the set of user IDs
    # that currently have mod/admin access; role membership is only
    # available via the live guild cache.
    mod_ids: set[int] = set()
    if guild:
        with ctx.open_db() as _conn_cfg:
            _mod_raw = get_config_value(_conn_cfg, "mod_role_ids", "")
            _admin_raw = get_config_value(_conn_cfg, "admin_role_ids", "")
        _configured_mod_roles = {
            int(x) for x in (_mod_raw + "," + _admin_raw).split(",") if x.strip().isdigit()
        }
        for _m in guild.members:
            if _m.bot:
                continue
            perms = _m.guild_permissions
            if perms.administrator or perms.manage_guild:
                mod_ids.add(_m.id)
                continue
            if _configured_mod_roles & {r.id for r in _m.roles}:
                mod_ids.add(_m.id)

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

            # Users who returned after 6+ hours of inactivity — persists
            # until a moderator has replied to or mentioned them.
            #
            # Look back 7 days for "return events" (a message preceded by a
            # 6h+ gap). Use a 30-day LAG window so returns near the edge of
            # the 7-day window still see their prior message. Keep the most
            # recent return per user.
            seven_days_ago = now - 7 * 86400
            thirty_days_ago = now - 30 * 86400
            return_rows = conn.execute(
                """
                WITH recent_msgs AS (
                    SELECT author_id, ts,
                           LAG(ts) OVER (PARTITION BY author_id ORDER BY ts) AS prev_ts
                    FROM messages
                    WHERE guild_id = ? AND ts >= ?
                ),
                returns AS (
                    SELECT author_id, ts AS return_ts, (ts - prev_ts) AS gap,
                           ROW_NUMBER() OVER (
                               PARTITION BY author_id ORDER BY ts DESC
                           ) AS rn
                    FROM recent_msgs
                    WHERE prev_ts IS NOT NULL
                      AND (ts - prev_ts) >= 21600
                      AND ts >= ?
                )
                SELECT author_id, return_ts, gap
                FROM returns
                WHERE rn = 1
                ORDER BY return_ts DESC
                LIMIT 50
                """,
                (ctx.guild_id, int(thirty_days_ago), int(seven_days_ago)),
            ).fetchall()

            return_candidates = [
                {
                    "author_id": int(r["author_id"]),
                    "return_ts": int(r["return_ts"]),
                    "gap": int(r["gap"]),
                }
                for r in return_rows
            ]

            # Acknowledgment check: for each candidate, has any moderator
            # replied to or @mentioned them since their return? We batch
            # this into a single query using a VALUES CTE.
            acknowledged: set[int] = set()
            if return_candidates and mod_ids:
                values_clause = ",".join("(?, ?)" for _ in return_candidates)
                values_params: list = []
                for c in return_candidates:
                    values_params.append(c["author_id"])
                    values_params.append(c["return_ts"])
                mod_list = list(mod_ids)
                mod_placeholders = ",".join("?" for _ in mod_list)
                ack_query = f"""
                    WITH cands(user_id, return_ts) AS (VALUES {values_clause})
                    SELECT DISTINCT cands.user_id
                    FROM cands
                    WHERE EXISTS (
                        SELECT 1
                        FROM messages reply
                        JOIN messages target
                          ON reply.reply_to_id = target.message_id
                        WHERE reply.guild_id = ?
                          AND reply.author_id IN ({mod_placeholders})
                          AND target.author_id = cands.user_id
                          AND reply.ts > cands.return_ts
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM message_mentions mm
                        JOIN messages mres ON mm.message_id = mres.message_id
                        WHERE mres.guild_id = ?
                          AND mres.author_id IN ({mod_placeholders})
                          AND mm.user_id = cands.user_id
                          AND mres.ts > cands.return_ts
                    )
                """
                ack_params = (
                    values_params
                    + [ctx.guild_id]
                    + mod_list
                    + [ctx.guild_id]
                    + mod_list
                )
                ack_rows = conn.execute(ack_query, ack_params).fetchall()
                acknowledged = {int(r[0]) for r in ack_rows}

            returned_users = [
                {
                    "user_id": str(c["author_id"]),
                    "user_name": "",
                    "gap_hours": round(c["gap"] / 3600, 1),
                }
                for c in return_candidates
                if c["author_id"] not in acknowledged
            ][:15]

            # Conversation starters — first message in a channel after 30min silence
            starter_rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT author_id, ts,
                           LAG(ts) OVER (PARTITION BY channel_id ORDER BY ts) AS prev_ts
                    FROM messages
                    WHERE guild_id = ? AND ts >= ? - 1800
                )
                SELECT author_id, COUNT(*) AS starts
                FROM ranked
                WHERE ts >= ? AND (prev_ts IS NULL OR (ts - prev_ts) >= 1800)
                GROUP BY author_id
                ORDER BY starts DESC
                LIMIT 5
                """,
                (ctx.guild_id, int(one_day), int(one_day)),
            ).fetchall()
            conversation_starters = [
                {"user_id": str(r["author_id"]), "user_name": "", "starts": int(r["starts"])}
                for r in starter_rows
            ]

            # Social butterfly — users who interacted with the most unique people
            butterfly_rows = conn.execute(
                """
                SELECT from_user_id AS user_id, COUNT(DISTINCT to_user_id) AS unique_partners
                FROM user_interactions_log
                WHERE guild_id = ? AND ts >= ?
                GROUP BY from_user_id
                ORDER BY unique_partners DESC
                LIMIT 5
                """,
                (ctx.guild_id, int(one_day)),
            ).fetchall()
            social_butterflies = [
                {"user_id": str(r["user_id"]), "user_name": "", "unique": int(r["unique_partners"])}
                for r in butterfly_rows
            ]

            # Channel loyalty — users who put 80%+ of messages in one channel
            loyalty_rows = conn.execute(
                """
                WITH user_totals AS (
                    SELECT author_id, COUNT(*) AS total
                    FROM messages
                    WHERE guild_id = ? AND ts >= ?
                    GROUP BY author_id
                    HAVING total >= 10
                ),
                user_channel AS (
                    SELECT author_id, channel_id, COUNT(*) AS ch_count
                    FROM messages
                    WHERE guild_id = ? AND ts >= ?
                    GROUP BY author_id, channel_id
                )
                SELECT uc.author_id, uc.channel_id, uc.ch_count, ut.total,
                       CAST(uc.ch_count AS REAL) / ut.total AS pct
                FROM user_channel uc
                JOIN user_totals ut ON uc.author_id = ut.author_id
                WHERE CAST(uc.ch_count AS REAL) / ut.total >= 0.8
                ORDER BY ut.total DESC
                LIMIT 5
                """,
                (ctx.guild_id, int(one_day), ctx.guild_id, int(one_day)),
            ).fetchall()
            channel_loyalists = [
                {
                    "user_id": str(r["author_id"]),
                    "user_name": "",
                    "channel_id": str(r["channel_id"]),
                    "channel_name": "",
                    "pct": round(r["pct"] * 100),
                    "count": int(r["ch_count"]),
                }
                for r in loyalty_rows
            ]

            # Resolve names
            all_user_ids: set[int] = set()
            for u in top_users:
                all_user_ids.add(int(u["user_id"]))
            for ru in returned_users:
                all_user_ids.add(int(ru["user_id"]))
            for cs in conversation_starters:
                all_user_ids.add(int(cs["user_id"]))
            for sb in social_butterflies:
                all_user_ids.add(int(sb["user_id"]))
            for cl in channel_loyalists:
                all_user_ids.add(int(cl["user_id"]))
            for a in actions_list:
                all_user_ids.add(int(a["actor_id"]))
                if a["target_id"]:
                    all_user_ids.add(int(a["target_id"]))
            all_channel_ids = [int(c["channel_id"]) for c in top_channels]
            for cl in channel_loyalists:
                all_channel_ids.append(int(cl["channel_id"]))

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
                "xp_today": round(xp_today, 1),
                "xp_users_today": xp_users_today,
                "active_jails": active_jails,
                "open_tickets": open_tickets,
                "active_warnings": active_warnings,
                "recent_actions": actions_list,
                "unique_today": unique_today,
                "returned_users": returned_users,
                "conversation_starters": conversation_starters,
                "social_butterflies": social_butterflies,
                "channel_loyalists": channel_loyalists,
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
        for ru in db_data["returned_users"]:
            m = guild.get_member(int(ru["user_id"]))
            ru["user_name"] = m.display_name if m else db_data["user_names"].get(ru["user_id"], "")
        for item in db_data["conversation_starters"] + db_data["social_butterflies"] + db_data["channel_loyalists"]:
            m = guild.get_member(int(item["user_id"]))
            item["user_name"] = m.display_name if m else db_data["user_names"].get(item["user_id"], "")
        for cl in db_data["channel_loyalists"]:
            ch = guild.get_channel(int(cl["channel_id"]))
            if ch:
                cl["channel_name"] = ch.name
            elif cl["channel_id"] in db_data["channel_names"]:
                cl["channel_name"] = db_data["channel_names"][cl["channel_id"]]
    else:
        for u in db_data["top_users"]:
            u["user_name"] = db_data["user_names"].get(u["user_id"], "")
        for c in db_data["top_channels"]:
            c["channel_name"] = db_data["channel_names"].get(c["channel_id"], "")
        for a in db_data["recent_actions"]:
            a["actor_name"] = db_data["user_names"].get(a["actor_id"], "")
            if a["target_id"]:
                a["target_name"] = db_data["user_names"].get(a["target_id"], "")
        for ru in db_data["returned_users"]:
            ru["user_name"] = db_data["user_names"].get(ru["user_id"], "")
        for item in db_data["conversation_starters"] + db_data["social_butterflies"] + db_data["channel_loyalists"]:
            item["user_name"] = db_data["user_names"].get(item["user_id"], "")
        for cl in db_data["channel_loyalists"]:
            cl["channel_name"] = db_data["channel_names"].get(cl["channel_id"], "")

    # Remove bulk lookup maps from response
    del db_data["user_names"]
    del db_data["channel_names"]

    return {
        "guild": guild_info,
        "presence": presence,
        "voice_channels": voice_channels,
        "recent_joins": recent_joins,
        "joins_1d": joins_1d,
        "joins_7d": joins_7d,
        "joins_30d": joins_30d,
        "joins_avg_daily_7d": round(joins_7d / 7, 1),
        "joins_avg_daily_30d": round(joins_30d / 30, 1),
        **db_data,
    }
