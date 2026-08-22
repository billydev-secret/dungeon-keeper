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
from bot_modules.games.mahjong.game_logic import AssistReadout, GameState, Outcome, Phase
from bot_modules.games.mahjong.tiles import Tile, sort_rack
from bot_modules.games.mahjong.tile_render import back_str, chip, rack_str, tile_str
from bot_modules.core.branding import DEFAULT_ACCENT_COLOR
from bot_modules.services.embeds import COLOR_GREEN

FOOTER = "🀄 Meadow Mahjong"
COLOR_VOID = discord.Color(0x546E7A)  # a voided hand is not a win — no green
MODE_NAMES = {2: "Duel", 4: "Full Table"}

_PASS_NAMES = {
    (1, 0): "Right", (1, 1): "Across", (1, 2): "Left",
    (2, 0): "Left", (2, 1): "Across", (2, 2): "Right",
}


def _footer(embed: discord.Embed, extra: str = "") -> discord.Embed:
    embed.set_footer(text=f"{FOOTER} • {extra}" if extra else FOOTER)
    return embed


def _clock(deadline_at: float | None) -> str:
    """Markdown countdown for a field/description — NEVER a footer:
    Discord renders <t:…:R> only in message/embed body text."""
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


def build_member_panel(
    card: Card | None, stakes: tuple[int, ...], balance: int,
    accent: discord.Color | None = None,
) -> discord.Embed:
    accent = accent or DEFAULT_ACCENT_COLOR
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


def build_table_panel(
    state: GameState,
    names: dict[int, str],
    stake: int,
    escrow: int,
    accent: discord.Color | None = None,
    deadline_at: float | None = None,
    *,
    practice: bool = False,
) -> discord.Embed:
    accent = accent or DEFAULT_ACCENT_COLOR
    mode = MODE_NAMES.get(state.seat_count, str(state.seat_count))
    e = discord.Embed(
        title=(f"Practice Table — {mode}" if practice
               else f"Mahjong Table — {mode}"),
        color=accent,
    )
    clock = _clock(deadline_at)

    if state.phase is Phase.LOBBY:
        rows = [
            f"✅ **{names.get(s.member_id, s.member_id)}**"
            + ("" if practice else " — escrow locked")
            for s in state.seats
        ]
        open_seats = state.seat_count - len(state.seats)
        rows += ["◻ *open seat*"] * open_seats
        e.description = "\n".join(rows)
        e.add_field(
            name="Stake",
            value=("Practice — no stakes, nothing recorded" if practice
                   else f"{stake} coins per point ({escrow} escrow per seat)"),
            inline=False,
        )
        e.add_field(
            name="Status",
            value=(f"Waiting for {open_seats} more{clock}" if open_seats
                   else f"Everyone's in — dealing{clock}"),
            inline=False,
        )
        return _footer(e, "Lobby")

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
            value=f"Tiles in: {ticks}{clock}", inline=False,
        )
        return _footer(e, "Charleston")

    if state.phase is Phase.CHARLESTON_VOTE:
        ticks = " ".join(
            "🗳" if i in state.votes else "…" for i in range(len(state.seats))
        )
        e.add_field(
            name="Second Charleston?",
            value=f"Unanimous yes runs it again. Votes: {ticks}{clock}",
            inline=False,
        )
        return _footer(e, "Vote")

    if state.phase in (Phase.COURTESY_PROPOSE, Phase.COURTESY_PICK):
        if state.phase is Phase.COURTESY_PROPOSE:
            ticks = " ".join(
                "✅" if i in state.proposals else "…"
                for i in range(len(state.seats))
            )
            e.add_field(name="Courtesy Pass",
                        value=f"Offers in: {ticks}{clock}", inline=False)
        else:
            owe = ", ".join(
                f"{seat_names[i]}: {n}" for i, n in state.courtesy_owed.items()
            )
            e.add_field(name="Courtesy Exchange",
                        value=f"Trading tiles — {owe}{clock}", inline=False)
        return _footer(e, "Courtesy")

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
                f"Responses: {ticks}{clock}"
            ),
            inline=False,
        )
        return _footer(e, "Claim window")

    if state.phase is Phase.AWAIT_DISCARD:
        e.add_field(
            name="Turn",
            value=f"{seat_names[state.turn]} to discard{clock}", inline=True,
        )
        return _footer(e, f"Hand {state.hand_no}")

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
            survivor = seat_names.get(out.winner, "—") if out.winner is not None else "—"
            e.add_field(
                name="Hand Over",
                value=f"{survivor} stands alone; the folded seats pay out.",
                inline=False,
            )
        else:
            e.add_field(name="Hand Over",
                        value="Nobody went out — escrow returns.", inline=False)
        ready = " ".join(
            "🔁" if i in state.rematch_votes else "…"
            for i in range(len(state.seats))
        )
        e.add_field(name="Rematch?",
                    value=f"Everyone must press it: {ready}{clock}",
                    inline=False)
        return _footer(e, "Settled")

    return _footer(e, "Closed")


def _assist_field(assist: AssistReadout) -> str:
    """The Closest Hands block (plans/mahjong-assist.md stage 3). One
    renderer for every panel that shows a rack, so the modes can't drift.

    Two rules from the stage-4 review round: "Dead weight" is the
    intersection across the hands actually shown — the copy promises
    "tiles none of your closest hands can use", so a tile another shown
    hand still needs must never print as dead. And the block guarantees
    Discord's 1024-char field bound itself, degrading emoji to text chips
    (then trimming hands) rather than blind-slicing — a cut used to land
    mid-<:mm_…:id> token and silently drop the coach suggestion, the one
    line coach mode exists to deliver.
    """
    if not assist.prospects:
        return ("No line on the card is still reachable from your tiles — "
                "play for the wall.")

    def tiles_str(pairs, render) -> str:
        return " ".join(
            render(tile) if n == 1 else f"{render(tile)}×{n}"
            for tile, n in pairs
        )

    def render_block(shown_n: int, render) -> str:
        shown = assist.prospects[:shown_n]
        lines: list[str] = []
        for rank, p in enumerate(shown, start=1):
            away = "ready!" if p.distance == 0 else (
                "1 tile away" if p.distance == 1 else f"{p.distance} tiles away")
            lines.append(
                f"**{rank}. {p.hand.name}** — {away} · {p.hand.value} pts"
            )
            if assist.mode in ("gap", "coach") and p.needed:
                lines.append(f"    need {tiles_str(p.needed, render)}")
        if assist.live_count > len(shown):
            lines.append(f"*…of {assist.live_count} lines still live*")
        if assist.mode == "coach":
            # dead for EVERY shown hand; prospects[0]'s order is kept
            common = list(shown[0].dead_weight)
            for p in shown[1:]:
                dw = dict(p.dead_weight)
                common = [
                    (tile, min(n, dw[tile])) for tile, n in common if tile in dw
                ]
            if common:
                lines.append(f"Dead weight: {tiles_str(common, render)}")
            if assist.suggestion is not None:
                lines.append(f"Consider discarding {render(assist.suggestion)}")
            elif assist.prospects[0].dead_weight:
                lines.append("*No clearly safe discard — your call.*")
        return "\n".join(lines)

    attempts = [(len(assist.prospects), tile_str), (len(assist.prospects), chip),
                (2, chip), (1, chip)]
    for shown_n, render in attempts:
        text = render_block(shown_n, render)
        if len(text) <= 1024:
            return text
    # unreachable — one chip-rendered hand is far under 1024 — but if a
    # future card breaks the assumption, a clean tail beats a cut token.
    return render_block(1, chip)[:1024]


def build_rack_panel(
    state: GameState, seat: int, accent: discord.Color | None = None,
    deadline_at: float | None = None, *, context: str | None = None,
    assist: AssistReadout | None = None,
) -> discord.Embed:
    accent = accent or DEFAULT_ACCENT_COLOR
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
    turn_note = context or (
        "**It's your turn.**" if state.turn == seat
        and state.phase is Phase.AWAIT_DISCARD
        else ""
    )
    if turn_note:
        e.add_field(name="Now", value=turn_note + _clock(deadline_at),
                    inline=False)
    if assist is not None:
        # _assist_field guarantees the 1024 bound itself — no slicing here.
        e.add_field(name="Closest Hands", value=_assist_field(assist),
                    inline=False)
    return _footer(e, f"Hand {state.hand_no}")


def build_mahjong_reveal(
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


def build_settlement(
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
            color=COLOR_VOID,
        )
        return _footer(e)

    width = max((len(n) for n in names.values()), default=4)
    rows = []
    for seat in sorted(out.point_deltas):
        name = names.get(seat, f"Seat {seat + 1}")
        delta = out.point_deltas[seat] * stake
        rows.append(f"{name:<{width}}  {delta:+7d} coins")
    note = []
    duel = len(out.point_deltas) == 2
    if out.won_by == "discard" and out.discarder is not None:
        note.append(
            f"discard win 2× — {names.get(out.discarder, '?')} fed the tile"
            if duel else
            f"discard win — {names.get(out.discarder, '?')} pays 2×, others 1×"
        )
    if out.won_by == "self_pick":
        note.append("self-pick 3×" if duel else "self-pick — everyone pays 2×")
    if out.jokerless_double:
        note.append("jokerless: doubled")
    e = discord.Embed(
        title="Settlement",
        description="```\n" + "\n".join(rows) + "\n```"
        + (" · ".join(note) if note else ""),
        color=COLOR_GREEN,
    )
    return _footer(e, f"{out.value} points × {stake}/point")


def build_joker_redeemed(
    redeemer: str, owner: str, tile: Tile,
    accent: discord.Color | None = None,
) -> discord.Embed:
    e = discord.Embed(
        title="Joker Redeemed",
        description=(
            f"**{redeemer}** swapped a natural {tile_str(tile)} for the joker "
            f"in **{owner}**'s exposure — it stands natural now."
        ),
        color=accent or DEFAULT_ACCENT_COLOR,
    )
    return _footer(e)


def build_card_viewer(card: Card, accent: discord.Color | None = None) -> list[discord.Embed]:
    accent = accent or DEFAULT_ACCENT_COLOR
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
        if len(e.fields) >= 5:
            embeds.append(_footer(e))
            e = discord.Embed(color=accent)
        e.add_field(name=section, value=value[:1024], inline=False)
    embeds.append(_footer(e, card.season))
    return embeds


def build_my_stats(rows: list[dict], accent: discord.Color | None = None) -> discord.Embed:
    accent = accent or DEFAULT_ACCENT_COLOR
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
