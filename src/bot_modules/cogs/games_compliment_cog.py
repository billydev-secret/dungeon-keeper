import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items, is_host_or_mod
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    check_allowed_channel,
    check_game_enabled,
    create_game,
    update_game_message,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_names,
    channel_name,
)
from bot_modules.core.branding import safe_resolve_accent
from bot_modules.services.game_start_ping_service import (
    extract_start_epoch,
    resolve_start_epoch,
)
from bot_modules.services.name_resolver import build_name_fn, mention
from bot_modules.services.no_contact_service import no_contact_pairs_among
from bot_modules.games_compliment.embeds import (
    build_lobby_embed,
    build_pairings_embed,
)
from bot_modules.games_compliment.logic import (
    generate_pairings,
    pairing_ids,
    serialize_pairings,
    toggle_participant,
)
from bot_modules.games.utils.audit import audit_anonymous
from bot_modules.services.anon_audit_service import (
    EVENT_PAIRINGS_GENERATED,
)

log = logging.getLogger(__name__)


class ComplimentView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="comp_addme")
    async def add_me(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        user_id = interaction.user.id
        action_holder: dict[str, str] = {}

        def _toggle(payload):
            action_holder["action"] = toggle_participant(payload, user_id)

        payload = await modify_payload(self.db, self.game_id, _toggle)
        action = action_holder["action"]
        log.info("%s %s game %s in #%s", interaction.user.display_name, action.split()[0], self.game_id, channel_name(interaction.channel))

        names = resolve_names(interaction.guild, payload.get("participants", []))
        host_member = interaction.guild.get_member(self.host_id) if interaction.guild else None
        guild = interaction.guild
        color = await safe_resolve_accent(self.bot, guild, log_label="compliment")
        embed = build_lobby_embed(
            host_member.display_name if host_member else "Host",
            names,
            color=color,
            # Re-read from the payload, not the view: the countdown must
            # survive a restart that rebuilt this view from the DB.
            start_at=extract_start_epoch(payload),
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"✅ You've been {action} the pool.", ephemeral=True
        )

    @discord.ui.button(label="Close & Generate", style=discord.ButtonStyle.primary, custom_id="comp_generate")
    async def close_generate(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can generate pairings.", ephemeral=True)
            return

        payload = await get_game_payload(self.db, self.game_id)
        participants = payload.get("participants", [])
        guild = interaction.guild

        # The no-contact pairs inside the pool are the derangement's forbidden
        # set: a blocked pair is never giver→receiver in either direction. If
        # that leaves no valid pairing at all, generate_pairings returns {} —
        # and that gets the same refusal a one-player pool gets, so the
        # protected member can't tell which one fired.
        def _draw() -> dict[int, int]:
            forbidden: set[tuple[int, int]] = set()
            if guild is not None and len(participants) >= 2:
                forbidden = no_contact_pairs_among(
                    self.bot.ctx.db_path, guild.id, participants
                )
            # The constrained derangement is a search, so it stays off the
            # event loop with the read that feeds it.
            return generate_pairings(participants, forbidden)

        pairings = await asyncio.to_thread(_draw)
        if not pairings:
            await interaction.response.send_message("Need at least 2 players in the pool!", ephemeral=True)
            return

        await interaction.response.defer()

        # The card is the only record of who compliments whom once the ping
        # below is gone, so it names members (never <@id>, which the reading
        # client resolves from its own cache). The mentions go in content.
        name_fn = await build_name_fn(
            guild=guild,
            db_path=self.bot.ctx.db_path,
            guild_id=guild.id if guild is not None else 0,
            user_ids=pairing_ids(pairings),
        )
        color = await safe_resolve_accent(self.bot, guild, log_label="compliment")
        embed = build_pairings_embed(pairings, color=color, name_fn=name_fn)
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "compliment")
        # Ping all participants (preserve order from pairings dict)
        unique_mentions = [mention(uid) for uid in pairing_ids(pairings)]

        self.stop()
        disable_all_items(self)

        await interaction.edit_original_response(view=self)
        if unique_mentions:
            ping_msg = await interaction.followup.send(content=" ".join(unique_mentions), wait=True)
            async def _delete_ping():
                await asyncio.sleep(15)
                try:
                    await ping_msg.delete()
                except discord.HTTPException:
                    pass
            asyncio.create_task(_delete_ping())
        pairings_msg = await interaction.followup.send(embed=embed, wait=True)

        # Spin the Compliment pairs people at random rather than hiding
        # authorship — the giver→receiver map is posted in the open. So this
        # records who rolled the pairing and points at the message that shows
        # it, rather than duplicating the map into the audit table.
        if guild is not None:
            await audit_anonymous(
                self.bot, self.db, guild,
                game_type="compliment", user=interaction.user,
                event=EVENT_PAIRINGS_GENERATED,
                game_id=self.game_id,
                message_id=getattr(pairings_msg, "id", None),
                channel_id=interaction.channel.id if interaction.channel else None,
                extra={"pair_count": len(pairings)},
            )

        log.info("Game %s ended — %d players", self.game_id, len(participants))
        await end_game(
            self.db,
            self.game_id,
            player_count=len(participants),
            payload={"pairings": serialize_pairings(pairings)},
            bot=self.bot, player_ids=list(participants),
        )
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="comp_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["compliment"], ephemeral=True)


class ComplimentCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self) -> None:
        rows = await self.db.fetchall(
            "SELECT game_id, host_id FROM games_active_games WHERE game_type = 'compliment'"
        )
        for row in rows:
            view = ComplimentView(row["game_id"], row["host_id"], self.db, self.bot)
            self.bot.add_view(view)
            self.bot.active_views[row["game_id"]] = view
        log.info("compliment: re-registered %d active ComplimentView(s)", len(rows))

    @app_commands.command(name="compliment", description="Start Spin the Compliment — random anonymous pairing!")
    @app_commands.describe(
        start_in="Show a lobby countdown — game starts in this many minutes (host still closes the pool)",
    )
    async def compliment(
        self,
        interaction: discord.Interaction,
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info("%s used /games play compliment in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "compliment", interaction.guild_id or 0):
            await interaction.response.send_message(
                "Spin the Compliment is currently disabled on this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"start_in": start_in},
        )
        await finish_launch_response(interaction, game_id)

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None."""
        start_epoch = resolve_start_epoch(options)
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "compliment",
            state="joining",
            payload={"start_epoch": start_epoch} if start_epoch else None,
        )

        log.info("Game %s (compliment) created by %s in #%s", game_id, host_name, getattr(channel, "name", channel.id))
        guild = getattr(channel, "guild", None)
        color = await safe_resolve_accent(self.bot, guild, log_label="compliment")
        embed = build_lobby_embed(host_name, [], color=color, start_at=start_epoch)
        view = ComplimentView(game_id, host_id, self.db, self.bot)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("compliment launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id


async def setup(bot: "Bot"):
    cog = ComplimentCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("compliment")
    play.add_command(cog.compliment, override=True)
    bot.game_launchers["compliment"] = cog.launch
