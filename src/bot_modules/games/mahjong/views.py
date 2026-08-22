"""Meadow Mahjong views — buttons and selects, no rules (stage 6, spec §6).

The public table message carries phase-appropriate buttons (custom_id
``mmj:{table_id}:{action}``, stable across restarts); every member-private
surface hangs off the ephemeral rack panel, which the table's Open Rack
button can always re-summon — that is the §6.4 "Refresh Rack" fallback for
expired interactions. All callbacks route to cog handlers; the cog routes
to the service; the service routes to the engine. Nothing here decides
anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot_modules.games.mahjong.game_logic import GameState, Phase
from bot_modules.games.mahjong.tile_render import chip
from bot_modules.games.mahjong.tiles import Tile, sort_rack

if TYPE_CHECKING:
    from bot_modules.cogs.mahjong_cog import MahjongCog


def _cid(table_id: int, action: str) -> str:
    return f"mmj:{table_id}:{action}"


class TableView(discord.ui.View):
    """The persistent view on the sticky table message, built per phase."""

    ACTIONS = ("join", "cancel", "rack", "vote_y", "vote_n", "claim_mj",
               "claim_call", "claim_pass", "rematch", "close")

    def __init__(self, cog: "MahjongCog", table_id: int, phase: Phase | None = None,
                 *, register_all: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.table_id = table_id

        def button(label, action, style=discord.ButtonStyle.secondary, emoji=None):
            b = discord.ui.Button(
                label=label, style=style, emoji=emoji,
                custom_id=_cid(table_id, action),
            )
            b.callback = self._make_callback(action)
            self.add_item(b)

        if register_all:
            # resume-time registration only (never posted): the sticky message
            # on disk may carry any phase's buttons until its next repost, and
            # an unregistered custom_id dies as "interaction failed" instead of
            # routing to the stale-table copy.
            for action in self.ACTIONS:
                button(action, action)
            return
        if phase is Phase.LOBBY:
            button("Join", "join", discord.ButtonStyle.primary)
            button("Cancel", "cancel", discord.ButtonStyle.danger)
        elif phase is Phase.CHARLESTON_VOTE:
            button("Run It Again", "vote_y", discord.ButtonStyle.success)
            button("No Thanks", "vote_n", discord.ButtonStyle.secondary)
            button("Open Rack", "rack")
        elif phase is Phase.CLAIM_WINDOW:
            button("Mahjong", "claim_mj", discord.ButtonStyle.success, emoji="🀄")
            button("Call", "claim_call", discord.ButtonStyle.primary, emoji="✋")
            button("Pass", "claim_pass")
            button("Open Rack", "rack")
        elif phase is Phase.SETTLE:
            button("Rematch", "rematch", discord.ButtonStyle.primary, emoji="🔁")
            button("Close Table", "close", discord.ButtonStyle.danger)
        elif phase is Phase.CLOSED:
            pass
        else:  # charleston / courtesy / await_discard: self-serve via rack
            button("Open Rack", "rack", discord.ButtonStyle.primary)

    def _make_callback(self, action: str):
        async def cb(interaction: discord.Interaction):
            await self.cog.handle_table_button(interaction, self.table_id, action)
        return cb


def _tile_options(tiles: list[Tile]) -> list[discord.SelectOption]:
    """One option per rack slot; duplicate kinds get indexed values so a
    member can pick two of the same tile."""
    counts: dict[str, int] = {}
    options = []
    for t in sort_rack(tiles):
        n = counts.get(t.code, 0)
        counts[t.code] = n + 1
        options.append(discord.SelectOption(
            label=f"{chip(t)} — {t.label}", value=f"{t.code}#{n}",
        ))
    return options[:25]


def parse_tile_values(values: list[str]) -> list[Tile]:
    return [Tile(v.split("#")[0]) for v in values]


class CharlestonPickView(discord.ui.View):
    """Ephemeral pass picker: pick 3 (jokers pre-filtered), or blind some
    slots on the final pass of each Charleston."""

    def __init__(self, cog: "MahjongCog", table_id: int, seat_rack: list[Tile],
                 *, final_pass: bool, blind_n: int = 0, to_label: str | None = None):
        super().__init__(timeout=600)
        self.cog = cog
        self.table_id = table_id
        pool = [t for t in seat_rack if t is not Tile.JOKER]
        picks = 3 - blind_n
        if picks > 0:
            select = discord.ui.Select(
                placeholder=f"Pick {picks} tile{'s' if picks != 1 else ''}"
                + (f" for {to_label}" if to_label else " to pass")
                + (f" ({blind_n} blind)" if blind_n else ""),
                min_values=picks, max_values=picks,
                options=_tile_options(pool),
            )

            async def on_pick(interaction: discord.Interaction):
                await cog.handle_charleston_pick(
                    interaction, table_id, parse_tile_values(select.values), blind_n
                )
            select.callback = on_pick
            self.add_item(select)
        if final_pass:
            for n in range(1 if picks > 0 else 3, 4):
                if n == blind_n:
                    continue
                b = discord.ui.Button(
                    label=f"Blind ×{n}", style=discord.ButtonStyle.secondary
                )

                async def on_blind(interaction: discord.Interaction, n=n):
                    if n == 3:
                        await cog.handle_charleston_pick(interaction, table_id, [], 3)
                    else:
                        await interaction.response.edit_message(
                            view=CharlestonPickView(
                                cog, table_id, seat_rack,
                                final_pass=True, blind_n=n, to_label=to_label,
                            )
                        )
                b.callback = on_blind
                self.add_item(b)


class CourtesyProposeView(discord.ui.View):
    def __init__(self, cog: "MahjongCog", table_id: int):
        super().__init__(timeout=600)
        for n in range(4):
            b = discord.ui.Button(
                label=f"Offer {n}", style=discord.ButtonStyle.secondary
            )

            async def cb(interaction: discord.Interaction, n=n):
                await cog.handle_courtesy_propose(interaction, table_id, n)
            b.callback = cb
            self.add_item(b)


class CourtesyGiveView(discord.ui.View):
    def __init__(self, cog: "MahjongCog", table_id: int,
                 seat_rack: list[Tile], owed: int):
        super().__init__(timeout=600)
        pool = [t for t in seat_rack if t is not Tile.JOKER]
        select = discord.ui.Select(
            placeholder=f"Give {owed} tile{'s' if owed != 1 else ''}",
            min_values=owed, max_values=owed, options=_tile_options(pool),
        )

        async def cb(interaction: discord.Interaction):
            await cog.handle_courtesy_give(
                interaction, table_id, parse_tile_values(select.values)
            )
        select.callback = cb
        self.add_item(select)


class TurnView(discord.ui.View):
    """Your-turn panel: discard select, Redeem Joker when legal, Mahjong."""

    def __init__(self, cog: "MahjongCog", table_id: int, state: GameState,
                 seat: int):
        super().__init__(timeout=600)
        rack = state.seats[seat].rack
        select = discord.ui.Select(
            placeholder="Discard a tile…", min_values=1, max_values=1,
            options=_tile_options(rack),
        )

        async def on_discard(interaction: discord.Interaction):
            await cog.handle_discard(
                interaction, table_id, parse_tile_values(select.values)[0]
            )
        select.callback = on_discard
        self.add_item(select)

        redeemable = _redeemable(state, seat)
        redeem = discord.ui.Button(
            label="Redeem Joker", style=discord.ButtonStyle.secondary,
            emoji="🃏", disabled=not redeemable,
        )

        async def on_redeem(interaction: discord.Interaction):
            await interaction.response.edit_message(
                view=RedeemView(cog, table_id, state, seat)
            )
        redeem.callback = on_redeem
        self.add_item(redeem)

        mahjong = discord.ui.Button(
            label="Mahjong", style=discord.ButtonStyle.success, emoji="🀄"
        )

        async def on_mahjong(interaction: discord.Interaction):
            await cog.handle_self_mahjong(interaction, table_id)
        mahjong.callback = on_mahjong
        self.add_item(mahjong)


def _redeemable(state: GameState, seat: int) -> list[tuple[int, Tile]]:
    """(exposure_id, natural) pairs this seat could redeem right now."""
    rack = set(state.seats[seat].rack)
    out = []
    for s in state.seats:
        for e in s.exposures:
            if e.jokers > 0 and e.natural in rack:
                out.append((e.exposure_id, e.natural))
    return out


class RedeemView(discord.ui.View):
    def __init__(self, cog: "MahjongCog", table_id: int, state: GameState,
                 seat: int):
        super().__init__(timeout=600)
        options = [
            discord.SelectOption(
                label=f"Swap your {chip(natural)} into exposure #{exposure_id}",
                value=f"{exposure_id}:{natural.code}",
            )
            for exposure_id, natural in _redeemable(state, seat)[:25]
        ]
        select = discord.ui.Select(
            placeholder="Redeem which joker?", options=options,
        )

        async def cb(interaction: discord.Interaction):
            exposure_id, code = select.values[0].split(":")
            await cog.handle_redeem(
                interaction, table_id, int(exposure_id), Tile(code)
            )
        select.callback = cb
        self.add_item(select)


class CallTilesView(discord.ui.View):
    """After ✋ Call: pick the 2–5 rack tiles completing the exposure."""

    def __init__(self, cog: "MahjongCog", table_id: int,
                 seat_rack: list[Tile], discard_tile: Tile):
        super().__init__(timeout=120)
        pool = [t for t in seat_rack if t is discard_tile or t is Tile.JOKER]
        # the cog never builds this with fewer than 2 matching tiles (it
        # routes such taps straight to the engine's private downgrade), so
        # min<=max<=len(options) always holds
        select = discord.ui.Select(
            placeholder=f"Expose with your {chip(discard_tile)}s/jokers…",
            min_values=2, max_values=min(5, len(pool)),
            options=_tile_options(pool),
        )

        async def cb(interaction: discord.Interaction):
            await cog.handle_call_submit(
                interaction, table_id, parse_tile_values(select.values),
                for_discard=discard_tile,
            )
        select.callback = cb
        self.add_item(select)


class MemberPanelView(discord.ui.View):
    def __init__(self, cog: "MahjongCog"):
        super().__init__(timeout=600)
        for label, style, handler in (
            ("Create Table", discord.ButtonStyle.primary, cog.handle_create_start),
            ("Card Viewer", discord.ButtonStyle.secondary, cog.handle_card_viewer),
            ("My Stats", discord.ButtonStyle.secondary, cog.handle_my_stats),
            ("My Settings", discord.ButtonStyle.secondary, cog.handle_my_settings),
        ):
            b = discord.ui.Button(label=label, style=style)

            async def cb(interaction: discord.Interaction, handler=handler):
                await handler(interaction)
            b.callback = cb
            self.add_item(b)


ASSIST_CHOICES = (
    ("off", "Off", "Pure card-reading — no readout."),
    ("target", "Target", "Closest hands and how far away each is."),
    ("gap", "Target + gap", "…plus the tiles you still need."),
    ("coach", "Coach", "…plus dead weight and a suggested discard."),
)


class MySettingsView(discord.ui.View):
    """The /mahjong My Settings menu (plans/mahjong-assist.md A10) — an
    ephemeral container for per-player preferences; assistance is its first
    tenant. One select today; future settings join this view, not the panel."""

    def __init__(self, cog: "MahjongCog", current: str):
        super().__init__(timeout=600)
        select = discord.ui.Select(
            placeholder="Assistance level",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label=label, value=value, description=desc,
                    default=value == current,
                )
                for value, label, desc in ASSIST_CHOICES
            ],
        )

        async def on_pick(interaction: discord.Interaction):
            await cog.handle_assist_pick(interaction, select.values[0])
        select.callback = on_pick
        self.add_item(select)


class CreateTableView(discord.ui.View):
    """Size picker → stake select with escrow labels → open."""

    def __init__(self, cog: "MahjongCog", stakes: tuple[int, ...],
                 escrow_for, seat_count: int | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        if seat_count is None:
            for label, count in (("Duel (2)", 2), ("Full Table (4)", 4)):
                b = discord.ui.Button(label=label,
                                      style=discord.ButtonStyle.primary)

                async def cb(interaction: discord.Interaction, count=count):
                    await interaction.response.edit_message(
                        content="Pick the stake — escrow locks when you sit:",
                        view=CreateTableView(cog, stakes, escrow_for, count),
                    )
                b.callback = cb
                self.add_item(b)
            return
        options = [
            discord.SelectOption(
                label=f"{s} coin{'s' if s != 1 else ''} per point",
                description=f"{escrow_for(seat_count, s)} coins escrow per seat",
                value=str(s),
            )
            for s in stakes[:25]
        ]
        select = discord.ui.Select(placeholder="Stake…", options=options)

        async def on_stake(interaction: discord.Interaction):
            stake = int(select.values[0])
            await interaction.response.edit_message(
                content=(
                    f"{'Duel' if seat_count == 2 else 'Full Table'} at "
                    f"{stake}/point — {escrow_for(seat_count, stake)} coins "
                    "escrow locks when you open it."
                ),
                view=OpenTableView(cog, seat_count, stake),
            )
        select.callback = on_stake
        self.add_item(select)


class OpenTableView(discord.ui.View):
    def __init__(self, cog: "MahjongCog", seat_count: int, stake: int):
        super().__init__(timeout=300)
        b = discord.ui.Button(label="Open Table",
                              style=discord.ButtonStyle.success)

        async def cb(interaction: discord.Interaction):
            await cog.handle_create_open(interaction, seat_count, stake)
        b.callback = cb
        self.add_item(b)
