"""Survivor embeds — pure builders, accent-colored per the house contract.

Every builder takes ``color`` and falls back to the branding default, so it
rides the shared accent contract (tests/test_embed_accent_contract.py — one
``case()`` row each, never per-file accent copies). Copy voice: the meadow —
lowercase warmth, wry, never sneering at the member.
"""

from __future__ import annotations

import discord

from bot_modules.services.branding_service import DEFAULT_ACCENT
from bot_modules.survivor.logic import OpenGame

# Strike hearts (§1.4): the display for "used / allowed".
def strike_hearts(used: int, allowed: int) -> str:
    if allowed <= 0:
        return "☠️ sudden death"
    return "🖤" * min(used, allowed) + "💛" * max(allowed - used, 0)


def _wealth(count: int, low: bool) -> str:
    return f"{'🟡' if low else '🟢'} {count} teams left"


def _rel(ts: float | None) -> str:
    return f"<t:{int(ts)}:R>" if ts else "—"


def build_status_embed(
    st: dict, *, season_name: str, color: discord.Color | None = None
) -> discord.Embed:
    """The ephemeral /survivor status card (§2.4)."""
    embed = discord.Embed(
        title="🏈 Your Season",
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    dead = st["status"] == "ghost"
    state = (
        f"👻 ghost since week {st['eliminated_week']}" if dead
        else "🌾 alive"
    )
    embed.add_field(name="Standing", value=state, inline=True)
    embed.add_field(
        name="Strikes",
        value=strike_hearts(st["strikes_used"], st["strikes_allowed"]),
        inline=True,
    )
    embed.add_field(
        name="Satchel",
        value=_wealth(st["satchel_count"], st["satchel_low"]),
        inline=True,
    )
    if st["week"] is None:
        pick_line = "the season is settling — no games left to pick."
    elif st["pick"] is None:
        pick_line = f"week {st['week']}: **no pick yet** — `/survivor pick`"
    else:
        lock = (
            "🔒 locked" if st["pick_locked"]
            else f"locks {_rel(st['pick_kickoff_ts'])}"
        )
        tag = " 📎" if st["pick"]["auto_assigned"] else ""
        pick_line = f"week {st['week']}: **{st['pick']['team']}**{tag} · {lock}"
    embed.add_field(name="Pick", value=pick_line, inline=False)
    embed.set_footer(text=season_name)
    return embed


def build_pick_confirm_embed(
    game: OpenGame,
    st: dict,
    *,
    changed: bool,
    color: discord.Color | None = None,
) -> discord.Embed:
    """The ephemeral confirmation after a pick lands (§2.4)."""
    vs = f"{'vs' if game.is_home else 'at'} {game.opponent}"
    embed = discord.Embed(
        title=f"🏈 {'Pick changed' if changed else 'Pick made'}: {game.team}",
        description=(
            f"**{game.team}** {vs} · locks <t:{int(game.kickoff_ts)}:R>\n"
            "change it freely until kickoff. the bot tells no one. 🤫"
        ),
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    embed.add_field(
        name="Satchel",
        value=_wealth(st["satchel_count"], st["satchel_low"]),
        inline=True,
    )
    embed.add_field(
        name="Strikes",
        value=strike_hearts(st["strikes_used"], st["strikes_allowed"]),
        inline=True,
    )
    return embed


def build_board_embed(
    board: dict,
    name_of,
    *,
    season_name: str,
    strikes_allowed: int = 1,
    color: discord.Color | None = None,
) -> discord.Embed:
    """The public board (§2.6). ``name_of(user_id) -> str`` is injected so
    the builder stays pure of Discord lookups."""
    embed = discord.Embed(
        title=f"🏈 {season_name} — The Board",
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    week = board["week"]
    pots = board["pots"]
    embed.description = (
        f"week {week if week is not None else '—'} · "
        f"pot **{pots['main']:,}** · ghost pot **{pots['ghost']:,}**"
    )
    if board["alive"]:
        lines = [
            f"{name_of(p['user_id'])} · {p['weeks_survived']} wk · "
            f"{strike_hearts(p['strikes_used'], strikes_allowed)}"
            for p in board["alive"][:25]
        ]
        extra = len(board["alive"]) - 25
        if extra > 0:
            lines.append(f"…and {extra} more souls")
        embed.add_field(
            name=f"🌾 Alive ({len(board['alive'])})",
            value="\n".join(lines),
            inline=False,
        )
    if board["graveyard"]:
        lines = [
            f"{name_of(p['user_id'])} · wk {p['eliminated_week'] or '?'}"
            for p in board["graveyard"][:15]
        ]
        extra = len(board["graveyard"]) - 15
        if extra > 0:
            lines.append(f"…and {extra} more ghosts")
        embed.add_field(
            name=f"👻 Graveyard ({len(board['graveyard'])})",
            value="\n".join(lines),
            inline=False,
        )
    if board["most_burned"]:
        embed.add_field(
            name="🔥 Most-burned teams",
            value=" · ".join(f"{t} ({n})" for t, n in board["most_burned"]),
            inline=False,
        )
    return embed


def build_announcement_embed(
    *,
    season_name: str,
    entrants: int,
    buyin: int,
    gauntlet_mode: bool,
    color: discord.Color | None = None,
) -> discord.Embed:
    """The pinned season announcement (§2.2). ``gauntlet_mode`` flips the
    copy once Week 1 has kicked off — the button stays, the road gets real."""
    if gauntlet_mode:
        description = (
            "the season is underway. the door is open; the road is real.\n"
            f"🌾 **{entrants}** souls walking."
        )
    else:
        description = (
            "pick one NFL team to win each week. no team twice. "
            "your team loses, you're out.\n"
            "last one standing takes the pot. the meadow watches.\n"
            f"🌾 **{entrants}** souls enrolled."
        )
    embed = discord.Embed(
        title=f"🏈 {season_name}",
        description=description,
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    embed.add_field(
        name="Entry",
        value=f"{buyin:,} coins" if buyin else "free — walk in",
        inline=True,
    )
    embed.add_field(
        name="The rules, briefly",
        value=(
            "• one team a week, straight up — win or die\n"
            "• each team usable **once** all season\n"
            "• picks lock at that game's kickoff; secret until Tuesday\n"
            "• one strike of grace 💛 — the second is the end\n"
            "• join any week: the gauntlet replays what you missed"
        ),
        inline=False,
    )
    embed.set_footer(text="picks are private acts; results are communal theater")
    return embed
