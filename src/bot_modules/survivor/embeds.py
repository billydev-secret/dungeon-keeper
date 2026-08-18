"""Survivor embeds — pure builders, accent-colored per the house contract.

Every builder takes ``color`` and falls back to the branding default, so it
rides the shared accent contract (tests/test_embed_accent_contract.py — one
``case()`` row each, never per-file accent copies).

Copy voice (decided 2026-08-18): **standard sports register** — Title Case
labels per the style guide, scoreboard framing, light humor. These strings
ship to every server; per-guild personality lives in the guild-scoped
flavor corpus and the branding kit (accent + bot identity), never here.
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
    return f"{'🟡' if low else '🟢'} {count} teams available"


def _rel(ts: float | None) -> str:
    return f"<t:{int(ts)}:R>" if ts else "—"


def _clip_field(lines: list[str], noun: str) -> str:
    """Join lines under Discord's 1024-char field cap, with an honest
    overflow line — a row cap alone lets 25 ordinary display names blow the
    limit and 400 the whole board."""
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        cost = len(line) + (1 if out else 0)
        if used + cost > 984:  # headroom for the overflow line below
            out.append(f"…and {len(lines) - i} more {noun}")
            break
        out.append(line)
        used += cost
    return "\n".join(out)


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
        f"👻 Eliminated — Week {st['eliminated_week']}" if dead
        else "✅ Alive"
    )
    streak = st.get("streak")
    if dead and streak:
        state += (
            f"\nStreak **{streak['current']}** · Best {streak['best']}"
        )
    embed.add_field(name="Status", value=state, inline=True)
    embed.add_field(
        name="Strikes",
        value=strike_hearts(st["strikes_used"], st["strikes_allowed"]),
        inline=True,
    )
    embed.add_field(
        name="Teams Left",
        value=_wealth(st["satchel_count"], st["satchel_low"]),
        inline=True,
    )
    if st["week"] is None:
        pick_line = "No games left to pick — the season is wrapping up."
    elif st["pick"] is None:
        pick_line = f"Week {st['week']}: **no pick yet** — `/survivor pick`"
    else:
        lock = (
            "🔒 locked" if st["pick_locked"]
            else f"locks {_rel(st['pick_kickoff_ts'])}"
        )
        tag = " 📎" if st["pick"]["auto_assigned"] else ""
        pick_line = f"Week {st['week']}: **{st['pick']['team']}**{tag} · {lock}"
    pending = st.get("pending")
    if pending:
        pick_line += (
            f"\nWeek {pending['week']}: **{pending['team']}** · "
            "⏳ awaiting results"
        )
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
        title=f"🏈 {'Pick Changed' if changed else 'Pick Confirmed'}: {game.team}",
        description=(
            f"**{game.team}** {vs} · locks <t:{int(game.kickoff_ts)}:R>\n"
            "Change it any time before kickoff. Picks stay hidden until "
            "the weekly results."
        ),
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    embed.add_field(
        name="Teams Left",
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
        title=f"🏈 {season_name} — Standings",
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    week = board["week"]
    pots = board["pots"]
    embed.description = (
        f"Week {week if week is not None else '—'} · "
        f"Pot **{pots['main']:,}** · Ghost Pot **{pots['ghost']:,}**"
    )
    if board["alive"]:
        lines = [
            f"{name_of(p['user_id'])} · {p['weeks_survived']}W · "
            f"{strike_hearts(p['strikes_used'], strikes_allowed)}"
            for p in board["alive"]
        ]
        embed.add_field(
            name=f"✅ Alive ({len(board['alive'])})",
            value=_clip_field(lines, "players"),
            inline=False,
        )
    if board["graveyard"]:
        lines = [
            f"{name_of(p['user_id'])} · Week {p['eliminated_week'] or '?'}"
            for p in board["graveyard"]
        ]
        embed.add_field(
            name=f"👻 Eliminated ({len(board['graveyard'])})",
            value=_clip_field(lines, "players"),
            inline=False,
        )
    if board["most_burned"]:
        embed.add_field(
            name="🔥 Most-Picked Teams",
            value=" · ".join(f"{t} ({n})" for t, n in board["most_burned"]),
            inline=False,
        )
    return embed


def build_panel_embed(
    *,
    season_name: str,
    entrants: int,
    buyin: int,
    gauntlet_mode: bool,
    late_entry: str = "gauntlet",
    strikes: int = 1,
    week: int | None = None,
    games: list[dict] | None = None,
    alive: int = 0,
    eliminated: int = 0,
    picked: int = 0,
    pot: int = 0,
    ghost_pot: int = 0,
    color: discord.Color | None = None,
) -> discord.Embed:
    """THE channel panel — the one updating message (decided 2026-08-18).

    Replaces the separate announcement and slate posts: season pitch, the
    current week's games, a standings line, and the joining door in one
    embed. The bot edits it in place on joins and settles, and reposts it
    to the channel bottom each Wednesday with the week-open ping — that
    repost IS the slate moment. Enrolling seasons (no week yet) show the
    pre-kickoff face.
    """
    from bot_modules.survivor.reckoning import slate_join_line

    active = week is not None
    title = f"🏈 {season_name}"
    if active:
        title += f" — Week {week}"
    if active:
        description = (
            "Pick one team to **win**. Picks lock at each game's kickoff "
            "and stay hidden until the results post.\n"
            f"✅ Alive **{alive}** · 👻 Eliminated **{eliminated}** · "
            f"Pot **{pot:,}** · Ghost Pot **{ghost_pot:,}**"
        )
    else:
        description = (
            "Pick one NFL team to win each week. No team twice. "
            "Lose and you're out.\n"
            "Last one standing takes the pot.\n"
            f"👥 **{entrants}** players in · Pot **{pot:,}**"
        )
    embed = discord.Embed(
        title=title,
        description=description,
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    if active and games:
        lines = [
            f"**{g['away']}** @ **{g['home']}** · <t:{int(g['kickoff_ts'])}:f>"
            for g in games
        ]
        embed.add_field(
            name="This Week's Games",
            value=_clip_field(lines, "games"),
            inline=False,
        )
    strike_lines = {
        0: "• Sudden death ☠️ — one loss and you're out",
        1: "• One strike of grace 💛 — the second ends your run",
        2: "• Two strikes of grace 💛💛 — the third ends your run",
    }
    late_lines = {
        "gauntlet": "• Join any week — missed weeks auto-replay as the Gauntlet",
        "ghost_only": "• Late entry joins the Ghost Streak side game",
        "closed": "• Entry closes at Week 1 kickoff",
    }
    embed.add_field(
        name="The Rules",
        value=(
            "• One team each week to **win**, straight up\n"
            "• Each team usable **once** all season\n"
            "• Picks lock at that game's kickoff — hidden until the results post\n"
            + strike_lines.get(strikes, strike_lines[1]) + "\n"
            + late_lines.get(late_entry, late_lines["gauntlet"])
        ),
        inline=False,
    )
    join_line = slate_join_line(
        buyin=buyin, late_entry=late_entry, gauntlet_mode=gauntlet_mode
    )
    if join_line:
        embed.add_field(name="New Here?", value=join_line, inline=False)
    if active:
        embed.set_footer(
            text=f"Picks close at kickoff · {picked} of {alive} alive have picked"
        )
    else:
        embed.set_footer(text="Picks stay secret until the weekly results post")
    return embed


_RESULT_ICON = {"win": "✅", "loss": "💀", "tie": "🤝", "void": "🌫️"}


def build_gauntlet_receipt_embed(
    fate, *, buyin: int, color: discord.Color | None = None
) -> discord.Embed:
    """The private gauntlet receipt (§4.2): the inherited fate, week by week,
    shown BEFORE anyone pays — nobody buys in blind."""
    embed = discord.Embed(
        title="🏈 Late Entry — The Gauntlet",
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    lines = []
    for rw in fate.weeks:
        if rw.team is None:
            lines.append(f"Week {rw.week} · — · 🌫️ void (no eligible pick)")
            continue
        icon = _RESULT_ICON.get(rw.result, "·")
        note = " 💀 **eliminated here**" if rw.fatal else ""
        lines.append(f"Week {rw.week} · **{rw.team}** · {icon} {rw.result}{note}")
    embed.add_field(
        name="Missed Weeks (Auto-Replayed)",
        value=_clip_field(lines, "weeks") or "—", inline=False,
    )
    if fate.dead:
        state = (
            f"💀 You enter **eliminated** (Week {fate.death_week}) and join "
            "the Ghost Streak side game immediately — keep picking"
        )
    else:
        hearts = strike_hearts(fate.strikes_used, max(fate.strikes_used, 1))
        state = (
            f"✅ You enter **alive** · Strikes {hearts or '—'} · "
            f"{len(fate.burned)} teams already used"
        )
    embed.add_field(name="Where You Land", value=state, inline=False)
    cost = [f"Late fee: **{fate.fee:,}** ({fate.elapsed_count} missed weeks)"]
    if buyin:
        cost.append(f"Buy-in: **{buyin:,}**")
    embed.add_field(name="Entry Cost", value=" · ".join(cost), inline=False)
    embed.set_footer(text="Shown before you pay — no surprises")
    return embed


def build_history_embed(
    rows: list[dict],
    *,
    display_name: str,
    revealed_week: int,
    own: bool,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Pick history, one builder for both faces (§2.6): the public
    /survivor history shows revealed weeks only; the panel's My History
    button is ephemeral and personal, so the viewer's own unrevealed picks
    appear too — tagged as hidden from everyone else. ``rows``:
    ``{week, team, result, auto_assigned}`` ascending."""
    icons = {"win": "✅", "loss": "💀", "tie": "🤝", "void": "🌫️", None: "⏳"}
    lines = []
    for r in rows:
        if not own and int(r["week"]) > revealed_week:
            continue  # secrecy: the public face never shows a live pick
        tag = " 📎" if r["auto_assigned"] else ""
        line = f"Week {r['week']}: **{r['team']}**{tag} {icons.get(r['result'], '·')}"
        if own and int(r["week"]) > revealed_week:
            line += " · 🤫 hidden from others"
        lines.append(line)
    embed = discord.Embed(
        title=f"📜 {display_name} — Pick History",
        description=_clip_field(lines, "weeks") if lines else
        "No picks on record yet.",
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    embed.set_footer(
        text="Only you can see this — including your current pick" if own
        else "Revealed picks only — current picks stay hidden"
    )
    return embed