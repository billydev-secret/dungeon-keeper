"""Meadow Mahjong embeds — presentation only, no rules (stage 6, spec §6).

Every builder takes pre-resolved inputs (state, names, accent color) and
returns an embed; the cog owns Discord lookups and the service owns truth.
House style: accent from resolve_accent_color, semantic green/red reserved
for results, Title Case labels, the 🀄 footer signature, monospace payout
tables. Tiles render through tile_render only — racks can never blank.
"""

from __future__ import annotations

import time

import discord

from bot_modules.games.mahjong.card_logic import Card
from bot_modules.games.mahjong.game_logic import GameState, Outcome, Phase
from bot_modules.games.mahjong.tiles import Tile, sort_rack
from bot_modules.games.mahjong.tile_render import back_str, rack_str, tile_str
from bot_modules.services.embeds import COLOR_GREEN

FOOTER = "🀄 Meadow Mahjong"
MODE_NAMES = {2: "Duel", 4: "Full Table"}

_PASS_NAMES = {
    (1, 0): "Right", (1, 1): "Across", (1, 2): "Left",
    (2, 0): "Left", (2, 1): "Across", (2, 2): "Right",
}


def _footer(embed: discord.Embed, extra: str = "") -> discord.Embed:
    embed.set_footer(text=f"{FOOTER} • {extra}" if extra else FOOTER)
    return embed


def _clock(deadline_at: float | None) -> str:
    if not deadline_at or deadline_at <= time.time():
        return ""
    return f" — ⏱ <t:{int(deadline_at)}:R>"


def _seat_line(
    state: GameState, seat: int, name: str, *, turn_arrow: bool
) -> str:
    s = state.seats[seat]
    bits = []
    if turn_arrow and state.turn == seat and state.phase in (
        Phase.AWAIT_DISCARD, Phase.CLAIM_WINDOW
    ):
        bits.append("▶")
    bits.append(f"**{name}**")
    if s.fallow:
        bits.append("💤 folded")
    elif s.strikes:
        bits.append("⚠" * s.strikes)
    line = " ".join(bits)
    if s.exposures:
        shown = "   ".join(
            " ".join([tile_str(e.natural)] * (e.count - e.jokers)
                     + [f"{tile_str(Tile.JOKER)}"] * e.jokers)
            for e in s.exposures
        )
        line += f"\n{shown}"
    return line


def member_panel(
    card: Card | None, stakes: tuple[int, ...], balance: int,
    accent: discord.Color,
) -> discord.Embed:
    e = discord.Embed(title="Meadow Mahjong", color=accent)
    if card is None:
        e.description = "No Meadow Card is active right now — check back soon."
    else:
        e.description = (
            f"Card in season: **{card.display_name}** "
            f"({len(card.hands)} hands)\n"
            f"Stakes: {', '.join(str(s) for s in stakes)} coins per point\n"
            f"Your balance: **{balance}** coins"
        )
    return _footer(e, "American-style, card-driven")


def table_panel(
    state: GameState,
    names: dict[int, str],
    stake: int,
    escrow: int,
    accent: discord.Color,
    deadline_at: float | None,
) -> discord.Embed:
    mode = MODE_NAMES.get(state.seat_count, str(state.seat_count))
    e = discord.Embed(
        title=f"Mahjong Table — {mode}",
        color=accent,
    )
    clock = _clock(deadline_at)

    if state.phase is Phase.LOBBY:
        rows = [
            f"✅ **{names.get(s.member_id, s.member_id)}** — escrow locked"
            for s in state.seats
        ]
        open_seats = state.seat_count - len(state.seats)
        rows += ["◻ *open seat*"] * open_seats
        e.description = "\n".join(rows)
        e.add_field(
            name="Stake",
            value=f"{stake} coins per point ({escrow} escrow per seat)",
            inline=False,
        )
        if open_seats:
            return _footer(e, f"Waiting for {open_seats} more{clock}")
        return _footer(e, f"Everyone's in — dealing{clock}")

    seat_names = {i: names.get(s.member_id, str(s.member_id))
                  for i, s in enumerate(state.seats)}
    lines = [
        _seat_line(state, i, seat_names[i], turn_arrow=True)
        for i in range(len(state.seats))
    ]
    e.description = "\n".join(lines)

    if state.phase is Phase.CHARLESTON:
        rnd = "First" if state.charleston_round == 1 else "Second"
        direction = _PASS_NAMES[(state.charleston_round, state.pass_index)]
        if state.seat_count == 2:
            direction = "to your opponent"
        ticks = " ".join(
            "✅" if i in state.pending_picks else "…"
            for i in range(len(state.seats))
        )
        e.add_field(
            name=f"{rnd} Charleston — Pass {state.pass_index + 1} ({direction})",
            value=f"Tiles in: {ticks}", inline=False,
        )
        return _footer(e, f"Charleston{clock}")

    if state.phase is Phase.CHARLESTON_VOTE:
        ticks = " ".join(
            "🗳" if i in state.votes else "…" for i in range(len(state.seats))
        )
        e.add_field(
            name="Second Charleston?",
            value=f"Unanimous yes runs it again. Votes: {ticks}",
            inline=False,
        )
        return _footer(e, f"Vote{clock}")

    if state.phase in (Phase.COURTESY_PROPOSE, Phase.COURTESY_PICK):
        if state.phase is Phase.COURTESY_PROPOSE:
            ticks = " ".join(
                "✅" if i in state.proposals else "…"
                for i in range(len(state.seats))
            )
            e.add_field(name="Courtesy Pass",
                        value=f"Offers in: {ticks}", inline=False)
        else:
            owe = ", ".join(
                f"{seat_names[i]}: {n}" for i, n in state.courtesy_owed.items()
            )
            e.add_field(name="Courtesy Exchange",
                        value=f"Trading tiles — {owe}", inline=False)
        return _footer(e, f"Courtesy{clock}")

    # play phases share the pit/wall furniture
    pit = ""
    if state.discards:
        older = " ".join(tile_str(t) for _, t in state.discards[:-1][-16:])
        latest_seat, latest = state.discards[-1]
        pit = (older + "   " if older else "") + f"__{tile_str(latest)}__"
    e.add_field(
        name="Discards",
        value=pit or "*none yet*",
        inline=False,
    )
    e.add_field(name="Wall", value=f"{back_str()} × {len(state.wall)}", inline=True)

    if state.phase is Phase.CLAIM_WINDOW:
        assert state.live_discard is not None and state.live_discarder is not None
        responders = [s for s in state.live_seats() if s != state.live_discarder]
        ticks = " ".join(
            "✅" if s in state.claims else "…" for s in responders
        )
        e.add_field(
            name="Claim Window",
            value=(
                f"{seat_names[state.live_discarder]} threw "
                f"{tile_str(state.live_discard)} — 🀄 Mahjong · ✋ Call · Pass\n"
                f"Responses: {ticks}"
            ),
            inline=False,
        )
        return _footer(e, f"Claim window{clock}")

    if state.phase is Phase.AWAIT_DISCARD:
        e.add_field(
            name="Turn", value=f"{seat_names[state.turn]} to discard", inline=True
        )
        return _footer(e, f"Hand {state.hand_no}{clock}")

    if state.phase is Phase.SETTLE:
        out = state.outcome
        if out is not None and out.kind == "mahjong" and out.winner is not None:
            e.add_field(
                name="Hand Over",
                value=f"🎉 {seat_names[out.winner]} went Mahjong — "
                      f"**{out.line_name}**",
                inline=False,
            )
        elif out is not None and out.kind == "fallow_end":
            e.add_field(
                name="Hand Over",
                value=f"{seat_names.get(out.winner or -1, '—')} stands alone; "
                      "the folded seats pay out.",
                inline=False,
            )
        else:
            e.add_field(name="Hand Over",
                        value="Nobody went out — escrow returns.", inline=False)
        ready = " ".join(
            "🔁" if i in state.rematch_votes else "…"
            for i in range(len(state.seats))
        )
        e.add_field(name="Rematch?", value=f"Everyone must press it: {ready}",
                    inline=False)
        return _footer(e, f"Settled{clock}")

    return _footer(e, "Closed")


def rack_panel(
    state: GameState, seat: int, accent: discord.Color,
    deadline_at: float | None,
) -> discord.Embed:
    s = state.seats[seat]
    rack = list(s.rack)
    drawn = state.drawn if (state.turn == seat and state.drawn in rack) else None
    if drawn is not None:
        rack.remove(drawn)
    shown = rack_str(sort_rack(rack))
    if drawn is not None:
        shown += f"   ➜ **{tile_str(drawn)}**"
    e = discord.Embed(title="Your Rack", description=shown or "*empty*",
                      color=accent)
    if s.exposures:
        e.add_field(
            name="Your Exposures",
            value="   ".join(
                " ".join([tile_str(x.natural)] * (x.count - x.jokers)
                         + [tile_str(Tile.JOKER)] * x.jokers)
                for x in s.exposures
            ),
            inline=False,
        )
    turn_note = (
        "**It's your turn.**" if state.turn == seat
        and state.phase is Phase.AWAIT_DISCARD
        else f"Waiting on seat {state.turn + 1}."
        if state.phase is Phase.AWAIT_DISCARD else ""
    )
    if turn_note:
        e.add_field(name="Now", value=turn_note + _clock(deadline_at),
                    inline=False)
    return _footer(e, f"Hand {state.hand_no}")


def mahjong_reveal(
    out: Outcome, winner_name: str, winning_tiles: str,
) -> discord.Embed:
    how = {
        "discard": "claimed from the discard pit",
        "self_pick": "self-picked",
    }.get(out.won_by or "", "won")
    e = discord.Embed(
        title=f"🀄 Mahjong — {out.line_name}",
        description=(
            f"**{winner_name}** {how} for **{out.value}** points"
            + (" — **jokerless, doubled!**" if out.jokerless_double else "")
            + f"\n\n{winning_tiles}"
        ),
        color=COLOR_GREEN,
    )
    return _footer(e, out.line_id or "")


def settlement(
    out: Outcome, names: dict[int, str], stake: int,
) -> discord.Embed:
    if out.kind == "wall_game":
        e = discord.Embed(
            title="Wall Game",
            description="The wall ran dry with nobody out — every escrow "
                        "returns untouched.",
            color=COLOR_GREEN,
        )
        return _footer(e, "Rematch?")
    if out.kind == "all_fallow":
        e = discord.Embed(
            title="Table Timed Out",
            description="Every seat folded together — the hand voids and "
                        "escrow returns.",
            color=COLOR_GREEN,
        )
        return _footer(e)

    width = max((len(n) for n in names.values()), default=4)
    rows = []
    for seat in sorted(out.point_deltas):
        name = names.get(seat, f"Seat {seat + 1}")
        delta = out.point_deltas[seat] * stake
        rows.append(f"{name:<{width}}  {delta:+7d} coins")
    note = []
    if out.won_by == "discard" and out.discarder is not None:
        note.append(f"{names.get(out.discarder, '?')} fed the winning tile (2×)")
    if out.won_by == "self_pick":
        note.append("self-pick")
    if out.jokerless_double:
        note.append("jokerless double")
    e = discord.Embed(
        title="Settlement",
        description="```\n" + "\n".join(rows) + "\n```"
        + (" · ".join(note) if note else ""),
        color=COLOR_GREEN,
    )
    return _footer(e, f"{out.value} points × {stake}/point")


def joker_redeemed(redeemer: str, owner: str, tile: Tile) -> discord.Embed:
    e = discord.Embed(
        title="Joker Redeemed",
        description=(
            f"**{redeemer}** swapped a natural {tile_str(tile)} for the joker "
            f"in **{owner}**'s exposure — it stands natural now."
        ),
        color=discord.Color.purple(),
    )
    return _footer(e)


def card_viewer(card: Card, accent: discord.Color) -> list[discord.Embed]:
    """The active card by section, for study — split to respect field caps."""
    embeds: list[discord.Embed] = []
    e = discord.Embed(
        title=card.display_name,
        description=f"Season {card.season} — {len(card.hands)} hands. "
        "`C` hands must stay concealed (claim only for Mahjong).",
        color=accent,
    )
    for section in card.sections():
        hands = [h for h in card.hands if h.section == section]
        value = "\n".join(
            f"`{h.display}` — **{h.name}** · {h.value}"
            + (" · C" if h.concealed else "")
            for h in hands
        )
        if len(e.fields) >= 6:
            embeds.append(_footer(e))
            e = discord.Embed(color=accent)
        e.add_field(name=section, value=value[:1024], inline=False)
    embeds.append(_footer(e, "First Light"))
    return embeds


def my_stats(rows: list[dict], accent: discord.Color) -> discord.Embed:
    e = discord.Embed(title="Your Mahjong Record", color=accent)
    if not rows:
        e.description = "No hands on record yet — pull up a chair."
        return _footer(e)
    for row in rows:
        mode = MODE_NAMES.get(row["mode"], str(row["mode"]))
        e.add_field(
            name=mode,
            value=(
                f"{row['wins']}/{row['hands_played']} hands won"
                f" · {row['jokerless_wins']} jokerless\n"
                f"+{row['coins_won']} / -{row['coins_lost']} coins"
                f" · best win {row['biggest_win']}"
            ),
            inline=True,
        )
    return _footer(e)
