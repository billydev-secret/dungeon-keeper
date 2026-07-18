"""Rules Watch — Discord embed posting and persistent label-capture buttons."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot_modules.core.utils import disable_all_items

from bot_modules.rules_watch import service
from bot_modules.rules_watch.scorer import PriorityResult, Signals, TargetResult

if TYPE_CHECKING:
    from bot_modules.services.ai_moderation_service import RulesWatchGuardResult

log = logging.getLogger("dungeonkeeper.rules_watch")

# Embed color per tier
_COLOR = {
    "immediate": discord.Color.red(),
    "digest": discord.Color.orange(),
    "logged": discord.Color.light_gray(),
}


# ---------------------------------------------------------------------------
# Embed construction
# ---------------------------------------------------------------------------

def _build_embed(
    message: discord.Message,
    event_id: int,
    guard: RulesWatchGuardResult,
    sigs: Signals,
    priority: PriorityResult,
    target: TargetResult,
) -> discord.Embed:
    rule_label = f"Rule {guard.rule}" if guard.rule else "Unknown Rule"
    conf_pct = f"{guard.confidence:.0%}"
    channel_name = getattr(message.channel, "name", str(message.channel.id))

    # Resolve target display name from guild cache
    target_mention = "`unknown`"
    if target.target_id and message.guild:
        m = message.guild.get_member(target.target_id)
        target_mention = m.mention if m else f"`{target.target_id}`"

    embed = discord.Embed(
        title=f"🚨 Rules Watch — {rule_label} ({conf_pct} confidence)",
        color=_COLOR.get(priority.tier, discord.Color.greyple()),
    )
    embed.add_field(
        name="Message",
        value=f"**Author:** {message.author.mention} → **Target:** {target_mention}\n"
              f"**Channel:** #{channel_name}\n"
              f"[Jump to message]({message.jump_url})",
        inline=False,
    )

    # Context window excerpt (truncated)
    content_preview = (message.content or "*[no text content]*")[:500]
    if guard.reason:
        content_preview += f"\n\n*Guard: {guard.reason}*"
    embed.add_field(name="Content", value=content_preview, inline=False)

    # Priority + signals chip line
    sig_parts = [
        f"priority: **{priority.score:.1f}**",
        f"mutual: {sigs.mutual_interaction_count}",
        f"persist: {sigs.persistence_count}",
    ]
    if sigs.consent_pair_active:
        sig_parts.append("consent ✓")
    if sigs.consent_pair_recently_revoked:
        sig_parts.append("⚠️ consent revoked")
    if sigs.boundary_token_crossed:
        sig_parts.append("🛑 boundary token")
    if sigs.slur_signal:
        sig_parts.append("🔴 slur")
    if sigs.target_withdrew:
        sig_parts.append("😶 target withdrew")

    embed.add_field(
        name=f"Signals — {priority.reason}",
        value=" | ".join(sig_parts),
        inline=False,
    )
    embed.set_footer(text=f"Event #{event_id} · target confidence: {target.confidence}")
    return embed


# ---------------------------------------------------------------------------
# Persistent button view
# ---------------------------------------------------------------------------

class LabelView(discord.ui.View):
    """Persistent view; registered on bot startup via add_view()."""

    def __init__(self, event_id: int, db_path) -> None:
        super().__init__(timeout=None)
        self.event_id = event_id
        self.db_path = db_path
        # Stable custom_ids so they survive bot restarts
        self.confirm_button.custom_id = f"rw_confirm:{event_id}"
        self.dismiss_button.custom_id = f"rw_dismiss:{event_id}"

    @discord.ui.button(label="✅ Confirmed violation", style=discord.ButtonStyle.danger)
    async def confirm_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        from bot_modules.core.db_utils import open_db
        with open_db(self.db_path) as conn:
            service.upsert_label(
                conn,
                self.event_id,
                is_violation=True,
                labeled_by=interaction.user.id,
            )
        await interaction.response.send_message(
            f"Labeled event #{self.event_id} as **violation**. Thank you.", ephemeral=True
        )
        await _disable_view(interaction.message, self)

    @discord.ui.button(label="❌ False positive", style=discord.ButtonStyle.secondary)
    async def dismiss_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        from bot_modules.core.db_utils import open_db
        with open_db(self.db_path) as conn:
            service.upsert_label(
                conn,
                self.event_id,
                is_violation=False,
                labeled_by=interaction.user.id,
            )
        await interaction.response.send_message(
            f"Labeled event #{self.event_id} as **false positive**. Dismissed.", ephemeral=True
        )
        await _disable_view(interaction.message, self)


async def _disable_view(
    msg: discord.Message | None, view: discord.ui.View
) -> None:
    """Gray out all buttons after labeling."""
    if msg is None:
        return
    disable_all_items(view)
    try:
        await msg.edit(view=view)
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------------
# Public entry point called by monitor.py
# ---------------------------------------------------------------------------

async def post_immediate_alert(
    channel: discord.TextChannel | discord.Thread,
    message: discord.Message,
    event_id: int,
    guard: RulesWatchGuardResult,
    sigs: Signals,
    priority: PriorityResult,
    target: TargetResult,
    db_path=None,
) -> discord.Message | None:
    embed = _build_embed(message, event_id, guard, sigs, priority, target)
    try:
        if db_path is not None:
            return await channel.send(embed=embed, view=LabelView(event_id, db_path))
        return await channel.send(embed=embed)
    except discord.HTTPException as exc:
        log.warning("rules_watch: failed to post alert for event %s: %s", event_id, exc)
        return None


def register_persistent_views(bot, db_path) -> None:
    """Re-register LabelView instances for all unlabeled events that have a posted alert.

    Call once at startup (after extensions are loaded) so buttons survive bot restarts.
    """
    from bot_modules.core.db_utils import open_db
    try:
        with open_db(db_path) as conn:
            rows = conn.execute(
                """
                SELECT e.id FROM rules_events e
                LEFT JOIN rules_labels l ON l.event_id = e.id
                WHERE e.alert_message_id IS NOT NULL AND l.event_id IS NULL
                """
            ).fetchall()
        for row in rows:
            bot.add_view(LabelView(row["id"], db_path))
        log.debug("rules_watch: registered %d persistent label views", len(rows))
    except Exception:
        log.exception("rules_watch: failed to register persistent views")


def build_digest_embed(
    events: list,
    guild: discord.Guild,
) -> discord.Embed:
    """Build a summary embed for /rules-watch digest."""
    embed = discord.Embed(
        title=f"📋 Rules Watch Digest — {len(events)} pending",
        color=discord.Color.orange(),
        description="Events below have not yet been labeled. Use the web dashboard or "
                    "`/rules-watch label` to review.",
    )
    for ev in events[:10]:
        rule = ev["guard_rule"] or "?"
        score = ev["priority_score"] or 0
        author_id = ev["author_id"]
        m = guild.get_member(author_id)
        author_name = m.display_name if m else f"User {author_id}"
        ch = guild.get_channel(ev["channel_id"])
        ch_name = f"#{ch.name}" if ch and hasattr(ch, "name") else str(ev["channel_id"])
        embed.add_field(
            name=f"Event #{ev['id']} — Rule {rule} ({score:.1f})",
            value=f"{author_name} in {ch_name} · {ev['priority_reason'] or ''}",
            inline=False,
        )
    if len(events) > 10:
        embed.set_footer(text=f"Showing 10 of {len(events)}. See dashboard for full list.")
    return embed
