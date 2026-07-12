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
    check_allowed_channel,
    check_game_enabled,
    create_game,
    update_game_message,
    update_game_payload,
    modify_payload,
    get_game_payload,
    end_game,
    update_session,
    is_game_expired,
    resolve_name,
    resolve_names,
    channel_name,
)
from bot_modules.games.utils.question_source import (
    get_mlt_prompt,
    has_matching_questions,
    channel_allows_nsfw,
)
from bot_modules.games_mlt.embeds import (
    build_final_standings_embed,
    build_join_embed,
    build_results_embed,
    build_round_embed,
)
from bot_modules.games_mlt.logic import (
    MIN_PLAYERS,
    add_player,
    apply_vote,
    bump_crowns,
    can_start,
    encode_round_votes,
    find_round_winners,
    is_eligible_voter,
    pop_next_prompt,
    queue_prompt,
    remove_player,
    tally_votes,
)

log = logging.getLogger(__name__)

# Cap the player-submitted prompt queue to prevent flooding.
_MAX_QUEUED_PROMPTS = 15


class MLTJoinView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="mlt_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        log.info("%s joined game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.setdefault("players", [])
        add_player(players, interaction.user.id)
        await update_game_payload(self.db, self.game_id, payload)

        guild = interaction.guild
        names = resolve_names(guild, players)
        host_member = guild.get_member(self.host_id) if guild else None
        embed = build_join_embed(
            host_member.display_name if host_member else "Host", names
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've joined the pool!", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="mlt_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        log.info("%s left game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.setdefault("players", [])
        remove_player(players, interaction.user.id)
        await update_game_payload(self.db, self.game_id, payload)

        guild = interaction.guild
        names = resolve_names(guild, players)
        host_member = guild.get_member(self.host_id) if guild else None
        embed = build_join_embed(
            host_member.display_name if host_member else "Host", names
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've left the pool.", ephemeral=True)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, custom_id="mlt_start")
    async def start_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        if not can_start(players):
            await interaction.response.send_message(
                f"❌ Need at least {MIN_PLAYERS} players to start!", ephemeral=True
            )
            return

        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)

        # Ping joined players
        if interaction.guild:
            mentions = [
                member.mention
                for uid in players
                if (member := interaction.guild.get_member(uid)) is not None
            ]
            if mentions:
                channel = interaction.channel
                assert isinstance(channel, discord.abc.Messageable)
                await channel.send(
                    f"👑 **Most Likely To is starting!** {' '.join(mentions)} — get ready!",
                    delete_after=15,
                )

        await self.cog._run_round(
            interaction=interaction,
            game_id=self.game_id,
            host_id=self.host_id,
            host_name=interaction.user.display_name,
            round_num=1,
            players=players,
            channel=interaction.channel,
            custom_prompt=payload.get("opening_prompt"),
        )

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="mlt_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["mlt"], ephemeral=True)


class PoseMLTModal(discord.ui.Modal, title="Pose a Prompt"):
    prompt = discord.ui.TextInput(
        label="Most likely to...",
        placeholder="e.g. win a staring contest",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, view, message: discord.Message):
        super().__init__()
        self._view = view
        self._message = message

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Pose a Prompt", channel_name(interaction.channel))
        if self._view._closed:
            await interaction.response.send_message("This round already ended.", ephemeral=True)
            return
        if len(self._view.queued_prompts) >= _MAX_QUEUED_PROMPTS:
            await interaction.response.send_message(
                f"The prompt queue is full ({_MAX_QUEUED_PROMPTS}). Let some play first!",
                ephemeral=True,
            )
            return
        count = queue_prompt(self._view.queued_prompts, self.prompt.value)
        self._view.next_btn.label = f"⏭️ Next ({count} queued)"
        try:
            await self._message.edit(view=self._view)
        except discord.HTTPException:
            pass
        await interaction.response.send_message("✅ Your prompt has been queued!", ephemeral=True)


class MLTVoteView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        prompt: str,
        round_num: int,
        players: list[int],
        db,
        bot,
        host_name: str,
        guild,
        advance_callback,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.prompt = prompt
        self.round_num = round_num
        self.players = players
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.guild = guild
        self.advance_callback = advance_callback
        self.votes: dict[int, int] = {}
        self._closed = False
        self.queued_prompts: list[str] = []

        options = []
        for uid in players:
            member = guild.get_member(uid) if guild else None
            name = member.display_name if member else str(uid)
            options.append(discord.SelectOption(label=name, value=str(uid)))
        self.select = discord.ui.Select(
            placeholder="🗳️ Vote: Select a player",
            options=options,
            custom_id="mlt_vote_select",
        )
        self.select.callback = self._vote_select_callback
        self.add_item(self.select)

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    async def _vote_select_callback(self, interaction: discord.Interaction):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        if not is_eligible_voter(interaction.user.id, self.players):
            await interaction.response.send_message("You're not in the player pool.", ephemeral=True)
            return
        values = (interaction.data or {}).get("values") or []
        target_id = int(values[0])
        changed = apply_vote(self.votes, interaction.user.id, target_id)

        # Persist live votes so a crash mid-round doesn't lose them.
        def _save(payload):
            rounds = payload.setdefault("rounds", {})
            rd = rounds.setdefault(str(self.round_num), {})
            rd["votes"] = encode_round_votes(self.votes)

        await modify_payload(self.db, self.game_id, _save)

        member = self.guild.get_member(target_id) if self.guild else None
        name = member.display_name if member else str(target_id)
        msg = f"✅ Voted for **{name}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True)

    def _build_embed(self, closed=False) -> discord.Embed:
        return build_round_embed(
            prompt=self.prompt,
            round_num=self.round_num,
            vote_count=len(self.votes),
            closed=closed,
        )

    def _build_results_embed(self, tally: dict) -> discord.Embed:
        return build_results_embed(
            prompt=self.prompt,
            round_num=self.round_num,
            tally=tally,
            guild=self.guild,
        )

    @discord.ui.button(label="✍️ Pose Prompt", style=discord.ButtonStyle.primary, custom_id="mlt_pose", row=1)
    async def pose_prompt(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        assert interaction.message
        await interaction.response.send_modal(PoseMLTModal(self, interaction.message))

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="mlt_next", row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can advance.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This round is already over.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="mlt_htp2", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["mlt"], ephemeral=True)


class MLTCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="mlt", description="Start a Most Likely To game!")
    @app_commands.describe(
        question="Opening prompt (e.g. 'win a staring contest') — defaults to question bank",
        tags="Comma-separated tags to filter the question bank",
    )
    async def mlt(
        self,
        interaction: discord.Interaction,
        question: str = "",
        tags: str = "",
    ):
        log.info("%s used /games play mlt in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "mlt", interaction.guild_id or 0):
            await interaction.response.send_message("Most Likely To is currently disabled on this server.", ephemeral=True)
            return

        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list and not question.strip() and not await has_matching_questions(
            self.db, "mlt", tag_list, allow_nsfw=channel_allows_nsfw(interaction.channel)
        ):
            await interaction.response.send_message(
                f"No questions match tags: {', '.join(tag_list)} for this game.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"question": question, "tags": tag_list},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I don't have access to send messages in that channel. "
                    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**.",
                    ephemeral=True,
                )
            except discord.HTTPException:
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
        question = options.get("question", "")
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "mlt",
            state="joining",
            payload={"opening_prompt": question.strip() or None, "rounds": {}, "crowns": {}, "players": [], "tags": options.get("tags") or []},
        )

        log.info("Game %s (mlt) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        embed = build_join_embed(host_name, [])
        view = MLTJoinView(game_id, host_id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("mlt launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _emit_final_standings(self, channel, game_id: str) -> None:
        """Post the cumulative-crown standings when a game ends (skipped if no
        crowns were ever awarded). Best-effort — never blocks teardown."""
        try:
            payload = await get_game_payload(self.db, game_id)
            crowns = payload.get("crowns") or {}
            if not any(int(c) > 0 for c in crowns.values()):
                return
            guild = getattr(channel, "guild", None)
            await channel.send(embed=build_final_standings_embed(crowns, guild))
        except Exception:
            log.exception("MLT: failed to emit final standings for %s", game_id)

    async def _voter_roster(self, game_id: str) -> list[int]:
        """Everyone who cast a vote in any completed round — the real
        participant set for economy payouts (survivors-only ``players`` would
        drop members who voted for several rounds then left)."""
        payload = await get_game_payload(self.db, game_id)
        return sorted({
            int(v)
            for rd in payload.get("rounds", {}).values()
            for v in (rd.get("votes") or {})
        })

    async def _run_round(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        players: list[int],
        channel,
        custom_prompt: str | None = None,
        carry_over_queue: list[str] | None = None,
    ):
        if custom_prompt:
            prompt = custom_prompt
        else:
            tags = (await get_game_payload(self.db, game_id)).get("tags") or None
            prompt = await get_mlt_prompt(
                self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel)
            )
        if not prompt:
            await channel.send(
                "❌ The prompt bank is empty! Use **✍️ Pose Prompt** to submit your own, "
                "or ask an admin to add prompts with `/bank add`."
            )
            await self._emit_final_standings(channel, game_id)
            await end_game(self.db, game_id, bot=self.bot, player_ids=await self._voter_roster(game_id))
            self.bot.active_views.pop(game_id, None)
            return

        payload = await get_game_payload(self.db, game_id)
        rounds_data = payload.setdefault("rounds", {})
        rounds_data[str(round_num)] = {"votes": {}, "prompt": prompt}
        await update_game_payload(self.db, game_id, payload)

        view = self._build_vote_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=round_num,
            players=players,
            channel=channel,
            prompt=prompt,
            interaction=interaction,
        )
        if carry_over_queue:
            view.queued_prompts = carry_over_queue
            count = len(carry_over_queue)
            view.next_btn.label = f"⏭️ Next ({count} queued)"
        self.bot.active_views[game_id] = view

        embed = view._build_embed()
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            if game_id in self.bot.active_views:
                del self.bot.active_views[game_id]
            try:
                await interaction.followup.send(
                    "❌ I don't have permission to send messages in that channel. "
                    "Please grant me **Send Messages** and **Embed Links** permissions.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return
        await update_game_message(self.db, game_id, msg.id)

    def _build_vote_view(
        self,
        *,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        players: list[int],
        channel,
        prompt: str,
        interaction=None,
    ) -> "MLTVoteView":
        """Construct a vote-round view with its advance callback wired.

        Shared by _run_round (fresh round) and recover_game (post-restart) so
        round-to-round advancement behaves identically after a crash.
        """
        guild = getattr(channel, "guild", None)

        async def advance(message: discord.Message):
            if view._closed:
                return
            view._closed = True

            tally = tally_votes(view.votes, players)

            results_embed = view._build_results_embed(tally)
            disable_all_items(view)
            try:
                await message.edit(embed=view._build_embed(closed=True), view=view)
            except discord.HTTPException:
                pass
            await channel.send(embed=results_embed)

            if await is_game_expired(self.db, game_id):
                await end_game(self.db, game_id)
                if game_id in self.bot.active_views:
                    del self.bot.active_views[game_id]
                return

            payload = await get_game_payload(self.db, game_id)
            crowns = payload.setdefault("crowns", {})
            bump_crowns(crowns, find_round_winners(tally))
            payload["rounds"][str(round_num)]["votes"] = encode_round_votes(view.votes)
            await update_game_payload(self.db, game_id, payload)

            # Re-read the roster so /games join and /games leave take effect next
            # round. NOTE: keep this round's `players` (used above by tally_votes)
            # untouched — only the next round runs with the updated roster.
            next_players = [int(p) for p in payload.get("players", players)]

            # Mid-game leaves can drop the roster below a playable size — end
            # cleanly rather than trying to build a vote with < 2 candidates.
            if len(next_players) < 2:
                await channel.send("🎲 Not enough players left — ending the game.")
                await self._emit_final_standings(channel, game_id)
                await end_game(self.db, game_id, bot=self.bot, player_ids=await self._voter_roster(game_id))
                self.bot.active_views.pop(game_id, None)
                return

            next_custom, remaining = pop_next_prompt(view.queued_prompts)
            try:
                await self._run_round(
                    interaction=interaction,
                    game_id=game_id,
                    host_id=host_id,
                    host_name=host_name,
                    round_num=round_num + 1,
                    players=next_players,
                    channel=channel,
                    custom_prompt=next_custom,
                    carry_over_queue=remaining if remaining else None,
                )
            except Exception:
                log.exception("Error advancing MLT game %s to round %d", game_id, round_num + 1)
                await end_game(self.db, game_id)
                self.bot.active_views.pop(game_id, None)
                try:
                    await channel.send("❌ Something went wrong advancing the round. Game ended.")
                except discord.HTTPException:
                    pass

        view = MLTVoteView(
            game_id=game_id,
            host_id=host_id,
            prompt=prompt,
            round_num=round_num,
            players=players,
            db=self.db,
            bot=self.bot,
            host_name=host_name,
            guild=guild,
            advance_callback=advance,
        )
        return view

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Rebuild the current phase's view after a restart.

        Join lobby -> MLTJoinView; a started game -> the current vote round's
        view with its live votes restored.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        rounds = payload.get("rounds", {})

        if not rounds:
            view = MLTJoinView(game_id, host_id, self.db, self.bot, self)
            self.bot.active_views[game_id] = view
            self.bot.add_view(view, message_id=message.id)
            log.info("Recovered mlt game %s (join phase) in #%s", game_id, getattr(channel, "name", channel.id))
            return True

        cur = max(rounds, key=lambda k: int(k))
        rd = rounds.get(cur, {})
        prompt = rd.get("prompt", "") or ""
        players = [int(p) for p in payload.get("players", [])]
        guild = getattr(channel, "guild", None)
        host_name = resolve_name(guild, host_id) if guild else "Host"

        view = self._build_vote_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=int(cur),
            players=players,
            channel=channel,
            prompt=prompt,
            interaction=None,
        )
        view.votes = {int(k): int(v) for k, v in (rd.get("votes") or {}).items()}
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        log.info("Recovered mlt game %s (round %s) in #%s", game_id, cur, getattr(channel, "name", channel.id))
        return True


    async def mid_game_join(self, channel, game_id: str, member):
        """Add *member* to a running game; they're in from the next round."""
        uid = member.id
        state: dict = {}

        def _add(payload):
            players = payload.setdefault("players", [])
            state["added"] = add_player(players, uid)

        await modify_payload(self.db, game_id, _add)
        if not state.get("added"):
            return False, f"**{member.display_name}** is already in this game."
        return True, f"🎲 **{member.display_name}** joined Most Likely To — in from the next round!"

    async def mid_game_leave(self, channel, game_id: str, member):
        """Remove *member* from a running game. Their crowns stay on the board."""
        uid = member.id
        state: dict = {}

        def _remove(payload):
            players = payload.setdefault("players", [])
            state["removed"] = remove_player(players, uid)

        await modify_payload(self.db, game_id, _remove)
        if not state.get("removed"):
            return False, f"**{member.display_name}** isn't in this game."
        return True, f"🎲 **{member.display_name}** left Most Likely To — their crowns stay on the board."


async def setup(bot: "Bot"):
    cog = MLTCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("mlt")
    play.add_command(cog.mlt, override=True)
    bot.game_launchers["mlt"] = cog.launch
    bot.game_recoverers["mlt"] = cog.recover_game
    bot.game_joiners["mlt"] = cog.mid_game_join
    bot.game_leavers["mlt"] = cog.mid_game_leave
