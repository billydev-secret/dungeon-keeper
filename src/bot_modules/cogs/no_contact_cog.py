"""No-contact list — member self-service command + the mention/reply watcher.

Configuration (where alerts go, which role is pinged) lives on the dashboard,
per the project's config-belongs-on-the-web rule. What lives HERE is member
self-service: a person adding an entry to protect themselves. Those are
different things, and the same paragraph of CLAUDE.md that sends admin config
to the web reserves Discord for member self-service and mod actions.

Making a member open a dashboard — or worse, ask a moderator — to protect
themselves puts the highest barrier exactly where someone is least willing to
explain themselves. ``/nocontact add`` is two taps from the message that
upset them. That is the entire justification for this command surface
existing after the audit that removed nine others.

Everything a member sees here is ephemeral, and nothing confirms the
existence of an entry created by the OTHER party — see
``no_contact_logic.is_visible_to``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.services import no_contact_service
from bot_modules.services.no_contact_logic import (
    KIND_MENTION,
    REMOVAL_DENIED_MISSING,
    alert_ping_prefix,
    alerts_for_message,
    can_remove,
    is_visible_to,
    jump_url,
    removal_denied_message,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.no_contact")

# Shown on both a fresh add and a duplicate. The wording must not vary with
# whether a row already existed: "you already have one" would tell him she
# had added one first.
ADDED_TEXT = (
    "✅ Done. **{name}** can no longer reach you through this bot — "
    "whispers, AMA questions, confession replies, Guess Who, Pen Pals "
    "matching, and voice rooms are all closed between you.\n\n"
    "They are **not** told about this. Use `/nocontact remove` if you ever "
    "want to undo it."
)

REMOVED_TEXT = "✅ Removed. **{name}** can reach you through the bot again."

SELF_TEXT = "❌ You can't add yourself to your own no-contact list."
BOT_TEXT = "❌ Bots can't be added to a no-contact list."


class NoContactCog(commands.Cog):
    """Member-facing no-contact commands and the mention/reply watcher."""

    nocontact = app_commands.Group(
        name="nocontact",
        description="Stop someone reaching you through the bot.",
        guild_only=True,
    )

    def __init__(self, bot: "Bot", ctx: "AppContext") -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    # ── Member self-service ──────────────────────────────────────────────

    @nocontact.command(
        name="add",
        description="Stop a member reaching you through any of the bot's features.",
    )
    @app_commands.describe(
        member="They won't be told, and they won't be able to tell.",
        reason="Optional, for moderators only. They never see it.",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: Optional[str] = None,
    ) -> None:
        if interaction.guild is None:
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(SELF_TEXT, ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message(BOT_TEXT, ephemeral=True)
            return

        await asyncio.to_thread(
            no_contact_service.add_pair,
            self.ctx.db_path,
            interaction.guild.id,
            interaction.user.id,
            member.id,
            created_by=interaction.user.id,
            protected_user_id=interaction.user.id,
            reason=(reason or "").strip(),
        )
        await interaction.response.send_message(
            ADDED_TEXT.format(name=member.display_name), ephemeral=True
        )

    @nocontact.command(
        name="remove", description="Undo a no-contact entry you added."
    )
    @app_commands.describe(member="The member to allow contact from again.")
    async def remove(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if interaction.guild is None:
            return
        entry = await asyncio.to_thread(
            no_contact_service.get_pair,
            self.ctx.db_path,
            interaction.guild.id,
            interaction.user.id,
            member.id,
        )
        protected = entry["protected_user_id"] if entry else None
        # A missing entry and an entry the caller may not know about produce
        # the SAME response — see ``removal_denied_message``.
        if entry is None or not can_remove(
            protected_user_id=protected,
            actor_id=interaction.user.id,
            actor_is_mod=False,
        ):
            await interaction.response.send_message(
                removal_denied_message(protected, actor_id=interaction.user.id)
                if entry
                else REMOVAL_DENIED_MISSING,
                ephemeral=True,
            )
            return

        await asyncio.to_thread(
            no_contact_service.remove_pair,
            self.ctx.db_path,
            interaction.guild.id,
            interaction.user.id,
            member.id,
        )
        await interaction.response.send_message(
            REMOVED_TEXT.format(name=member.display_name), ephemeral=True
        )

    @nocontact.command(name="list", description="See your own no-contact entries.")
    async def list_entries(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        rows = await asyncio.to_thread(
            no_contact_service.list_pairs_for_user,
            self.ctx.db_path,
            interaction.guild.id,
            interaction.user.id,
        )
        # Filter to what this member is allowed to know exists. An entry the
        # OTHER party created against them is deliberately absent.
        visible = [
            r
            for r in rows
            if is_visible_to(
                protected_user_id=r["protected_user_id"],
                viewer_id=interaction.user.id,
            )
        ]
        if not visible:
            await interaction.response.send_message(
                "You have no no-contact entries. Use `/nocontact add` to create one.",
                ephemeral=True,
            )
            return

        lines = []
        for row in visible:
            other = (
                row["user_high"]
                if row["user_low"] == interaction.user.id
                else row["user_low"]
            )
            member = interaction.guild.get_member(other)
            name = member.display_name if member else f"User {other}"
            tag = " *(set by a moderator)*" if row["protected_user_id"] is None else ""
            lines.append(f"• **{name}**{tag}")

        accent = await resolve_accent_color(self.ctx.db_path, interaction.guild)
        embed = discord.Embed(
            title="Your no-contact list",
            description="\n".join(lines),
            color=accent,
        )
        embed.set_footer(text="They are not told, and cannot tell.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Mention / reply watcher ──────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Alert staff when one member of a pair mentions or replies to the other.

        Reads mentions off the live message rather than ``message_mentions``
        so the alert doesn't depend on ingest having already run, and falls
        back to the DB only to resolve a reply's author when Discord didn't
        hand us the referenced message.
        """
        if message.guild is None or message.author.bot:
            return

        mentioned_ids = [u.id for u in message.mentions if not u.bot]
        reply_author_id = await self._reply_author_id(message)
        if not mentioned_ids and reply_author_id is None:
            return

        partners = await asyncio.to_thread(
            no_contact_service.no_contact_partners,
            self.ctx.db_path,
            message.guild.id,
            message.author.id,
        )
        if not partners:
            return

        alerts = alerts_for_message(
            author_id=message.author.id,
            mentioned_ids=mentioned_ids,
            reply_to_author_id=reply_author_id,
            no_contact_partners=partners,
        )
        for alert in alerts:
            await asyncio.to_thread(
                no_contact_service.record_event,
                self.ctx.db_path,
                message.guild.id,
                actor_id=alert.actor_id,
                target_id=alert.target_id,
                kind=alert.kind,
                channel_id=message.channel.id,
                message_id=message.id,
            )
            await self._post_alert(message, alert)

    async def _reply_author_id(self, message: discord.Message) -> Optional[int]:
        """Author of the message this one replies to, or None.

        A Discord reply with the ping switched off still lands in front of the
        person replied to and never appears in ``message_mentions``, so this
        is not a redundant second trigger — it is the one someone reaches for
        once they notice mentions are being watched.
        """
        ref = message.reference
        if ref is None:
            return None
        resolved = ref.resolved
        if isinstance(resolved, discord.Message):
            return None if resolved.author.bot else resolved.author.id
        if ref.message_id is None:
            return None

        def _lookup() -> Optional[int]:
            from bot_modules.core.db_utils import open_db  # noqa: PLC0415

            with open_db(self.ctx.db_path) as conn:
                row = conn.execute(
                    "SELECT author_id FROM messages WHERE message_id = ?",
                    (ref.message_id,),
                ).fetchone()
            return int(row["author_id"]) if row else None

        return await asyncio.to_thread(_lookup)

    async def _post_alert(self, message: discord.Message, alert) -> None:
        """Post one alert to the configured staff channel. Best-effort."""
        assert message.guild is not None
        settings = await asyncio.to_thread(
            no_contact_service.get_settings, self.ctx.db_path, message.guild.id
        )
        channel_id = settings["alert_channel_id"]
        if not channel_id:
            # Unconfigured guilds still get full enforcement — the event row
            # is already written and shows on the dashboard. Only the Discord
            # ping is optional.
            return
        channel = message.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        actor = message.guild.get_member(alert.actor_id)
        target = message.guild.get_member(alert.target_id)
        actor_name = actor.display_name if actor else f"User {alert.actor_id}"
        target_name = target.display_name if target else f"User {alert.target_id}"
        verb = "mentioned" if alert.kind == KIND_MENTION else "replied to"

        accent = await resolve_accent_color(self.ctx.db_path, message.guild)
        embed = discord.Embed(
            title="No-contact alert",
            description=(
                f"**{actor_name}** {verb} **{target_name}**, "
                f"who they are under a no-contact rule with."
            ),
            color=accent,
        )
        # ``message.guild`` is non-None here, so this is always a guild
        # channel — but the union still includes the DM types, so reach for
        # the mention defensively rather than narrowing on every subclass.
        embed.add_field(
            name="Channel",
            value=getattr(message.channel, "mention", f"<#{message.channel.id}>"),
            inline=True,
        )
        link = jump_url(message.guild.id, message.channel.id, message.id)
        if link:
            embed.add_field(name="Message", value=f"[Jump]({link})", inline=True)
        # No message text: content retention is off by default, so it usually
        # isn't stored, and widening retention server-wide to serve this
        # feature would cost every other member's privacy. The link is enough
        # for a moderator to read it in place.
        embed.set_footer(text="The member being protected is not notified.")

        try:
            await channel.send(
                content=alert_ping_prefix(settings["alert_role_id"]) or None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    roles=True, users=False, everyone=False
                ),
            )
        except discord.HTTPException:
            log.warning("Failed to post no-contact alert in guild %s", message.guild.id)


async def setup(bot: "Bot") -> None:
    await bot.add_cog(NoContactCog(bot, bot.ctx))
