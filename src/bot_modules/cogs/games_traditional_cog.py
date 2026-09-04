import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items, is_host_or_mod
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.services.no_contact_service import no_contact_partners
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    ConfirmCloseView,
    create_game,
    end_game,
    get_game_options,
    get_game_payload,
    modify_payload,
    update_game_message,
    update_session,
    channel_name,
)
from bot_modules.games.utils.launch_guard import launch_refusal
from bot_modules.games_traditional.embeds import (
    build_lobby_embed,
    build_question_embed,
    build_recap_embed,
    build_tod_embed,
)
from bot_modules.games.utils.question_source import (
    channel_allows_nsfw,
    get_traditional_question,
)
from bot_modules.games_traditional.logic import (
    CAT_LABELS,
    IDLE_MINUTES_KEY,
    category_allowed,
    clamp_idle_minutes,
    current_pass,
    filter_nsfw_prefs,
    idle_close_notice,
    idle_seconds_left,
    pass_complete,
    pass_complete_notice,
    record_asked,
    select_bank_categories_for_all,
    select_next_question_target,
    start_next_pass,
    toggle_pref,
    touch_activity,
)

log = logging.getLogger(__name__)

ALL_ASKED_REPLY = "All player/category combinations have been asked!"
NO_PLAYERS_REPLY = "No players have joined yet!"
IDLE_CLOSE_REASON = "idle"


class AskQuestionModal(discord.ui.Modal):
    """The host's question for one player.

    ``bank_default`` pre-fills the box with a bank question (Ask Question);
    the host can send it as-is, edit it, or replace it. Write My Own opens
    the same modal empty.
    """

    question = discord.ui.TextInput(
        label="Your Question",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(
        self, game_id: str, db, channel, host_id: int, bot, target_id: str,
        target_name: str, category: str, bank_default: str | None = None,
    ):
        super().__init__(title=f"{CAT_LABELS[category]} for {target_name}"[:45])
        self.game_id = game_id
        self.db = db
        self.channel = channel
        self.host_id = host_id
        self.bot = bot
        self.target_id = target_id
        self.target_name = target_name
        self.cat = category
        self.bank_default = bank_default
        if bank_default:
            self.question.default = bank_default[:500]

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, self.title, channel_name(interaction.channel))
        text = self.question.value
        bank_default = self.bank_default
        completed: dict[str, int] = {}

        # One locked read-modify-write: a category toggle landing while the
        # modal was open is kept, not overwritten (trivia-tail-96).
        def _record(payload: dict) -> None:
            record_asked(payload, self.target_id, self.cat, text)
            if bank_default:
                used = payload.setdefault("bank_used", [])
                if bank_default not in used:
                    used.append(bank_default)
                if text.strip() == bank_default.strip():
                    payload["bank_asked"] = payload.get("bank_asked", 0) + 1
            touch_activity(payload, time.time())
            pass_no = current_pass(payload)
            if pass_complete(payload.get("prefs", {}), payload.get("asked", {}), pass_no):
                completed["pass"] = pass_no

        payload = await modify_payload(self.db, self.game_id, _record)

        target_member = interaction.guild.get_member(int(self.target_id)) if interaction.guild else None
        mention = target_member.mention if target_member else f"**{self.target_name}**"

        await self.channel.send(
            content=mention,
            embed=build_question_embed(self.cat, text, self.target_name),
        )
        if "pass" in completed:
            # The loud moment: the whole room hears the pass close, and what
            # the host can do next (trivia-tail-84 / 89).
            await self.channel.send(
                pass_complete_notice(completed["pass"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await interaction.response.defer()

        view = self.bot.active_views.get(self.game_id)
        if view:
            await view.refresh_embed(interaction.guild, payload)
            view.arm_idle(payload)


class TraditionalHostView(discord.ui.View):
    """Main embed view — host controls + player preference toggles."""

    def __init__(self, game_id: str, host_id: int, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self._message: discord.Message | None = None
        self._idle_task: asyncio.Task | None = None
        self._closed = False

    async def _get_payload(self) -> dict:
        return await get_game_payload(self.db, self.game_id)

    def _resolve_names(self, guild: discord.Guild | None, payload: dict) -> dict[str, str]:
        if not guild:
            return {}
        names: dict[str, str] = {}
        for uid in payload.get("prefs", {}):
            member = guild.get_member(int(uid))
            if member:
                names[uid] = member.display_name
        return names

    async def refresh_embed(self, guild: discord.Guild | None, payload: dict):
        host_member = guild.get_member(self.host_id) if guild else None
        host_name = host_member.display_name if host_member else "Host"
        names = self._resolve_names(guild, payload)
        color = await safe_resolve_accent(self.bot, guild, log_label="traditional")
        embed = build_tod_embed(host_name, payload, names=names, color=color)
        if hasattr(self, '_message') and self._message:
            try:
                await self._message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

    async def _update_embed(self, interaction: discord.Interaction, payload: dict):
        await self.refresh_embed(interaction.guild, payload)

    # --- Idle close -------------------------------------------------------
    #
    # The room ends itself after the dashboard's quiet window (payload
    # ``idle_minutes``, 0 = off). Every press re-arms it through arm_idle;
    # after a restart recover_game re-arms from the stored last_activity.

    def arm_idle(self, payload: dict) -> None:
        """(Re)start the idle countdown from the payload's last activity."""
        if self._closed:
            return
        current = asyncio.current_task()
        if self._idle_task is not None and self._idle_task is not current:
            self._idle_task.cancel()
        self._idle_task = None
        left = idle_seconds_left(payload, time.time())
        if left is None:
            return
        self._idle_task = asyncio.create_task(self._idle_wait(left))

    async def _idle_wait(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        await self._idle_fire()

    async def _idle_fire(self) -> None:
        """The window ran out: re-check against the stored payload (a press
        may have landed on another worker), then close with the recap."""
        if self._closed:
            return
        try:
            payload = await self._get_payload()
        except Exception:
            log.exception("traditional idle check failed for %s", self.game_id)
            return
        if not payload:
            return  # already archived elsewhere
        left = idle_seconds_left(payload, time.time())
        if left is None:
            return
        if left > 0:
            self.arm_idle(payload)
            return
        minutes = clamp_idle_minutes(payload.get(IDLE_MINUTES_KEY), default=0)
        channel = getattr(self._message, "channel", None)
        if channel is None:
            log.warning("traditional idle close for %s has no channel to post in", self.game_id)
        log.info("Game %s (traditional) idle for %d minutes — closing", self.game_id, minutes)
        await self._do_close(
            None, game_msg=self._message, channel=channel,
            note=idle_close_notice(minutes), reason=IDLE_CLOSE_REASON,
        )

    # --- Player preference toggles (rows 0-1) ---
    #
    # Two buttons per row, deliberately: the four category toggles pair up
    # SFW over NSFW, so the panel reads as a 2x4 grid rather than one packed
    # row of four and a ragged row of three underneath.

    @discord.ui.button(label="SFW Truth", style=discord.ButtonStyle.primary, custom_id="tod_sfw_truth", row=0)
    async def sfw_truth(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "sfw_truth")

    @discord.ui.button(label="SFW Dare", style=discord.ButtonStyle.primary, custom_id="tod_sfw_dare", row=0)
    async def sfw_dare(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "sfw_dare")

    @discord.ui.button(label="NSFW Truth", style=discord.ButtonStyle.danger, custom_id="tod_nsfw_truth", row=1)
    async def nsfw_truth(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "nsfw_truth")

    @discord.ui.button(label="NSFW Dare", style=discord.ButtonStyle.danger, custom_id="tod_nsfw_dare", row=1)
    async def nsfw_dare(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await self._toggle_pref(interaction, "nsfw_dare")

    async def _toggle_pref(self, interaction: discord.Interaction, category: str):
        # NSFW prompts ride Discord's own age gate, never a bot-side toggle.
        # Gating the preference is what keeps NSFW content out of a SFW
        # channel: every serve path draws only from opted-in categories.
        if not category_allowed(category, channel_allows_nsfw(interaction.channel)):
            await interaction.response.send_message(
                "❌ NSFW categories are only available in age-restricted channels.",
                ephemeral=True,
            )
            return
        user_id = interaction.user.id
        action_holder: dict[str, str] = {}

        def _do_toggle(payload):
            single_choice = bool(payload.get("single_choice", False))
            action_holder["action"] = toggle_pref(
                payload, user_id, category, single_choice=single_choice
            )
            touch_activity(payload, time.time())

        payload = await modify_payload(self.db, self.game_id, _do_toggle)
        await self._update_embed(interaction, payload)
        self.arm_idle(payload)
        action = action_holder["action"]
        if action == "switched":
            msg = f"Switched to **{CAT_LABELS[category]}**."
        else:
            msg = f"**{CAT_LABELS[category]}** {action} from your preferences."
        await interaction.response.send_message(msg, ephemeral=True)

    # --- Host controls (row 2) ---

    async def _pick_target(self, interaction: discord.Interaction) -> tuple[str, str, str] | None:
        """Choose who answers next: ``(user_id, category, display_name)``.

        Shared by Ask Question and Write My Own. Replies for the caller and
        returns None when there is nobody to ask. Applies, in order: the
        host/mod gate; the channel's age-gate on every player's preferences
        (a room that lost its age-restriction mid-game stops serving NSFW at
        once — safety-sweep-10); the presser's no-contact partners; and the
        pass rollover — once every pair on the current pass has been asked,
        the game moves to the next pass and picks again, so Ask never dead-ends.
        """
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can ask questions.", ephemeral=True)
            return None
        payload = await self._get_payload()
        prefs = filter_nsfw_prefs(payload.get("prefs", {}), channel_allows_nsfw(interaction.channel))
        asked = payload.get("asked", {})
        if not any(prefs.values()):
            await interaction.response.send_message(NO_PLAYERS_REPLY, ephemeral=True)
            return None

        # The no-contact gate. The bot picks who answers and the presser writes
        # a directed question at them, so the two must not be a blocked pair.
        # Keyed on the presser, not the host — a mod can ask too. When the
        # only open player is the blocked partner the picker reports
        # exhaustion and the presser sees the ordinary "all asked" reply.
        guild_id = interaction.guild_id or 0
        actor_id = interaction.user.id
        excluded = await asyncio.to_thread(
            no_contact_partners, self.bot.ctx.db_path, guild_id, actor_id
        )
        pass_no = current_pass(payload)
        choice = select_next_question_target(prefs, asked, excluded=excluded, pass_no=pass_no)
        if choice is None and pass_complete(prefs, asked, pass_no):
            # Every pair on this pass has been asked: roll over, but only if
            # the next pass actually has someone this presser may ask.
            choice = select_next_question_target(prefs, asked, excluded=excluded, pass_no=pass_no + 1)
            if choice is not None:
                def _roll(p: dict) -> None:
                    if current_pass(p) == pass_no:
                        start_next_pass(p)
                await modify_payload(self.db, self.game_id, _roll)
        if choice is None:
            await interaction.response.send_message(ALL_ASKED_REPLY, ephemeral=True)
            return None

        chosen_uid, chosen_cat = choice
        member = interaction.guild.get_member(int(chosen_uid)) if interaction.guild else None
        chosen_name = member.display_name if member else str(chosen_uid)
        return chosen_uid, chosen_cat, chosen_name

    @discord.ui.button(label="Ask Question", style=discord.ButtonStyle.success, custom_id="tod_ask", row=2)
    async def ask_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Pick a player and open the question box pre-filled from the bank.

        The bank is the default path (trivia-tail-89 / discovery-9): the host
        can send the drawn question, edit it, or type over it. An empty bank
        for that category opens the box empty, the way Write My Own does.
        """
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        target = await self._pick_target(interaction)
        if target is None:
            return
        chosen_uid, chosen_cat, chosen_name = target
        payload = await self._get_payload()
        used: list[str] = list(payload.get("bank_used", []))
        bank_default = await get_traditional_question(self.db, chosen_cat, exclude=used)
        modal = AskQuestionModal(
            self.game_id, self.db, interaction.channel, self.host_id, self.bot,
            target_id=chosen_uid, target_name=chosen_name, category=chosen_cat,
            bank_default=bank_default,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Write My Own", style=discord.ButtonStyle.secondary, custom_id="tod_write_own", row=2)
    async def write_own(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Pick a player and open an empty question box."""
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        target = await self._pick_target(interaction)
        if target is None:
            return
        chosen_uid, chosen_cat, chosen_name = target
        modal = AskQuestionModal(
            self.game_id, self.db, interaction.channel, self.host_id, self.bot,
            target_id=chosen_uid, target_name=chosen_name, category=chosen_cat,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Bank Round", emoji="🎲", style=discord.ButtonStyle.primary, custom_id="tod_bank_round", row=2)
    async def bank_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Serve every opted-in player a fresh question pulled from the bank.

        Each participant gets one question in a category they opted into,
        drawn from the web-managed question bank (no repeats within a game).
        Bank questions land in the same ``asked`` history as written ones,
        so each player is served at most once per opted-in category on the
        current pass — pressing the button again after new people join only
        serves the newcomers. Players with no available bank question for
        their picked category are reported back.
        """
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can run a bank round.", ephemeral=True)
            return

        payload = await self._get_payload()
        prefs = payload.get("prefs", {})
        if not prefs:
            await interaction.response.send_message(NO_PLAYERS_REPLY, ephemeral=True)
            return

        await interaction.response.defer()

        guild = interaction.guild
        channel = interaction.channel
        assert isinstance(channel, discord.abc.Messageable)  # games run in text channels
        used: list[str] = list(payload.get("bank_used", []))
        asked = payload.get("asked", {})
        pass_no = current_pass(payload)
        # Belt-and-braces: prefs set while the channel was still age-restricted
        # must not serve here if the flag has since been removed.
        prefs = filter_nsfw_prefs(prefs, channel_allows_nsfw(channel))
        choices = select_bank_categories_for_all(prefs, asked, pass_no=pass_no)  # {uid: category}
        already_asked = sum(1 for cats in prefs.values() if cats) - len(choices)

        served: list[tuple[str, str, str]] = []
        unserved: list[str] = []
        for uid, cat in choices.items():
            member = guild.get_member(int(uid)) if guild else None
            name = member.display_name if member else str(uid)
            question = await get_traditional_question(self.db, cat, exclude=used)
            if question is None:
                unserved.append(f"{name} ({CAT_LABELS.get(cat, cat)})")
                continue
            used.append(question)
            served.append((uid, cat, question))
            mention = member.mention if member else f"**{name}**"
            await channel.send(content=mention, embed=build_question_embed(cat, question, name))

        completed: dict[str, int] = {}

        def _record(p: dict) -> None:
            for uid, cat, question in served:
                record_asked(p, uid, cat, question, pass_no=pass_no)
            p["bank_used"] = used
            p["bank_asked"] = p.get("bank_asked", 0) + len(served)
            touch_activity(p, time.time())
            if served and pass_complete(p.get("prefs", {}), p.get("asked", {}), current_pass(p)):
                completed["pass"] = current_pass(p)

        payload = await modify_payload(self.db, self.game_id, _record)
        await self.refresh_embed(guild, payload)
        self.arm_idle(payload)
        if "pass" in completed:
            await channel.send(
                pass_complete_notice(completed["pass"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        if not choices and already_asked:
            msg = (
                "Everyone has already been asked in all their chosen categories — "
                "press **Ask Question** to start another pass, or run this again when new players join."
            )
        elif not served and unserved:
            msg = (
                "No bank questions were available. Add some in the web dashboard "
                "under **Games → Traditional Truth or Dare → Questions**."
            )
        else:
            msg = f"Served **{len(served)}** question{'s' if len(served) != 1 else ''} from the bank."
            if unserved:
                msg += "\nNo bank question available for: " + ", ".join(unserved) + "."
            if already_asked:
                msg += f"\nSkipped {already_asked} player{'s' if already_asked != 1 else ''} already asked in all their categories."
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="tod_htp", row=3)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["traditional"], ephemeral=True)

    @discord.ui.button(label="End Game", style=discord.ButtonStyle.secondary, custom_id="tod_end", row=3)
    async def end_game_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Close the game, post the recap, and pay the room.

        Truth or Dare shipped without this button, so ``_do_close`` — the only
        call site that passes ``bot=``/``player_ids=`` to ``end_game`` — was
        unreachable and no game ever paid out.
        """
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can end the game.", ephemeral=True)
            return

        channel = interaction.channel

        async def _confirmed(confirm_interaction: discord.Interaction) -> None:
            # Close through the game message, not the confirm interaction —
            # ConfirmCloseView has already responded to that one.
            await self._do_close(confirm_interaction, game_msg=self._message, channel=channel)

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=view, ephemeral=True,
        )

    async def _do_close(
        self, interaction: discord.Interaction | None, game_msg=None, channel=None,
        *, note: str | None = None, reason: str | None = None,
    ):
        """Post the recap, pay the room, archive the row.

        Reached from End Game (``interaction`` in hand, no ``note``) and from
        the idle close (no interaction; ``note`` says why the recap appeared
        and ``reason`` lands in the archived payload).
        """
        if self._closed:
            return
        self._closed = True
        if self._idle_task is not None and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
        self._idle_task = None

        payload = await self._get_payload()
        participants = payload.get("participants", [])
        asked = payload.get("asked", {})
        total_q = len(asked)

        guild = (interaction.guild if interaction else None) or getattr(channel, "guild", None)
        color = await safe_resolve_accent(self.bot, guild, log_label="traditional")
        embed = build_recap_embed(payload, color=color, note=note)
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "traditional")

        self.stop()
        disable_all_items(self)

        if game_msg:
            try:
                await game_msg.edit(view=self)
            except discord.HTTPException:
                pass
            if channel is not None:
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    log.info("traditional recap not delivered for %s", self.game_id)
        elif interaction is not None:
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(embed=embed)

        log.info("Game %s ended — %d players", self.game_id, len(participants))
        await end_game(self.db, self.game_id, player_count=len(participants), round_count=total_q, payload=payload,
                       bot=self.bot, player_ids=participants, reason=reason)
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]


class TraditionalCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="traditional", description=play_description("traditional"))
    @app_commands.describe(
        single_choice="Each player picks only one category (radio-style) instead of as many as they like",
    )
    async def traditional(self, interaction: discord.Interaction, single_choice: bool = False):
        log.info("%s used /games play traditional in #%s", interaction.user.display_name, channel_name(interaction.channel))
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await launch_refusal(
            self.db, "traditional", interaction.channel_id, interaction.guild_id or 0,
        )
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"single_choice": single_choice},
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
        single_choice = bool(options.get("single_choice", False))
        # The quiet window comes from the per-server dashboard dial and is
        # stored on the payload, so a restart re-arms it without a config read.
        game_opts = await get_game_options(self.db, "traditional", guild_id)
        idle_minutes = clamp_idle_minutes(options.get("idle_minutes", game_opts.get("idle_minutes")))
        payload: dict = {IDLE_MINUTES_KEY: idle_minutes}
        touch_activity(payload, time.time())
        if single_choice:
            payload["single_choice"] = True
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "traditional",
            state="joining",
            payload=payload,
            guild_id=guild_id,
        )

        guild = getattr(channel, "guild", None)
        color = await safe_resolve_accent(self.bot, guild, log_label="traditional")
        embed = build_lobby_embed(host_name, color=color, single_choice=single_choice)

        log.info("Game %s (traditional) created by %s in #%s", game_id, host_name, getattr(channel, "name", channel.id))
        host_view = TraditionalHostView(game_id, host_id, self.db, self.bot)
        self.bot.active_views[game_id] = host_view

        try:
            msg = await channel.send(embed=embed, view=host_view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("traditional launch lacked send perms in channel %s", channel.id)
            return None
        host_view._message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        host_view.arm_idle(payload)
        return game_id

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register the host view after a restart so its buttons work again,
        and re-arm the idle close from the payload's last activity."""
        game_id = row["game_id"]
        host_view = TraditionalHostView(game_id, int(row["host_id"]), self.db, self.bot)
        host_view._message = message
        self.bot.active_views[game_id] = host_view
        self.bot.add_view(host_view, message_id=message.id)
        host_view.arm_idle(payload or {})
        log.info("Recovered traditional game %s in #%s", game_id, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = TraditionalCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("traditional")
    play.add_command(cog.traditional, override=True)
    bot.game_launchers["traditional"] = cog.launch
    bot.game_recoverers["traditional"] = cog.recover_game
