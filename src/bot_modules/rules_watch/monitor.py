"""Rules Watch — passive all-channel monitor cog.

Hooks into every public guild message, applies a cheap pre-filter, then runs
the guard model and context scorer to produce a priority-tiered event.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot_modules.core.db_utils import get_config_value
from bot_modules.rules_watch import service
from bot_modules.rules_watch.scorer import (
    Signals,
    TargetResult,
    compute_priority,
    compute_thread_reciprocity,
    compute_vader_trajectory,
    count_persistence,
    detect_boundary_crossing,
    detect_slur,
    get_consent_state,
    get_mutual_count,
    get_reciprocity_ratio,
    identify_target,
    is_dm_tier_mismatch,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.rules_watch")

# Messages within this many seconds after the monitored event to consider as
# "target replied" when checking for withdrawal.
_WITHDRAWAL_CHECK_DELAY = 30 * 60  # 30 minutes

# Number of surrounding messages to include in the conversation window.
_WINDOW_SIZE = 8


class RulesWatchMonitor(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_enabled(self, guild_id: int) -> bool:
        with self.ctx.open_db() as conn:
            return (
                get_config_value(conn, "rules_watch_enabled", "0", guild_id).strip() == "1"
            )

    def _alert_channel_id(self, guild_id: int) -> int:
        with self.ctx.open_db() as conn:
            raw = get_config_value(conn, "rules_watch_channel_id", "0", guild_id)
        try:
            return int(raw.strip())
        except ValueError:
            return 0

    # ------------------------------------------------------------------
    # Main listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Hard filters
        if message.author.bot:
            return
        if not message.guild:
            return
        guild_id = message.guild.id
        if not self._is_enabled(guild_id):
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return

        asyncio.create_task(
            self._process(message),
            name=f"rules_watch:{message.id}",
        )

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    async def _process(self, message: discord.Message) -> None:
        guild_id = message.guild.id  # type: ignore[union-attr]
        channel_id = message.channel.id
        author_id = message.author.id
        content = message.content or ""

        # Pre-compute message fields before entering the thread so Discord
        # cache reads stay on the event-loop thread.
        mention_ids = [m.id for m in message.mentions if not m.bot]
        reply_to_id = (
            message.reference.message_id
            if message.reference and message.reference.message_id
            else None
        )
        message_id = message.id
        message_created_ts = message.created_at.timestamp()
        message_author_display = getattr(message.author, "display_name", f"User {author_id}")
        guild_ref = message.guild  # checked non-None in on_message

        def _do_pre_filter_and_window():
            with self.ctx.open_db() as conn:
                # 1. Pre-filter ---------------------------------------------------
                vader_row = conn.execute(
                    "SELECT sentiment FROM messages WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                _vader_compound: float | None = None
                if vader_row and vader_row["sentiment"] is not None:
                    _vader_compound = float(vader_row["sentiment"])

                _slur_hit = detect_slur(content)

                target_preliminary = identify_target(
                    conn, guild_id, channel_id, author_id,
                    reply_to_id, mention_ids,
                )

                # A boundary event is the TARGET telling THIS author to stop and
                # the author continuing — not the author's own use of the word
                # "no". Requires a target, so it is computed here rather than
                # from `content`.
                _persistence = 0
                _boundary_hit = False
                if target_preliminary.target_id is not None:
                    _persistence = count_persistence(
                        conn, guild_id, channel_id, author_id,
                        target_preliminary.target_id,
                    )
                    _boundary_hit = detect_boundary_crossing(
                        conn, guild_id, channel_id, author_id,
                        target_preliminary.target_id, message_created_ts,
                    )

                # Pre-filter gate: skip LLM unless at least one signal fires
                if not (
                    (_vader_compound is not None and _vader_compound < -0.25)
                    or _boundary_hit
                    or _slur_hit
                    or _persistence >= 3
                ):
                    return None

                # 2. Build conversation window ------------------------------------
                window_rows = conn.execute(
                    """
                    SELECT m.message_id, m.author_id, m.content, m.reply_to_id, m.ts,
                           ku.display_name
                    FROM messages m
                    LEFT JOIN known_users ku
                        ON ku.user_id = m.author_id AND ku.guild_id = m.guild_id
                    WHERE m.guild_id = ? AND m.channel_id = ?
                    ORDER BY m.ts DESC
                    LIMIT ?
                    """,
                    (guild_id, channel_id, _WINDOW_SIZE),
                ).fetchall()
                window_rows = list(reversed(window_rows))  # oldest first

                if window_rows:
                    wids = [r["message_id"] for r in window_rows]
                    placeholders = ",".join("?" * len(wids))
                    mention_rows = conn.execute(
                        f"SELECT message_id, user_id FROM message_mentions WHERE message_id IN ({placeholders})",
                        wids,
                    ).fetchall()
                    _msg_mentions: dict[int, list[int]] = {}
                    for mr in mention_rows:
                        _msg_mentions.setdefault(mr["message_id"], []).append(mr["user_id"])
                else:
                    _msg_mentions = {}

                id_to_name: dict[int, str] = {}
                for r in window_rows:
                    aid = r["author_id"]
                    if aid not in id_to_name:
                        mem = guild_ref.get_member(aid) if guild_ref is not None else None
                        id_to_name[aid] = (
                            mem.display_name if mem
                            else (r["display_name"] or f"User {aid}")
                        )

                id_to_author = {r["message_id"]: r["author_id"] for r in window_rows}
                _window_lines: list[str] = []
                for r in window_rows:
                    ts_str = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%H:%M")
                    name = id_to_name.get(r["author_id"], f"User {r['author_id']}")
                    text = (r["content"] or "").replace("\n", " ")[:400]
                    reply_note = ""
                    if r["reply_to_id"] and r["reply_to_id"] in id_to_author:
                        reply_author_name = id_to_name.get(
                            id_to_author[r["reply_to_id"]], "?"
                        )
                        reply_note = f" [↩ replying to {reply_author_name}]"
                    _window_lines.append(f"[{ts_str}] {name}{reply_note}: {text}")

                # The triggering message is dispatched (create_task) concurrently
                # with the events_cog message store, and _process reaches this
                # window build before the store completes — so the message is
                # usually NOT yet in the `messages` table above. Append it
                # explicitly from memory so the guard evaluates the message the
                # alert is actually about, as the final (most-recent) line.
                if message_id not in id_to_author:
                    trig_ts = datetime.fromtimestamp(
                        message_created_ts, tz=timezone.utc
                    ).strftime("%H:%M")
                    trig_name = id_to_name.get(author_id) or message_author_display
                    trig_reply = ""
                    if reply_to_id and reply_to_id in id_to_author:
                        trig_reply = f" [↩ replying to {id_to_name.get(id_to_author[reply_to_id], '?')}]"
                    trig_text = content.replace("\n", " ")[:400]
                    _window_lines.append(f"[{trig_ts}] {trig_name}{trig_reply}: {trig_text}")

                _window_text = "\n".join(_window_lines)

                # 3. Context signals (DB-bound) -----------------------------------
                _target = identify_target(
                    conn, guild_id, channel_id, author_id,
                    reply_to_id, mention_ids,
                    window_messages=[
                        {"author_id": r["author_id"], "mentions": _msg_mentions.get(r["message_id"], [])}
                        for r in window_rows
                    ],
                )
                _tid = _target.target_id

                _mutual_count = get_mutual_count(conn, guild_id, author_id, _tid) if _tid else 0
                _recip_ratio = get_reciprocity_ratio(conn, guild_id, author_id, _tid) if _tid else None
                _consent_active, _consent_revoked = (
                    get_consent_state(conn, guild_id, author_id, _tid) if _tid else (False, False)
                )
                _thread_recip = (
                    compute_thread_reciprocity(conn, guild_id, channel_id, author_id, _tid)
                    if _tid else None
                )
                _vader_traj = compute_vader_trajectory(conn, guild_id, channel_id, _tid) if _tid else None
                _tenure = service.compute_tenure_days(conn, guild_id, author_id)

            return (
                _vader_compound, _boundary_hit, _slur_hit, _persistence,
                _window_text, _window_lines, _target, _tid,
                _mutual_count, _recip_ratio, _consent_active, _consent_revoked,
                _thread_recip, _vader_traj, _tenure,
            )

        pre = await asyncio.to_thread(_do_pre_filter_and_window)
        if pre is None:
            return
        (
            vader_compound, boundary_hit, slur_hit, persistence,
            window_text, window_lines, target, tid,
            mutual_count, recip_ratio, consent_active, consent_revoked,
            thread_recip, vader_traj, tenure,
        ) = pre

        # DM tier mismatch requires live discord.Member objects
        author_mode = "ask"
        target_mode = "ask"
        if tid and message.guild:
            from bot_modules.services.dm_perms_service import (
                get_dm_mode_role_ids,
                resolve_mode,
            )
            mode_role_ids = await asyncio.to_thread(
                get_dm_mode_role_ids, self.ctx.db_path, guild_id
            )
            target_member = message.guild.get_member(tid)
            author_member = message.guild.get_member(author_id)
            if author_member:
                author_mode = resolve_mode(author_member, mode_role_ids)
            if target_member:
                target_mode = resolve_mode(target_member, mode_role_ids)
        tier_mismatch = is_dm_tier_mismatch(author_mode, target_mode) if tid else False

        # 4. Guard model call -------------------------------------------------------
        from bot_modules.services import ollama_client
        if not ollama_client.is_available():
            return

        is_nsfw = getattr(message.channel, "nsfw", False)
        try:
            from bot_modules.services.ai_moderation_service import ai_rules_watch_check
            guard = await ai_rules_watch_check(
                window_text,
                channel_is_nsfw=is_nsfw,
                db_path=self.ctx.db_path,
                guild_id=guild_id,
            )
        except Exception:
            log.exception("rules_watch guard model error for message %s", message.id)
            return

        # 5. Priority scoring -------------------------------------------------------
        sigs = Signals(
            guard_verdict=guard.verdict,
            guard_rule=guard.rule,
            guard_confidence=guard.confidence,
            slur_signal=slur_hit,
            vader_compound=vader_compound,
            vader_trajectory=vader_traj,
            boundary_token_crossed=boundary_hit,
            target_confidence=target.confidence,
            mutual_interaction_count=mutual_count,
            reciprocity_ratio=recip_ratio,
            consent_pair_active=consent_active,
            consent_pair_recently_revoked=consent_revoked,
            dm_tier_mismatch=tier_mismatch,
            thread_reciprocity_ratio=thread_recip,
            persistence_count=persistence,
            tenure_days=tenure,
        )
        priority = compute_priority(sigs)

        # 6. Store event -----------------------------------------------------------
        import json as _json
        _window_json = _json.dumps(window_lines)
        _target_confidence = target.confidence
        _guard_verdict = guard.verdict
        _guard_rule = guard.rule
        _guard_reason = guard.reason
        _guard_confidence = guard.confidence
        _priority_score = priority.score
        _priority_tier = priority.tier
        _priority_reason = priority.reason

        def _do_store_event():
            with self.ctx.open_db() as conn:
                return service.insert_event(
                    conn,
                    guild_id=guild_id,
                    message_id=message_id,
                    author_id=author_id,
                    channel_id=channel_id,
                    target_id=tid,
                    target_confidence=_target_confidence,
                    window_json=_window_json,
                    guard_verdict=_guard_verdict,
                    guard_rule=_guard_rule,
                    guard_reason=_guard_reason,
                    guard_confidence=_guard_confidence,
                    slur_signal=int(slur_hit),
                    vader_compound=vader_compound,
                    vader_trajectory=vader_traj,
                    mutual_interaction_count=mutual_count,
                    reciprocity_ratio=recip_ratio,
                    consent_pair_active=int(consent_active),
                    consent_pair_recently_revoked=int(consent_revoked),
                    dm_tier_mismatch=int(tier_mismatch),
                    thread_reciprocity_ratio=thread_recip,
                    persistence_count=persistence,
                    boundary_token_crossed=int(boundary_hit),
                    tenure_days=tenure,
                    priority_score=_priority_score,
                    priority_tier=_priority_tier,
                    priority_reason=_priority_reason,
                )

        event_id = await asyncio.to_thread(_do_store_event)

        # 7. Alert routing ---------------------------------------------------------
        if priority.tier == "immediate":
            asyncio.create_task(
                self._post_alert(message, event_id, guard, sigs, priority, target),
                name=f"rules_watch_alert:{event_id}",
            )

        # 8. Withdrawal check: re-examine in 30 minutes ---------------------------
        if priority.tier in ("immediate", "digest") and tid is not None:
            asyncio.create_task(
                self._check_withdrawal(
                    event_id, guild_id, channel_id, tid, priority, sigs
                ),
                name=f"rules_watch_withdraw:{event_id}",
            )

    # ------------------------------------------------------------------
    # Alert posting (delegates to alert module)
    # ------------------------------------------------------------------

    async def _post_alert(
        self,
        message: discord.Message,
        event_id: int,
        guard,
        sigs: Signals,
        priority,
        target: TargetResult,
    ) -> None:
        from bot_modules.rules_watch.alert import post_immediate_alert

        guild_id = message.guild.id  # type: ignore[union-attr]
        channel_id = self._alert_channel_id(guild_id)
        if not channel_id:
            return
        alert_channel = self.bot.get_channel(channel_id)
        if not isinstance(alert_channel, (discord.TextChannel, discord.Thread)):
            return

        alert_msg = await post_immediate_alert(
            alert_channel, message, event_id, guard, sigs, priority, target,
            db_path=self.ctx.db_path,
        )
        if alert_msg:
            _alert_msg_id = alert_msg.id

            def _do_update_alert_msg():
                with self.ctx.open_db() as conn:
                    service.update_alert_message_id(conn, event_id, _alert_msg_id)

            await asyncio.to_thread(_do_update_alert_msg)

    # ------------------------------------------------------------------
    # Withdrawal async check
    # ------------------------------------------------------------------

    async def _check_withdrawal(
        self,
        event_id: int,
        guild_id: int,
        channel_id: int,
        target_id: int,
        original_priority,
        original_sigs: Signals,
    ) -> None:
        await asyncio.sleep(_WITHDRAWAL_CHECK_DELAY)
        try:
            def _do_withdrawal_check():
                with self.ctx.open_db() as conn:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) as cnt FROM messages
                        WHERE guild_id = ? AND channel_id = ? AND author_id = ?
                          AND ts > (
                              SELECT detected_at FROM rules_events WHERE id = ?
                          )
                        """,
                        (guild_id, channel_id, target_id, event_id),
                    ).fetchone()
                    withdrew = (row["cnt"] == 0) if row else False

                    if not withdrew:
                        service.update_withdrawal_flag(conn, event_id, withdrew=False)
                        return None  # sentinel: caller should return

                    # Re-score with withdrawal flag set
                    import dataclasses
                    new_sigs = dataclasses.replace(original_sigs, target_withdrew=True)
                    _new_priority = compute_priority(new_sigs)
                    service.update_withdrawal_flag(
                        conn, event_id, withdrew=True,
                        new_priority_score=_new_priority.score,
                        new_priority_tier=_new_priority.tier,
                        new_priority_reason=_new_priority.reason,
                    )
                return _new_priority

            new_priority = await asyncio.to_thread(_do_withdrawal_check)
            if new_priority is None:
                return

            # If tier escalated to immediate, post (or re-post note) to alert channel
            if (
                original_priority.tier != "immediate"
                and new_priority.tier == "immediate"
            ):
                channel_id_alert = self._alert_channel_id(guild_id)
                if channel_id_alert:
                    alert_ch = self.bot.get_channel(channel_id_alert)
                    if isinstance(alert_ch, (discord.TextChannel, discord.Thread)):
                        await alert_ch.send(
                            f"⚠️ **Rules Watch — Withdrawal escalation** (event `{event_id}`)\n"
                            f"Target went silent after the flagged message. "
                            f"Priority re-scored to **{new_priority.score:.1f}** "
                            f"({new_priority.reason}). Review queued event."
                        )
        except Exception:
            log.exception("rules_watch withdrawal check failed for event %s", event_id)


async def setup(bot: Bot) -> None:
    await bot.add_cog(RulesWatchMonitor(bot, bot.ctx))
