"""Collect results from an external game bot (e.g. "Gamebot" Cards Against
Humanity) so we can build our own leaderboards/streaks over games we don't run.

Design (per review): a format-agnostic collector. An on_message listener scoped
to one configured channel + bot user banks every watched message RAW into
games_external_messages, keyed on message_id so restarts/edits/backfills all
de-duplicate. Nothing is parsed here — metrics are derived later from the raw
table, so re-parsing on a format change never loses history.
"""
from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.economy.game_rewards import pay_cat_catch, pay_game_rewards
from bot_modules.games.command_groups import games
from bot_modules.games_config.logic import has_mod_or_admin_permissions
from bot_modules.games_external import logic, parser

log = logging.getLogger(__name__)


def is_mod_or_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return has_mod_or_admin_permissions(interaction.user.guild_permissions)

    return app_commands.check(predicate)


class GamesExternalCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # guild_id -> {bot_user_id: (channel_id, kind)}. Warmed on load; kept in
        # sync by the config commands so the on_message hot path never hits DB.
        self._watch: dict[int, dict[int, tuple[int, str]]] = {}

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self) -> None:
        try:
            for row in await logic.load_all_watches(self.db):
                self._watch.setdefault(int(row["guild_id"]), {})[
                    int(row["bot_user_id"])
                ] = (int(row["channel_id"]), str(row["kind"]))
            if self._watch:
                n = sum(len(v) for v in self._watch.values())
                log.info(
                    "External game tracking: %d watch(es) across %d guild(s)",
                    n, len(self._watch),
                )
        except Exception:
            log.exception("External game tracking: failed to warm watch cache")

    # ── collection ────────────────────────────────────────────────────────
    def _watched_kind(self, message: discord.Message) -> str | None:
        """The parser kind for a message's (channel, bot), or None if unwatched."""
        if message.guild is None:
            return None
        watches = self._watch.get(message.guild.id)
        if not watches:
            return None
        cfg = watches.get(message.author.id)
        if cfg is None or message.channel.id != cfg[0]:
            return None
        return cfg[1]

    async def _capture(self, message: discord.Message, kind: str) -> None:
        try:
            await logic.store_message(self.db, message)
        except Exception:
            log.exception("External game tracking: failed to store message %s", message.id)
            return
        # Bank first, then pay: the CAH payout reads the just-banked window back
        # out; the Cat Bot payout keys off this message's content.
        if kind == "gamebot_cah" and parser.is_game_over(
            [e.to_dict() for e in message.embeds]
        ):
            await self._pay_cah_game(message)
        elif kind == "catbot":
            await self._pay_cat_catch(message)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        kind = self._watched_kind(message)
        if kind is not None:
            await self._capture(message, kind)

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        # Gamebot posts "Loading…" then edits in the real embed — re-capture so
        # we keep the final content, not the placeholder (and the real Game
        # over! embed only appears on this edit, so payout fires here).
        kind = self._watched_kind(after)
        if kind is not None:
            await self._capture(after, kind)

    async def _pay_cah_game(self, message: discord.Message) -> None:
        """Pay participation + a win bonus for a finished Gamebot CAH game.

        Idempotent: ``claim_payout`` reserves the game (keyed on the Game over!
        message id) before any credit, so a re-captured edit or a restart never
        double-pays. Reuses ``pay_game_rewards`` so external games pay exactly
        like native ones (faucet + party_game/game_win quest triggers).
        """
        guild = message.guild
        if guild is None:
            return
        try:
            first = await logic.claim_payout(
                self.db, message.id, guild.id, "gamebot_cah"
            )
            if not first:
                return
            rows = await logic.recent_channel_messages(
                self.db, guild.id, message.channel.id, message.author.id,
                message.created_at.isoformat(),
            )
            parsed = [
                {"embeds": json.loads(r["embeds_json"] or "[]")} for r in rows
            ]
            idx = next(
                (i for i, r in enumerate(rows) if int(r["message_id"]) == message.id),
                len(parsed) - 1,
            )
            roster, winner = parser.extract_cah_game(
                parser.current_game_window(parsed, idx)
            )
            if not roster:
                await logic.mark_parsed(self.db, message.id, "skip")
                return
            await pay_game_rewards(
                self.bot, guild.id, sorted(roster),
                [winner] if winner is not None else [], "cah",
                occurrence=str(message.id),
            )
            await logic.mark_parsed(self.db, message.id, "ok")
            log.info(
                "CAH payout: guild %s game %s — %d players, winner %s",
                guild.id, message.id, len(roster), winner,
            )
        except Exception:
            log.exception("CAH payout failed for message %s", message.id)

    async def _pay_cat_catch(self, message: discord.Message) -> None:
        """Pay a Cat Bot catch: rarity-tiered coins + the cat_catch trigger.

        Cat Bot names the catcher by username, not a mention, so we resolve it
        to a guild member by name. Unresolvable catchers (left / renamed) and
        non-catch messages (spawns, the bonus blurb) pay nobody. Idempotent via
        the payout ledger, keyed on the catch message id.
        """
        guild = message.guild
        if guild is None:
            return
        try:
            catch = parser.parse_cat_catch(message.content or "")
            if catch is None:
                return
            member = guild.get_member_named(catch.username)
            if member is None or member.bot:
                log.info(
                    "Cat catch by unresolved user %r in guild %s — skipped",
                    catch.username, guild.id,
                )
                return
            first = await logic.claim_payout(self.db, message.id, guild.id, "catbot")
            if not first:
                return
            await pay_cat_catch(
                self.bot, guild.id, member.id,
                coins=catch.coins, rarity=catch.rarity, doubled=catch.doubled,
                occurrence=str(message.id),
            )
            await logic.mark_parsed(self.db, message.id, "ok")
            log.info(
                "Cat catch payout: guild %s %s caught a %s cat (%d coins%s)",
                guild.id, member.id, catch.rarity, catch.coins,
                ", doubled" if catch.doubled else "",
            )
        except Exception:
            log.exception("Cat catch payout failed for message %s", message.id)

    # ── config commands: /games track … ───────────────────────────────────
    track = app_commands.Group(
        name="track",
        description="Track results from an external game bot (mods only).",
    )

    @track.command(name="watch", description="Watch a channel + bot and start banking its game results.")
    @is_mod_or_admin()
    @app_commands.describe(
        channel="The channel the external game bot posts results in.",
        bot="The external game bot to track (e.g. Gamebot or Cat Bot).",
        kind="Which bot's format this is — selects the parser + payout.",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name=label, value=key)
            for key, label in logic.WATCH_KIND_LABELS.items()
        ]
    )
    async def track_watch(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        bot: discord.User,
        kind: app_commands.Choice[str],
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        if not bot.bot:
            await interaction.response.send_message(
                f"⚠️ {bot.mention} isn't a bot account. Pick the game bot itself "
                "(the one that posts the results).",
                ephemeral=True,
            )
            return
        await logic.set_watch(
            self.db, interaction.guild.id, channel.id, bot.id, kind.value,
            interaction.user.id,
        )
        self._watch.setdefault(interaction.guild.id, {})[bot.id] = (
            channel.id, kind.value,
        )
        log.info(
            "External game tracking enabled by %s: #%s watching bot %s (%s)",
            interaction.user.display_name, channel.name, bot.id, kind.value,
        )
        await interaction.response.send_message(
            f"✅ Now banking {bot.mention}'s messages in {channel.mention} as "
            f"**{kind.name}**. Run `/games track sample` after a game or two to "
            "confirm the format.",
            ephemeral=True,
        )

    @track.command(name="status", description="Show external game-tracking status for this server.")
    @is_mod_or_admin()
    async def track_status(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        watches = await logic.list_watches(self.db, interaction.guild.id)
        if not watches:
            await interaction.response.send_message(
                "No external game bot is being tracked. Use `/games track watch`.",
                ephemeral=True,
            )
            return
        lines = ["**External game tracking**"]
        for w in watches:
            n = await logic.count_messages(
                self.db, interaction.guild.id, int(w["bot_user_id"])
            )
            state = "enabled" if w["enabled"] else "paused"
            label = logic.WATCH_KIND_LABELS.get(str(w["kind"]), str(w["kind"]))
            lines.append(
                f"• <@{w['bot_user_id']}> — {label} in <#{w['channel_id']}> "
                f"({state}, **{n}** banked)"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _resolve_one_bot(
        self, guild_id: int, bot: discord.User | None
    ) -> tuple[int | None, str]:
        """The bot id to act on for disable/enable/sample, or (None, error)."""
        if bot is not None:
            return bot.id, ""
        watches = await logic.list_watches(self.db, guild_id)
        if not watches:
            return None, "Nothing configured yet — use `/games track watch` first."
        if len(watches) == 1:
            return int(watches[0]["bot_user_id"]), ""
        names = ", ".join(f"<@{w['bot_user_id']}>" for w in watches)
        return None, f"Several bots are tracked ({names}) — pass the `bot` option."

    @track.command(name="disable", description="Pause banking for a tracked bot (keeps all data).")
    @is_mod_or_admin()
    @app_commands.describe(bot="Which tracked bot to pause (optional if only one).")
    async def track_disable(
        self, interaction: discord.Interaction, bot: discord.User | None = None
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        bot_id, err = await self._resolve_one_bot(interaction.guild.id, bot)
        if bot_id is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        ok = await logic.set_watch_enabled(self.db, interaction.guild.id, bot_id, False)
        if ok:
            self._watch.get(interaction.guild.id, {}).pop(bot_id, None)
        msg = f"⏸️ Paused tracking <@{bot_id}>." if ok else "That bot wasn't being tracked."
        await interaction.response.send_message(msg, ephemeral=True)

    @track.command(name="enable", description="Resume banking a previously-configured bot.")
    @is_mod_or_admin()
    @app_commands.describe(bot="Which tracked bot to resume (optional if only one).")
    async def track_enable(
        self, interaction: discord.Interaction, bot: discord.User | None = None
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        bot_id, err = await self._resolve_one_bot(interaction.guild.id, bot)
        if bot_id is None:
            await interaction.response.send_message(err, ephemeral=True)
            return
        ok = await logic.set_watch_enabled(self.db, interaction.guild.id, bot_id, True)
        if not ok:
            await interaction.response.send_message(
                "That bot isn't configured — use `/games track watch` first.",
                ephemeral=True,
            )
            return
        row = await logic.get_watch_for_bot(self.db, interaction.guild.id, bot_id)
        if row:
            self._watch.setdefault(interaction.guild.id, {})[bot_id] = (
                int(row["channel_id"]), str(row["kind"]),
            )
        await interaction.response.send_message(
            f"▶️ Resumed tracking <@{bot_id}>.", ephemeral=True
        )

    @track.command(name="sample", description="Dump recent bot messages (raw content + embeds) to confirm the format.")
    @is_mod_or_admin()
    @app_commands.describe(
        channel="Channel to sample (defaults to the watched channel).",
        bot="Which tracked bot to sample (optional if only one).",
        count="How many recent messages to scan (1–100, default 40).",
    )
    async def track_sample(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        bot: discord.User | None = None,
        count: app_commands.Range[int, 1, 100] = 40,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        target = channel
        watched_bot: int | None = bot.id if bot is not None else None
        if target is None:
            bot_id, err = await self._resolve_one_bot(interaction.guild.id, bot)
            if bot_id is None:
                await interaction.followup.send(
                    err or "No watched channel — pass one with the `channel` option.",
                    ephemeral=True,
                )
                return
            row = await logic.get_watch_for_bot(
                self.db, interaction.guild.id, bot_id
            )
            if row is None:
                await interaction.followup.send(
                    "No watched channel — pass one with the `channel` option.",
                    ephemeral=True,
                )
                return
            watched_bot = int(row["bot_user_id"])
            ch = interaction.guild.get_channel(int(row["channel_id"]))
            if not isinstance(ch, discord.TextChannel):
                await interaction.followup.send(
                    "Watched channel is missing or not a text channel.", ephemeral=True
                )
                return
            target = ch

        dumped = []
        try:
            async for msg in target.history(limit=count):
                if not msg.author.bot:
                    continue
                if watched_bot is not None and msg.author.id != watched_bot:
                    continue
                dumped.append(
                    {
                        "message_id": msg.id,
                        "author": f"{msg.author} ({msg.author.id})",
                        "created_at": msg.created_at.isoformat(),
                        "content": msg.content,
                        "embeds": [e.to_dict() for e in msg.embeds],
                    }
                )
        except discord.Forbidden:
            await interaction.followup.send(
                f"I can't read history in {target.mention} (missing permission).",
                ephemeral=True,
            )
            return

        if not dumped:
            await interaction.followup.send(
                f"No bot messages found in the last {count} messages of {target.mention}.",
                ephemeral=True,
            )
            return

        blob = json.dumps(dumped, indent=2, ensure_ascii=False)
        file = discord.File(
            io.BytesIO(blob.encode("utf-8")), filename="gamebot_sample.json"
        )
        await interaction.followup.send(
            f"Dumped **{len(dumped)}** bot message(s) from {target.mention}.",
            file=file,
            ephemeral=True,
        )

    @track_watch.error
    @track_status.error
    @track_disable.error
    @track_enable.error
    @track_sample.error
    async def _track_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            try:
                await interaction.response.send_message(
                    "❌ You need moderator or admin permissions for this command.",
                    ephemeral=True,
                )
            except discord.NotFound:
                pass
        else:
            log.error("Error in /games track command: %s", error, exc_info=True)


async def setup(bot: "Bot"):
    cog = GamesExternalCog(bot)
    await bot.add_cog(cog)
    # add_cog auto-registers the `track` group at the top level of the tree;
    # pull it off so it only lives under /games (same pattern as the other
    # games subgroup cogs). Leaving it top-level registers an empty /track
    # group, which Discord rejects on sync.
    bot.tree.remove_command("track")
    games.add_command(cog.track, override=True)
