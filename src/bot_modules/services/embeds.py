"""Centralized embed color palette for Dungeon Keeper.

Different domains intentionally use different visual identities so that
users can recognize at-a-glance which subsystem produced a message:

- Wellness leans calmer/softer (gentle green) to match its supportive tone
- Moderation uses high-visibility colors for enforcement actions
- Starboard / reports use the dashboard's gold accent
- Birthday, welcome, etc. use celebratory or contextual colors

Within each domain, embeds use the same primary color so messages from
the same subsystem feel cohesive. Cross-cutting status indicators
(success / warning / danger / info) live in the shared dashboard palette
at the top so they visually match the web dashboard's chip colors.

Usage:
    from bot_modules.services.embeds import WELLNESS_PRIMARY, COLOR_GREEN

    embed = discord.Embed(title="Streak update", color=WELLNESS_PRIMARY)
"""
from __future__ import annotations

import re

import discord

# ──────────────────────────────────────────────────────────────────
# Dashboard palette (mirrors web/static/app.css :root tokens 1:1)
# ──────────────────────────────────────────────────────────────────
COLOR_GOLD    = 0xE6B84C   # --gold-solid (brand primary)
COLOR_GREEN   = 0x23A55A   # --green (success)
COLOR_RED     = 0xF23F43   # --red (danger)
COLOR_YELLOW  = 0xF0B232   # --yellow (warning)
COLOR_BLURPLE = 0x5865F2   # --blurple (link / external)
COLOR_PLUM    = 0xC07AA1   # --plum (secondary / info)


# ──────────────────────────────────────────────────────────────────
# Wellness — soft, supportive
# ──────────────────────────────────────────────────────────────────
WELLNESS_PRIMARY  = 0x7BC97B   # soft green (existing wellness identity)
WELLNESS_OVERVIEW = 0x5A8A6B   # darker forest for admin overview


# ──────────────────────────────────────────────────────────────────
# Moderation — high-visibility for enforcement actions
# (CLR_* names from commands/jail_commands.py re-export these; MOD_SUCCESS
# is the canonical semantic green per the 2026-07-21 style-guide ruling)
# ──────────────────────────────────────────────────────────────────
MOD_JAIL    = 0xE74C3C   # bright red — locked-in enforcement
MOD_TICKET  = 0x3498DB   # blue — open question / conversation
MOD_POLICY  = 0x9B59B6   # purple — formal policy
MOD_SUCCESS = COLOR_GREEN  # green — resolved / approved (canonical semantic success)
MOD_INFO    = 0x95A5A6   # gray — informational
MOD_WARNING = 0xF1C40F   # yellow — pending warning


# ──────────────────────────────────────────────────────────────────
# Other domains
# ──────────────────────────────────────────────────────────────────
STARBOARD_PRIMARY = COLOR_GOLD     # gold star
BIRTHDAY_PRIMARY  = 0xEB459E       # Discord pink (celebratory)
WELCOME_PRIMARY   = 0x57F287       # Discord brand-green (greeting)
XP_PRIMARY        = COLOR_BLURPLE  # achievement / level-up

# DM permissions — tri-state (request → accept | deny)
DM_PRIMARY = COLOR_GOLD       # general info / panels
DM_ACCEPT  = COLOR_GREEN
DM_DENY    = COLOR_RED
DM_PENDING = 0xE67E22         # orange

# Activity / inactivity warnings and purges
ACTIVITY_PRIMARY = 0xE67E22   # orange (caution)
ACTIVITY_DANGER  = COLOR_RED

# Auto-delete operations (mass deletion is destructive — dark red)
AUTO_DELETE_PRIMARY = 0x992D22

# Bios — single ember accent, identical across every bio
BIOS_PRIMARY = 0xC8763E

# Generic / fallback
GENERIC_PRIMARY = COLOR_GOLD


# ──────────────────────────────────────────────────────────────────
# Footer helpers
# ──────────────────────────────────────────────────────────────────
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")


def footer_emoji(emoji: str, fallback: str = "") -> str:
    """An emoji safe to place in an embed **footer**.

    Footers render as plain text, so a custom emoji (``<:name:id>``) shows as
    its raw tag there. This passes plain **unicode** emoji through unchanged
    and swaps a custom one for ``fallback`` (default: nothing). See
    ``docs/embed_style_guide.md`` → Footers.
    """
    return fallback if _CUSTOM_EMOJI_RE.fullmatch((emoji or "").strip()) else emoji


# ──────────────────────────────────────────────────────────────────
# Monospace table helpers
# ──────────────────────────────────────────────────────────────────
def pad_cell(text: str, width: int) -> str:
    """Clip + left-pad a table cell for a fixed-width inline-code column.

    Sections align their columns by wrapping cells in backticks (monospace)
    and padding to the column width — code blocks would align too, but they
    swallow bold and ``<t:…:R>`` timestamps, which must stay live. See
    ``docs/embed_style_guide.md`` → Monospace tables, which names this helper.
    """
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.ljust(width)


# Discord rejects an embed field over 1024 chars — and rejects the whole
# embed with it, not just the offending field.
EMBED_FIELD_LIMIT = 1024


def fit_lines(lines: list[str], limit: int = EMBED_FIELD_LIMIT) -> str:
    """Join as many leading lines as fit an embed field.

    Variable-length rows (wallet memos, quest titles) can overrun the field
    cap and make Discord reject the entire embed. Dropping the overflow keeps
    the leading rows visible rather than 400-ing the whole render.
    """
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > limit:
            break
        out.append(line)
        used += cost
    return "\n".join(out)


def rel_ts(ts: float) -> str:
    """A Discord relative timestamp — ticks live in every client."""
    return f"<t:{int(ts)}:R>"


# Compact per-source XP labels, shared by every surface that prints an XP
# breakdown (``/modinfo`` and ``/info``). Order and source keys mirror the
# stacked-bar chart palette in ``bot_modules.services.activity_graphs`` so the
# text breakdown and the chart always tell the same story.
XP_SOURCE_DISPLAY: tuple[tuple[str, str], ...] = (
    ("text", "\U0001F4AC Text"),
    ("voice", "\U0001F50A Voice"),
    ("reply", "\u21A9\uFE0F Reply"),
    ("image_react", "\U0001F5BC React"),
    ("grant", "\U0001F381 Grant"),
)


def fmt_xp(amount: float) -> str:
    """Human-compact XP number: 950 -> ``950``, 31234 -> ``31.2k``, 1.2M."""
    n = float(amount)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:.0f}"


def xp_breakdown_parts(xp_by_source: "dict[str, float] | None") -> list[str]:
    """``["\U0001F4AC Text 12.3k", ...]`` for the known sources, then any others.

    Unknown sources are not dropped: a new XP source added to the ledger shows
    up under its raw name rather than silently vanishing from the breakdown,
    which is how a mis-keyed source would otherwise go unnoticed.
    """
    if not xp_by_source:
        return []
    known = {src for src, _ in XP_SOURCE_DISPLAY}
    parts = [
        f"{label} {fmt_xp(xp_by_source[src])}"
        for src, label in XP_SOURCE_DISPLAY
        if xp_by_source.get(src)
    ]
    parts += [
        f"{src} {fmt_xp(amt)}"
        for src, amt in xp_by_source.items()
        if src not in known and amt
    ]
    return parts


def build_admin_mirror_embed(
    *, domain: str, action: str, summary: str, actor_name: str, actor_id: int
) -> discord.Embed:
    """The web admin mod-log mirror's embed (sent by web_server.helpers).

    Lives here rather than in web_server so the embed-accent contract's
    bot_modules walk can see it — it rides KNOWN_UNCOVERED as a semantic
    exemption: orange is the mod-audit color on every sanction surface and
    never takes the guild accent. Moved from voice_master.embeds when the
    mirror was shared with Survivor (2026-08-17).
    """
    embed = discord.Embed(
        title=f"{domain} — {action}",
        description=summary,
        color=discord.Color.orange(),
    )
    embed.set_footer(text=f"by {actor_name} ({actor_id})")
    return embed
