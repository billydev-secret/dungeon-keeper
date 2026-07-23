"""String builders for the daily-login quest digest (the "Daily Streak" DM).

Pure formatting — no Discord objects. The cog turns the returned
``(field_name, field_value)`` pairs into embed fields, so the layout the
member sees (aligned bars, per-quest blurbs, channel links, cadence grouping,
char-limit splitting) is unit-tested here directly rather than through Discord
mocks.

Each open quest renders as a three-line block::

    🔹 **Server Buzz**
    `▰▰▱▱▱▱▱▱▱▱  2,196 / 16,635`
    _Keep the whole server chatting today._ → <#123>

The bar sits in a monospace code span so bars and counts line up down the
column. Blocks are grouped by cadence into embed fields; a "biggest movers
yesterday" field leads when there is community-goal history to show.
"""

from __future__ import annotations

from bot_modules.economy.leaderboard import bar_fill
from bot_modules.economy.quests import TRIGGER_KINDS

# Discord caps an embed field value at 1024 chars; a cadence group that would
# overrun splits into "<heading> (cont.)" fields.
FIELD_LIMIT = 1024

# Longest a quest description is shown before it's clipped, so one wordy quest
# can't blow the field budget.
_BLURB_MAX = 160

MOVERS_HEADING = "📈 Biggest Movers Yesterday"

# Cadence → field heading, in the order they appear in the digest. "quest" is
# kept in each heading so the field reads as part of the checklist.
GROUP_ORDER: list[tuple[str, str]] = [
    ("daily", "🎯 Daily Quests"),
    ("weekly", "📅 Weekly Quests"),
    ("monthly", "🗓️ Monthly Quests"),
    ("community", "🌍 Community Goals"),
    ("event", "✨ Anytime Quests"),
]

# Light context for a quest that carries no description of its own, so every
# block still has a blurb line.
_FALLBACK_BLURB: dict[str, str] = {
    "daily": "A daily quest — resets tomorrow.",
    "weekly": "A weekly quest — resets next week.",
    "monthly": "A monthly quest — resets next month.",
    "community": "A shared server goal — everyone chips in.",
}

_MOVER_MEDALS = ["🥇", "🥈", "🥉"]


def bar_meter(current: int, target: int, width: int = 10) -> str:
    """A monospace meter — ``▰▱`` fill plus spaced counts — in a code span."""
    if target <= 0:
        return f"`{current:,}`"
    return f"`{bar_fill(current, target, width)}  {current:,} / {target:,}`"


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _blurb(q: dict) -> str | None:
    """The italic context line under a quest — its description + any channel."""
    desc = (q.get("description") or "").strip()
    text = _shorten(desc, _BLURB_MAX) if desc else _fallback_text(q)
    link = ""
    channel_id = q.get("trigger_channel_id")
    if channel_id:
        link = f" → <#{int(channel_id)}>"
    if not text and not link:
        return None
    body = f"_{text}_" if text else ""
    return (body + link).strip() or None


def _fallback_text(q: dict) -> str:
    hint = TRIGGER_KINDS.get(q.get("state") or "", "")
    if hint:
        return hint
    return _FALLBACK_BLURB.get(str(q.get("qtype") or ""), "")


def quest_block(q: dict) -> str:
    """The multi-line block for one open quest: title, meter/status, blurb."""
    lines = [f"🔹 **{q['title']}**"]
    state = q.get("state")
    if state == "community":
        lines.append(bar_meter(int(q.get("current", 0)), int(q.get("target", 0))))
    elif q.get("progress_target"):
        lines.append(
            bar_meter(int(q["progress_current"]), int(q["progress_target"]))
        )
    elif state == "claimable":
        lines.append("✅ Ready to claim!")
    elif state == "pending":
        lines.append("⏳ Awaiting sign-off")
    blurb = _blurb(q)
    if blurb:
        lines.append(blurb)
    return "\n".join(lines)


def _movers_value(gains: list[dict]) -> str:
    lines = []
    for i, g in enumerate(gains):
        medal = _MOVER_MEDALS[i] if i < len(_MOVER_MEDALS) else "▪️"
        lines.append(f"{medal} **{g['title']}** +{int(g['gain']):,}")
    return "\n".join(lines)


def _pack(heading: str, blocks: list[str]) -> list[tuple[str, str]]:
    """Group blocks into ≤``FIELD_LIMIT`` fields, ``… (cont.)`` on overflow."""
    chunks: list[list[str]] = []
    current: list[str] = []
    length = 0
    for block in blocks:
        added = (2 if current else 0) + len(block)  # 2 = "\n\n" separator
        if current and length + added > FIELD_LIMIT:
            chunks.append(current)
            current, length, added = [], 0, len(block)
        current.append(block)
        length += added
    if current:
        chunks.append(current)
    out = []
    for i, chunk in enumerate(chunks):
        name = heading if i == 0 else f"{heading} (cont.)"
        out.append((name, "\n\n".join(chunk)))
    return out


def digest_sections(
    quests_out: list[dict], gains: list[dict] | None = None
) -> list[tuple[str, str]]:
    """Embed fields for the login digest, in order.

    A "biggest movers yesterday" field leads (when there are movers), then the
    member's open quests grouped by cadence — every open quest, no cap.
    Returns ``[]`` when there is nothing to show, so a quiet guild's DM doesn't
    grow empty fields.
    """
    sections: list[tuple[str, str]] = []
    if gains:
        sections.append((MOVERS_HEADING, _movers_value(gains)))
    open_quests = [q for q in quests_out if q.get("state") != "done"]
    by_type: dict[str, list[dict]] = {}
    for q in open_quests:
        by_type.setdefault(str(q.get("qtype") or ""), []).append(q)
    for qtype, heading in GROUP_ORDER:
        group = by_type.get(qtype)
        if not group:
            continue
        sections.extend(_pack(heading, [quest_block(q) for q in group]))
    return sections
