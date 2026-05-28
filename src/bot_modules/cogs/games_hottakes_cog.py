import asyncio
import logging
import random
import statistics
import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GAME_ICONS, HOW_TO_PLAY, PHASE_JOINING, PHASE_PLAYING, PHASE_RESULTS, PHASE_RECAP
from bot_modules.games.utils.audit import send_audit_log
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    ConfirmCloseView,
)
from bot_modules.games.utils.live_bar import LiveBarUpdater, build_bar

log = logging.getLogger(__name__)


VOTE_LABELS = ["🧊 Strongly Disagree", "👎 Disagree", "😐 Meh", "👍 Agree", "🔥 Strongly Agree"]
VOTE_VALUES = [1, 2, 3, 4, 5]  # temperature values
VOTE_KEYS = ["cold", "disagree", "meh", "agree", "hot"]


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
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Your Hot Take", interaction.channel.name if interaction.channel else "unknown")

        def _add_take(payload):
            takes = payload.setdefault("takes", [])
            takes.append({
                "user_id": interaction.user.id,
                "text": self.take.value,
                "display_order": len(takes),
            })

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
            except Exception:
                pass


class HotTakesSubmitView(discord.ui.View):
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
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Submit Hot Take", style=discord.ButtonStyle.primary, custom_id="ht_submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        modal = SubmitHotTakeModal(self.game_id, self.db, origin_message=interaction.message)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Start Voting", style=discord.ButtonStyle.success, custom_id="ht_start")
    async def start_voting(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start voting.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        takes = payload.get("takes", [])
        if not takes:
            await interaction.response.send_message("❌ No hot takes submitted yet!", ephemeral=True)
            return

        shuffled = takes[:]
        random.shuffle(shuffled)
        for i, t in enumerate(shuffled):
            t["display_order"] = i
        payload["takes"] = shuffled
        await update_game_payload(self.db, self.game_id, payload)

        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        # Ping submitters
        if interaction.guild:
            submitter_ids = {t["user_id"] for t in takes}
            mentions = [
                interaction.guild.get_member(uid).mention
                for uid in submitter_ids
                if interaction.guild.get_member(uid)
            ]
            if mentions:
                await interaction.channel.send(
                    f"🔥 **Hot Takes voting is starting!** {' '.join(mentions)} — get ready to vote!",
                    delete_after=15,
                )

        try:
            await self.cog._run_voting(
                interaction=interaction,
                game_id=self.game_id,
                host_id=self.host_id,
                host_name=interaction.user.display_name,
                channel=interaction.channel,
            )
        except Exception as e:
            log.error("Failed to start voting for game %s: %s", self.game_id, e, exc_info=True)
            await interaction.channel.send("❌ Something went wrong starting the vote. Game ended.")
            await end_game(self.db, self.game_id)
            self.bot.active_views.pop(self.game_id, None)

    @discord.ui.button(label="🛑 Cancel", style=discord.ButtonStyle.danger, custom_id="ht_cancel")
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

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="ht_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
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
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _build_embed(self, closed: bool = False) -> discord.Embed:
        title = f"{GAME_ICONS['hottakes']} HOT TAKE #{self.take_num}"
        if closed:
            title += " — ROUND OVER"
        embed = discord.Embed(title=title, color=PHASE_RESULTS if closed else PHASE_PLAYING)
        embed.add_field(name="Take", value=discord.utils.escape_markdown(self.take_text), inline=False)

        vote_counts = [0] * len(VOTE_LABELS)
        for v in self.votes.values():
            vote_counts[v] += 1
        total = sum(vote_counts)

        bars = []
        for i, label in enumerate(VOTE_LABELS):
            bar, pct = build_bar(vote_counts[i], total)
            bars.append(f"{label}\n{bar} {pct} ({vote_counts[i]})")
        embed.add_field(name="Votes", value="\n".join(bars), inline=False)
        embed.add_field(
            name="Progress",
            value=f"Take {self.take_num}/{self.total_takes}",
            inline=False,
        )
        embed.set_footer(text=f"{GAME_ICONS['hottakes']} Hot Takes  •  👁 Anonymous")
        return embed

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
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
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
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if self.game_id not in self.bot.active_views:
            await interaction.response.send_message("This game has already ended.", ephemeral=True)
            return
        modal = SubmitHotTakeModal(self.game_id, self.db, queue_mode=True)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="⏭️ Next Take", style=discord.ButtonStyle.secondary, custom_id="ht_next", row=1)
    async def next_take(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can advance.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="ht_close", row=1)
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
            # Flush in-memory votes for the current round into the payload
            payload = await get_game_payload(self.db, self.game_id)
            if self.votes:
                vote_counts = [0] * len(VOTE_LABELS)
                for v in self.votes.values():
                    vote_counts[v] += 1
                total = sum(vote_counts)
                if total > 0:
                    weighted_sum = sum(VOTE_VALUES[idx] * c for idx, c in enumerate(vote_counts))
                    avg = weighted_sum / total
                else:
                    avg = 0.0
                results = payload.get("results", [])
                results.append({
                    "text": self.take_text,
                    "counts": vote_counts,
                    "avg": avg,
                    "std": 0.0,
                    "voters": list(self.votes.keys()),
                    "author": 0,
                })
                payload["results"] = results
            await self._post_recap(channel, payload)
            await end_game(self.db, self.game_id, payload=payload)
            if self.game_id in self.bot.active_views:
                del self.bot.active_views[self.game_id]
            # Unblock the voting loop so it can exit cleanly
            if self._advanced_event:
                self._advanced_event.set()

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)

    async def _post_recap(self, channel, payload: dict):
        results = payload.get("results", [])
        if not results:
            return
        embed = discord.Embed(
            title=f"{GAME_ICONS['hottakes']} HOT TAKES — FINAL RESULTS",
            color=PHASE_RECAP,
        )
        if results:
            hottest = max(results, key=lambda x: x.get("avg", 0))
            coldest = min(results, key=lambda x: x.get("avg", 0))
            embed.add_field(name="🔥 Hottest Take", value=f'"{hottest["text"]}" (avg {hottest["avg"]:.1f}/5)', inline=False)
            embed.add_field(name="🧊 Coldest Take", value=f'"{coldest["text"]}" (avg {coldest["avg"]:.1f}/5)', inline=False)

            if len(results) > 1:
                midpoint = (VOTE_VALUES[0] + VOTE_VALUES[-1]) / 2
                most_divisive = max(
                    results,
                    key=lambda x: (x.get("std", 0), abs(x.get("avg", 0) - midpoint)),
                )
                embed.add_field(name="⚡ Most Divisive", value=f'"{most_divisive["text"]}"', inline=False)

        total_voters = set()
        for r in results:
            total_voters.update(r.get("voters", []))
        embed.add_field(name="Total Takes", value=str(len(results)), inline=True)
        embed.add_field(name="Total Voters", value=str(len(total_voters)), inline=True)
        await channel.send(embed=embed)


class HotTakesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="hottakes", description="Start a Hot Takes / Unpopular Opinions game!")
    async def hottakes(self, interaction: discord.Interaction):
        log.info("%s used /hottakes in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/config allow-channel`.",
                ephemeral=True,
            )
            return
        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "hottakes",
            state="joining",
            payload={"takes": [], "results": []},
        )

        embed = discord.Embed(
            title=f"{GAME_ICONS['hottakes']} HOT TAKES",
            description="Submit your spiciest take — all entries are anonymous.",
            color=PHASE_JOINING,
        )
        embed.add_field(name="Host", value=interaction.user.display_name, inline=True)
        embed.add_field(name="Submissions", value="0", inline=True)
        embed.set_footer(text=f"{GAME_ICONS['hottakes']} Hot Takes  •  👁 Anonymous")

        log.info("Game %s (hottakes) created by %s in #%s", game_id, interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        view = HotTakesSubmitView(game_id, interaction.user.id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        view._message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])

    async def _run_voting(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        channel,
    ):
        results = []
        processed = 0

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

            async def advance(message: discord.Message, _take=take_text, _num=take_num, _taker_id=take_data["user_id"]):
                if view._closed:
                    return
                view._closed = True

                vote_counts = [0] * len(VOTE_LABELS)
                voters = list(view.votes.keys())
                for v in view.votes.values():
                    vote_counts[v] += 1

                total = sum(vote_counts)
                if total > 0:
                    weighted_sum = sum(VOTE_VALUES[idx] * count for idx, count in enumerate(vote_counts))
                    avg = weighted_sum / total
                    values = [VOTE_VALUES[v] for v in view.votes.values()]
                    std = statistics.stdev(values) if len(values) > 1 else 0.0
                else:
                    avg = 0.0
                    std = 0.0

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
                for item in view.children:
                    item.disabled = True
                try:
                    await message.edit(embed=final_embed, view=view)
                except Exception:
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
            await view._post_recap(channel, payload)
        await end_game(
            self.db, game_id,
            player_count=len({v for r in results for v in r.get("voters", [])}),
            round_count=processed,
            payload=payload,
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]


async def setup(bot: commands.Bot):
    await bot.add_cog(HotTakesCog(bot))
