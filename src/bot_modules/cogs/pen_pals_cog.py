"""Pen Pals — private 1-on-1 matched text channels with prompted questions."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.ai_client import generate_text
from bot_modules.games.utils.question_source import get_ai_config

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.pen_pals")

_SESSION_SECS = 24 * 3600       # 24-hour session
_Q_INTERVAL = 24 * 3600         # auto-question every 24 h
_WARN_SECS = 3600                # post 1-h warning when this much time remains
_Q_SUPPRESS_SECS = 2 * 3600     # skip auto-question if fewer than 2 h remain
_MAX_SWAPS = 3
_TICK_SECS = 300                 # background loop tick every 5 min
_RECENT_LIMIT = 10               # past pairings to check for repeats
_MATCH_COOLDOWN_SECS = 30 * 86400  # only re-match a member once they've had no pen pal for a month
_GAME_TYPE = "pen_pals"


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_config(conn, guild_id: int):
    return conn.execute(
        "SELECT * FROM pen_pals_config WHERE guild_id = ?", (guild_id,)
    ).fetchone()


def _set_config(
    conn,
    guild_id: int,
    *,
    enabled: bool,
    category_id: int,
    opt_in_role_id: int,
    question_category: str,
    log_channel_id: int,
    auto_round_dow: int,
    auto_round_hour: int,
    panel_channel_id: int,
) -> None:
    conn.execute("INSERT OR IGNORE INTO pen_pals_config (guild_id) VALUES (?)", (guild_id,))
    conn.execute(
        """UPDATE pen_pals_config
           SET enabled=?, category_id=?, opt_in_role_id=?, question_category=?,
               log_channel_id=?, auto_round_dow=?, auto_round_hour=?, panel_channel_id=?
           WHERE guild_id=?""",
        (int(enabled), category_id, opt_in_role_id, question_category,
         log_channel_id, auto_round_dow, auto_round_hour, panel_channel_id, guild_id),
    )


def _set_panel_message_id(conn, guild_id: int, message_id: int) -> None:
    conn.execute(
        "UPDATE pen_pals_config SET panel_message_id=? WHERE guild_id=?",
        (message_id, guild_id),
    )


def _in_pool(conn, guild_id: int, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM pen_pals_pool WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone() is not None


def _add_to_pool(conn, guild_id: int, user_id: int, joined_at: float | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO pen_pals_pool (guild_id, user_id, joined_at) VALUES (?, ?, ?)",
        (guild_id, user_id, joined_at if joined_at is not None else time.time()),
    )


def _remove_from_pool(conn, guild_id: int, user_id: int) -> None:
    conn.execute(
        "DELETE FROM pen_pals_pool WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def _get_pool(conn, guild_id: int) -> list:
    return conn.execute(
        "SELECT user_id, joined_at FROM pen_pals_pool WHERE guild_id = ? ORDER BY joined_at ASC",
        (guild_id,),
    ).fetchall()


def _get_active_session(conn, guild_id: int, user_id: int):
    return conn.execute(
        """
        SELECT * FROM pen_pals_sessions
        WHERE guild_id = ? AND (user1_id = ? OR user2_id = ?) AND state = 'active'
        """,
        (guild_id, user_id, user_id),
    ).fetchone()


def _get_session_by_channel(conn, channel_id: int):
    return conn.execute(
        "SELECT * FROM pen_pals_sessions WHERE channel_id = ? AND state = 'active'",
        (channel_id,),
    ).fetchone()


def _get_all_active_sessions(conn) -> list:
    return conn.execute(
        "SELECT * FROM pen_pals_sessions WHERE state = 'active'",
    ).fetchall()


def _create_session(
    conn, session_id: str, guild_id: int, channel_id: int,
    user1_id: int, user2_id: int, now: float,
) -> None:
    expiry = now + _SESSION_SECS
    conn.execute(
        """
        INSERT INTO pen_pals_sessions
            (session_id, guild_id, channel_id, user1_id, user2_id,
             started_at, expiry_at, next_question_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, guild_id, channel_id, user1_id, user2_id,
         now, expiry, now + _Q_INTERVAL),
    )


def _close_session(conn, session_id: str, reason: str) -> None:
    conn.execute(
        """
        UPDATE pen_pals_sessions
        SET state = 'closed', closed_at = ?, close_reason = ?
        WHERE session_id = ?
        """,
        (time.time(), reason, session_id),
    )


def _set_close_warning_sent(conn, session_id: str) -> None:
    conn.execute(
        "UPDATE pen_pals_sessions SET close_warning_sent = 1 WHERE session_id = ?",
        (session_id,),
    )


def _advance_next_question(conn, session_id: str, next_at: float) -> None:
    conn.execute(
        "UPDATE pen_pals_sessions SET next_question_at = ? WHERE session_id = ?",
        (next_at, session_id),
    )


def _increment_swaps(conn, session_id: str) -> int:
    conn.execute(
        """
        UPDATE pen_pals_sessions
        SET question_swaps_used = question_swaps_used + 1
        WHERE session_id = ?
        """,
        (session_id,),
    )
    row = conn.execute(
        "SELECT question_swaps_used FROM pen_pals_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return row[0] if row else 0


def _record_question(conn, session_id: str, question_text: str) -> None:
    conn.execute(
        "INSERT INTO pen_pals_questions (session_id, question_text, shown_at) VALUES (?, ?, ?)",
        (session_id, question_text, time.time()),
    )


def _get_shown_questions(conn, session_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT question_text FROM pen_pals_questions WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _recent_partners(conn, guild_id: int, user_id: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT CASE WHEN user1_id = ? THEN user2_id ELSE user1_id END AS partner
        FROM pen_pals_sessions
        WHERE guild_id = ? AND (user1_id = ? OR user2_id = ?)
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (user_id, guild_id, user_id, user_id, _RECENT_LIMIT),
    ).fetchall()
    return {r[0] for r in rows}


def _last_matched_at(conn, guild_id: int, user_id: int) -> float | None:
    """When *user_id* was last paired in this guild, or None if never.

    Any session (active or closed) counts — the cooldown is about how long
    since a member last *had* a pen pal, not whether that pairing is still open.
    """
    row = conn.execute(
        """
        SELECT MAX(started_at) FROM pen_pals_sessions
        WHERE guild_id = ? AND (user1_id = ? OR user2_id = ?)
        """,
        (guild_id, user_id, user_id),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def _cfg_allows_nsfw(cfg) -> bool:
    """True when the guild's configured question pool includes NSFW prompts."""
    return cfg is not None and (cfg["question_category"] or "sfw") == "all"


def _parse_tags(tags_json) -> set[str]:
    """Parse a bank row's JSON tags column into a set, tolerating bad data."""
    try:
        return set(json.loads(tags_json or "[]"))
    except (json.JSONDecodeError, TypeError):
        return set()


def _draw_from_bank(conn, allow_nsfw: bool, exclude: list[str]) -> str | None:
    """Random unshown bank question; rows tagged 'nsfw' need *allow_nsfw*."""
    rows = conn.execute(
        "SELECT question_text, tags FROM games_question_bank WHERE game_type = ?",
        (_GAME_TYPE,),
    ).fetchall()
    seen = set(exclude)
    candidates = [
        r["question_text"]
        for r in rows
        if r["question_text"] not in seen
        and (allow_nsfw or "nsfw" not in _parse_tags(r["tags"]))
    ]
    return random.choice(candidates) if candidates else None


def _update_last_auto_round(conn, guild_id: int) -> None:
    conn.execute(
        "UPDATE pen_pals_config SET last_auto_round_at = ? WHERE guild_id = ?",
        (time.time(), guild_id),
    )


# ── Question draw ─────────────────────────────────────────────────────────────


_FALLBACK_QUESTION = "What's something about you that most people in this server don't know?"


async def _draw_question(db_path: Path, session_id: str, allow_nsfw: bool) -> str:
    def _from_bank():
        with open_db(db_path) as conn:
            shown = _get_shown_questions(conn, session_id)
            return _draw_from_bank(conn, allow_nsfw, shown)

    question = await asyncio.to_thread(_from_bank)
    if question:
        return question

    # Bank exhausted (or empty): AI fallback via the shared per-game prompt
    # config. Always SFW — NSFW prompts come only from the curated bank.
    ai_cfg = get_ai_config(_GAME_TYPE, "sfw")
    if ai_cfg:
        system, user, max_tokens = ai_cfg
        ai_text = await generate_text(system, user, max_tokens=max_tokens)
        if ai_text:
            line = ai_text.strip().splitlines()[0].strip()
            if line:
                return line

    return _FALLBACK_QUESTION


# ── Channel helpers ───────────────────────────────────────────────────────────


def _channel_name(name1: str, name2: str) -> str:
    def _slug(s: str) -> str:
        out = "".join(c if c.isalnum() else "-" for c in s.lower())
        return out[:20].strip("-")

    return f"penpals-{_slug(name1)}-{_slug(name2)}"[:100]


async def _create_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    user1: discord.Member,
    user2: discord.Member,
    *,
    nsfw: bool = False,
) -> discord.TextChannel:
    overwrites: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user1: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        user2: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            manage_messages=True, manage_channels=True,
        ),
    }
    # NSFW-flagged when the guild's question pool includes NSFW prompts, so the
    # channel age-gate matches the content that can appear in it.
    return await guild.create_text_channel(
        _channel_name(user1.display_name, user2.display_name),
        category=category,
        overwrites=overwrites,
        nsfw=nsfw,
        reason="Pen Pals session",
    )


async def _post_intro(
    channel: discord.TextChannel,
    user1: discord.Member,
    user2: discord.Member,
    expiry_at: float,
    question: str,
    color: "discord.Color | None" = None,
) -> None:
    if color is None:
        color = discord.Color.blurple()
    embed = discord.Embed(title="🖊️ Pen Pals", color=color)
    embed.add_field(
        name="Matched with",
        value=f"{user1.mention} × {user2.mention}",
        inline=False,
    )
    embed.add_field(
        name="Session ends",
        value=f"<t:{int(expiry_at)}:F> (<t:{int(expiry_at)}:R>)",
        inline=False,
    )
    embed.set_footer(
        text="Admins can see this channel. "
             "Use /penpals new-question to swap the prompt (3 times max)."
    )
    intro_msg = await channel.send(embed=embed)
    await intro_msg.pin()

    await channel.send(
        f"{user1.mention} {user2.mention}\n"
        f"💬 Here's your first question:\n> {question}"
    )


# ── Pair logic ────────────────────────────────────────────────────────────────


async def _do_pair(
    bot: discord.Client,
    db_path: Path,
    guild_id: int,
    user1_id: int,
    user2_id: int,
) -> bool:
    """Create a session and channel for two users. Returns True on success."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return False

    user1 = guild.get_member(user1_id)
    user2 = guild.get_member(user2_id)
    if user1 is None or user2 is None:
        log.warning("pen_pals: member(s) missing in guild %d (%d, %d)", guild_id, user1_id, user2_id)
        return False

    def _load_cfg():
        with open_db(db_path) as conn:
            return _get_config(conn, guild_id)

    cfg = await asyncio.to_thread(_load_cfg)
    if cfg is None or not cfg["enabled"] or not cfg["category_id"]:
        return False

    category = guild.get_channel(cfg["category_id"])
    if not isinstance(category, discord.CategoryChannel):
        return False

    allow_nsfw = _cfg_allows_nsfw(cfg)
    session_id = str(uuid.uuid4())
    now = time.time()

    # Draw question before the channel exists so we can post it immediately
    # (the session has no shown-question history yet).
    question = await _draw_question(db_path, session_id, allow_nsfw)

    try:
        channel = await _create_channel(guild, category, user1, user2, nsfw=allow_nsfw)
    except discord.Forbidden:
        log.warning("pen_pals: missing permission to create channel in guild %d", guild_id)
        return False
    except discord.HTTPException as exc:
        log.error("pen_pals: channel creation failed in guild %d: %s", guild_id, exc)
        return False

    def _save() -> bool:
        with open_db(db_path) as conn:
            # Guard against a concurrent pairing that won the race while the
            # channel was being created: never give a user two sessions.
            if (
                _get_active_session(conn, guild_id, user1_id)
                or _get_active_session(conn, guild_id, user2_id)
            ):
                return False
            _create_session(conn, session_id, guild_id, channel.id, user1_id, user2_id, now)
            _record_question(conn, session_id, question)
            _remove_from_pool(conn, guild_id, user1_id)
            _remove_from_pool(conn, guild_id, user2_id)
            # Pen-pal quest trigger for both matched members, keyed to the
            # session so one pairing pays each side once.
            from bot_modules.services.economy_quests_service import fire_trigger_inline

            for m in (user1, user2):
                fire_trigger_inline(
                    conn,
                    guild_id,
                    "pen_pal",
                    m.id,
                    occurrence=session_id,
                    booster=m.premium_since is not None,
                )
            return True

    if not await asyncio.to_thread(_save):
        log.warning(
            "pen_pals: aborted duplicate pairing %d ↔ %d in guild %d",
            user1_id, user2_id, guild_id,
        )
        try:
            await channel.delete(reason="Pen Pals: duplicate pairing aborted")
        except discord.HTTPException:
            pass
        return False

    expiry_at = now + _SESSION_SECS
    accent = await resolve_accent_color(db_path, guild)
    try:
        await _post_intro(channel, user1, user2, expiry_at, question, color=accent)
    except discord.HTTPException as exc:
        log.error("pen_pals: failed to post intro in channel %d: %s", channel.id, exc)

    # Post to log channel if configured
    if cfg["log_channel_id"]:
        log_ch = guild.get_channel(cfg["log_channel_id"])
        if isinstance(log_ch, discord.TextChannel):
            try:
                await log_ch.send(
                    f"🖊️ Pen Pals: {user1.mention} × {user2.mention} paired → {channel.mention}"
                )
            except discord.HTTPException:
                pass

    log.info("pen_pals: paired %d ↔ %d in guild %d (session %s)", user1_id, user2_id, guild_id, session_id)
    return True


async def _do_round(bot: discord.Client, db_path: Path, guild_id: int) -> tuple[int, int]:
    """Drain the pool for a guild. Returns (pairs_made, still_waiting).

    Only members who haven't had a pen pal in the last month are eligible;
    anyone still inside that cooldown is left untouched in the pool. Users
    whose pairing fails likewise stay in the DB pool. ``still_waiting`` is the
    pool size once the round settles, so it counts cooled-down members, the
    odd one out, and any failed pairs alike.
    """
    def _load_eligible_and_stamp():
        with open_db(db_path) as conn:
            now = time.time()
            eligible = [
                r["user_id"]
                for r in _get_pool(conn, guild_id)
                if (last := _last_matched_at(conn, guild_id, r["user_id"])) is None
                or now - last >= _MATCH_COOLDOWN_SECS
            ]
            _update_last_auto_round(conn, guild_id)
            return eligible

    remaining = await asyncio.to_thread(_load_eligible_and_stamp)
    pairs_made = 0

    while len(remaining) >= 2:
        u1 = remaining.pop(0)

        def _recent(uid: int = u1):
            with open_db(db_path) as conn:
                return _recent_partners(conn, guild_id, uid)

        recent = await asyncio.to_thread(_recent)
        partner = next((u for u in remaining if u not in recent), remaining[0])
        remaining.remove(partner)

        if await _do_pair(bot, db_path, guild_id, u1, partner):
            pairs_made += 1

    def _pool_count():
        with open_db(db_path) as conn:
            return len(_get_pool(conn, guild_id))

    return pairs_made, await asyncio.to_thread(_pool_count)


# ── Background loop ───────────────────────────────────────────────────────────


async def _pen_pals_loop(bot: discord.Client, db_path: Path) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _tick(bot, db_path)
        except Exception:
            log.exception("pen_pals_loop tick failed")
        await asyncio.sleep(_TICK_SECS)


async def _tick(bot: discord.Client, db_path: Path) -> None:
    def _load_all():
        with open_db(db_path) as conn:
            sessions = list(_get_all_active_sessions(conn))
            guild_ids = {s["guild_id"] for s in sessions}
            configs = {
                gid: _get_config(conn, gid)
                for gid in guild_ids
            }
            auto_cfgs = conn.execute(
                "SELECT * FROM pen_pals_config WHERE enabled = 1 AND auto_round_dow >= 0"
            ).fetchall()
            return sessions, configs, list(auto_cfgs)

    sessions, configs, auto_cfgs = await asyncio.to_thread(_load_all)
    now = time.time()

    for row in sessions:
        session_id = row["session_id"]
        guild_id = row["guild_id"]
        channel_id = row["channel_id"]
        expiry_at = row["expiry_at"]
        next_q_at = row["next_question_at"]
        warned = row["close_warning_sent"]
        user1_id = row["user1_id"]
        user2_id = row["user2_id"]

        raw = bot.get_channel(channel_id)
        if raw is None:
            try:
                raw = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                def _close_missing(sid: str = session_id):
                    with open_db(db_path) as conn:
                        _close_session(conn, sid, "channel_missing")
                await asyncio.to_thread(_close_missing)
                continue
            except discord.HTTPException:
                continue

        if not isinstance(raw, discord.TextChannel):
            continue
        channel: discord.TextChannel = raw

        # Expiry
        if now >= expiry_at:
            try:
                await channel.delete(reason="Pen Pals session expired")
            except (discord.NotFound, discord.HTTPException):
                pass
            def _close_exp(sid: str = session_id):
                with open_db(db_path) as conn:
                    _close_session(conn, sid, "expired")
            await asyncio.to_thread(_close_exp)
            log.info("pen_pals: session %s expired", session_id)

            # Quest hook: running the full 24h is "seeing it through" — both
            # members fire; early-ended sessions never reach this path.
            from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

            for uid in (user1_id, user2_id):
                await fire_member_trigger(
                    cast("Bot", bot), guild_id, uid, "pen_pal_complete",
                    occurrence=str(session_id),
                )
            continue

        # 1-hour close warning
        if not warned and (expiry_at - now) <= _WARN_SECS:
            try:
                await channel.send("⏰ This pen pal channel closes in 1 hour.")
            except discord.HTTPException:
                pass
            def _mark_warned(sid: str = session_id):
                with open_db(db_path) as conn:
                    _set_close_warning_sent(conn, sid)
            await asyncio.to_thread(_mark_warned)

        # Auto question (skip if < 2 h remain)
        if next_q_at <= now and (expiry_at - now) >= _Q_SUPPRESS_SECS:
            cfg = configs.get(guild_id)
            question = await _draw_question(db_path, session_id, _cfg_allows_nsfw(cfg))
            try:
                await channel.send(
                    f"<@{user1_id}> <@{user2_id}>\n"
                    f"💬 A new question to keep things going:\n> {question}"
                )
            except discord.HTTPException as exc:
                log.warning("pen_pals: failed to post auto question in %d: %s", channel_id, exc)

            def _save_q(sid: str = session_id, q: str = question, nq: float = next_q_at):
                with open_db(db_path) as conn:
                    _record_question(conn, sid, q)
                    _advance_next_question(conn, sid, nq + _Q_INTERVAL)
            await asyncio.to_thread(_save_q)

    # Auto-round
    now_utc = datetime.now(timezone.utc)
    current_dow = now_utc.weekday()
    current_hour = now_utc.hour
    for cfg in auto_cfgs:
        if cfg["auto_round_dow"] != current_dow or cfg["auto_round_hour"] != current_hour:
            continue
        if now - (cfg["last_auto_round_at"] or 0) < 3600:
            continue
        pairs, left = await _do_round(bot, db_path, cfg["guild_id"])
        log.info("pen_pals: auto-round guild %d — %d pairs, %d left over", cfg["guild_id"], pairs, left)


# ── Signup panel ──────────────────────────────────────────────────────────────


def _build_panel_embed(
    pool_size: int, color: "discord.Color | None" = None
) -> discord.Embed:
    if color is None:
        color = discord.Color.from_str("#5865F2")
    embed = discord.Embed(
        title="🖊️ Pen Pals",
        description=(
            "Get matched 1-on-1 with another server member for 24 hours.\n"
            "A private channel opens for just the two of you, "
            "with a conversation starter already waiting."
        ),
        color=color,
    )
    label = f"{pool_size} member{'s' if pool_size != 1 else ''} waiting" if pool_size else "No one waiting yet"
    embed.add_field(name="Pool", value=label, inline=True)
    embed.set_footer(text="Matches open a private channel visible to just the two of you (and admins).")
    return embed


def _build_panel_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(_PenPalsPanelJoinButton())
    view.add_item(_PenPalsPanelLeaveButton())
    return view


# One refresh at a time per guild: concurrent delete+repost calls would each
# post a panel and orphan all but the last-saved message.
_panel_refresh_locks: dict[int, asyncio.Lock] = {}


async def _refresh_panel(
    bot: discord.Client,
    db_path: Path,
    guild_id: int,
    *,
    repost: bool = False,
) -> None:
    """Edit the panel embed in place (or delete+repost when repost=True)."""
    lock = _panel_refresh_locks.setdefault(guild_id, asyncio.Lock())
    async with lock:
        await _refresh_panel_locked(bot, db_path, guild_id, repost=repost)


async def _refresh_panel_locked(
    bot: discord.Client,
    db_path: Path,
    guild_id: int,
    *,
    repost: bool = False,
) -> None:
    def _load():
        with open_db(db_path) as conn:
            cfg = _get_config(conn, guild_id)
            pool_size = len(_get_pool(conn, guild_id))
            return cfg, pool_size

    cfg, pool_size = await asyncio.to_thread(_load)
    if cfg is None or not cfg["panel_channel_id"]:
        return

    panel_channel_id = int(cfg["panel_channel_id"])
    panel_message_id = int(cfg["panel_message_id"] or 0)

    channel = bot.get_channel(panel_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    guild = bot.get_guild(guild_id)
    accent = await resolve_accent_color(db_path, guild) if guild else None
    embed = _build_panel_embed(pool_size, color=accent)
    view = _build_panel_view()

    if not repost and panel_message_id:
        try:
            old = await channel.fetch_message(panel_message_id)
            await old.edit(embed=embed, view=view)
            return
        except (discord.NotFound, discord.HTTPException):
            pass

    if panel_message_id:
        try:
            old = await channel.fetch_message(panel_message_id)
            await old.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    msg = await channel.send(embed=embed, view=view)

    def _save(mid: int = msg.id):
        with open_db(db_path) as conn:
            _set_panel_message_id(conn, guild_id, mid)

    await asyncio.to_thread(_save)


# ── Join / leave flows (shared by the panel buttons and slash commands) ──────


async def _handle_join(interaction: discord.Interaction, db_path: Path) -> None:
    if not interaction.guild:
        await interaction.response.send_message("This only works in a server.", ephemeral=True)
        return

    guild = interaction.guild
    guild_id = guild.id
    user_id = interaction.user.id

    def _load_cfg():
        with open_db(db_path) as conn:
            return _get_config(conn, guild_id)

    cfg = await asyncio.to_thread(_load_cfg)
    if cfg is None or not cfg["enabled"]:
        await interaction.response.send_message(
            "Pen Pals isn't set up yet — ask an admin.", ephemeral=True
        )
        return

    if cfg["opt_in_role_id"]:
        role = guild.get_role(int(cfg["opt_in_role_id"]))
        member = guild.get_member(user_id)
        if role is not None and (member is None or role not in member.roles):
            await interaction.response.send_message(
                f"You need the **{role.name}** role to join Pen Pals.", ephemeral=True
            )
            return

    def _check() -> str:
        with open_db(db_path) as conn:
            if _get_active_session(conn, guild_id, user_id):
                return "active"
            if _in_pool(conn, guild_id, user_id):
                return "in_pool"
            _add_to_pool(conn, guild_id, user_id)
            return "queued"

    status = await asyncio.to_thread(_check)

    if status == "active":
        await interaction.response.send_message(
            "You already have an active pen pal. Use `/penpals status` to see it.", ephemeral=True
        )
        return
    if status == "in_pool":
        await interaction.response.send_message(
            "You're already in the pool. Use `/penpals status` to check your position.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        "✅ You're in the pool! You'll get a private channel the next time matches are drawn.",
        ephemeral=True,
    )
    await _refresh_panel(interaction.client, db_path, guild_id)


async def _handle_leave(interaction: discord.Interaction, db_path: Path) -> None:
    if not interaction.guild:
        await interaction.response.send_message("This only works in a server.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    user_id = interaction.user.id

    def _remove():
        with open_db(db_path) as conn:
            if not _in_pool(conn, guild_id, user_id):
                return False
            _remove_from_pool(conn, guild_id, user_id)
            return True

    removed = await asyncio.to_thread(_remove)
    if removed:
        await interaction.response.send_message("You've left the Pen Pals pool.", ephemeral=True)
        await _refresh_panel(interaction.client, db_path, guild_id)
    else:
        await interaction.response.send_message(
            "You're not in the pool. Use `/penpals status` to check your status.", ephemeral=True
        )


class _PenPalsPanelJoinButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"pen_pals:join",
):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Join Pool",
                emoji="✉️",
                style=discord.ButtonStyle.success,
                custom_id="pen_pals:join",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = cast("Bot", interaction.client).ctx
        await _handle_join(interaction, ctx.db_path)


class _PenPalsPanelLeaveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"pen_pals:leave",
):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Leave Pool",
                emoji="🚪",
                style=discord.ButtonStyle.secondary,
                custom_id="pen_pals:leave",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = cast("Bot", interaction.client).ctx
        await _handle_leave(interaction, ctx.db_path)


# ── Confirm view for /penpals end ─────────────────────────────────────────────


class _EndConfirmView(discord.ui.View):
    def __init__(
        self,
        db_path: Path,
        session_id: str,
        channel: discord.TextChannel,
        other_user_id: int,
        invoker_id: int,
    ) -> None:
        super().__init__(timeout=15)
        self.db_path = db_path
        self.session_id = session_id
        self.channel = channel
        self.other_user_id = other_user_id
        self.invoker_id = invoker_id
        self._msg: discord.Message | None = None
        self._done = False

    @discord.ui.button(label="End Session", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who initiated can confirm.", ephemeral=True)
            return
        self._done = True
        self.stop()
        await interaction.response.edit_message(content="Closing pen pal session…", view=None)

        # DM the other member before the channel (and this interaction) go away
        guild = self.channel.guild
        other = guild.get_member(self.other_user_id) if guild else None
        if other:
            try:
                await other.send(
                    f"Your pen pal session in **{guild.name}** was ended early by your partner."
                )
            except discord.HTTPException:
                pass

        # Delete the channel first, then close the session row. If the close
        # doesn't happen (crash), the loop finds the channel missing and marks
        # the session closed itself — the reverse order could orphan a live
        # channel behind an already-closed session.
        try:
            await self.channel.delete(reason="Pen Pals ended early")
        except discord.HTTPException as exc:
            log.warning("pen_pals: failed to delete channel %d on early end: %s", self.channel.id, exc)

        def _close(sid: str = self.session_id):
            with open_db(self.db_path) as conn:
                _close_session(conn, sid, "early")
        await asyncio.to_thread(_close)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._done = True
        self.stop()
        await interaction.response.edit_message(content="Close cancelled.", view=None)

    async def on_timeout(self) -> None:
        if self._done:
            return
        if self._msg:
            try:
                await self._msg.edit(content="Close cancelled.", view=None)
            except discord.HTTPException:
                pass


# ── Cog ───────────────────────────────────────────────────────────────────────


class PenPalsCog(commands.Cog):
    penpals = app_commands.Group(
        name="penpals",
        description="Pen Pals — get matched with someone for a 24-hour private chat.",
    )

    def __init__(self, bot: "Bot", ctx: "AppContext") -> None:
        self.bot = bot
        self.ctx = ctx
        self._panel_channels: dict[int, int] = {}  # panel_channel_id → guild_id
        self._panel_repost_pending: set[int] = set()  # guild_ids with a repost queued
        super().__init__()

    async def cog_load(self) -> None:
        bot = self.bot
        db_path = self.ctx.db_path

        bot.add_dynamic_items(_PenPalsPanelJoinButton)
        bot.add_dynamic_items(_PenPalsPanelLeaveButton)

        def _load_panels():
            with open_db(db_path) as conn:
                rows = conn.execute(
                    "SELECT guild_id, panel_channel_id FROM pen_pals_config WHERE panel_channel_id != 0"
                ).fetchall()
                return {int(r["panel_channel_id"]): int(r["guild_id"]) for r in rows}

        self._panel_channels = await asyncio.to_thread(_load_panels)
        self.bot.startup_task_factories.append(lambda: _pen_pals_loop(bot, db_path))

    @commands.Cog.listener("on_message")
    async def _on_message_panel(self, message: discord.Message) -> None:
        if message.guild is None or message.author.id == self.bot.user.id:  # type: ignore[union-attr]
            return
        guild_id = self._panel_channels.get(message.channel.id)
        if guild_id is None:
            return
        # Debounce: a burst of messages triggers a single repost after a short
        # settle window instead of one delete+send per message.
        if guild_id in self._panel_repost_pending:
            return
        self._panel_repost_pending.add(guild_id)
        try:
            await asyncio.sleep(2)
        finally:
            self._panel_repost_pending.discard(guild_id)
        try:
            await _refresh_panel(self.bot, self.ctx.db_path, guild_id, repost=True)
        except discord.HTTPException as exc:
            log.warning("pen_pals: panel repost failed in guild %d: %s", guild_id, exc)

    @commands.Cog.listener("on_pen_pals_panel_refresh")
    async def _on_panel_refresh(
        self, guild_id: int, new_channel_id: int, old_channel_id: int, old_message_id: int
    ) -> None:
        # Delete old panel from the previous channel if the channel changed
        if old_channel_id and old_channel_id != new_channel_id and old_message_id:
            old_ch = self.bot.get_channel(old_channel_id)
            if isinstance(old_ch, discord.TextChannel):
                try:
                    old = await old_ch.fetch_message(old_message_id)
                    await old.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

            def _clear():
                with open_db(self.ctx.db_path) as conn:
                    _set_panel_message_id(conn, guild_id, 0)

            await asyncio.to_thread(_clear)

        # Update in-memory channel map
        self._panel_channels = {ch: g for ch, g in self._panel_channels.items() if g != guild_id}
        if new_channel_id:
            self._panel_channels[new_channel_id] = guild_id
            await _refresh_panel(self.bot, self.ctx.db_path, guild_id, repost=True)

    # ── /penpals join ─────────────────────────────────────────────────

    @penpals.command(name="join", description="Join the Pen Pals pool to be matched in the next round.")
    async def penpals_join(self, interaction: discord.Interaction) -> None:
        await _handle_join(interaction, self.ctx.db_path)

    # ── /penpals leave ────────────────────────────────────────────────

    @penpals.command(name="leave", description="Leave the Pen Pals pool before being matched.")
    async def penpals_leave(self, interaction: discord.Interaction) -> None:
        await _handle_leave(interaction, self.ctx.db_path)

    # ── /penpals status ───────────────────────────────────────────────

    @penpals.command(name="status", description="Check your current Pen Pals status.")
    async def penpals_status(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id
        db_path = self.ctx.db_path

        def _check():
            with open_db(db_path) as conn:
                session = _get_active_session(conn, guild_id, user_id)
                if session:
                    return "active", dict(session)
                pool = [r["user_id"] for r in _get_pool(conn, guild_id)]
                if user_id in pool:
                    return "pool", pool.index(user_id) + 1
                return "none", None

        status, data = await asyncio.to_thread(_check)

        if status == "active":
            assert isinstance(data, dict)
            ch = interaction.guild.get_channel(data["channel_id"])
            other_id = data["user2_id"] if data["user1_id"] == user_id else data["user1_id"]
            other = interaction.guild.get_member(other_id)
            expiry_at = int(data["expiry_at"])
            swaps_left = _MAX_SWAPS - data["question_swaps_used"]
            lines = [
                f"You have an active pen pal: {other.mention if other else f'<@{other_id}>'}",
                f"Channel: {ch.mention if ch else '(channel missing)'}",
                f"Expires: <t:{expiry_at}:R>",
                f"Question swaps remaining: **{swaps_left}**",
            ]
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
        elif status == "pool":
            pos = data
            await interaction.response.send_message(
                f"You're in the pool at position **#{pos}**. Hang tight!", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "You're not in the pool and have no active pen pal. Use `/penpals join` to sign up.",
                ephemeral=True,
            )

    # ── /penpals new-question ─────────────────────────────────────────

    @penpals.command(name="new-question", description="Swap the current question for a fresh one (3 times max).")
    async def penpals_new_question(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        db_path = self.ctx.db_path
        if interaction.channel_id is None:
            await interaction.response.send_message("This command only works in an active pen pal channel.", ephemeral=True)
            return
        channel_id: int = interaction.channel_id

        def _load():
            with open_db(db_path) as conn:
                return _get_session_by_channel(conn, channel_id)

        session = await asyncio.to_thread(_load)
        if session is None:
            await interaction.response.send_message(
                "This command only works in an active pen pal channel.", ephemeral=True
            )
            return

        if interaction.user.id not in (session["user1_id"], session["user2_id"]):
            await interaction.response.send_message(
                "Only the two pen pals can swap the question.", ephemeral=True
            )
            return

        if session["question_swaps_used"] >= _MAX_SWAPS:
            await interaction.response.send_message(
                "You've used all 3 question swaps for this session.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        def _load_cfg():
            with open_db(db_path) as conn:
                return _get_config(conn, session["guild_id"])

        cfg = await asyncio.to_thread(_load_cfg)
        question = await _draw_question(db_path, session["session_id"], _cfg_allows_nsfw(cfg))

        def _save():
            with open_db(db_path) as conn:
                swaps_used = _increment_swaps(conn, session["session_id"])
                _record_question(conn, session["session_id"], question)
                return swaps_used

        swaps_used = await asyncio.to_thread(_save)
        swaps_left = _MAX_SWAPS - swaps_used

        user1_id = session["user1_id"]
        user2_id = session["user2_id"]
        chan = interaction.channel
        if isinstance(chan, discord.TextChannel):
            try:
                await chan.send(
                    f"<@{user1_id}> <@{user2_id}>\n"
                    f"🔄 New question ({swaps_left} swap{'s' if swaps_left != 1 else ''} remaining):\n"
                    f"> {question}"
                )
            except discord.HTTPException as exc:
                log.warning("pen_pals: failed to post swap question in %d: %s", channel_id, exc)

        await interaction.followup.send("Question swapped!", ephemeral=True)

    # ── /penpals end ──────────────────────────────────────────────────

    @penpals.command(name="end", description="End your current pen pal session early.")
    async def penpals_end(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        db_path = self.ctx.db_path
        user_id = interaction.user.id
        if interaction.channel_id is None:
            await interaction.response.send_message("This command only works in your active pen pal channel.", ephemeral=True)
            return
        channel_id: int = interaction.channel_id

        def _load():
            with open_db(db_path) as conn:
                return _get_session_by_channel(conn, channel_id)

        session = await asyncio.to_thread(_load)
        if session is None or user_id not in (session["user1_id"], session["user2_id"]):
            await interaction.response.send_message(
                "This command only works in your active pen pal channel.", ephemeral=True
            )
            return

        chan = interaction.channel
        if not isinstance(chan, discord.TextChannel):
            await interaction.response.send_message("This command only works in your active pen pal channel.", ephemeral=True)
            return

        other_id = session["user2_id"] if session["user1_id"] == user_id else session["user1_id"]
        view = _EndConfirmView(
            db_path=db_path,
            session_id=session["session_id"],
            channel=chan,
            other_user_id=other_id,
            invoker_id=user_id,
        )
        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this pen pal session early?", view=view, ephemeral=True
        )
        msg = await interaction.original_response()
        view._msg = msg

    # ── /penpals pair (admin) ─────────────────────────────────────────

    @penpals.command(name="pair", description="Force-pair two specific members.")
    @app_commands.describe(user1="First member", user2="Second member")
    @app_commands.default_permissions(manage_guild=True)
    async def penpals_pair(
        self,
        interaction: discord.Interaction,
        user1: discord.Member,
        user2: discord.Member,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        if user1 == user2:
            await interaction.response.send_message("You can't pair someone with themselves.", ephemeral=True)
            return

        db_path = self.ctx.db_path
        guild_id = interaction.guild.id

        def _check():
            with open_db(db_path) as conn:
                cfg = _get_config(conn, guild_id)
                if cfg is None or not cfg["enabled"]:
                    return "disabled", None
                s1 = _get_active_session(conn, guild_id, user1.id)
                s2 = _get_active_session(conn, guild_id, user2.id)
                return "ok", (s1, s2)

        status, data = await asyncio.to_thread(_check)
        if status == "disabled":
            await interaction.response.send_message("Pen Pals isn't enabled on this server.", ephemeral=True)
            return

        assert data is not None
        s1, s2 = data
        if s1:
            await interaction.response.send_message(f"{user1.mention} already has an active pen pal.", ephemeral=True)
            return
        if s2:
            await interaction.response.send_message(f"{user2.mention} already has an active pen pal.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        success = await _do_pair(self.bot, db_path, guild_id, user1.id, user2.id)
        if success:
            await interaction.followup.send(
                f"✅ Paired {user1.mention} × {user2.mention}.", ephemeral=True
            )
        else:
            await interaction.followup.send("Failed to create the channel — check bot permissions.", ephemeral=True)

    # ── /penpals round (admin) ────────────────────────────────────────

    @penpals.command(name="round", description="Pair everyone currently in the pool.")
    @app_commands.default_permissions(manage_guild=True)
    async def penpals_round(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        pairs, left = await _do_round(self.bot, self.ctx.db_path, interaction.guild.id)
        msg = f"✅ Paired **{pairs}** {'pair' if pairs == 1 else 'pairs'}."
        if left:
            msg += f" **{left}** member{'s' if left != 1 else ''} still in the pool (waiting or on cooldown)."
        else:
            msg += " Pool is now empty."
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: "Bot") -> None:
    await bot.add_cog(PenPalsCog(bot, bot.ctx))
