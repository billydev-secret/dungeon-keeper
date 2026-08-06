"""Collect results from an external game bot (e.g. "Gamebot" — Cards Against
Humanity, Connect 4) so we can build our own leaderboards/streaks over games
we don't run.

Design (per review): a format-agnostic collector. An on_message listener scoped
to one configured channel + bot user banks every watched message RAW into
games_external_messages, keyed on message_id so restarts/edits/backfills all
de-duplicate. Nothing is parsed here — metrics are derived later from the raw
table, so re-parsing on a format change never loses history.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_modules.economy.game_rewards import (
    pay_cah_game_by_score,
    pay_cat_catch,
    pay_game_rewards,
    resolve_named_scores,
)
from bot_modules.games_config.logic import has_mod_or_admin_permissions
from bot_modules.games_external import logic, parser
from bot_modules.services.event_echo_service import echo_gamebot_lobby

log = logging.getLogger(__name__)


def _load_catcatch_tier_coins(db_path, guild_id: int) -> dict[str, int]:
    """The guild's six cat-catch tier dials as the parser's override table."""
    from bot_modules.core.db_utils import open_db
    from bot_modules.services.economy_service import load_econ_settings

    with open_db(db_path) as conn:
        settings = load_econ_settings(conn, guild_id)
    return {
        tier: int(getattr(settings, f"catcatch_coins_{tier}"))
        for tier in ("common", "uncommon", "rare", "epic", "mythic", "divine")
    }


def is_mod_or_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return has_mod_or_admin_permissions(interaction.user.guild_permissions)

    return app_commands.check(predicate)


class GamesExternalCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # guild_id -> {(bot_user_id, channel_id): kind}. Warmed on load; kept in
        # sync by the config commands so the on_message hot path never hits DB.
        # Keyed on the *pair* since migration 135 — keyed on the bot alone, a
        # bot playing in a second channel was silently ignored there.
        self._watch: dict[int, dict[tuple[int, int], str]] = {}

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self) -> None:
        try:
            for row in await logic.load_all_watches(self.db):
                self._watch.setdefault(int(row["guild_id"]), {})[
                    (int(row["bot_user_id"]), int(row["channel_id"]))
                ] = str(row["kind"])
            if self._watch:
                n = sum(len(v) for v in self._watch.values())
                log.info(
                    "External game tracking: %d watch(es) across %d guild(s)",
                    n, len(self._watch),
                )
        except Exception:
            log.exception("External game tracking: failed to warm watch cache")
        self._buffer_sweep_loop.start()

    async def cog_unload(self) -> None:
        self._buffer_sweep_loop.cancel()

    @tasks.loop(hours=24)
    async def _buffer_sweep_loop(self) -> None:
        """Retention: the capture buffer's payouts are booked at parse time,
        so rows past 30 days have no read-back use — sweep them."""
        try:
            removed = await logic.sweep_old_buffer_rows(self.db)
            if removed:
                log.info("External game tracking: swept %d old buffer rows", removed)
        except Exception:
            log.exception("External game tracking: buffer sweep failed")

    @_buffer_sweep_loop.before_loop
    async def _before_buffer_sweep(self) -> None:
        await self.bot.wait_until_ready()

    # ── collection ────────────────────────────────────────────────────────
    def _watched_kind(self, message: discord.Message) -> str | None:
        """The parser kind for a message's (bot, channel), or None if unwatched."""
        if message.guild is None:
            return None
        watches = self._watch.get(message.guild.id)
        if not watches:
            return None
        return watches.get((message.author.id, message.channel.id))

    async def _capture(self, message: discord.Message, kind: str) -> None:
        try:
            await logic.store_message(self.db, message)
        except Exception:
            log.exception("External game tracking: failed to store message %s", message.id)
            return
        # Bank first, then pay: the Gamebot payouts read the just-banked window
        # back out; the Cat Bot payout keys off this message's content.
        if kind == "gamebot":
            embeds = [e.to_dict() for e in message.embeds]
            if parser.is_terminal(embeds):
                await self._pay_gamebot_game(message)
            # Two independent positive tests, not an if/else: hanging the echo
            # off "not terminal" would hardcode "a terminal embed is never also
            # a lobby embed" as control flow, so any later change to
            # is_terminal (an abandoned lobby, say) would silently disable the
            # echo with nothing in the echo feature to explain why.
            #
            # This runs from the edit path too: Gamebot posts "Loading…" and
            # edits the real embed in, so the lobby is often only visible on
            # the edit. The echo's dedupe key is the message id, identical
            # across both, so post-then-edit yields one echo rather than two.
            sub_game = parser.game_from_start(embeds)
            if sub_game is not None:
                await echo_gamebot_lobby(self.bot, message, sub_game)
        elif kind == "catbot":
            await self._pay_cat_catch(message)
        elif kind == "wordle":
            await self._pay_wordle_results(message)
        elif kind == "coordle":
            await self._pay_coordle_round(message)

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

    async def _game_window(self, message: discord.Message):
        """The banked messages making up the game this terminal message ends.

        Scoped to the terminal's own channel, so games running concurrently in
        different channels never see each other's messages.
        """
        guild = message.guild
        assert guild is not None
        rows = await logic.recent_channel_messages(
            self.db, guild.id, message.channel.id, message.author.id,
            message.created_at.isoformat(),
        )
        parsed = [{"embeds": json.loads(r["embeds_json"] or "[]")} for r in rows]
        idx = next(
            (i for i, r in enumerate(rows) if int(r["message_id"]) == message.id),
            len(parsed) - 1,
        )
        return parser.current_game_window(parsed, idx)

    @staticmethod
    def _lobby_host(guild: discord.Guild, window) -> int | None:
        """The member who started this game, from its lobby embed.

        Gamebot names the host by username in the lobby title, so it resolves
        the same way Anagrams' scoreboard does. External games passed no host
        at all before 2026-07-26 and so never paid the host bounty that native
        party games have always paid.
        """
        name = parser.host_from_window(window)
        if not name:
            return None
        member = guild.get_member_named(name)
        return None if member is None or member.bot else member.id

    async def _pay_gamebot_game(self, message: discord.Message) -> None:
        """Pay a finished Gamebot game — whichever of its sub-games it was.

        The sub-game is identified from the run's **lobby embed**, never from
        the *Game over!* message: CAH and Anagrams share the identical
        ``<@id> is the winner!`` wording, so dispatching on the terminal
        credited every Anagrams game as a one-player CAH game (fixed
        2026-07-26).

        Parses before claiming, since the claim records which game it was.
        That's still safe against double-payment — nothing is credited unless
        ``claim_payout`` wins the race for the *Game over!* message id, so a
        re-captured edit or a restart can parse twice but pay once.
        """
        guild = message.guild
        if guild is None:
            return
        try:
            window = await self._game_window(message)
            game = parser.identify_game(window)

            if parser.is_abandoned(window):
                # A lobby that timed out with too few players. Gamebot posts a
                # *Game over!* for it anyway; nobody played, so nobody is paid.
                # Claimed regardless so it's never reconsidered.
                await logic.claim_payout(
                    self.db, message.id, guild.id, "gamebot_abandoned"
                )
                await logic.mark_parsed(self.db, message.id, "skip")
                log.info("Gamebot game %s abandoned (too few players)", message.id)
                return

            if game is None:
                # A Gamebot game we have no parser for (Chess, Poker, …).
                # Deliberately left *unclaimed* so that teaching the parser a
                # new sub-game later can replay it; marking it 'skip' keeps it
                # out of the unparsed backlog meanwhile.
                await logic.mark_parsed(self.db, message.id, "skip")
                return

            payer = {
                parser.GAME_CAH: self._pay_cah_game,
                parser.GAME_CONNECT4: self._pay_connect4_game,
                parser.GAME_ANAGRAMS: self._pay_anagrams_game,
            }[game]
            await payer(message, window, self._lobby_host(guild, window))
        except Exception:
            log.exception("Gamebot payout failed for message %s", message.id)

    async def _pay_cah_game(self, message, window, host_id=None) -> None:
        """Pay a finished Gamebot CAH game proportional to each player's score.

        The top scorer (the winner) earns the configured cap and everyone else
        a ratio of it, but the same party_game/game_win quest triggers fire as
        a flat payout would.
        """
        guild = message.guild
        assert guild is not None
        scores, winner = parser.extract_cah_game(window)
        if not scores:
            await logic.mark_parsed(self.db, message.id, "skip")
            return
        if not await logic.claim_payout(self.db, message.id, guild.id, "gamebot_cah"):
            return
        await pay_cah_game_by_score(
            self.bot, guild.id, scores, winner, occurrence=str(message.id),
            host_id=host_id,
        )
        await logic.mark_parsed(self.db, message.id, "ok")
        log.info(
            "CAH payout: guild %s game %s — %d players, winner %s",
            guild.id, message.id, len(scores), winner,
        )

    async def _pay_connect4_game(self, message, window, host_id=None) -> None:
        """Pay participation + a win bonus for a finished Gamebot Connect 4 game.

        Connect 4 has no per-round score to scale by (like CAH does) — it's a
        single win/lose outcome — so this reuses the flat ``pay_game_rewards``
        faucet instead of a score-proportional one.
        """
        guild = message.guild
        assert guild is not None
        roster, winner = parser.extract_connect4_game(window)
        if not roster:
            await logic.mark_parsed(self.db, message.id, "skip")
            return
        if not await logic.claim_payout(
            self.db, message.id, guild.id, "gamebot_connect4"
        ):
            return
        await pay_game_rewards(
            self.bot, guild.id, sorted(roster),
            [winner] if winner is not None else [], "connect4",
            occurrence=str(message.id), host_id=host_id,
        )
        await logic.mark_parsed(self.db, message.id, "ok")
        log.info(
            "Connect 4 payout: guild %s game %s — %d players, winner %s",
            guild.id, message.id, len(roster), winner,
        )

    async def _pay_anagrams_game(self, message, window, host_id=None) -> None:
        """Pay a finished Gamebot Anagrams game proportional to points scored.

        Anagrams' *Scoreboard* names players by **username**, not mention, so
        they're resolved to members by name the way Cat Bot catches are; a
        player who has since left or renamed is logged and skipped rather than
        guessed at. The winner comes from *Game over!* as a mention and is
        folded in at 0 if the scoreboard somehow missed them.
        """
        guild = message.guild
        assert guild is not None
        named_scores, winner = parser.extract_anagrams_game(window)
        scores, unresolved = resolve_named_scores(guild, named_scores)
        if winner is not None:
            member = guild.get_member(winner)
            if member is not None and not member.bot:
                scores.setdefault(winner, 0)
        if not scores:
            await logic.mark_parsed(self.db, message.id, "skip")
            return
        if not await logic.claim_payout(
            self.db, message.id, guild.id, "gamebot_anagrams"
        ):
            return
        await pay_cah_game_by_score(
            self.bot, guild.id, scores, winner,
            occurrence=str(message.id), game_key="anagrams", host_id=host_id,
        )
        await logic.mark_parsed(self.db, message.id, "ok")
        log.info(
            "Anagrams payout: guild %s game %s — %d players, winner %s%s",
            guild.id, message.id, len(scores), winner,
            f", unresolved {unresolved}" if unresolved else "",
        )

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
            # Dial-load failure must never cost a member their catch — fall
            # back to the parser's shipped defaults and pay anyway.
            try:
                tier_coins = await asyncio.to_thread(
                    _load_catcatch_tier_coins, self.bot.ctx.db_path, guild.id
                )
            except Exception:
                log.exception("cat_catch: dial load failed — using shipped defaults")
                tier_coins = None
            catch = parser.parse_cat_catch(message.content or "", tier_coins)
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
            credited = await pay_cat_catch(
                self.bot, guild.id, member.id,
                coins=catch.coins, rarity=catch.rarity, doubled=catch.doubled,
                occurrence=str(message.id),
            )
            await logic.mark_parsed(self.db, message.id, "ok")
            # The credited amount, not catch.coins — the daily cap can clip a
            # catch to nothing, and this log is the only per-catch trace
            # outside the ledger.
            log.info(
                "Cat catch payout: guild %s %s caught a %s cat (%d coins%s)",
                guild.id, member.id, catch.rarity, credited,
                ", doubled" if catch.doubled else "",
            )
        except Exception:
            log.exception("Cat catch payout failed for message %s", message.id)

    async def _pay_wordle_results(self, message: discord.Message) -> None:
        """Pay a Wordle daily group digest, proportional to how few guesses
        each player needed.

        The digest is entirely self-contained — one message, no embeds, no
        preceding lobby — so unlike the Gamebot games this needs no backward
        scan at all and is keyed on the digest's own message id. Wordle's
        scoring is inverted (1/6 is best), so ``parse_wordle_results`` flips it
        before the shared score-proportional payout sees it. Ties on the 👑
        line are normal, so every crowned player is a winner.
        """
        guild = message.guild
        if guild is None:
            return
        try:
            results = parser.parse_wordle_results(message.content or "")
            if results is None:
                return
            scores = {
                uid: score
                for uid, score in results.scores.items()
                if (m := guild.get_member(uid)) is not None and not m.bot
            }
            winners = set(results.winners)
            # Players Wordle printed as plain "@Name" instead of mentioning are
            # resolved by name; a real mention always wins on a collision.
            by_name, unresolved = resolve_named_scores(guild, results.named_scores)
            for uid, score in by_name.items():
                scores.setdefault(uid, score)
            named_winner_ids, _ = resolve_named_scores(
                guild, {n: 1 for n in results.named_winners}
            )
            winners |= set(named_winner_ids)
            if not scores:
                await logic.mark_parsed(self.db, message.id, "skip")
                return
            if not await logic.claim_payout(self.db, message.id, guild.id, "wordle"):
                return
            await pay_cah_game_by_score(
                self.bot, guild.id, scores, sorted(winners),
                occurrence=str(message.id), game_key="wordle",
            )
            await logic.mark_parsed(self.db, message.id, "ok")
            log.info(
                "Wordle payout: guild %s digest %s — %d players, %d winner(s)%s",
                guild.id, message.id, len(scores), len(winners),
                f", unresolved {unresolved}" if unresolved else "",
            )
        except Exception:
            log.exception("Wordle payout failed for message %s", message.id)

    async def _pay_coordle_round(self, message: discord.Message) -> None:
        """Pay a finished Co-ordle round from its final board.

        Co-ordle posts a **new board message per guess**, each showing the whole
        round so far, and never posts a terminal message. So this fires on every
        board, pays only once the board reads as final (solved, or every row
        used), and claims on the round's own scheduled timestamp rather than a
        message id — otherwise each guess would look like a separate game. A
        round that times out with rows to spare stays open and pays nobody;
        there is no signal that it ended.
        """
        guild = message.guild
        if guild is None:
            return
        try:
            embeds = [e.to_dict() for e in message.embeds]
            if not parser.is_coordle_board(embeds):
                return
            round_key = parser.coordle_game_key(embeds)
            if round_key is None:
                return
            raw_scores, winner, state = parser.extract_coordle_game(embeds)
            if state not in (parser.COORDLE_SOLVED, parser.COORDLE_EXHAUSTED):
                return
            scores = {
                uid: pts
                for uid, pts in raw_scores.items()
                if (m := guild.get_member(uid)) is not None and not m.bot
            }
            if not scores:
                return
            # Keyed on the round, not this message — see the docstring.
            if not await logic.claim_payout(self.db, round_key, guild.id, "coordle"):
                return
            await pay_cah_game_by_score(
                self.bot, guild.id, scores, winner,
                occurrence=str(round_key), game_key="coordle",
            )
            await logic.mark_parsed(self.db, message.id, "ok")
            log.info(
                "Co-ordle payout: guild %s round %s (%s) — %d players, winner %s",
                guild.id, round_key, state, len(scores), winner,
            )
        except Exception:
            log.exception("Co-ordle payout failed for message %s", message.id)

    # ── watch cache ───────────────────────────────────────────────────────
    #
    # /games track watch|status|disable|enable|sample were replaced by
    # Games → External Tracking on the dashboard (2026-07-28). The listener
    # matches messages against this in-memory map, so a dashboard write has to
    # refresh it — otherwise a newly-watched channel is ignored until restart.

    async def refresh_watch_cache(self, guild_id: int) -> None:
        """Re-read this guild's watches into the listener's fast path.

        Public: the dashboard route calls it after any write. Rebuilds the
        guild's whole entry rather than patching one key, so a pause that
        spans several channels and a re-point to a new channel both land
        correctly without the caller having to know which happened.
        """
        rows = await logic.list_watches(self.db, guild_id)
        self._watch[guild_id] = {
            (int(r["bot_user_id"]), int(r["channel_id"])): str(r["kind"])
            for r in rows
            if r["enabled"]
        }


async def setup(bot: "Bot"):
    # No commands: this cog is a message listener plus the watch cache the
    # dashboard writes through. The /games track group it used to register was
    # removed 2026-07-28.
    await bot.add_cog(GamesExternalCog(bot))
