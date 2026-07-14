"""Chat Revive slash commands — thin cog; brains in chat_revive/ + services.

All SQLite work runs through module-level sync helpers dispatched with
``asyncio.to_thread`` so nothing blocks the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.chat_revive.actions import (
    ReviveOptInButton,
    channel_is_busy,
    send_revive,
)
from bot_modules.chat_revive.logic import FLOURISHES, band_label, should_ping
from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.games.utils.question_source import channel_allows_nsfw
from bot_modules.games_config.logic import has_mod_or_admin_permissions
from bot_modules.services.chat_revive_loop import (
    ReviveInFlight,
    evaluate_sync,
    pick_sync,
    record_sync,
    send_guard,
)
from bot_modules.services.chat_revive_service import (
    KNOWN_CATEGORIES,
    ChannelConfig,
    GuildConfig,
    Question,
    add_question,
    bulk_add_questions,
    get_channel_config,
    get_guild_config,
    list_questions,
    retire_question,
    revive_stats,
    ReviveStats,
    save_channel_config,
    save_guild_config,
    seed_starter_pack,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.chat_revive")

BULK_MAX_BYTES = 256 * 1024


def is_mod_or_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return has_mod_or_admin_permissions(interaction.user.guild_permissions)

    return app_commands.check(predicate)


# ── sync DB helpers (run via asyncio.to_thread) ───────────────────────


def _setup_sync(
    db_path: Path,
    guild_id: int,
    *,
    role_id: int | None,
    quiet_start: int,
    quiet_end: int,
    daily_budget: int,
    now_ts: float,
) -> tuple[GuildConfig, int, float]:
    with open_db(db_path) as conn:
        cfg = replace(
            get_guild_config(conn, guild_id),
            enabled=True,
            role_id=role_id,
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            daily_budget=daily_budget,
        )
        save_guild_config(conn, cfg)
        seeded = seed_starter_pack(conn, guild_id, now_ts)
        offset = get_tz_offset_hours(conn, guild_id)
    return cfg, seeded, offset


def _channel_sync(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    *,
    enabled: bool,
    categories: tuple[str, ...] | None,
    ping: bool | None,
    rest_hours: float | None,
    role_override_id: int | None,
    fire_multiplier: float | None,
) -> ChannelConfig:
    with open_db(db_path) as conn:
        cfg = get_channel_config(conn, guild_id, channel_id) or ChannelConfig(
            guild_id=guild_id, channel_id=channel_id
        )
        cfg = replace(cfg, enabled=enabled)
        if categories is not None:
            cfg = replace(cfg, categories=categories)
        if ping is not None:
            cfg = replace(cfg, ping_enabled=ping)
        if rest_hours is not None:
            cfg = replace(cfg, rest_hours=rest_hours)
        if role_override_id is not None:
            cfg = replace(cfg, role_id_override=role_override_id)
        if fire_multiplier is not None:
            cfg = replace(cfg, fire_multiplier=fire_multiplier)
        save_channel_config(conn, cfg)
    return cfg


def _question_add_sync(
    db_path: Path,
    guild_id: int,
    text: str,
    *,
    category: str,
    nsfw: bool,
    created_by: int,
    now_ts: float,
) -> int | None:
    with open_db(db_path) as conn:
        return add_question(
            conn,
            guild_id,
            text,
            category=category,
            nsfw=nsfw,
            created_by=created_by,
            now_ts=now_ts,
        )


def _bulk_sync(
    db_path: Path,
    guild_id: int,
    lines: list[str],
    *,
    created_by: int,
    now_ts: float,
) -> tuple[int, int]:
    with open_db(db_path) as conn:
        return bulk_add_questions(
            conn, guild_id, lines, created_by=created_by, now_ts=now_ts
        )


def _list_sync(
    db_path: Path, guild_id: int, *, category: str | None, include_retired: bool
) -> list[Question]:
    with open_db(db_path) as conn:
        return list_questions(
            conn, guild_id, category=category, include_retired=include_retired
        )


def _retire_sync(db_path: Path, guild_id: int, question_id: int) -> bool:
    with open_db(db_path) as conn:
        return retire_question(conn, guild_id, question_id)


def _guild_config_sync(db_path: Path, guild_id: int) -> GuildConfig:
    with open_db(db_path) as conn:
        return get_guild_config(conn, guild_id)


def _stats_sync(db_path: Path, guild_id: int, now_ts: float) -> ReviveStats:
    with open_db(db_path) as conn:
        return revive_stats(conn, guild_id, now_ts=now_ts)


def _flourish_sync(db_path: Path, guild_id: int, enabled: bool) -> None:
    with open_db(db_path) as conn:
        cfg = replace(get_guild_config(conn, guild_id), flourish_enabled=enabled)
        save_guild_config(conn, cfg)


# ── the cog ───────────────────────────────────────────────────────────


class ChatReviveCog(commands.Cog):
    revive = app_commands.Group(
        name="revive",
        description="Chat Revive — wake a quiet channel with a good question (mods only).",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )
    question = app_commands.Group(
        name="question",
        description="Manage the Chat Revive question bank.",
        parent=revive,
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Mods only.", ephemeral=True
                )
            return
        raise error

    @staticmethod
    def _target_channel(
        interaction: discord.Interaction, channel: discord.TextChannel | None
    ) -> discord.TextChannel | None:
        target = channel or interaction.channel
        return target if isinstance(target, discord.TextChannel) else None

    # ── /revive setup ─────────────────────────────────────────────────

    @revive.command(
        name="setup",
        description="Enable Chat Revive: opt-in role, quiet hours, daily budget. Seeds starter questions.",
    )
    @app_commands.describe(
        role="Opt-in role to ping (omit to create a 'chat-revive' role)",
        quiet_start="Quiet hours start (local hour 0-23, default 0)",
        quiet_end="Quiet hours end (local hour 0-23, default 8)",
        daily_budget="Max revives per day across the server (default 3)",
    )
    @is_mod_or_admin()
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
        quiet_start: app_commands.Range[int, 0, 23] = 0,
        quiet_end: app_commands.Range[int, 0, 23] = 8,
        daily_budget: app_commands.Range[int, 1, 10] = 3,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True)
        role_note = ""
        if role is None:
            try:
                role = await guild.create_role(
                    name="chat-revive",
                    mentionable=False,
                    reason="Chat Revive opt-in role",
                )
                role_note = f"Created {role.mention} — advertise it as *“wake me when the room needs a spark”*."
            except discord.HTTPException:
                role_note = "Couldn't create a role (missing permission?) — revives will post un-pinged until one is set."
        now = time.time()
        cfg, seeded, offset = await asyncio.to_thread(
            _setup_sync,
            self.ctx.db_path,
            guild.id,
            role_id=role.id if role else None,
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            daily_budget=daily_budget,
            now_ts=now,
        )
        local = time.strftime("%H:%M", time.gmtime(now + offset * 3600))
        lines = [
            f"Chat Revive is **on**. Role: {role.mention if role else '*none*'}.",
            f"Quiet hours **{cfg.quiet_start:02d}:00–{cfg.quiet_end:02d}:00** local · "
            f"budget **{cfg.daily_budget}/day** · breathing room **{cfg.guild_gap_minutes}m**.",
            f"Server-local time is **{local}** (UTC{offset:+g}) — if that looks wrong, fix `tz_offset_hours` first.",
            f"Seeded **{seeded}** starter questions." if seeded else "Question bank already has entries — not re-seeded.",
            "Next: `/revive channel` to invite it into channels.",
        ]
        if role_note:
            lines.insert(1, role_note)
        embed = discord.Embed(
            title="🔥 Chat Revive",
            description="\n".join(lines),
            color=await resolve_accent_color(self.ctx.db_path, guild),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /revive channel ───────────────────────────────────────────────

    @revive.command(
        name="channel",
        description="Enable/disable a channel for revives and tune its flavor.",
    )
    @app_commands.describe(
        channel="The channel to configure",
        enabled="Whether revives may fire here (default on)",
        categories="Comma-separated question categories (e.g. deep,music) — 'all' clears the filter",
        ping="Tag the opt-in role here (at most once per day)",
        rest_hours="How long this channel rests after a revive (default 8)",
        role_override="Ping a different role in this channel",
        sensitivity="Lull multiplier — higher = rarer (default 4)",
    )
    @is_mod_or_admin()
    async def channel_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        enabled: bool = True,
        categories: str | None = None,
        ping: bool | None = None,
        rest_hours: app_commands.Range[float, 1.0, 72.0] | None = None,
        role_override: discord.Role | None = None,
        sensitivity: app_commands.Range[float, 2.0, 10.0] | None = None,
    ) -> None:
        cats: tuple[str, ...] | None = None
        if categories is not None:
            tokens = [t.strip().lower() for t in categories.split(",") if t.strip()]
            if any(t in ("all", "*") for t in tokens):
                cats = ()
            else:
                bad = [t for t in tokens if not t.isalpha()]
                if bad:
                    await interaction.response.send_message(
                        f"Categories must be single words — didn't understand: {', '.join(bad)}",
                        ephemeral=True,
                    )
                    return
                cats = tuple(dict.fromkeys(tokens))
        cfg = await asyncio.to_thread(
            _channel_sync,
            self.ctx.db_path,
            interaction.guild_id or 0,
            channel.id,
            enabled=enabled,
            categories=cats,
            ping=ping,
            rest_hours=rest_hours,
            role_override_id=role_override.id if role_override else None,
            fire_multiplier=sensitivity,
        )
        flavor = ", ".join(cfg.categories) if cfg.categories else "all categories"
        state = "enabled" if cfg.enabled else "disabled"
        await interaction.response.send_message(
            f"{channel.mention} {state} — {flavor} · ping "
            f"{'on' if cfg.ping_enabled else 'off'} · rests {cfg.rest_hours:g}h · "
            f"sensitivity ×{cfg.fire_multiplier:g}.",
            ephemeral=True,
        )

    # ── /revive check ─────────────────────────────────────────────────

    @revive.command(
        name="check",
        description="Would it fire right now? Explains the lull, the rhythm, and any blocker.",
    )
    @app_commands.describe(channel="Channel to inspect (default: here)")
    @is_mod_or_admin()
    async def check_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = self._target_channel(interaction, channel)
        if target is None or interaction.guild is None:
            await interaction.response.send_message(
                "Pick a regular text channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        now = time.time()
        busy = await channel_is_busy(self.bot, target.id)
        ev = await asyncio.to_thread(
            evaluate_sync,
            self.ctx.db_path,
            interaction.guild.id,
            target.id,
            now_ts=now,
            busy=busy,
            slowmode_delay=target.slowmode_delay or 0,
        )
        v = ev.inputs
        verdict = ev.verdict
        head = "🔥 **Would fire right now.**" if verdict.fire else "😴 **Holding back.**"
        lines = [head, verdict.reason]
        if v.last_human_ts is not None:
            lines.append(
                f"Quiet for **{(now - v.last_human_ts) / 60:.0f}m** · "
                f"history **{v.history_days:.0f}d** · mode **{verdict.mode or 'n/a'}**"
                + (f" · band **{band_label(verdict.band)}**" if verdict.band is not None else "")
            )
        if verdict.threshold_s:
            lines.append(f"Fires after **{verdict.threshold_s / 60:.0f}m** of silence.")
        cats = ev.channel_cfg.categories if ev.channel_cfg else ()
        q = await asyncio.to_thread(
            pick_sync,
            self.ctx.db_path,
            interaction.guild.id,
            categories=cats,
            allow_nsfw=channel_allows_nsfw(target),
            now_ts=now,
        )
        lines.append(f"Would ask: *{q.text}*" if q else "⚠️ No eligible question in the bank right now.")
        role_id = (
            (ev.channel_cfg.role_id_override if ev.channel_cfg else None)
            or ev.guild_cfg.role_id
        )
        if ev.channel_cfg and ev.channel_cfg.ping_enabled and role_id:
            ok = should_ping(ev.freq.last_ping_ts, now)
            lines.append(
                f"Would ping <@&{role_id}>." if ok else "Role already pinged here today — would post un-pinged."
            )
        else:
            lines.append("Pings are off in this channel.")
        embed = discord.Embed(
            title=f"Chat Revive · #{target.name}",
            description="\n".join(lines),
            color=await resolve_accent_color(self.ctx.db_path, interaction.guild),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /revive fire ──────────────────────────────────────────────────

    @revive.command(
        name="fire",
        description="Post a revive now (manual: skips lull detection, keeps ping scarcity).",
    )
    @app_commands.describe(channel="Channel to revive (default: here)")
    @is_mod_or_admin()
    async def fire_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = self._target_channel(interaction, channel)
        if target is None or interaction.guild is None:
            await interaction.response.send_message(
                "Pick a regular text channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        now = time.time()
        ev = await asyncio.to_thread(
            evaluate_sync,
            self.ctx.db_path,
            interaction.guild.id,
            target.id,
            now_ts=now,
            busy=False,
            slowmode_delay=0,
        )
        if not ev.guild_cfg.enabled:
            await interaction.followup.send(
                "Chat Revive isn't set up yet — run `/revive setup` first.",
                ephemeral=True,
            )
            return
        cats = ev.channel_cfg.categories if ev.channel_cfg else ()
        q = await asyncio.to_thread(
            pick_sync,
            self.ctx.db_path,
            interaction.guild.id,
            categories=cats,
            allow_nsfw=channel_allows_nsfw(target),
            now_ts=now,
        )
        if q is None:
            await interaction.followup.send(
                "No eligible question — the bank is empty, filtered too narrowly, "
                "or everything was used within the last month.",
                ephemeral=True,
            )
            return
        role_id = (
            (ev.channel_cfg.role_id_override if ev.channel_cfg else None)
            or ev.guild_cfg.role_id
        )
        ping = bool(
            ev.channel_cfg
            and ev.channel_cfg.ping_enabled
            and role_id
            and should_ping(ev.freq.last_ping_ts, now)
        )
        flourish = (
            random.choice(FLOURISHES) if ev.guild_cfg.flourish_enabled else None
        )
        try:
            async with send_guard(target.id):
                msg = await send_revive(
                    target,
                    question_text=q.text,
                    role_id=role_id if ping else None,
                    flourish=flourish,
                )
        except ReviveInFlight:
            await interaction.followup.send(
                "A revive is already being posted in that channel.", ephemeral=True
            )
            return
        except discord.HTTPException:
            log.exception("manual revive send failed in #%s", target.name)
            await interaction.followup.send(
                "Couldn't post in that channel (permissions?).", ephemeral=True
            )
            return
        await asyncio.to_thread(
            record_sync,
            self.ctx.db_path,
            interaction.guild.id,
            target.id,
            question_id=q.id,
            message_id=msg.id,
            trigger_kind="manual",
            pinged=ping,
            now_ts=now,
            offset_hours=ev.offset_hours,
        )
        await interaction.followup.send(
            f"Revived {target.mention}{' with a ping' if ping else ''}.",
            ephemeral=True,
        )

    # ── /revive question … ────────────────────────────────────────────

    @question.command(name="add", description="Add one question to the bank.")
    @app_commands.describe(
        text="The question",
        category="Which flavor bucket it belongs to",
        nsfw="Adult-only (will only ever appear in age-restricted channels)",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name=c, value=c) for c in KNOWN_CATEGORIES
        ]
    )
    @is_mod_or_admin()
    async def question_add(
        self,
        interaction: discord.Interaction,
        text: str,
        category: str = "general",
        nsfw: bool = False,
    ) -> None:
        qid = await asyncio.to_thread(
            _question_add_sync,
            self.ctx.db_path,
            interaction.guild_id or 0,
            text,
            category=category,
            nsfw=nsfw,
            created_by=interaction.user.id,
            now_ts=time.time(),
        )
        if qid is None:
            await interaction.response.send_message(
                "That question is already in the bank (or was blank).", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Added **#{qid}** to *{category}*{' (NSFW)' if nsfw else ''}.",
                ephemeral=True,
            )

    @question.command(
        name="bulk",
        description="Add many questions from a text file (one per line; 'category: text' tags them).",
    )
    @app_commands.describe(file="UTF-8 text file, one question per line")
    @is_mod_or_admin()
    async def question_bulk(
        self, interaction: discord.Interaction, file: discord.Attachment
    ) -> None:
        if file.size > BULK_MAX_BYTES:
            await interaction.response.send_message(
                "File too large (256 KB max).", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            text = (await file.read()).decode("utf-8")
        except (discord.HTTPException, UnicodeDecodeError):
            await interaction.followup.send(
                "Couldn't read that file — it must be UTF-8 text.", ephemeral=True
            )
            return
        added, skipped = await asyncio.to_thread(
            _bulk_sync,
            self.ctx.db_path,
            interaction.guild_id or 0,
            text.splitlines(),
            created_by=interaction.user.id,
            now_ts=time.time(),
        )
        await interaction.followup.send(
            f"Added **{added}** question(s); skipped **{skipped}** (duplicates/blank).",
            ephemeral=True,
        )

    @question.command(name="list", description="Browse the question bank.")
    @app_commands.describe(
        category="Only show one category", include_retired="Show retired questions too"
    )
    @is_mod_or_admin()
    async def question_list(
        self,
        interaction: discord.Interaction,
        category: str | None = None,
        include_retired: bool = False,
    ) -> None:
        qs = await asyncio.to_thread(
            _list_sync,
            self.ctx.db_path,
            interaction.guild_id or 0,
            category=category.lower().strip() if category else None,
            include_retired=include_retired,
        )
        if not qs:
            await interaction.response.send_message(
                "No questions found — `/revive question add` or `/revive setup` to seed the starter pack.",
                ephemeral=True,
            )
            return
        if category is None and len(qs) > 25:
            counts: dict[str, int] = {}
            for q in qs:
                counts[q.category] = counts.get(q.category, 0) + 1
            summary = " · ".join(f"**{c}** {n}" for c, n in sorted(counts.items()))
            await interaction.response.send_message(
                f"{len(qs)} questions: {summary}\nPass a category to see them.",
                ephemeral=True,
            )
            return
        lines = [
            f"`#{q.id}` [{q.category}{'·nsfw' if q.nsfw else ''}"
            f"{'·retired' if not q.active else ''}] {q.text[:80]} — used {q.use_count}×"
            for q in qs[:25]
        ]
        if len(qs) > 25:
            lines.append(f"…and {len(qs) - 25} more.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /revive optin-post / stats / flourish ─────────────────────────

    @revive.command(
        name="optin-post",
        description="Post a persistent join/leave button for the revive ping role.",
    )
    @app_commands.describe(channel="Where to post it (default: here)")
    @is_mod_or_admin()
    async def optin_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        target = self._target_channel(interaction, channel)
        if target is None or interaction.guild is None:
            await interaction.response.send_message(
                "Pick a regular text channel.", ephemeral=True
            )
            return
        cfg = await asyncio.to_thread(
            _guild_config_sync, self.ctx.db_path, interaction.guild.id
        )
        if cfg.role_id is None:
            await interaction.response.send_message(
                "No opt-in role configured — run `/revive setup` first.",
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=None)
        view.add_item(ReviveOptInButton(cfg.role_id))
        try:
            await target.send(
                "🔥 **Chat Revive** — take the role and get summoned (rarely — "
                "a few times a week at most) when a favorite channel needs a "
                "spark. Tap to join or leave any time.",
                view=view,
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "Couldn't post in that channel (permissions?).", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Opt-in button posted in {target.mention}.", ephemeral=True
        )

    @revive.command(
        name="stats",
        description="The scoreboard: how often we revive, how often it works, what carries.",
    )
    @is_mod_or_admin()
    async def stats_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        await interaction.response.defer(ephemeral=True)
        s = await asyncio.to_thread(
            _stats_sync, self.ctx.db_path, guild.id, time.time()
        )
        if s.total == 0:
            await interaction.followup.send(
                "No revives yet — the scoreboard starts after the first one.",
                ephemeral=True,
            )
            return
        rate = f"{s.successes}/{s.measured}" if s.measured else "n/a"
        lines = [
            f"**{s.total}** revives all-time · **{s.week_revives}** this week · "
            f"sparked conversation **{rate}** of measured.",
        ]
        if s.channels:
            lines.append("\n**Channels (30d):**")
            lines += [
                f"<#{c.channel_id}> — {c.revives} revives, "
                f"{c.successes}/{c.measured} sparked"
                for c in s.channels
            ]
        if s.top_questions:
            lines.append("\n**Carrying the team:**")
            lines += [
                f"`#{q.question_id}` {q.text[:60]} — {q.successes}/{q.uses}"
                for q in s.top_questions
            ]
        if s.dud_questions:
            lines.append("\n**Dead weight (consider retiring):**")
            lines += [
                f"`#{q.question_id}` {q.text[:60]} — 0/{q.uses}"
                for q in s.dud_questions
            ]
        cfg = await asyncio.to_thread(
            _guild_config_sync, self.ctx.db_path, guild.id
        )
        role = guild.get_role(cfg.role_id) if cfg.role_id else None
        if role is not None:
            lines.append(f"\nOpt-in role: {len(role.members)} member(s).")
        embed = discord.Embed(
            title="🔥 Chat Revive — scoreboard",
            description="\n".join(lines),
            color=await resolve_accent_color(self.ctx.db_path, guild),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @revive.command(
        name="flourish",
        description="Toggle the little 'stirring the coals…' flourish line.",
    )
    @app_commands.describe(enabled="Off = bone-dry delivery, question only")
    @is_mod_or_admin()
    async def flourish_cmd(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await asyncio.to_thread(
            _flourish_sync, self.ctx.db_path, interaction.guild_id or 0, enabled
        )
        await interaction.response.send_message(
            "Flourish on — *stirring the coals…*" if enabled else "Bone-dry mode.",
            ephemeral=True,
        )

    @question.command(name="retire", description="Retire a question (kept for stats, never asked again).")
    @app_commands.describe(question_id="The #id shown by /revive question list")
    @is_mod_or_admin()
    async def question_retire(
        self, interaction: discord.Interaction, question_id: int
    ) -> None:
        ok = await asyncio.to_thread(
            _retire_sync, self.ctx.db_path, interaction.guild_id or 0, question_id
        )
        await interaction.response.send_message(
            f"Retired **#{question_id}**." if ok else f"No question #{question_id} here.",
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(ChatReviveCog(bot, bot.ctx))
