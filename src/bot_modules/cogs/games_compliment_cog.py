"""Spin the Compliment — lobby, pairings, and the wrap-up that closes it.

The round has three beats since 2026-09-04 (social-prompt-39): the host's
**Close & Generate** posts the pairings; five minutes later the pairings are
reposted with a ✅ on every giver seen delivering (a reply to, or @mention
of, their receiver in the channel — read from the message archive's
metadata, so it works with content storage off) and the stragglers get one
nudge; at ten minutes a short **Wrap-Up** card says how many compliments
landed, carries the payout footer, and pays the pool. The game row stays
live (``state = 'wrapping'``) for that window, so the channel is busy and a
restart re-arms the wrap from the epochs in the payload. ``/games end`` during
the wrap finishes it on the spot (``end_with_recap``) — and whichever of the
two gets there first claims the wrap in the payload, so the card is posted
and the pool paid exactly once (``claim_wrap``). The recap's
**🔁 Spin Again** relaunches under whoever pressed it, through the shared
launch guard.
"""

import asyncio
import logging
import time as _time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items, is_host_or_mod
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    create_game,
    get_active_game_by_id,
    sign_off_game_chore,
    update_game_message,
    update_game_state,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_names,
    channel_name,
)
from bot_modules.games.utils.launch_guard import refuse_launch
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
    build_wrap_recap_embed,
)
from bot_modules.games_compliment.logic import (
    STATE_WRAPPING,
    claim_wrap,
    delivered_givers,
    generate_pairings,
    join_participant,
    leave_participant,
    pairing_ids,
    parse_pairings,
    release_wrap_claim,
    serialize_pairings,
    stragglers,
    wrap_schedule,
    wrap_seconds_remaining,
)
from bot_modules.games.utils.audit import audit_anonymous
from bot_modules.services.anon_audit_service import (
    EVENT_PAIRINGS_GENERATED,
)

log = logging.getLogger(__name__)

SPIN_AGAIN_DENIED_TEXT = "❌ Only the host or a mod can spin again."


class ComplimentView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog: "ComplimentCog"):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog

    async def _redraw_lobby(self, interaction: discord.Interaction, payload: dict, note: str) -> None:
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
        await interaction.followup.send(note, ephemeral=True)

    # Two buttons, not one toggle (social-prompt-46): a double-tap on the old
    # single "Join" silently took the member back out of the pool.
    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="comp_addme")
    async def add_me(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        user_id = interaction.user.id
        state: dict[str, bool] = {}

        def _join(payload):
            state["joined"] = join_participant(payload, user_id)

        payload = await modify_payload(self.db, self.game_id, _join)
        if not state.get("joined"):
            await interaction.response.send_message("You're already in the pool.", ephemeral=True)
            return
        log.info("%s joined game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        await self._redraw_lobby(interaction, payload, "✅ You've been added to the pool.")

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="comp_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        user_id = interaction.user.id
        state: dict[str, bool] = {}

        def _leave(payload):
            state["left"] = leave_participant(payload, user_id)

        payload = await modify_payload(self.db, self.game_id, _leave)
        if not state.get("left"):
            await interaction.response.send_message("You're not in the pool.", ephemeral=True)
            return
        log.info("%s left game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        await self._redraw_lobby(interaction, payload, "You've left the pool.")

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

        generated_at = int(_time.time())
        nudge_at, ends_at = wrap_schedule(generated_at)

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
        embed = build_pairings_embed(pairings, color=color, name_fn=name_fn, ends_at=ends_at)
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

        # The round is not over: the wrap-up window opens. Everything the
        # wrap needs rides in the payload so a restart can re-arm it.
        def _open_wrap(p):
            p["pairings"] = serialize_pairings(pairings)
            p["generated_at"] = generated_at
            p["wrap_nudge_at"] = nudge_at
            p["wrap_ends_at"] = ends_at
            p["pairings_message_id"] = getattr(pairings_msg, "id", None)

        await modify_payload(self.db, self.game_id, _open_wrap)
        await update_game_state(self.db, self.game_id, STATE_WRAPPING)
        log.info("Game %s paired %d players; wrap-up until %s", self.game_id, len(participants), ends_at)
        self.cog.arm_wrap(interaction.channel, self.game_id)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="comp_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["compliment"], ephemeral=True)


class ComplimentRecapView(discord.ui.View):
    """Rides on the Wrap-Up card: one press opens the next round."""

    def __init__(self, game_id: str, host_id: int, cog: "ComplimentCog"):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.cog = cog

    @discord.ui.button(label="🔁 Spin Again", style=discord.ButtonStyle.primary, custom_id="comp_spin_again")
    async def spin_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message(SPIN_AGAIN_DENIED_TEXT, ephemeral=True)
            return
        # Same gate as the slash entry: allowed channel, enabled dial, and no
        # game already running here.
        refusal = await refuse_launch(self.cog.db, interaction, "compliment")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        disable_all_items(self)
        assert interaction.message
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        self.stop()
        await interaction.response.defer()
        game_id = await self.cog.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={},
        )
        if game_id:
            await sign_off_game_chore(self.cog.bot, interaction.guild_id, interaction.user.id)


class ComplimentCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        self._wrap_tasks: dict[str, asyncio.Task] = {}

    @property
    def db(self):
        return self.bot.games_db

    async def cog_unload(self) -> None:
        for task in self._wrap_tasks.values():
            task.cancel()
        self._wrap_tasks.clear()

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Rebuild after a restart: a lobby gets its buttons back on the
        anchor message; a wrap in progress is re-armed from its epochs."""
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        if row["state"] == STATE_WRAPPING:
            if not parse_pairings(payload.get("pairings")):
                # Nothing to wrap — a malformed row; archive it quietly.
                await end_game(self.db, game_id, reason="crash")
                return True
            # Still live means the wrap that claimed it died before end_game
            # archived and paid, so the claim is stale — drop it, or the
            # re-armed wrap would refuse itself and the game would hang.
            def _drop_claim(payload: dict) -> None:
                # Returns None on purpose: modify_payload writes back any
                # non-None return, and release_wrap_claim answers a bool.
                release_wrap_claim(payload)

            await modify_payload(self.db, game_id, _drop_claim)
            self.arm_wrap(channel, game_id)
            log.info("Recovered compliment game %s (wrap-up) in #%s", game_id, getattr(channel, "name", channel.id))
            return True
        view = ComplimentView(game_id, host_id, self.db, self.bot, self)
        self.bot.add_view(view, message_id=int(message.id))
        self.bot.active_views[game_id] = view
        log.info("Recovered compliment game %s (lobby) in #%s", game_id, getattr(channel, "name", channel.id))
        return True

    # ── the wrap-up ──────────────────────────────────────────────────

    def arm_wrap(self, channel, game_id: str) -> None:
        """Schedule the wrap's two beats for this game (idempotent per game)."""
        existing = self._wrap_tasks.get(game_id)
        if existing and not existing.done():
            existing.cancel()
        self._wrap_tasks[game_id] = asyncio.create_task(self._run_wrap(channel, game_id))

    async def _run_wrap(self, channel, game_id: str) -> None:
        try:
            payload = await get_game_payload(self.db, game_id)
            now = int(_time.time())
            nudge_wait = wrap_seconds_remaining(payload.get("wrap_nudge_at"), now)
            end_wait = wrap_seconds_remaining(payload.get("wrap_ends_at"), now)
            if nudge_wait < end_wait:
                await asyncio.sleep(nudge_wait)
                if not await self._wrap_nudge(channel, game_id):
                    return
                end_wait = wrap_seconds_remaining(payload.get("wrap_ends_at"), int(_time.time()))
            await asyncio.sleep(end_wait)
            await self.finish_wrap(channel, game_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("compliment: wrap-up failed for %s", game_id)
        finally:
            self._wrap_tasks.pop(game_id, None)

    async def _delivered(self, guild_id: int, channel_id: int, since_ts: int, pairings: dict[int, int]) -> set[int]:
        """Who has delivered — read off the main DB's message archive."""
        db_path = self.bot.ctx.db_path

        def _read() -> set[int]:
            with open_db(db_path) as conn:
                return delivered_givers(
                    conn, guild_id=guild_id, channel_id=channel_id,
                    since_ts=since_ts, pairings=pairings,
                )

        return await asyncio.to_thread(_read)

    async def _wrap_nudge(self, channel, game_id: str) -> bool:
        """Halfway: repost the pairings with a ✅ per delivered giver and nudge
        the stragglers once. False when the game is no longer live."""
        if await get_active_game_by_id(self.db, game_id) is None:
            return False
        payload = await get_game_payload(self.db, game_id)
        pairings = parse_pairings(payload.get("pairings"))
        guild = getattr(channel, "guild", None)
        guild_id = guild.id if guild is not None else 0
        delivered = await self._delivered(
            guild_id, channel.id, int(payload.get("generated_at") or 0), pairings,
        )
        owed = stragglers(pairings, delivered)
        if not owed:
            return True  # nothing to nudge; the wrap-up will say so
        name_fn = await build_name_fn(
            guild=guild, db_path=self.bot.ctx.db_path, guild_id=guild_id,
            user_ids=pairing_ids(pairings),
        )
        color = await safe_resolve_accent(self.bot, guild, log_label="compliment")
        embed = build_pairings_embed(
            pairings, color=color, name_fn=name_fn, delivered=delivered,
            ends_at=payload.get("wrap_ends_at"),
        )
        try:
            await channel.send(
                content=(
                    " ".join(mention(uid) for uid in owed)
                    + " ⏳ Still waiting on your compliment — reply to your partner or @mention them before the wrap-up!"
                ),
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.HTTPException:
            log.info("compliment: nudge not delivered in channel %s", channel.id)
        return True

    async def finish_wrap(self, channel, game_id: str) -> bool:
        """The closing beat: recap with the payout footer, then pay the pool.
        Also what ``/games end`` runs on a wrapping game (``end_with_recap``).
        Runs **once**: False when the game was already ended elsewhere, or
        when another caller has already claimed the wrap."""
        row = await get_active_game_by_id(self.db, game_id)
        if row is None:
            return False
        task = self._wrap_tasks.get(game_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        # Claim the wrap under the payload lock before anything is sent or
        # paid. The timer and the host's own /games end can arrive together,
        # and the row is still live for both of them — without the claim the
        # loser reposted the wrap-up card and paid the pool twice.
        claimed = False

        def _claim(payload: dict) -> None:
            nonlocal claimed
            claimed = claim_wrap(payload)

        payload = await modify_payload(self.db, game_id, _claim)
        if not claimed:
            return False
        pairings = parse_pairings(payload.get("pairings"))
        participants = [int(uid) for uid in payload.get("participants", [])]
        guild = getattr(channel, "guild", None)
        guild_id = guild.id if guild is not None else 0
        delivered = await self._delivered(
            guild_id, channel.id, int(payload.get("generated_at") or 0), pairings,
        )
        color = await safe_resolve_accent(self.bot, guild, log_label="compliment")
        embed = build_wrap_recap_embed(len(delivered), len(pairings), color=color)
        if guild is not None:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "compliment")
        try:
            await channel.send(embed=embed, view=ComplimentRecapView(game_id, int(row["host_id"]), self))
        except discord.HTTPException:
            log.info("compliment: wrap-up card not delivered in channel %s", channel.id)

        payload["delivered"] = sorted(delivered)
        log.info("Game %s ended — %d players, %d/%d delivered", game_id, len(participants), len(delivered), len(pairings))
        await end_game(
            self.db, game_id,
            player_count=len(participants), payload=payload,
            bot=self.bot, player_ids=participants,
        )
        self.bot.active_views.pop(game_id, None)
        return True

    async def end_with_recap(self, channel, game_id: str) -> bool:
        """``/games end`` on a wrapping Compliment: post the wrap-up now
        rather than the red Force-Closed card. A lobby has nothing to recap
        and answers False, so it takes the ordinary force-close."""
        row = await get_active_game_by_id(self.db, game_id)
        if row is None or row["state"] != STATE_WRAPPING:
            return False
        return await self.finish_wrap(channel, game_id)

    @app_commands.command(name="compliment", description=play_description("compliment"))
    @app_commands.describe(
        start_in="Countdown before the start, in minutes",
    )
    async def compliment(
        self,
        interaction: discord.Interaction,
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info("%s used /games play compliment in #%s", interaction.user.display_name, channel_name(interaction.channel))
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await refuse_launch(self.db, interaction, "compliment")
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
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
        view = ComplimentView(game_id, host_id, self.db, self.bot, self)
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
    bot.game_recoverers["compliment"] = cog.recover_game
