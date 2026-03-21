"""Drama analysis commands.

Provides a single mod-only slash command:
  /chilling_effect — identify members whose arrival in a channel (after a gap)
                     correlates with other active members going quiet.

Uses the local message archive populated by /interaction_scan and on_message.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from reports import send_ephemeral_text

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.drama")


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def _analyze_chilling_effect(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    cutoff_ts: int,
    entry_gap_seconds: int,
    window_seconds: int,
    channel_id: int | None = None,
) -> tuple[list[tuple[int, int, float, Counter]], int, int]:
    """
    Scan message history for 'entry events' — a user posts in a channel after
    being absent from it for at least entry_gap_seconds — and measure how
    many currently-active users stop posting in the following window.

    Returns:
        results        – list of (user_id, entry_count, avg_silenced, victim_counter)
                         sorted by avg_silenced descending.
        total_entries  – total number of entry events found.
        channel_count  – number of channels analysed.
    """
    query = (
        "SELECT channel_id, author_id, ts "
        "FROM messages "
        "WHERE guild_id = ? AND ts >= ?"
    )
    params: list[object] = [guild_id, cutoff_ts]
    if channel_id is not None:
        query += " AND channel_id = ?"
        params.append(channel_id)
    query += " ORDER BY channel_id, ts ASC"

    rows = conn.execute(query, params).fetchall()

    # Group (ts, author_id) pairs by channel
    msgs_by_channel: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in rows:
        msgs_by_channel[int(row[0])].append((int(row[2]), int(row[1])))

    entry_count: Counter[int] = Counter()
    silenced_by: dict[int, Counter[int]] = defaultdict(Counter)

    for ch_msgs in msgs_by_channel.values():
        ch_msgs.sort()  # should be sorted already, but guarantee it

        last_ts_by_user: dict[int, int] = {}

        for i, (ts, author_id) in enumerate(ch_msgs):
            prev_ts = last_ts_by_user.get(author_id)
            last_ts_by_user[author_id] = ts

            # Entry condition: user was present before AND the gap is large enough.
            # First-ever message in this channel doesn't count — we have no baseline.
            if prev_ts is None or (ts - prev_ts) < entry_gap_seconds:
                continue

            # Who was active in [ts - window, ts)?  (excluding the entrant)
            active_before: set[int] = set()
            for j in range(i - 1, -1, -1):
                if ch_msgs[j][0] < ts - window_seconds:
                    break
                if ch_msgs[j][1] != author_id:
                    active_before.add(ch_msgs[j][1])

            if not active_before:
                continue  # nobody else was talking — not an interesting entry

            # Who posts in [ts, ts + window)?  (excluding the entrant)
            active_after: set[int] = set()
            for j in range(i + 1, len(ch_msgs)):
                if ch_msgs[j][0] >= ts + window_seconds:
                    break
                if ch_msgs[j][1] != author_id:
                    active_after.add(ch_msgs[j][1])

            silenced = active_before - active_after

            entry_count[author_id] += 1
            for victim in silenced:
                silenced_by[author_id][victim] += 1

    total_entries = sum(entry_count.values())
    channel_count = len(msgs_by_channel)

    results: list[tuple[int, int, float, Counter]] = [
        (uid, count, sum(silenced_by[uid].values()) / count, silenced_by[uid])
        for uid, count in entry_count.items()
    ]
    results.sort(key=lambda r: r[2], reverse=True)

    return results, total_entries, channel_count


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------

def register_drama_commands(bot: "Bot", ctx: "AppContext") -> None:

    @bot.tree.command(
        name="chilling_effect",
        description="Find members whose arrival in a channel causes others to stop posting.",
    )
    @app_commands.describe(
        lookback_days="Days of message history to analyse (default 30).",
        entry_gap_minutes="Minutes of silence before counting someone as 'arriving' (default 30).",
        window_minutes="Activity window (minutes) measured before and after each arrival (default 15).",
        min_entries="Minimum arrivals needed to include someone in the report (default 3).",
        top="How many members to show (default 10).",
        channel="Limit to a specific channel (default: all channels).",
    )
    async def chilling_effect(
        interaction: discord.Interaction,
        lookback_days: app_commands.Range[int, 1, 90] = 30,
        entry_gap_minutes: app_commands.Range[int, 5, 240] = 30,
        window_minutes: app_commands.Range[int, 5, 60] = 15,
        min_entries: app_commands.Range[int, 1, 20] = 3,
        top: app_commands.Range[int, 1, 25] = 10,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not ctx.is_mod(interaction):
            await interaction.followup.send(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "This command only works in a server.", ephemeral=True
            )
            return

        cutoff_ts = int(
            (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
        )
        entry_gap = entry_gap_minutes * 60
        window = window_minutes * 60

        with ctx.open_db() as conn:
            results, total_entries, channel_count = _analyze_chilling_effect(
                conn,
                guild.id,
                cutoff_ts=cutoff_ts,
                entry_gap_seconds=entry_gap,
                window_seconds=window,
                channel_id=channel.id if channel else None,
            )

        # Filter by min_entries
        results = [(uid, cnt, avg, victims) for uid, cnt, avg, victims in results if cnt >= min_entries]

        scope = f"#{channel.name}" if channel else f"{channel_count} channels"
        header = (
            f"**Chilling Effect Analysis** "
            f"(last {lookback_days}d · {entry_gap_minutes}m gap · {window_minutes}m window · "
            f"{total_entries} arrivals across {scope})\n\n"
        )

        if not results:
            await send_ephemeral_text(
                interaction,
                header + f"No member had ≥{min_entries} qualifying arrivals. "
                         "Try lowering `min_entries`, widening the window, or running `/interaction_scan`.",
            )
            return

        lines: list[str] = []
        for rank, (uid, cnt, avg, victims) in enumerate(results[:top], start=1):
            member = guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"

            top_victims = victims.most_common(3)
            victim_parts: list[str] = []
            for victim_id, times in top_victims:
                vm = guild.get_member(victim_id)
                vname = vm.display_name if vm else f"User {victim_id}"
                victim_parts.append(f"{vname} ({times}×)")

            victim_str = ", ".join(victim_parts) if victim_parts else "—"
            lines.append(
                f"**{rank}. {name}** — {cnt} arrivals · avg **{avg:.1f}** silenced/arrival\n"
                f"    Silences: {victim_str}"
            )

        lines.append(
            "\n_Correlation only — arrivals when no one else was talking are excluded. "
            "High scores warrant a closer look, not automatic conclusions._"
        )

        await send_ephemeral_text(interaction, header + "\n".join(lines))
