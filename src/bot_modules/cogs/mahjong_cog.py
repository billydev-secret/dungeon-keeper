"""Meadow Mahjong cog — Discord glue only, no rules (stage 6 of
docs/plans/meadow-mahjong.md, spec §6).

Thin by design: every decision lives in games/mahjong (engine + service);
this file renders state, routes taps, and keeps the sticky table message
at the bottom of its channel. Member self-service only — the entire admin
surface is the dashboard panel (route id ``mahjong``); this cog registers
exactly one command, ``/mahjong``.

One StickyPanel per live table (tables are per-channel, the house sticky
is per-guild-key — each panel here loads/saves its own table's message id
and the on_message listener routes by channel). The one-table-per-channel
schema rule is what keeps two panels from ever sharing a channel.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

from bot_modules.core.branding import DEFAULT_ACCENT_COLOR, safe_resolve_accent
from bot_modules.core.db_utils import open_db
from bot_modules.core.utils import has_mod_or_admin_permissions
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.games.mahjong import embeds as mj_embeds
from bot_modules.games.mahjong import match_logic as mj_match
from bot_modules.games.mahjong import views as mj_views
from bot_modules.games.mahjong.game_logic import (
    ActionRejected,
    GameState,
    Phase,
)
from bot_modules.games.mahjong.mahjong_service import (
    STALE_TABLE,
    MahjongService,
    TableError,
    activate_due_cards,
    escrow_amount,
    get_active_card,
    load_settings,
)
from bot_modules.games.mahjong.tile_render import rack_str
from bot_modules.games.mahjong.tiles import Tile, sort_rack
from bot_modules.services.economy_service import get_balance

log = logging.getLogger(__name__)


class MahjongCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        self.service = MahjongService(bot.ctx.db_path)
        self.service.set_listener(self._on_transition)
        #: table_id → its sticky panel; rebuilt on resume.
        self.panels: dict[int, StickyPanel] = {}
        #: channel_id → table_id for the on_message sticky routing.
        self.channel_tables: dict[int, int] = {}

    async def cog_load(self) -> None:
        self.card_scheduler.start()
        asyncio.create_task(self._resume())

    async def cog_unload(self) -> None:
        self.card_scheduler.cancel()
        for panel in self.panels.values():
            panel.cancel_all()
        await self.service.shutdown()

    async def _resume(self) -> None:
        await self.bot.wait_until_ready()
        table_ids = await self.service.resume_tables()
        for table_id in table_ids:
            meta = await self.service.table_meta(table_id)
            state = await self.service.load_state(table_id)
            if meta is None or state is None or meta["status"] != "live":
                continue  # a past-due timer already closed it (resume race)
            self._track_table(table_id, meta["channel_id"])
            self.bot.add_view(
                mj_views.TableView(self, table_id, register_all=True)
            )
        if table_ids:
            log.info("Mahjong: resumed %d live table(s).", len(table_ids))

    # ── Sticky plumbing (one panel per table) ───────────────────────────

    def _track_table(self, table_id: int, channel_id: int) -> None:
        self.channel_tables[channel_id] = table_id
        if table_id in self.panels:
            return

        def load_ids(_guild_id: int) -> tuple[int, int]:
            with open_db(self.bot.ctx.db_path) as conn:
                row = conn.execute(
                    "SELECT channel_id, sticky_message_id FROM mahjong_tables "
                    "WHERE id = ?", (table_id,),
                ).fetchone()
            if row is None or row["sticky_message_id"] is None:
                return (0, 0)
            return (int(row["channel_id"]), int(row["sticky_message_id"]))

        def save_ids(_guild_id: int, channel_id: int, message_id: int) -> None:
            with open_db(self.bot.ctx.db_path) as conn:
                conn.execute(
                    "UPDATE mahjong_tables SET sticky_message_id = ? "
                    "WHERE id = ?", (message_id, table_id),
                )
                conn.commit()

        async def build(guild: discord.Guild) -> PanelContent:
            content = await self._build_table_panel(guild, table_id)
            if content is None:
                raise RuntimeError("table closed between trigger and restick")
            return content

        self.panels[table_id] = StickyPanel(
            f"mahjong table {table_id}", self.bot,
            load_ids=load_ids, save_ids=save_ids, build=build,
        )

    def _untrack_table(self, table_id: int, channel_id: int | None) -> None:
        panel = self.panels.pop(table_id, None)
        if panel is not None:
            panel.cancel_all()
        if channel_id is not None and self.channel_tables.get(channel_id) == table_id:
            self.channel_tables.pop(channel_id, None)

    @commands.Cog.listener("on_member_remove")
    async def _fold_leaver(self, member: discord.Member) -> None:
        """A seated member left the guild: their seat folds fallow (escrow
        stays held and pays per the fallow rules). The economy cog's generic
        leaver-refund sweep excludes mahjong for exactly this reason."""
        try:
            await self.service.member_left(member.guild.id, member.id)
        except Exception:
            log.exception("Mahjong: leaver fold failed for %d", member.id)

    @commands.Cog.listener("on_message")
    async def _sticky_on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        table_id = self.channel_tables.get(message.channel.id)
        if table_id is None:
            return
        panel = self.panels.get(table_id)
        if panel is not None:
            await panel.on_message(message)

    async def _build_table_panel(
        self, guild: discord.Guild, table_id: int
    ) -> PanelContent | None:
        meta = await self.service.table_meta(table_id)
        state = await self.service.load_state(table_id)
        if meta is None or state is None or meta["status"] != "live":
            return None
        card = await self.service.card_for_table(table_id)
        escrow = (
            escrow_amount(card, meta["mode"], meta["stake"]) if card else 0
        )
        accent = await safe_resolve_accent(self.bot, guild, default=DEFAULT_ACCENT_COLOR)
        embed = mj_embeds.build_table_panel(
            state, self._names(guild, state), meta["stake"], escrow,
            accent, meta["deadline_at"],
        )
        return PanelContent(
            embed=embed, view=mj_views.TableView(self, table_id, state.phase)
        )

    def _names(self, guild: discord.Guild, state: GameState) -> dict[int, str]:
        out: dict[int, str] = {}
        for s in state.seats:
            member = guild.get_member(s.member_id)
            out[s.member_id] = member.display_name if member else f"#{s.member_id}"
        return out

    def _seat_names(self, guild: discord.Guild, state: GameState) -> dict[int, str]:
        names = self._names(guild, state)
        return {i: names[s.member_id] for i, s in enumerate(state.seats)}

    # ── Service listener: render after every transition ─────────────────

    async def _on_transition(
        self, table_id: int, state: GameState, events: list
    ) -> None:
        meta = await self.service.table_meta(table_id)
        if meta is None:
            return
        guild = self.bot.get_guild(meta["guild_id"])
        if guild is None:
            return
        channel = guild.get_channel(meta["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        names = self._seat_names(guild, state)

        # public announcement embeds ride their own messages
        for kind, data in events:
            if data.get("private"):
                continue  # delivered on the tap's own response, never here
            if kind == "joker_redeemed":
                await self._safe_send(channel, embed=mj_embeds.build_joker_redeemed(
                    names.get(data["seat"], "?"), names.get(data["owner"], "?"),
                    Tile(data["tile"]),
                ))
            elif kind == "mahjong":
                out = state.outcome
                if out is not None and out.winner is not None:
                    card = await self.service.card_for_table(table_id)
                    shown = self._winning_groups(state, out.winner, card)
                    await self._safe_send(channel, embed=mj_embeds.build_mahjong_reveal(
                        out, names.get(out.winner, "?"), shown,
                    ))
            elif kind == "hand_settled":
                out = state.outcome
                if out is not None:
                    await self._safe_send(channel, embed=mj_embeds.build_settlement(
                        out, names, meta["stake"],
                    ))
            elif kind == "table_closed":
                self._untrack_table(table_id, meta["channel_id"])

        if meta["status"] == "live":
            self._track_table(table_id, meta["channel_id"])
            panel = self.panels.get(table_id)
            if panel is not None:
                await panel.place_or_refresh(guild, channel)
        elif meta["sticky_message_id"]:
            # closed: leave the final card in place, buttons gone
            try:
                msg = channel.get_partial_message(meta["sticky_message_id"])
                await msg.edit(view=None)
            except discord.HTTPException:
                pass

    def _winning_groups(self, state: GameState, seat: int, card) -> str:
        """The winning 14 rendered group by group (§6.12) — re-matched from
        the final state; falls back to the sorted rack if the card moved."""
        winner = state.seats[seat]
        try:
            if card is None:
                raise LookupError("card gone")
            matches = mj_match.match_hand(
                list(winner.rack), [e.as_match() for e in winner.exposures],
                card,
            )
            best = mj_match.best_match([
                m for m in matches if m.hand.id == (state.outcome.line_id if state.outcome else None)
            ] or matches)
            if best is not None:
                return "  ·  ".join(
                    " ".join([mj_embeds.tile_str(nat)] * count)
                    for nat, count in mj_match.resolved_groups(best)
                )
        except Exception:
            log.debug("reveal group render fell back", exc_info=True)
        shown = rack_str(sort_rack(winner.rack))
        for e in winner.exposures:
            shown += "   " + " ".join(
                [mj_embeds.tile_str(e.natural)] * (e.count - e.jokers)
                + [mj_embeds.tile_str(Tile.JOKER)] * e.jokers
            )
        return shown

    async def _safe_send(self, channel: discord.TextChannel, **kwargs) -> None:
        try:
            await channel.send(
                allowed_mentions=discord.AllowedMentions.none(), **kwargs
            )
        except discord.HTTPException:
            log.warning("Mahjong: send failed in #%s", channel, exc_info=True)

    # ── /mahjong ────────────────────────────────────────────────────────

    @app_commands.command(
        name="mahjong",
        description="Meadow Mahjong — open your panel: play, study the card, see your stats.",
    )
    @app_commands.guild_only()
    async def mahjong(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        def _q():
            with open_db(self.bot.ctx.db_path) as conn:
                settings = load_settings(conn, guild.id)
                active = get_active_card(conn, guild.id)
                balance = get_balance(conn, guild.id, interaction.user.id)
                return settings, active, balance

        settings, active, balance = await asyncio.to_thread(_q)
        if not settings.enabled:
            await interaction.response.send_message(
                "Meadow Mahjong isn't open on this server yet.", ephemeral=True
            )
            return
        accent = await safe_resolve_accent(self.bot, guild, default=DEFAULT_ACCENT_COLOR)
        embed = mj_embeds.build_member_panel(
            active[1] if active else None, settings.stakes_allowed, balance, accent
        )
        await interaction.response.send_message(
            embed=embed, view=mj_views.MemberPanelView(self), ephemeral=True
        )

    # ── Member-panel handlers ───────────────────────────────────────────

    async def handle_create_start(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        def _q():
            with open_db(self.bot.ctx.db_path) as conn:
                settings = load_settings(conn, guild.id)
                active = get_active_card(conn, guild.id)
                return settings, active

        settings, active = await asyncio.to_thread(_q)
        if active is None:
            await interaction.response.send_message(
                "No Meadow Card is active — an admin sets one on the dashboard.",
                ephemeral=True,
            )
            return
        card = active[1]

        def escrow_for(seat_count: int, stake: int) -> int:
            return escrow_amount(card, seat_count, stake)

        await interaction.response.send_message(
            "Table size?",
            view=mj_views.CreateTableView(self, settings.stakes_allowed, escrow_for),
            ephemeral=True,
        )

    async def handle_create_open(
        self, interaction: discord.Interaction, seat_count: int, stake: int
    ) -> None:
        guild = interaction.guild
        assert guild is not None and interaction.channel_id is not None
        if not isinstance(interaction.channel, discord.TextChannel):
            # a thread/voice-text table could hold escrow behind a card the
            # sticky machinery can never post (stage-6 review)
            await interaction.response.send_message(
                "❌ Tables live in regular text channels — open one there.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            table_id = await self.service.create_table(
                guild.id, interaction.channel_id, interaction.user.id,
                seat_count, stake,
            )
        except (TableError, ActionRejected) as e:
            await interaction.followup.send(self._dress(str(e)), ephemeral=True)
            return
        self._track_table(table_id, interaction.channel_id)
        panel = self.panels.get(table_id)
        channel = guild.get_channel(interaction.channel_id)
        if panel is not None and isinstance(channel, discord.TextChannel):
            await panel.place_or_refresh(guild, channel)
        await interaction.followup.send(
            "Table's open — the card is in the channel. Escrow locks in as "
            "seats fill.", ephemeral=True,
        )

    async def handle_card_viewer(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        def _q():
            with open_db(self.bot.ctx.db_path) as conn:
                return get_active_card(conn, guild.id)

        active = await asyncio.to_thread(_q)
        if active is None:
            await interaction.response.send_message(
                "No card is active right now.", ephemeral=True
            )
            return
        accent = await safe_resolve_accent(self.bot, guild, default=DEFAULT_ACCENT_COLOR)
        await interaction.response.send_message(
            embeds=mj_embeds.build_card_viewer(active[1], accent)[:10], ephemeral=True
        )

    async def handle_my_stats(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        def _q():
            with open_db(self.bot.ctx.db_path) as conn:
                return [
                    dict(r) for r in conn.execute(
                        "SELECT * FROM mahjong_stats WHERE guild_id = ? "
                        "AND user_id = ?",
                        (guild.id, interaction.user.id),
                    )
                ]

        rows = await asyncio.to_thread(_q)
        accent = await safe_resolve_accent(self.bot, guild, default=DEFAULT_ACCENT_COLOR)
        await interaction.response.send_message(
            embed=mj_embeds.build_my_stats(rows, accent), ephemeral=True
        )

    # ── Table-button dispatch ───────────────────────────────────────────

    async def handle_table_button(
        self, interaction: discord.Interaction, table_id: int, action: str
    ) -> None:
        try:
            if action == "join":
                await self._act_simple(
                    interaction,
                    self.service.join_table(table_id, interaction.user.id),
                    "You're seated — escrow locked. 🀄",
                )
            elif action == "cancel":
                member = interaction.user
                is_mod = (
                    isinstance(member, discord.Member)
                    and has_mod_or_admin_permissions(member.guild_permissions)
                )
                await self._act_simple(
                    interaction,
                    self.service.act(
                        table_id, "cancel",
                        member_id=None if is_mod else member.id,
                    ),
                    "Table cancelled; every escrow returned.",
                )
            elif action == "close":
                state = await self.service.load_state(table_id)
                member = interaction.user
                is_mod = (
                    isinstance(member, discord.Member)
                    and has_mod_or_admin_permissions(member.guild_permissions)
                )
                if state is None or (
                    state.phase is not Phase.SETTLE and not is_mod
                ):
                    await self._reply(interaction, STALE_TABLE)
                    return
                if state.seat_of(member.id) is None and not is_mod:
                    await self._reply(
                        interaction, "Only the players (or a mod) can close a table."
                    )
                    return
                await self._act_simple(
                    interaction,
                    self.service.dissolve_table(table_id, "closed"),
                    "Table closed.",
                )
            elif action == "rack":
                await self._send_rack(interaction, table_id)
            elif action in ("vote_y", "vote_n"):
                await self._act_simple(
                    interaction,
                    self.service.act(table_id, "vote",
                                     member_id=interaction.user.id,
                                     yes=action == "vote_y"),
                    "Vote's in.",
                )
            elif action == "claim_pass":
                await self._act_simple(
                    interaction,
                    self.service.act(table_id, "claim",
                                     member_id=interaction.user.id,
                                     kind="pass"),
                    "Passed.",
                )
            elif action == "claim_mj":
                await self._claim_mahjong(interaction, table_id)
            elif action == "claim_call":
                await self._claim_call_start(interaction, table_id)
            elif action == "rematch":
                await self._act_simple(
                    interaction,
                    self.service.act(table_id, "rematch",
                                     member_id=interaction.user.id),
                    "Rematch vote in — everyone must press it.",
                )
            else:
                await interaction.response.send_message(
                    STALE_TABLE, ephemeral=True
                )
        except (TableError, ActionRejected) as e:
            await self._reply(interaction, self._dress(str(e)))

    async def _act_simple(self, interaction, coro, done: str) -> None:
        await interaction.response.defer(ephemeral=True)
        events = await coro
        note = self._private_note(interaction.user.id, events)
        await interaction.followup.send(note or done, ephemeral=True)

    @staticmethod
    def _dress(message: str) -> str:
        # STALE_TABLE stays bare everywhere it appears: dressing it only on
        # some paths would make the no-contact refusal distinguishable
        return message if message == STALE_TABLE else f"❌ {message}"

    def _private_note(self, member_id: int, events) -> str | None:
        """Turn a private engine event into the ❌ this member alone sees."""
        if not events:
            return None
        for kind, data in events:
            if not data.get("private"):
                continue
            if kind == "claim_downgraded":
                why = data.get("why", "")
                if why == "invalid_mahjong":
                    return ("❌ That 14 doesn't match a line on the card — "
                            "your response became a Pass. (Nobody else saw this.)")
                return ("❌ That call can't stand — your response became a "
                        "Pass. (Nobody else saw this.)")
        return None

    async def _reply(self, interaction: discord.Interaction, text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def _seat_of(
        self, interaction: discord.Interaction, table_id: int
    ) -> tuple[GameState, int] | None:
        state = await self.service.load_state(table_id)
        if state is None:
            await self._reply(interaction, STALE_TABLE)
            return None
        seat = state.seat_of(interaction.user.id)
        if seat is None:
            await self._reply(interaction, "You're not at this table.")
            return None
        return state, seat

    # ── Rack panel (the §6.4 fallback and every private picker) ─────────

    async def _send_rack(
        self, interaction: discord.Interaction, table_id: int
    ) -> None:
        got = await self._seat_of(interaction, table_id)
        if got is None:
            return
        state, seat = got
        guild = interaction.guild
        assert guild is not None
        meta = await self.service.table_meta(table_id)
        accent = await safe_resolve_accent(self.bot, guild, default=DEFAULT_ACCENT_COLOR)
        names = self._names(guild, state)
        embed = mj_embeds.build_rack_panel(
            state, seat, accent, meta["deadline_at"] if meta else None,
            context=self._rack_context(state, seat, names),
        )
        await self._reply_embed(
            interaction, embed, self._rack_view(state, seat, table_id, names)
        )

    def _rack_context(self, state: GameState, seat: int, names: dict[int, str]) -> str | None:
        if state.phase is Phase.CHARLESTON:
            rnd = "First" if state.charleston_round == 1 else "Second"
            return (f"{rnd} Charleston, pass {state.pass_index + 1} of 3 — "
                    "pick your tiles below."
                    if seat not in state.pending_picks
                    else "Your pass is in — waiting on the table.")
        if state.phase is Phase.CHARLESTON_VOTE:
            return "Vote on the table card: run the second Charleston?"
        if state.phase is Phase.COURTESY_PROPOSE:
            return "Offer 0–3 courtesy tiles below."
        if state.phase is Phase.COURTESY_PICK:
            if seat in state.courtesy_owed and seat not in state.courtesy_gives:
                return f"Give {state.courtesy_owed[seat]} tile(s) below."
            return "Courtesy trades are wrapping up."
        if state.phase is Phase.CLAIM_WINDOW:
            return "A discard is live — claim from the table card."
        if state.phase is Phase.AWAIT_DISCARD and state.turn != seat:
            turn_name = names.get(state.seats[state.turn].member_id,
                                  f"seat {state.turn + 1}")
            return f"Waiting on {turn_name}."
        return None

    def _rack_view(
        self, state: GameState, seat: int, table_id: int,
        names: dict[int, str] | None = None,
    ) -> discord.ui.View | None:
        rack = state.seats[seat].rack
        if state.phase is Phase.CHARLESTON and seat not in state.pending_picks:
            to_label = None
            if state.seat_count == 2 and names:
                to_label = names.get(state.seats[1 - seat].member_id)
            return mj_views.CharlestonPickView(
                self, table_id, rack,
                final_pass=state.pass_index == 2,
                to_label=to_label,
            )
        if state.phase is Phase.COURTESY_PROPOSE and seat not in state.proposals:
            return mj_views.CourtesyProposeView(self, table_id)
        if (state.phase is Phase.COURTESY_PICK
                and seat in state.courtesy_owed
                and seat not in state.courtesy_gives):
            return mj_views.CourtesyGiveView(
                self, table_id, rack, state.courtesy_owed[seat]
            )
        if state.phase is Phase.AWAIT_DISCARD and state.turn == seat:
            return mj_views.TurnView(self, table_id, state, seat)
        return None

    async def _reply_embed(self, interaction, embed, view) -> None:
        kwargs = {"embed": embed, "ephemeral": True}
        if view is not None:
            kwargs["view"] = view
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    # ── Phase handlers (called from views) ──────────────────────────────

    async def handle_charleston_pick(
        self, interaction, table_id: int, tiles: list[Tile], blind_n: int
    ) -> None:
        await self._do(interaction, table_id, "charleston_pick",
                       tiles=tiles, blind_n=blind_n,
                       done="Pass is in. 🤫" if blind_n else "Pass is in.")

    async def handle_courtesy_propose(
        self, interaction, table_id: int, n: int
    ) -> None:
        await self._do(interaction, table_id, "courtesy_propose", n=n,
                       done=f"Offered {n}.")

    async def handle_courtesy_give(
        self, interaction, table_id: int, tiles: list[Tile]
    ) -> None:
        await self._do(interaction, table_id, "courtesy_pick", tiles=tiles,
                       done="Tiles handed over.")

    async def handle_discard(
        self, interaction, table_id: int, tile: Tile
    ) -> None:
        await self._do(interaction, table_id, "discard", tile=tile,
                       done="Discarded.")

    async def handle_redeem(
        self, interaction, table_id: int, exposure_id: int, tile: Tile
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            await self.service.act(
                table_id, "redeem_joker", member_id=interaction.user.id,
                exposure_id=exposure_id, tile=tile,
            )
        except (TableError, ActionRejected) as e:
            await self._reply(interaction, self._dress(str(e)))
            return
        # §6.11: the redeemer's refreshed panel — joker in rack, discard next
        got = await self._seat_of(interaction, table_id)
        if got is None:
            return
        state, seat = got
        meta = await self.service.table_meta(table_id)
        accent = await safe_resolve_accent(
            self.bot, interaction.guild, default=DEFAULT_ACCENT_COLOR
        )
        embed = mj_embeds.build_rack_panel(
            state, seat, accent, meta["deadline_at"] if meta else None,
            context="Joker's yours — now discard.",
        )
        await self._reply_embed(
            interaction, embed, self._rack_view(state, seat, table_id)
        )

    async def handle_self_mahjong(self, interaction, table_id: int) -> None:
        await self._do(interaction, table_id, "mahjong", done="🀄 Mahjong!")

    async def _claim_mahjong(self, interaction, table_id: int) -> None:
        await self._do(interaction, table_id, "claim", kind="mahjong",
                       done="🀄 Mahjong!")

    async def _claim_call_start(self, interaction, table_id: int) -> None:
        got = await self._seat_of(interaction, table_id)
        if got is None:
            return
        state, seat = got
        if state.phase is not Phase.CLAIM_WINDOW or state.live_discard is None:
            await self._reply(interaction, STALE_TABLE)
            return
        pool = [
            t for t in state.seats[seat].rack
            if t is state.live_discard or t is Tile.JOKER
        ]
        if len(pool) < 2:
            # nothing pickable — submit the bare call and let the engine
            # downgrade it privately (§2.5: the tap never leaks)
            await self.handle_call_submit(interaction, table_id, pool)
            return
        await interaction.response.send_message(
            "Complete the exposure:",
            view=mj_views.CallTilesView(
                self, table_id, state.seats[seat].rack, state.live_discard
            ),
            ephemeral=True,
        )

    async def handle_call_submit(
        self, interaction, table_id: int, tiles: list[Tile],
        for_discard: Tile | None = None,
    ) -> None:
        if for_discard is not None:
            state = await self.service.load_state(table_id)
            if state is None or state.live_discard is not for_discard:
                # the picker outlived its window — never consume the new one
                await self._reply(interaction, STALE_TABLE)
                return
        await self._do(interaction, table_id, "claim", kind="call",
                       tiles=tiles,
                       done="Call's in — it stands unless a Mahjong outranks it.")

    async def _do(
        self, interaction, table_id: int, action: str, *, done: str, **kwargs
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            events = await self.service.act(
                table_id, action, member_id=interaction.user.id, **kwargs
            )
        except (TableError, ActionRejected) as e:
            await self._reply(interaction, self._dress(str(e)))
            return
        note = self._private_note(interaction.user.id, events)
        await interaction.followup.send(note or done, ephemeral=True)

    # ── Scheduled card activation ───────────────────────────────────────

    @tasks.loop(minutes=1)
    async def card_scheduler(self) -> None:
        def _q():
            with open_db(self.bot.ctx.db_path) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT guild_id FROM mahjong_cards "
                    "WHERE status = 'scheduled'"
                ).fetchall()
                changed = False
                for r in rows:
                    changed = activate_due_cards(conn, int(r["guild_id"])) or changed
                return changed

        try:
            await asyncio.to_thread(_q)
        except Exception:
            log.exception("Mahjong card scheduler tick failed")

    @card_scheduler.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: "Bot") -> None:
    await bot.add_cog(MahjongCog(bot))
