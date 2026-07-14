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
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_name,
    channel_name,
)
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games.utils.recovery import start_redrive
from bot_modules.games_hottakes.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_vote_embed,
)
from bot_modules.games_hottakes.logic import (
    VOTE_LABELS,
    VOTE_VALUES,
    add_take,
    shuffle_takes,
    tally_votes,
)

log = logging.getLogger(__name__)


class SubmitHotTakeModal(discord.ui.Modal, title="Your Hot Take"):
    take = discord.ui.TextInput(
        label="Hot Take",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Type your spiciest opinion here...",
    )

    def __init__(self, game_id: str, db, origin_message: discord.Message | None = None, *, queue_mode: bool = False):
        super().__init__()
        self.game_id = game_id
        self.db = db
        self._origin_message = origin_message
        self.queue_mode = queue_mode

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Your Hot Take", channel_name(interaction.channel))

        def _add_take(payload):
            add_take(payload, interaction.user.id, self.take.value)

        payload = await modify_payload(self.db, self.game_id, _add_take)

        # Audit log
        if interaction.guild:
            await send_audit_log(
                interaction.client, self.db, interaction.guild,
                game_type="hottakes", user=interaction.user,
                content=self.take.value, label="Hot Take Submission",
            )

        if self.queue_mode:
            await interaction.response.send_message(
                "✅ Hot take queued! It will be voted on after the current takes.",
                ephemeral=True,
            )
            return

        take_count = len(payload.get("takes", []))
        await interaction.response.send_message(
            f"✅ Hot take submitted! Total submissions: {take_count}", ephemeral=True
        )
        msg = self._origin_message or interaction.message
        if msg:
            embed = msg.embeds[0]
            for i, field in enumerate(embed.fields):
                if field.name == "Submissions":
                    embed.set_field_at(i, name="Submissions", value=str(take_count), inline=True)
                    break
            try:
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass


class HotTakesSubmitView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self._message: discord.Message | None = None

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Submit Hot Take", style=discord.ButtonStyle.primary, custom_id="ht_submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        modal = SubmitHotTakeModal(self.game_id, self.db, origin_message=interaction.message)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Start Voting", style=discord.ButtonStyle.primary, custom_id="ht_start")
    async def start_voting(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start voting.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        takes = payload.get("takes", [])
        if not takes:
            await interaction.response.send_message("❌ No hot takes submitted yet!", ephemeral=True)
            return

        payload["takes"] = shuffle_takes(takes)
        await update_game_payload(self.db, self.game_id, payload)

        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)

        channel = interaction.channel
        assert channel is not None and not isinstance(channel, (discord.ForumChannel, discord.CategoryChannel))

        # Ping submitters
        if interaction.guild:
            submitter_ids = {t["user_id"] for t in takes}
            mentions = [
                member.mention
                for uid in submitter_ids
                if (member := interaction.guild.get_member(uid))
            ]
            if mentions:
                await channel.send(
                    f"🔥 **Hot Takes voting is starting!** {' '.join(mentions)} — get ready to vote!",
                    delete_after=15,
                )

        try:
            await self.cog._run_voting(
                interaction=interaction,
                game_id=self.game_id,
                host_id=self.host_id,
                host_name=interaction.user.display_name,
                channel=channel,
            )
        except Exception as e:
            log.error("Failed to start voting for game %s: %s", self.game_id, e, exc_info=True)
            await channel.send("❌ Something went wrong starting the vote. Game ended.")
            await end_game(self.db, self.game_id)
            self.bot.active_views.pop(self.game_id, None)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="ht_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["hottakes"], ephemeral=True)


class HotTakeVoteView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        take_text: str,
        take_num: int,
        total_takes: int,
        db,
        bot,
        host_name: str,
        advance_callback,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.take_text = take_text
        self.take_num = take_num
        self.total_takes = total_takes
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.votes: dict[int, int] = {}  # user_id -> 0-4 index
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

    def _build_embed(self, closed: bool = False) -> discord.Embed:
        return build_vote_embed(
            take_text=self.take_text,
            take_num=self.take_num,
            total_takes=self.total_takes,
            votes_by_user=self.votes,
            closed=closed,
        )

    @discord.ui.button(label="🧊", style=discord.ButtonStyle.secondary, custom_id="ht_v0", row=0)
    async def vote_0(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, 0)

    @discord.ui.button(label="👎", style=discord.ButtonStyle.secondary, custom_id="ht_v1", row=0)
    async def vote_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, 1)

    @discord.ui.button(label="😐", style=discord.ButtonStyle.secondary, custom_id="ht_v2", row=0)
    async def vote_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, 2)

    @discord.ui.button(label="👍", style=discord.ButtonStyle.secondary, custom_id="ht_v3", row=0)
    async def vote_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, 3)

    @discord.ui.button(label="🔥", style=discord.ButtonStyle.secondary, custom_id="ht_v4", row=0)
    async def vote_4(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, 4)

    async def _do_vote(self, interaction: discord.Interaction, idx: int):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This vote is closed.", ephemeral=True)
            return
        prev = self.votes.get(interaction.user.id)
        self.votes[interaction.user.id] = idx
        label = VOTE_LABELS[idx]
        changed = prev is not None and prev != idx
        msg = f"✅ Voted **{label}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="📝 Submit Take", style=discord.ButtonStyle.secondary, custom_id="ht_v_submit", row=1)
    async def submit_take(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if self.game_id not in self.bot.active_views:
            await interaction.response.send_message("This game has already ended.", ephemeral=True)
            return
        modal = SubmitHotTakeModal(self.game_id, self.db, queue_mode=True)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⏭️ Next Take", style=discord.ButtonStyle.secondary, custom_id="ht_next", row=1)
    async def next_take(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can advance.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)

    async def _post_recap(self, channel, payload: dict):
        results = payload.get("results", [])
        embed = build_recap_embed(results)
        if embed is None:
            return
        await channel.send(embed=embed)


class HotTakesCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="hottakes", description="Start a Hot Takes / Unpopular Opinions game!")
    async def hottakes(self, interaction: discord.Interaction):
        log.info("%s used /games play hottakes in #%s", interaction.user.display_name, channel_name(interaction.channel))
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
            "hottakes",
            state="joining",
            payload={"takes": [], "results": []},
        )

        embed = build_lobby_embed(host_name)

        log.info("Game %s (hottakes) created by %s in #%s", game_id, host_name, getattr(channel, "name", channel.id))
        view = HotTakesSubmitView(game_id, host_id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("hottakes launch lacked send perms in channel %s", channel.id)
            return None
        view._message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _run_voting(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        channel,
        resume: bool = False,
    ):
        # On resume after a restart, seed from persisted results so already-voted
        # takes are skipped; the take whose round was interrupted is re-voted.
        if resume:
            payload = await get_game_payload(self.db, game_id)
            results = list(payload.get("results", []))
            processed = len(results)
        else:
            results = []
            processed = 0

        view: HotTakeVoteView | None = None
        while True:
            payload = await get_game_payload(self.db, game_id)
            if game_id not in self.bot.active_views:
                return

            all_takes = payload.get("takes", [])
            if processed >= len(all_takes):
                break

            take_data = all_takes[processed]
            processed += 1
            take_text = take_data["text"]
            take_num = processed
            total_takes = len(all_takes)

            advanced = asyncio.Event()

            async def advance(message: discord.Message, _take=take_text, _num=take_num, _taker_id=take_data["user_id"]) -> None:
                assert view is not None
                if view._closed:
                    return
                view._closed = True

                vote_counts, avg, std = tally_votes(view.votes)
                voters = list(view.votes.keys())

                result_entry = {
                    "text": _take,
                    "counts": vote_counts,
                    "avg": avg,
                    "std": std,
                    "voters": voters,
                    "author": _taker_id,
                }
                results.append(result_entry)

                # Persist incrementally so mid-game close doesn't lose prior results
                def _save_result(payload, _entry=result_entry):
                    payload.setdefault("results", []).append(_entry)
                await modify_payload(self.db, game_id, _save_result)

                final_embed = view._build_embed(closed=True)
                disable_all_items(view)
                try:
                    await message.edit(embed=final_embed, view=view)
                except discord.HTTPException:
                    pass
                advanced.set()

            view = HotTakeVoteView(
                game_id=game_id,
                host_id=host_id,
                take_text=take_text,
                take_num=take_num,
                total_takes=total_takes,
                db=self.db,
                bot=self.bot,
                host_name=host_name,
                advance_callback=advance,
            )
            view._advanced_event = advanced
            self.bot.active_views[game_id] = view

            embed = view._build_embed()
            msg = await channel.send(embed=embed, view=view)
            await update_game_message(self.db, game_id, msg.id)

            await advanced.wait()
            # If the game was closed mid-round, stop the loop
            if view._closed and game_id not in self.bot.active_views:
                break
            await asyncio.sleep(1)

        # If the game was already closed by the host, skip final results
        if game_id not in self.bot.active_views:
            return

        # Results were saved incrementally in advance(); just read final state
        payload = await get_game_payload(self.db, game_id)

        if processed > 0:
            assert view is not None
            await view._post_recap(channel, payload)
        # Roster = everyone who voted or authored a take; the winning take's
        # author may not have voted, so a voters-only set would drop their bonus.
        participants = sorted(
            {v for r in results for v in r.get("voters", [])}
            | {r["author"] for r in results if r.get("author") is not None}
        )
        await end_game(
            self.db, game_id,
            player_count=len(participants),
            round_count=processed,
            payload=payload,
            bot=self.bot, player_ids=participants,
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-drive the voting loop after a restart.

        Completed takes live in payload["results"]; the take being voted on at
        crash time can't be reconstructed (live votes aren't persisted), so we
        retire the stale message and re-vote that take. The re-driven loop seeds
        results from the payload and continues with the remaining takes.
        """
        takes = payload.get("takes", [])
        if not takes or len(payload.get("results", [])) >= len(takes):
            return False  # nothing left to resume; cleanup loop will archive it
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        host_name = resolve_name(guild, host_id) if guild else "Host"
        await start_redrive(
            self.bot, game_id, message,
            self._run_voting(
                interaction=None, game_id=game_id, host_id=host_id,
                host_name=host_name, channel=channel, resume=True,
            ),
            channel=channel, log_label=f"hottakes game {game_id} (re-driving voting)",
        )
        return True


async def setup(bot: "Bot"):
    cog = HotTakesCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("hottakes")
    play.add_command(cog.hottakes, override=True)
    bot.game_launchers["hottakes"] = cog.launch
    bot.game_recoverers["hottakes"] = cog.recover_game


# Re-export VOTE_VALUES so any tests / external callers that imported it
# from this module continue to work.
__all__ = [
    "HotTakesCog",
    "HotTakesSubmitView",
    "HotTakeVoteView",
    "SubmitHotTakeModal",
    "VOTE_LABELS",
    "VOTE_VALUES",
    "setup",
]
