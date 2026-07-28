"""Which channel panels the dashboard can post, and how to reach each one.

Before 2026-07-28 every panel had its own slash command whose whole job was
"put this panel in that channel" — six commands, one shape, and exactly the kind
of admin plumbing CLAUDE.md keeps on the web. They collapse into one route
(``POST /api/panels/{key}/post``) reading this table.

Each cog exposes a uniform ``async post_*(guild, channel)`` method rather than
the route reaching into cog internals, so the registry stays plain data: a key,
a place to find the method, and the copy the dashboard renders. That uniformity
is the point — the panels are otherwise a mix of ``StickyPanel`` instances, a
module-level helper, and one that built its message inline.

Deliberately *not* auto-posted on boot, unlike the DM request panel. That one is
the only route to a member's DM settings, so a guild whose admin never pressed
the button would have no surface at all. These six all sit alongside commands
that still work — ``/ticket open``, ``/guess submit``, ``/voice …``,
``/bank wallet`` — so a guild that never posts one loses discoverability, not
capability. Posting into a channel unasked is a bigger imposition than that.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PanelSpec:
    """One postable channel panel.

    ``cog`` / ``method`` name an ``async (guild, channel) -> message-or-None``
    entry point. Resolution happens in the route, which owns the live bot; this
    module stays importable without Discord so the table can be tested.
    """

    key: str
    label: str
    description: str
    cog: str
    method: str
    #: Nav page whose settings govern this panel, for a dashboard cross-link.
    related_page: str | None = None


PANEL_SPECS: tuple[PanelSpec, ...] = (
    PanelSpec(
        key="economy-guide",
        label="Economy How-To",
        description=(
            "Explains the currency, how to earn it, and what it buys. Sits at the "
            "bottom of the channel and refreshes in place when re-posted."
        ),
        cog="EconomyCog",
        method="post_guide_panel",
        related_page="economy-config",
    ),
    PanelSpec(
        key="economy-leaderboard",
        label="Economy Leaderboard",
        description=(
            "Auto-updating richest-members board. Re-posting moves it to a new "
            "channel; posting into the channel it already occupies just refreshes it."
        ),
        cog="EconomyCog",
        method="post_leaderboard_panel",
        related_page="economy-config",
    ),
    PanelSpec(
        key="economy-shop",
        label="Perk Shop",
        description=(
            "Browsable perk shop with rent-a-perk buttons. Mirrors whatever the "
            "Sinks page currently offers."
        ),
        cog="EconomyCog",
        method="post_shop_panel",
        related_page="economy-sinks",
    ),
    PanelSpec(
        key="voice-control",
        label="Voice Control Owner Panel",
        description=(
            "The persistent owner-control panel for temporary voice channels. Posts "
            "into the control channel set on the Voice Control config page, not the "
            "channel picked here."
        ),
        cog="VoiceMasterCog",
        method="post_control_panel",
        related_page="config-voice-master",
    ),
    PanelSpec(
        key="guess-prompt",
        label="Guess Who Submit Prompt",
        description=(
            "The channel-bottom Submit/Help prompt for Guess Who. Replaces the "
            "previous prompt rather than stacking a second one."
        ),
        cog="GuessCog",
        method="post_prompt_panel",
        related_page="config-guess",
    ),
    PanelSpec(
        key="ticket-panel",
        label="Support Ticket Panel",
        description=(
            "The Open Ticket button members use to reach the mod team. Each post "
            "creates a new panel; the old one keeps working until deleted."
        ),
        cog="JailCog",
        method="post_ticket_panel",
        related_page="mod-tickets",
    ),
)

_BY_KEY = {spec.key: spec for spec in PANEL_SPECS}


def get_panel_spec(key: str) -> PanelSpec | None:
    """Look up one spec, or None for an unknown key.

    Returning None rather than raising lets the route answer 404 with its own
    wording instead of leaking a KeyError.
    """
    return _BY_KEY.get(key)


def list_panel_specs() -> tuple[PanelSpec, ...]:
    """Every postable panel, in dashboard display order."""
    return PANEL_SPECS
