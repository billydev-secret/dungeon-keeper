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
    from services.embeds import WELLNESS_PRIMARY, COLOR_GREEN

    embed = discord.Embed(title="Streak update", color=WELLNESS_PRIMARY)
"""
from __future__ import annotations

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
# (preserves existing CLR_* values from commands/jail_commands.py)
# ──────────────────────────────────────────────────────────────────
MOD_JAIL    = 0xE74C3C   # bright red — locked-in enforcement
MOD_TICKET  = 0x3498DB   # blue — open question / conversation
MOD_POLICY  = 0x9B59B6   # purple — formal policy
MOD_SUCCESS = 0x2ECC71   # green — resolved / approved
MOD_INFO    = 0x95A5A6   # grey — informational
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

# Generic / fallback
GENERIC_PRIMARY = COLOR_GOLD
