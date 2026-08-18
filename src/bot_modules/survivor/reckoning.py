"""THE RECKONING — the Tuesday post, in three acts (spec §2.5, stage 5).

Act 1 the toll (week, survivors before → after, pot, a rotating flavor
line, the gate for new arrivals). Act 2 the ledger — the ONLY place picks
ever appear — plus the Ghost Streak strip. Act 3 the eulogies, one
name-slotted flavor line per death, source-aware: the groundskeeper's
decline and the leaver's exit have their own fixed lines.

Everything here is pure assembly over a connection; the weekly task in
``survivor/tasks.py`` does the posting, role swaps, and condolence DMs.
Flavor rotation is deterministic (week-keyed modulo), so a preview renders
exactly what Tuesday will post.
"""

from __future__ import annotations

import sqlite3

import discord

from bot_modules.services.branding_service import DEFAULT_ACCENT
from bot_modules.services.survivor_service import eliminate_player, list_flavor
from bot_modules.survivor.logic import (
    elapsed_weeks,
    ghost_streaks,
    pot_totals,
)

# Fixed lines for deaths that aren't the corpus's job (§1.2, §6.14). The
# groundskeeper survives the 2026-08-18 standard-sports copy pass — he is
# the spec's own auto-pick character and travels fine; the other two lines
# are server-neutral.
GROUNDSKEEPER_LINE = "the groundskeeper stopped covering for **{name}**."
MISSED_LINE = "**{name}** never picked. Eliminated."
LEAVER_LINE = "**{name}** left the server mid-season. 🚪"

_RESULT_ICON = {
    "win": "✅", "loss": "💀", "tie": "🤝", "void": "🌫️", None: "⏳",
}


def next_reckoning_week(conn: sqlite3.Connection, season: dict, now: float) -> int | None:
    """The week Tuesday's post reports: the first unreckoned week whose
    games have all kicked off. None = nothing to reckon yet."""
    candidate = int(season["config"].get("last_reckoned_week") or 0) + 1
    if candidate in elapsed_weeks(conn, season["season_year"], now):
        return candidate
    return None


def rotate(lines: list[str], key: int) -> str:
    """Deterministic corpus rotation — the same week always draws the same
    line, so the panel preview matches the Tuesday post exactly."""
    if not lines:
        return ""
    return lines[key % len(lines)]


def _fill(line: str, *, name: str = "", team: str = "", week: int = 0) -> str:
    # str.replace, never str.format — a brace in a nickname must not crash
    # a eulogy (the corpus's own contract since stage 1).
    return (
        line.replace("{name}", name)
        .replace("{team}", team)
        .replace("{week}", str(week))
    )


def eliminate_leavers(
    conn: sqlite3.Connection, season: dict, week: int, present_ids: set[int]
) -> list[int]:
    """§6.14: alive players no longer in the guild die at this Reckoning
    (source 'left', streak frozen). Returns who was marked."""
    rows = conn.execute(
        "SELECT user_id FROM survivor_players "
        "WHERE season_id = ? AND status = 'alive'",
        (season["id"],),
    ).fetchall()
    gone = [int(r["user_id"]) for r in rows if int(r["user_id"]) not in present_ids]
    for user_id in gone:
        eliminate_player(conn, season["id"], user_id, week, source="left")
    return gone


def build_reckoning_data(
    conn: sqlite3.Connection, season: dict, week: int, now: float
) -> dict:
    """Everything the three acts need, as plain data. No Discord objects."""
    players = conn.execute(
        "SELECT user_id, status, strikes_used, eliminated_week, elimination_source"
        " FROM survivor_players WHERE season_id = ?",
        (season["id"],),
    ).fetchall()
    picks = conn.execute(
        "SELECT user_id, slot, team, result, auto_assigned FROM survivor_picks "
        "WHERE season_id = ? AND week = ? ORDER BY user_id, slot",
        (season["id"], week),
    ).fetchall()
    by_user: dict[int, list] = {}
    for p in picks:
        by_user.setdefault(int(p["user_id"]), []).append(p)

    losing = {"loss", "tie"} if season["config"]["tie_rule"] == "loss" else {"loss"}
    deaths, ledger, stragglers = [], [], 0
    alive_now = 0
    for pl in players:
        user_id = int(pl["user_id"])
        died_this_week = pl["eliminated_week"] == week
        living_at_start = pl["status"] == "alive" or died_this_week
        if pl["status"] == "alive":
            alive_now += 1
        if not living_at_start:
            continue  # earlier ghosts live in the strip, not the ledger
        rows = by_user.get(user_id, [])
        stragglers += sum(1 for r in rows if r["result"] is None)
        teams = []
        lost = False
        for r in rows:
            icon = _RESULT_ICON.get(r["result"], "·")
            tag = " 📎" if r["auto_assigned"] else ""
            teams.append(f"{r['team']}{tag} {icon}")
            lost |= r["result"] in losing
        if died_this_week:
            state = "💀"
        elif lost:
            state = "💛→🖤"
        elif not rows:
            state = "🌫️"
        else:
            state = ""
        entry = {
            "user_id": user_id,
            "teams": " · ".join(teams) if teams else "—",
            "state": state,
            "died": died_this_week,
            "source": pl["elimination_source"] if died_this_week else None,
            "fatal_team": next(
                (r["team"] for r in rows if r["result"] in losing), None
            ),
        }
        (deaths if died_this_week else ledger).append(entry)

    flavor_toll = [f["line"] for f in list_flavor(conn, season["guild_id"], "toll")]
    flavor_none = [f["line"] for f in list_flavor(conn, season["guild_id"], "no_death")]
    flavor_eulogy = [f["line"] for f in list_flavor(conn, season["guild_id"], "eulogy")]

    # The gate reports gauntlet walkers only: joins since the last Reckoning
    # AND after the season's first kickoff. Pre-kickoff enrollees never
    # walked anything — the week-1 gate saying they did was the mockup's
    # catch (2026-08-18).
    last_at = int(season["config"].get("last_reckoned_at") or 0)
    first = conn.execute(
        "SELECT MIN(kickoff_utc) AS k FROM nfl_games WHERE season_year = ?",
        (season["season_year"],),
    ).fetchone()
    from bot_modules.survivor.logic import kickoff_ts

    gate_after = max(
        float(last_at), kickoff_ts(first["k"]) if first and first["k"] else 0.0
    )
    arrivals = [
        {"user_id": int(r["user_id"]), "dead": r["status"] == "ghost"}
        for r in conn.execute(
            "SELECT user_id, status, joined_at FROM survivor_players "
            "WHERE season_id = ?",
            (season["id"],),
        ).fetchall()
        if _joined_ts(r["joined_at"]) > gate_after
    ]

    streaks = ghost_streaks(conn, season, now)
    strip = sorted(
        ((uid, st) for uid, st in streaks.items() if st["current"] > 0),
        key=lambda t: (-t[1]["current"], t[0]),
    )[:3]
    record = max((st["best"] for st in streaks.values()), default=0)

    return {
        "week": week,
        "before": alive_now + len(deaths),
        "after": alive_now,
        "pots": pot_totals(conn, season),
        "toll_line": (
            _fill(rotate(flavor_toll, week), week=week)
            if deaths
            else _fill(rotate(flavor_none, week), week=week)
        ),
        "deaths": deaths,
        "ledger": ledger,
        "eulogy_lines": flavor_eulogy,
        "arrivals": arrivals,
        "stragglers": stragglers,
        "streak_strip": strip,
        "streak_record": record,
    }


def _joined_ts(joined_at: str) -> float:
    from bot_modules.survivor.logic import kickoff_ts

    try:
        return kickoff_ts(joined_at)
    except (ValueError, TypeError):
        return 0.0


def eulogy_for(entry: dict, data: dict, name: str, index: int) -> str:
    """Act 3, one line per death — fixed lines for the groundskeeper's
    decline, the missed-pick ruleset, and the leaver; the corpus for the
    honest football deaths, rotated deterministically."""
    if entry["source"] == "cap":
        return _fill(GROUNDSKEEPER_LINE, name=name)
    if entry["source"] == "missed":
        return _fill(MISSED_LINE, name=name)
    if entry["source"] == "left":
        return _fill(LEAVER_LINE, name=name)
    line = rotate(data["eulogy_lines"], data["week"] * 7 + index)
    return _fill(
        line, name=name, team=entry["fatal_team"] or "—", week=data["week"]
    )


def build_reckoning_embed(
    data: dict, name_of, *, season_name: str, color: discord.Color | None = None
) -> discord.Embed:
    """The one post. Three acts, one embed, clipped to Discord's limits."""
    from bot_modules.survivor.embeds import _clip_field

    week = data["week"]
    embed = discord.Embed(
        title=f"🏈 Week {week} — THE RECKONING",
        color=color or discord.Color(DEFAULT_ACCENT),
    )
    # Act 1 — the toll.
    toll = (
        f"{data['toll_line']}\n"
        f"👥 Survivors **{data['before']} → {data['after']}** · "
        f"Pot **{data['pots']['main']:,}** · Ghost Pot **{data['pots']['ghost']:,}**"
    )
    if data["stragglers"]:
        toll += (
            f"\n⏳ {data['stragglers']} result(s) still pending — an update "
            "follows when they're final"
        )
    embed.description = toll
    if data["arrivals"]:
        breathing = sum(1 for a in data["arrivals"] if not a["dead"])
        lines = [
            f"{name_of(a['user_id'])} — "
            + ("💀 arrived eliminated" if a["dead"] else "✅ arrived alive")
            for a in data["arrivals"]
        ]
        word = "player" if len(data["arrivals"]) == 1 else "players"
        lines.insert(
            0,
            f"{len(data['arrivals'])} {word} joined through the Gauntlet "
            f"this week — {breathing} arrived alive.",
        )
        embed.add_field(
            name="🚪 New Entries", value=_clip_field(lines, "arrivals"),
            inline=False,
        )
    # Act 2 — the ledger, deaths sorted first.
    rows = data["deaths"] + data["ledger"]
    if rows:
        lines = [
            f"{e['state']} {name_of(e['user_id'])} · {e['teams']}".strip()
            for e in rows
        ]
        embed.add_field(
            name="📜 The Ledger", value=_clip_field(lines, "players"), inline=False
        )
    if data["streak_strip"] or data["streak_record"]:
        lines = [
            f"👻 {name_of(uid)} · {st['current']} straight"
            for uid, st in data["streak_strip"]
        ] or ["No active streaks"]
        lines.append(f"Season record: {data['streak_record']}")
        embed.add_field(
            name="Ghost Streak", value="\n".join(lines), inline=False
        )
    # Act 3 — eulogies.
    if data["deaths"]:
        lines = [
            eulogy_for(e, data, name_of(e["user_id"]), i)
            for i, e in enumerate(data["deaths"])
        ]
        embed.add_field(
            name="🪦 Eliminations", value=_clip_field(lines, "eliminated"),
            inline=False,
        )
    embed.set_footer(text=f"{season_name} · results post every Tuesday")
    return embed


def slate_join_line(
    *, buyin: int, late_entry: str, gauntlet_mode: bool
) -> str | None:
    """The one-line door for the weekly slate. None = entry is closed and
    neither line nor button belongs on the post."""
    entry = f"{buyin:,} coins to enter" if buyin else "free entry"
    if not gauntlet_mode:
        return f"Join below — {entry}."
    if late_entry == "gauntlet":
        return (
            f"Join any week — missed weeks auto-replay as the Gauntlet. "
            f"({entry} + late fee)"
        )
    if late_entry == "ghost_only":
        return "Late entry joins the Ghost Streak side game — join below."
    return None  # closed
