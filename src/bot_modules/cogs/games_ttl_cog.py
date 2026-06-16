import asyncio
import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    update_game_message,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_name,
    ConfirmCloseView,
)
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games_ttl.embeds import (
    build_guess_embed,
    build_lobby_embed,
    build_recap_embed,
    build_reveal_embed,
)
from bot_modules.games_ttl.logic import (
    add_submission,
    compute_recap_winners,
    parse_lie_index,
    shuffle_statements,
    tally_votes,
    update_scores,
)

log = logging.getLogger(__name__)


class SubmitStatementsModal(discord.ui.Modal):
    s1 = discord.ui.TextInput(label="Statement 1", max_length=200)
    s2 = discord.ui.TextInput(label="Statement 2", max_length=200)
    s3 = discord.ui.TextInput(label="Statement 3", max_length=200)
    lie_index = discord.ui.TextInput(
        label="Which statement is the lie? (1, 2, or 3)",
        max_length=1,
        placeholder="1, 2, or 3",
    )

    def __init__(self, game_id: str, db, prompt: str | None = None, origin_message: discord.Message | None = None):
        super().__init__(title=f"Prompt: {prompt[:70]}" if prompt else "Submit Truths and a Lie")
        self.game_id = game_id
        self.db = db
        self._origin_message = origin_message

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Your Statements", interaction.channel.name if interaction.channel else "unknown")
        lie_idx = parse_lie_index(self.lie_index.value)
        if lie_idx is None:
            await interaction.response.send_message(
                "❌ Please enter **1**, **2**, or **3** to indicate which statement is the lie.",
                ephemeral=True,
            )
            return

        uid = interaction.user.id
        display_name = interaction.user.display_name
        statements = [self.s1.value, self.s2.value, self.s3.value]

        def _add_submission(payload):
            add_submission(payload, uid, display_name, statements, lie_idx)

        payload = await modify_payload(self.db, self.game_id, _add_submission)
        await interaction.response.send_message("✅ Your statements have been submitted!", ephemeral=True)

        msg = self._origin_message or interaction.message
        if msg:
            embed = msg.embeds[0]
            names = list(payload.get("submitter_names", {}).values())
            embed.set_field_at(
                0,
                name=f"Players ({len(names)})",
                value=", ".join(names),
                inline=True,
            )
            try:
                await msg.edit(embed=embed)
            except Exception:
                pass


class TTLSubmitView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog, prompt: str | None = None):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.prompt = prompt

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Submit Statements", style=discord.ButtonStyle.primary, custom_id="ttl_submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        modal = SubmitStatementsModal(self.game_id, self.db, prompt=self.prompt, origin_message=interaction.message)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Start Guessing", style=discord.ButtonStyle.success, custom_id="ttl_start")
    async def start_guessing(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start guessing.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        submissions = payload.get("submissions", {})
        if len(submissions) < 2:
            await interaction.response.send_message("❌ Need at least 2 players to start guessing!", ephemeral=True)
            return

        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        # Ping all submitters
        if interaction.guild:
            mentions = [
                interaction.guild.get_member(int(uid)).mention
                for uid in submissions
                if interaction.guild.get_member(int(uid))
            ]
            if mentions:
                await interaction.channel.send(
                    f"🎮 **Two Truths and a Lie is starting!** {' '.join(mentions)} — get ready!",
                    delete_after=15,
                )

        await self.cog._run_guessing(
            interaction=interaction,
            game_id=self.game_id,
            host_id=self.host_id,
            host_name=interaction.user.display_name,
            channel=interaction.channel,
        )

    @discord.ui.button(label="🛑 Cancel", style=discord.ButtonStyle.danger, custom_id="ttl_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can cancel.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Game cancelled.", view=self)
        await end_game(self.db, self.game_id)
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="ttl_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["ttl"], ephemeral=True)


class TTLGuessView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        subject_id: int,
        statements: list[str],
        lie_index: int,
        db,
        bot,
        host_name: str,
        advance_callback,
        prompt: str | None = None,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.subject_id = subject_id
        self.statements = statements
        self.lie_index = lie_index
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.prompt = prompt
        self.votes: dict[int, int] = {}
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

    def _build_embed(self, subject_name: str, closed: bool = False) -> discord.Embed:
        return build_guess_embed(subject_name, self.statements, self.votes, closed=closed)

    def _build_reveal_embed(self, subject_name: str, correct_voters: list, fooled_voters: list, guild) -> discord.Embed:
        def _resolver(uid_str: str) -> str:
            if guild is None:
                return uid_str
            try:
                m = guild.get_member(int(uid_str))
            except (TypeError, ValueError):
                m = None
            return m.display_name if m else uid_str

        return build_reveal_embed(
            subject_name=subject_name,
            statements=self.statements,
            lie_index=self.lie_index,
            correct_voters=correct_voters,
            fooled_voters=fooled_voters,
            name_resolver=_resolver,
        )

    @discord.ui.button(label="1️⃣", style=discord.ButtonStyle.primary, custom_id="ttl_v1", row=0)
    async def vote_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, 0)

    @discord.ui.button(label="2️⃣", style=discord.ButtonStyle.primary, custom_id="ttl_v2", row=0)
    async def vote_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, 1)

    @discord.ui.button(label="3️⃣", style=discord.ButtonStyle.primary, custom_id="ttl_v3", row=0)
    async def vote_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, 2)

    async def _vote(self, interaction: discord.Interaction, idx: int):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        if interaction.user.id == self.subject_id:
            await interaction.response.send_message("You can't vote on your own statements!", ephemeral=True)
            return
        prev = self.votes.get(interaction.user.id)
        self.votes[interaction.user.id] = idx
        num = ["1️⃣", "2️⃣", "3️⃣"][idx]
        changed = prev is not None and prev != idx
        msg = f"✅ Voted **{num}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        guild = interaction.guild
        member = guild.get_member(self.subject_id) if guild else None
        subject_name = member.display_name if member else str(self.subject_id)
        await self._updater.schedule_update(
            interaction.message, lambda: self._build_embed(subject_name)
        )

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="ttl_next", row=1)
    async def advance_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can advance.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="ttl_close", row=1)
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
            await end_game(self.db, self.game_id, payload=payload)
            self.bot.active_views.pop(self.game_id, None)
            await channel.send("🛑 Game ended by host.")
            # Unblock the guessing loop so it can exit cleanly
            if self._advanced_event:
                self._advanced_event.set()

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)

    @discord.ui.button(label="📝 Join", style=discord.ButtonStyle.primary, custom_id="ttl_join", row=1)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        payload = await get_game_payload(self.db, self.game_id)
        submissions = payload.get("submissions", {})
        if str(interaction.user.id) in submissions:
            await interaction.response.send_message("You've already submitted your statements!", ephemeral=True)
            return
        modal = SubmitStatementsModal(self.game_id, self.db, prompt=self.prompt)
        await interaction.response.send_modal(modal)


class TTLCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="twotruths", description="Start a Two Truths and a Lie game!")
    @app_commands.describe(prompt="Optional topic prompt for players' statements")
    async def twotruths(self, interaction: discord.Interaction, prompt: str | None = None):
        log.info("%s used /games play twotruths in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
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
            options={"prompt": prompt},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I don't have access to send messages in that channel. "
                    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**.",
                    ephemeral=True,
                )
            except Exception:
                pass

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
        prompt = options.get("prompt")
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ttl",
            state="joining",
            payload={"submissions": {}, "submission_count": 0, "submitter_names": {}, "scores": {}, "prompt": prompt},
        )

        embed = build_lobby_embed(prompt=prompt)

        log.info("Game %s (ttl) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        view = TTLSubmitView(game_id, host_id, self.db, self.bot, self, prompt=prompt)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("ttl launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _run_guessing(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        channel,
        resume: bool = False,
    ):
        guild = channel.guild if hasattr(channel, "guild") else None
        # On resume after a restart, seed from persisted scores so already-played
        # subjects are skipped; the subject whose round was interrupted is still
        # "unplayed" and gets a fresh round.
        if resume:
            payload = await get_game_payload(self.db, game_id)
            scores: dict[str, dict] = dict(payload.get("scores", {}))
            played_ids: set[str] = set(scores.keys())
        else:
            scores = {}
            played_ids = set()
        round_num = len(played_ids)

        while True:
            # Re-read submissions each iteration so late joiners are picked up
            payload = await get_game_payload(self.db, game_id)
            submissions = payload.get("submissions", {})
            unplayed = [uid for uid in submissions if uid not in played_ids]
            if not unplayed:
                break

            subject_id_str = unplayed[0]
            played_ids.add(subject_id_str)
            round_num += 1

            subject_id = int(subject_id_str)
            data = submissions[subject_id_str]
            # Shuffle statements so the lie isn't always in the submitted position
            statements, lie_index = shuffle_statements(
                data["statements"], data["lie"]
            )

            subject_member = guild.get_member(subject_id) if guild else None
            subject_name = subject_member.display_name if subject_member else subject_id_str

            if subject_member:
                await channel.send(f"{subject_member.mention} It's your turn!", delete_after=15)

            advanced = asyncio.Event()

            async def advance(
                message: discord.Message,
                _sub_id=subject_id,
                _sub_name=subject_name,
                _lie=lie_index,
                _round=round_num,
            ):
                if view._closed:
                    return
                view._closed = True

                correct, fooled = tally_votes(view.votes, _lie)
                update_scores(scores, _sub_id, correct, fooled, len(view.votes))

                def _flush_scores(p):
                    p["scores"] = dict(scores)
                await modify_payload(self.db, game_id, _flush_scores)

                reveal_embed = view._build_reveal_embed(_sub_name, correct, fooled, guild)
                for item in view.children:
                    item.disabled = True
                try:
                    await message.edit(embed=view._build_embed(_sub_name, closed=True), view=view)
                except Exception:
                    pass

                await channel.send(embed=reveal_embed)

                advanced.set()

            view = TTLGuessView(
                game_id=game_id,
                host_id=host_id,
                subject_id=subject_id,
                statements=statements,
                lie_index=lie_index,
                db=self.db,
                bot=self.bot,
                host_name=host_name,
                advance_callback=advance,
                prompt=payload.get("prompt"),
            )
            view._advanced_event = advanced
            self.bot.active_views[game_id] = view

            embed = view._build_embed(subject_name)
            msg = await channel.send(embed=embed, view=view)
            await update_game_message(self.db, game_id, msg.id)

            await advanced.wait()
            # If the game was closed mid-round, stop the loop
            if view._closed and game_id not in self.bot.active_views:
                break
            await asyncio.sleep(2)

        # If the game was already closed by the host, skip final results
        if game_id not in self.bot.active_views:
            return

        payload = await get_game_payload(self.db, game_id)
        payload["scores"] = scores
        player_ids = list(played_ids)

        stats = compute_recap_winners(scores, played_ids)

        def _name_resolver(uid_str: str) -> str:
            if guild is None:
                return uid_str
            try:
                m = guild.get_member(int(uid_str))
            except (TypeError, ValueError):
                m = None
            return m.mention if m else uid_str

        def _mention_resolver(uid_str: str) -> str | None:
            if guild is None:
                return None
            try:
                m = guild.get_member(int(uid_str))
            except (TypeError, ValueError):
                m = None
            return m.mention if m else None

        embed, mentions = build_recap_embed(stats, _name_resolver, _mention_resolver)

        ping_str = " ".join(mentions) if mentions else None
        await channel.send(content=ping_str, embed=embed)
        log.info("Game %s ended — %d players", game_id, len(player_ids))
        await end_game(
            self.db, game_id,
            player_count=len(player_ids),
            round_count=len(player_ids),
            payload=payload,
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-drive the guessing loop after a restart.

        Completed rounds live in payload["scores"]; the in-progress round can't
        be reconstructed (its shuffled statements/votes aren't persisted), so we
        retire the stale round message and restart that subject's round. The
        re-driven loop reconstructs scores from the payload and continues.
        """
        if not payload.get("submissions"):
            return False
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        host_name = resolve_name(guild, host_id) if guild else "Host"
        try:
            await message.edit(content="↻ Picking up where we left off after a restart…", view=None)
        except Exception:
            pass
        # Keep the game "live" until the re-driven loop posts its first round so
        # the loop's in-active_views guards (and final recap) behave correctly.
        self.bot.active_views[game_id] = _RecoverySentinel()
        asyncio.create_task(
            self._run_guessing(
                interaction=None,
                game_id=game_id,
                host_id=host_id,
                host_name=host_name,
                channel=channel,
                resume=True,
            )
        )
        log.info("Recovering ttl game %s (re-driving guessing) in #%s", game_id, getattr(channel, "name", channel.id))
        return True


class _RecoverySentinel:
    """Placeholder kept in bot.active_views while a re-driven loop spins up."""


async def setup(bot: commands.Bot):
    cog = TTLCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("twotruths")
    play.add_command(cog.twotruths)
    bot.game_launchers["ttl"] = cog.launch
    bot.game_recoverers["ttl"] = cog.recover_game
