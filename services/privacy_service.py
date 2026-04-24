"""Privacy data deletion — DB purge extracted from privacy_cog for testability."""

from __future__ import annotations

import sqlite3


def purge_user_data(conn: sqlite3.Connection, guild_id: int, user_id: int) -> int:
    """Delete all DB records for *user_id* in *guild_id*. Returns message count removed."""
    msg_ids = [
        r[0]
        for r in conn.execute(
            "SELECT message_id FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
        ).fetchall()
    ]

    if msg_ids:
        ph = ",".join("?" * len(msg_ids))
        for table in (
            "message_attachments",
            "message_mentions",
            "message_embeds",
            "message_reactions",
            "message_sentiment",
        ):
            conn.execute(f"DELETE FROM {table} WHERE message_id IN ({ph})", msg_ids)

        conn.execute(
            "DELETE FROM processed_messages WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        conn.execute(
            "DELETE FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
        )

    for table in (
        "member_xp",
        "voice_sessions",
        "member_activity",
        "quality_score_leaves",
        "member_gender",
        "member_events",
        "known_users",
    ):
        conn.execute(
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    conn.execute(
        "DELETE FROM xp_events WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    conn.execute(
        "DELETE FROM role_events WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )

    for col in ("from_user_id", "to_user_id"):
        conn.execute(
            f"DELETE FROM user_interactions WHERE guild_id = ? AND {col} = ?",
            (guild_id, user_id),
        )
        conn.execute(
            f"DELETE FROM user_interactions_log WHERE guild_id = ? AND {col} = ?",
            (guild_id, user_id),
        )

    for table in (
        "wellness_users",
        "wellness_caps",
        "wellness_cap_counters",
        "wellness_cap_overages",
        "wellness_blackouts",
        "wellness_blackout_overages",
        "wellness_blackout_active",
        "wellness_slow_mode",
        "wellness_streaks",
        "wellness_streak_history",
        "wellness_away_rate_limit",
        "wellness_weekly_reports",
    ):
        try:
            conn.execute(
                f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        except Exception:
            pass

    for col in ("user_id_a", "user_id_b"):
        try:
            conn.execute(
                f"DELETE FROM wellness_partners WHERE guild_id = ? AND {col} = ?",
                (guild_id, user_id),
            )
        except Exception:
            pass

    return len(msg_ids)
