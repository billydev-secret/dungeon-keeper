import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.command_groups import play
from bot_modules.games.utils.audit import send_audit_log
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    check_allowed_channel,
    create_game,
    update_game_message,
    get_game_payload,
    end_game,
    update_session,
    modify_payload,
    channel_name,
)
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.core.branding import resolve_accent_color
from bot_modules.games_fantasies.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_round_submit_embed,
    build_vote_embed,
)
from bot_modules.games_fantasies.logic import (
    add_entry,
    apply_vote,
    build_result_entry,
    get_round_entries,
    normalize_category,
)

log = logging.getLogger(__name__)


class SubmitEntryModal(discord.ui.Modal, title="Submit a Fantasy or Dealbreaker"):
    category = discord.ui.TextInput(
        label='Type "Fantasy" or "Dealbreaker"',
        max_length=20,
        placeholder="Fantasy",
    )
    entry = discord.ui.TextInput(
        label="Your Entry",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, game_id: str, db, round_num: int):
        super().__init__()
        self.game_id = game_id
        self.db = db
        self.round_num = round_num

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Submit Entry", channel_name(interaction.channel))
        category = normalize_category(self.category.value)
        if category is None:
            await interaction.response.send_message(
                "Category must be 'Fantasy' or 'Dealbreaker'.", ephemeral=True
            )
            return

        def _add_entry(payload):
            add_entry(
                payload,
                round_num=self.round_num,
                user_id=interaction.user.id,
                text=self.entry.value,
                category=category,
            )

        await modify_payload(self.db, self.game_id, _add_entry)

        # Audit log
        if interaction.guild:
            await send_audit_log(
                interaction.client, self.db, interaction.guild,
                game_type="fantasies", user=interaction.user,
                content=self.entry.value, label=f"{category} Submission",
            )

        await interaction.response.send_message(
            f"Your {category.lower()} has been submitted!", ephemeral=True
        )


class FantasiesMainView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.round_num = 0
        self._active_submit_view: SubmitRoundView | None = None

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Start Round", style=discord.ButtonStyle.primary, custom_id="fan_start_round")
    async def start_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start rounds.", ephemeral=True)
            return

        self.round_num += 1
        await interaction.response.defer()

        await self.cog._run_round(
            game_id=self.game_id,
            host_id=self.host_id,
            host_name=interaction.user.display_name,
            round_num=self.round_num,
            channel=interaction.channel,
        )

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="fan_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["fantasies"], ephemeral=True)


class SubmitRoundView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, round_num: int, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.round_num = round_num
        self.db = db
        self.bot = bot

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.primary, custom_id="fan_submit_entry")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        modal = SubmitEntryModal(self.game_id, self.db, self.round_num)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Close Submissions", style=discord.ButtonStyle.secondary, custom_id="fan_close_sub")
    async def close_submissions(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close submissions.", ephemeral=True)
            return
        self._closed = True
        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(content=f"✅ Submissions closed for Round {self.round_num}!", view=self)


class FantasiesVoteView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        entry_text: str,
        entry_num: int,
        category: str,
        db,
        bot,
        host_name: str,
        advance_callback,
        entry_author_id: int = 0,
        total_entries: int = 0,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.entry_text = entry_text
        self.entry_num = entry_num
        self.total_entries = total_entries
        self.category = category
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.entry_author_id = entry_author_id
        self.same_votes: list[int] = []
        self.nope_votes: list[int] = []
        self._updater = LiveBarUpdater()
        self._closed = False
        self._advanced_event: asyncio.Event | None = None
        self._accent_color: "discord.Color | None" = None

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _build_embed(self, closed: bool = False) -> discord.Embed:
        return build_vote_embed(
            entry_text=self.entry_text,
            entry_num=self.entry_num,
            category=self.category,
            same_votes=self.same_votes,
            nope_votes=self.nope_votes,
            total_entries=self.total_entries,
            closed=closed,
            color=self._accent_color,
        )

    @discord.ui.button(label="✅ Same", style=discord.ButtonStyle.success, custom_id="fan_same", row=0)
    async def vote_same(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return
        if interaction.user.id == self.entry_author_id:
            await interaction.response.send_message(
                "You can't vote on your own entry!", ephemeral=True
            )
            return
        changed = apply_vote(
            self.same_votes, self.nope_votes, interaction.user.id, "same"
        )
        msg = f"✅ Voted **Same**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="❌ Not for Me", style=discord.ButtonStyle.danger, custom_id="fan_nope", row=0)
    async def vote_nope(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return
        if interaction.user.id == self.entry_author_id:
            await interaction.response.send_message(
                "You can't vote on your own entry!", ephemeral=True
            )
            return
        changed = apply_vote(
            self.same_votes, self.nope_votes, interaction.user.id, "nope"
        )
        msg = f"✅ Voted **Not for me**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="fan_next", row=1)
    async def next_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can advance.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)


class FantasiesCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="fantasies", description="Start a Fantasies & Dealbreakers game!")
    async def fantasies(self, interaction: discord.Interaction):
        log.info("%s used /games play fantasies in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={},
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
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "fantasies",
            state="open",
            payload={"rounds": {}, "results": []},
        )

        guild = getattr(channel, "guild", None)
        color = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        embed = build_lobby_embed(host_name, color=color)

        log.info("Game %s (fantasies) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        view = FantasiesMainView(game_id, host_id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("fantasies launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _run_round(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
    ):
        guild = getattr(channel, "guild", None)
        accent_color = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        submit_embed = build_round_submit_embed(round_num, color=accent_color)
        submit_view = SubmitRoundView(game_id, host_id, round_num, self.db, self.bot)
        # Let the main view know so it can stop us on close
        main_view = self.bot.active_views.get(game_id)
        if isinstance(main_view, FantasiesMainView):
            main_view._active_submit_view = submit_view
        await channel.send(embed=submit_embed, view=submit_view)

        await submit_view.wait()

        # Clear reference now that submission phase is over
        if isinstance(main_view, FantasiesMainView):
            main_view._active_submit_view = None

        # If game was closed during submission, bail out
        if game_id not in self.bot.active_views:
            return

        payload = await get_game_payload(self.db, game_id)
        entries = get_round_entries(payload, round_num)

        if not entries:
            await channel.send("No entries submitted for this round.")
            return

        results = []
        for i, entry_data in enumerate(entries):
            entry_text = entry_data["text"]
            entry_category = entry_data.get("category", "Fantasy")
            entry_num = i + 1
            advanced = asyncio.Event()

            async def advance(message: discord.Message, _text=entry_text, _num=entry_num, _author=entry_data["user_id"], _cat=entry_category) -> None:
                if view._closed:
                    return
                view._closed = True

                result_entry = build_result_entry(
                    text=_text,
                    category=_cat,
                    author=_author,
                    same_votes=view.same_votes,
                    nope_votes=view.nope_votes,
                )
                results.append(result_entry)

                # Persist incrementally so mid-game close doesn't lose prior results
                def _save_result(payload, _entry=result_entry):
                    payload.setdefault("results", []).append(_entry)
                await modify_payload(self.db, game_id, _save_result)

                disable_all_items(view)
                try:
                    await message.edit(embed=view._build_embed(closed=True), view=view)
                except discord.HTTPException:
                    pass
                advanced.set()

            view = FantasiesVoteView(
                game_id=game_id,
                host_id=host_id,
                entry_text=entry_text,
                entry_num=entry_num,
                category=entry_category,
                db=self.db,
                bot=self.bot,
                host_name=host_name,
                advance_callback=advance,
                entry_author_id=entry_data["user_id"],
                total_entries=len(entries),
            )
            view._advanced_event = advanced
            view._accent_color = accent_color
            self.bot.active_views[game_id] = view

            embed = view._build_embed()
            await channel.send(embed=embed, view=view)
            await advanced.wait()
            # If the game was closed mid-round, stop the loop
            if view._closed and game_id not in self.bot.active_views:
                break
            await asyncio.sleep(1)

        # If the game was already closed by the host, skip saving
        if game_id not in self.bot.active_views:
            return

        # Results were saved incrementally in advance(); no extra save needed

    async def _post_recap(self, channel, payload: dict):
        results = payload.get("results", [])
        guild = getattr(channel, "guild", None)
        color = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        embed = build_recap_embed(results, color=color)
        if embed is None:
            return
        await channel.send(embed=embed)

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-register the host control panel after a restart.

        Fantasies is host-driven: the FantasiesMainView (the tracked message) is
        the persistent control panel, and the host presses "Start Round" to run
        each round. A round's submit/vote messages aren't tracked, so a round
        interrupted by a crash is abandoned — the host simply starts the next
        round. We restore the round counter so numbering continues correctly.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        view = FantasiesMainView(game_id, host_id, self.db, self.bot, self)
        rounds = payload.get("rounds", {})
        if rounds:
            try:
                view.round_num = max(int(k) for k in rounds)
            except ValueError:
                view.round_num = 0
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        log.info(
            "Recovered fantasies game %s (control panel, round_num=%d) in #%s",
            game_id, view.round_num, getattr(channel, "name", channel.id),
        )
        return True


async def setup(bot: "Bot"):
    cog = FantasiesCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("fantasies")
    play.add_command(cog.fantasies, override=True)
    bot.game_launchers["fantasies"] = cog.launch
    bot.game_recoverers["fantasies"] = cog.recover_game
