import asyncio
import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.utils.audit import send_audit_log
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    update_game_message,
    get_game_payload,
    end_game,
    update_session,
    modify_payload,
    ConfirmCloseView,
)
from bot_modules.games.utils.live_bar import LiveBarUpdater
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
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Submit Entry", interaction.channel.name if interaction.channel else "unknown")
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
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Start Round", style=discord.ButtonStyle.primary, custom_id="fan_start_round")
    async def start_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
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

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="fan_close")
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close.", ephemeral=True)
            return
        game_msg = interaction.message
        channel = interaction.channel

        async def _confirmed(confirm_interaction):
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await game_msg.edit(view=self)
            except Exception:
                pass

            # Unblock any waiting submit_view so _run_round doesn't hang
            if self._active_submit_view:
                self._active_submit_view.stop()

            payload = await get_game_payload(self.db, self.game_id)
            await self.cog._post_recap(channel, payload)
            log.info("Game %s ended — fantasies", self.game_id)
            await end_game(self.db, self.game_id, payload=payload)
            if self.game_id in self.bot.active_views:
                del self.bot.active_views[self.game_id]

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="fan_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
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
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.primary, custom_id="fan_submit_entry")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        modal = SubmitEntryModal(self.game_id, self.db, self.round_num)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Close Submissions", style=discord.ButtonStyle.secondary, custom_id="fan_close_sub")
    async def close_submissions(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close submissions.", ephemeral=True)
            return
        self._closed = True
        self.stop()
        for item in self.children:
            item.disabled = True
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
        self.same_votes: list[int] = []
        self.nope_votes: list[int] = []
        self._updater = LiveBarUpdater()
        self._closed = False
        self._advanced_event: asyncio.Event | None = None

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
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
        )

    @discord.ui.button(label="✅ Same", style=discord.ButtonStyle.success, custom_id="fan_same", row=0)
    async def vote_same(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return
        changed = apply_vote(
            self.same_votes, self.nope_votes, interaction.user.id, "same"
        )
        msg = f"✅ Voted **Same**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="❌ Not for me", style=discord.ButtonStyle.danger, custom_id="fan_nope", row=0)
    async def vote_nope(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return
        changed = apply_vote(
            self.same_votes, self.nope_votes, interaction.user.id, "nope"
        )
        msg = f"✅ Voted **Not for me**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="fan_next", row=1)
    async def next_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can advance.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="fan_vclose", row=1)
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close.", ephemeral=True)
            return
        game_msg = interaction.message
        channel = interaction.channel

        async def _confirmed(confirm_interaction):
            self._closed = True
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await game_msg.edit(view=self)
            except Exception:
                pass
            payload = await get_game_payload(self.db, self.game_id)
            cog = self.bot.cogs.get("FantasiesCog")
            if cog:
                await cog._post_recap(channel, payload)
            await end_game(self.db, self.game_id, payload=payload)
            if self.game_id in self.bot.active_views:
                del self.bot.active_views[self.game_id]
            # Unblock the voting loop so it can exit cleanly
            if self._advanced_event:
                self._advanced_event.set()

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)


class FantasiesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="fantasies", description="Start a Fantasies & Dealbreakers game!")
    async def fantasies(self, interaction: discord.Interaction):
        log.info("%s used /fantasies in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/games allow-channel`.",
                ephemeral=True,
            )
            return
        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "fantasies",
            state="open",
            payload={"rounds": {}, "results": []},
        )

        embed = build_lobby_embed(interaction.user.display_name)

        log.info("Game %s (fantasies) created by %s in #%s", game_id, interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        view = FantasiesMainView(game_id, interaction.user.id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])

    async def _run_round(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
    ):
        submit_embed = build_round_submit_embed(round_num)
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

            async def advance(message: discord.Message, _text=entry_text, _num=entry_num, _author=entry_data["user_id"], _cat=entry_category):
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

                for item in view.children:
                    item.disabled = True
                try:
                    await message.edit(embed=view._build_embed(closed=True), view=view)
                except Exception:
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
                total_entries=len(entries),
            )
            view._advanced_event = advanced
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
        embed = build_recap_embed(results)
        if embed is None:
            return
        await channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(FantasiesCog(bot))
