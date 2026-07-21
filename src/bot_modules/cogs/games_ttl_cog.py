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
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    check_allowed_channel,
    create_game,
    update_game_message,
    get_game_options,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_name,
    channel_name,
)
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games.utils.recovery import start_redrive
from bot_modules.games_ttl.embeds import (
    build_guess_embed,
    build_lobby_embed,
    build_recap_embed,
    build_reveal_embed,
)
from bot_modules.games_ttl.logic import (
    add_submission,
    compute_recap_winners,
    mark_played,
    parse_lie_index,
    played_ids_from_payload,
    shuffle_statements,
    submission_locked,
    tally_votes,
    update_scores,
)

log = logging.getLogger(__name__)


class SubmitStatementsModal(discord.ui.Modal):
    def __init__(
        self,
        game_id: str,
        db,
        prompt: str | None = None,
        origin_message: discord.Message | None = None,
        existing: dict | None = None,
    ):
        super().__init__(title="Two Truths and a Lie")
        self.game_id = game_id
        self.db = db
        self._origin_message = origin_message

        # Components-v2 layout: the full prompt rides as static text, so it
        # can't be missed the way the 45-char-truncated title used to be.
        if prompt:
            self.add_item(discord.ui.TextDisplay(
                f"**Prompt:** {prompt}\n"
                "Your statements should answer the prompt — two true, one lie."
            ))
        ex_statements = (existing or {}).get("statements") or ["", "", ""]
        ex_lie = (existing or {}).get("lie")
        self._inputs: list[discord.ui.TextInput] = []
        for i in range(3):
            ti = discord.ui.TextInput(
                label=None, max_length=200, default=ex_statements[i] or None,
            )
            self._inputs.append(ti)
            self.add_item(discord.ui.Label(text=f"Statement {i + 1}", component=ti))
        self._lie_input = discord.ui.TextInput(
            label=None,
            max_length=1,
            placeholder="1, 2, or 3",
            default=str(ex_lie + 1) if ex_lie is not None else None,
        )
        self.add_item(discord.ui.Label(
            text="Which statement is the lie? (1, 2, or 3)", component=self._lie_input,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Your Statements", channel_name(interaction.channel))
        lie_idx = parse_lie_index(self._lie_input.value)
        if lie_idx is None:
            await interaction.response.send_message(
                "❌ Please enter **1**, **2**, or **3** to indicate which statement is the lie.",
                ephemeral=True,
            )
            return

        uid = interaction.user.id
        display_name = interaction.user.display_name
        statements = [ti.value for ti in self._inputs]

        locked = False

        def _add_submission(payload):
            nonlocal locked
            # Once a player's round has been revealed their statements are
            # history — replacing them would rewrite a finished round.
            if submission_locked(payload, uid):
                locked = True
                return
            add_submission(payload, uid, display_name, statements, lie_idx)

        payload = await modify_payload(self.db, self.game_id, _add_submission)
        if locked:
            await interaction.response.send_message(
                "❌ Your round has already been played — statements can't change now.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("✅ Your statements have been submitted!", ephemeral=True)

        # Only ever touch the lobby message (threaded through as
        # origin_message). Falling back to interaction.message here used to
        # clobber statement 1's field on the active guess embed when someone
        # joined mid-game.
        msg = self._origin_message
        if msg and msg.embeds:
            embed = msg.embeds[0]
            if embed.fields and (embed.fields[0].name or "").startswith("Players ("):
                names = list(payload.get("submitter_names", {}).values())
                embed.set_field_at(
                    0,
                    name=f"Players ({len(names)})",
                    value=", ".join(names),
                    inline=True,
                )
                try:
                    await msg.edit(embed=embed)
                except discord.HTTPException:
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
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Submit Statements", style=discord.ButtonStyle.primary, custom_id="ttl_submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        payload = await get_game_payload(self.db, self.game_id)
        existing = payload.get("submissions", {}).get(str(interaction.user.id))
        modal = SubmitStatementsModal(
            self.game_id, self.db, prompt=self.prompt,
            origin_message=interaction.message, existing=existing,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Start Guessing", style=discord.ButtonStyle.primary, custom_id="ttl_start")
    async def start_guessing(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start guessing.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        submissions = payload.get("submissions", {})
        if len(submissions) < 2:
            await interaction.response.send_message("❌ Need at least 2 players to start guessing!", ephemeral=True)
            return

        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)

        # Ping all submitters
        if interaction.guild:
            mentions = [
                member.mention
                for uid in submissions
                if (member := interaction.guild.get_member(int(uid)))
            ]
            if mentions:
                channel = interaction.channel
                assert channel is not None and not isinstance(
                    channel, (discord.ForumChannel, discord.CategoryChannel)
                )
                await channel.send(
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

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="ttl_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
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
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _build_embed(self, subject_name: str, closed: bool = False) -> discord.Embed:
        return build_guess_embed(
            subject_name, self.statements, self.votes, closed=closed, prompt=self.prompt,
        )

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
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
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
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can advance.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)

    @discord.ui.button(label="Join / Edit", style=discord.ButtonStyle.success, custom_id="ttl_join", row=1)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        payload = await get_game_payload(self.db, self.game_id)
        if submission_locked(payload, interaction.user.id):
            await interaction.response.send_message(
                "Your round has already been played — statements can't change now. You can still vote!",
                ephemeral=True,
            )
            return
        existing = payload.get("submissions", {}).get(str(interaction.user.id))
        modal = SubmitStatementsModal(self.game_id, self.db, prompt=self.prompt, existing=existing)
        await interaction.response.send_modal(modal)


class TTLCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="twotruths", description="Start a Two Truths and a Lie game!")
    @app_commands.describe(prompt="Optional topic prompt for players' statements")
    async def twotruths(self, interaction: discord.Interaction, prompt: str | None = None):
        log.info("%s used /games play twotruths in #%s", interaction.user.display_name, channel_name(interaction.channel))
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
        prompt = options.get("prompt")
        game_opts = await get_game_options(self.db, "ttl", guild_id)
        try:
            vote_timer = int(options.get("vote_timer", game_opts.get("vote_timer", 0)))
        except (TypeError, ValueError):
            vote_timer = 0
        vote_timer = max(0, min(vote_timer, 300))
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "ttl",
            state="joining",
            payload={
                "submissions": {}, "submission_count": 0, "submitter_names": {},
                "scores": {}, "prompt": prompt, "vote_timer": vote_timer,
            },
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
            played_ids: set[str] = played_ids_from_payload(payload)
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
                _sub_id: int = subject_id,
                _sub_name: str = subject_name,
                _lie: int = lie_index,
                _round: int = round_num,
            ) -> None:
                if view._closed:
                    return
                view._closed = True

                correct, fooled = tally_votes(view.votes, _lie)
                update_scores(scores, _sub_id, correct, fooled, len(view.votes))

                def _flush_scores(p):
                    p["scores"] = dict(scores)
                    mark_played(p, _sub_id)
                await modify_payload(self.db, game_id, _flush_scores)

                reveal_embed = view._build_reveal_embed(_sub_name, correct, fooled, guild)
                disable_all_items(view)
                try:
                    await message.edit(embed=view._build_embed(_sub_name, closed=True), view=view)
                except discord.HTTPException:
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
                # pyright's flow analysis reports a circular inference here
                # (advance captures `view`, whose initializer takes `advance`);
                # the closure itself is fully annotated above.
                advance_callback=advance,  # pyright: ignore[reportGeneralTypeIssues]
                prompt=payload.get("prompt"),
            )
            view._advanced_event = advanced
            self.bot.active_views[game_id] = view

            embed = view._build_embed(subject_name)
            msg = await channel.send(embed=embed, view=view)
            await update_game_message(self.db, game_id, msg.id)

            # vote_timer == 0 keeps the classic host-presses-Next pacing;
            # otherwise the round closes itself when time runs out (the host
            # can still advance early).
            vote_timer = int(payload.get("vote_timer") or 0)
            if vote_timer > 0:
                try:
                    await asyncio.wait_for(advanced.wait(), timeout=vote_timer)
                except asyncio.TimeoutError:
                    await advance(msg)
            else:
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
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "ttl")

        ping_str = " ".join(mentions) if mentions else None
        await channel.send(content=ping_str, embed=embed)
        log.info("Game %s ended — %d players", game_id, len(player_ids))
        await end_game(
            self.db, game_id,
            player_count=len(player_ids),
            round_count=len(player_ids),
            payload=payload,
            bot=self.bot, player_ids=player_ids,
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
        await start_redrive(
            self.bot, game_id, message,
            self._run_guessing(
                interaction=None, game_id=game_id, host_id=host_id,
                host_name=host_name, channel=channel, resume=True,
            ),
            channel=channel, log_label=f"ttl game {game_id} (re-driving guessing)",
        )
        return True


async def setup(bot: "Bot"):
    cog = TTLCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("twotruths")
    play.add_command(cog.twotruths, override=True)
    bot.game_launchers["ttl"] = cog.launch
    bot.game_recoverers["ttl"] = cog.recover_game
