"""Drama analysis commands."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from reports import send_ephemeral_text

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.drama")

_MSG_PREVIEW = 90
_EVENTS_PER_PERSON = 2
_VICTIMS_PER_EVENT = 3

_VictimMsg = tuple[int, int, str | None]
_EntryEvent = tuple[int, int, int, str | None, list[_VictimMsg]]


def _analyze_chilling_effect(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    cutoff_ts: int,
    entry_gap_seconds: int,
    window_seconds: int,
    channel_id: int | None = None,
) -> tuple[list[_EntryEvent], int]:
    query = (
        "SELECT channel_id, author_id, ts, message_id, content "
        "FROM messages "
        "WHERE guild_id = ? AND ts >= ?"
    )
    params: list[object] = [guild_id, cutoff_ts]
    if channel_id is not None:
        query += " AND channel_id = ?"
        params.append(channel_id)
    query += " ORDER BY channel_id, ts ASC"

    rows = conn.execute(query, params).fetchall()

    msgs_by_channel: dict[int, list[tuple[int, int, int, str | None]]] = defaultdict(list)
    for row in rows:
        msgs_by_channel[int(row[0])].append(
            (int(row[2]), int(row[1]), int(row[3]), row[4])
        )

    events: list[_EntryEvent] = []

    for ch_id, ch_msgs in msgs_by_channel.items():
        ch_msgs.sort()
        last_ts_by_user: dict[int, int] = {}

        for i, (ts, author_id, _mid, content) in enumerate(ch_msgs):
            prev_ts = last_ts_by_user.get(author_id)
            last_ts_by_user[author_id] = ts

            if prev_ts is None or (ts - prev_ts) < entry_gap_seconds:
                continue

            last_before: dict[int, tuple[int, str | None]] = {}
            for j in range(i - 1, -1, -1):
                jts, jauthor, _jmid, jcontent = ch_msgs[j]
                if jts < ts - window_seconds:
                    break
                if jauthor != author_id and jauthor not in last_before:
                    last_before[jauthor] = (jts, jcontent)

            if not last_before:
                continue

            active_after: set[int] = set()
            for j in range(i + 1, len(ch_msgs)):
                jts, jauthor, _jmid, _jcontent = ch_msgs[j]
                if jts >= ts + window_seconds:
                    break
                if jauthor != author_id:
                    active_after.add(jauthor)

            silenced_victims: list[_VictimMsg] = [
                (victim, victim_ts, victim_content)
                for victim, (victim_ts, victim_content) in last_before.items()
                if victim not in active_after
            ]

            if not silenced_victims:
                continue

            silenced_victims.sort(key=lambda v: v[1], reverse=True)
            events.append((ch_id, ts, author_id, content, silenced_victims))

    return events, len(msgs_by_channel)


def _fmt(ts: int) -> str:
    return f"<t:{ts}:t>"


def _preview(text: str | None, limit: int = _MSG_PREVIEW) -> str:
    if not text:
        return "_[no text]_"
    text = text.replace("\n", " ")
    return text[:limit] + "…" if len(text) > limit else text


class DramaCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="chilling_effect",
        description="Who makes others go quiet when they show up? Correlation-based analysis.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        lookback_days="Days of history to analyze.",
        entry_gap_minutes="Minutes of silence before someone counts as 'arriving'.",
        window_minutes="Minutes of activity to measure before and after each arrival.",
        min_entries="Minimum arrivals needed to include someone.",
        top="How many members to show.",
        channel="Limit to one channel. Omit for all channels.",
    )
    async def chilling_effect(
        self,
        interaction: discord.Interaction,
        lookback_days: app_commands.Range[int, 1, 90] = 30,
        entry_gap_minutes: app_commands.Range[int, 5, 240] = 30,
        window_minutes: app_commands.Range[int, 5, 60] = 15,
        min_entries: app_commands.Range[int, 1, 20] = 3,
        top: app_commands.Range[int, 1, 25] = 10,
        channel: discord.TextChannel | None = None,
    ) -> None:
        ctx = self.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        cutoff_ts = int(
            (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
        )
        _ch_id = channel.id if channel else None

        def _analyze():
            with ctx.open_db() as conn:
                return _analyze_chilling_effect(
                    conn,
                    guild.id,
                    cutoff_ts=cutoff_ts,
                    entry_gap_seconds=entry_gap_minutes * 60,
                    window_seconds=window_minutes * 60,
                    channel_id=_ch_id,
                )

        events, channel_count = await asyncio.to_thread(_analyze)

        events_by_author: dict[int, list[_EntryEvent]] = defaultdict(list)
        for ev in events:
            events_by_author[ev[2]].append(ev)

        ranked = sorted(
            [
                (uid, len(evs), sum(len(ev[4]) for ev in evs) / len(evs))
                for uid, evs in events_by_author.items()
                if len(evs) >= min_entries
            ],
            key=lambda r: r[2],
            reverse=True,
        )

        scope = f"#{channel.name}" if channel else f"{channel_count} channels"
        header = (
            f"**Chilling Effect Analysis** "
            f"(last {lookback_days}d · {entry_gap_minutes}m gap · {window_minutes}m window · "
            f"{len(events)} arrivals across {scope})\n\n"
        )

        if not ranked:
            await send_ephemeral_text(
                interaction,
                header + f"No member had ≥{min_entries} qualifying arrivals. "
                "Try lowering `min_entries`, widening the window, or running `/interaction_scan`.",
            )
            return

        lines: list[str] = []
        for rank, (uid, cnt, avg) in enumerate(ranked[:top], start=1):
            member = guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"

            lines.append(
                f"**{rank}. {name}** — {cnt} arrivals · avg **{avg:.1f}** silenced/arrival"
            )

            examples = sorted(
                events_by_author[uid],
                key=lambda ev: len(ev[4]),
                reverse=True,
            )[:_EVENTS_PER_PERSON]

            for ch_id, ts, _author_id, entry_content, victims in examples:
                ch = guild.get_channel(ch_id)
                ch_name = ch.name if ch and hasattr(ch, "name") else str(ch_id)
                lines.append(
                    f"  [{_fmt(ts)} #{ch_name}] **arrived:** {_preview(entry_content)}"
                )
                for victim_id, v_ts, v_content in victims[:_VICTIMS_PER_EVENT]:
                    vm = guild.get_member(victim_id)
                    vname = vm.display_name if vm else f"User {victim_id}"
                    lines.append(
                        f"    ↳ **{vname}** last said {_fmt(v_ts)}: {_preview(v_content)}"
                    )

            lines.append("")

        lines.append(
            "_Correlation only — arrivals when no one else was talking are excluded. "
            "High scores warrant a closer look, not automatic conclusions._"
        )

        await send_ephemeral_text(interaction, header + "\n".join(lines))


async def setup(bot: Bot) -> None:
    await bot.add_cog(DramaCog(bot, bot.ctx))
