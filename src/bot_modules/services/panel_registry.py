"""Which channel panels the dashboard can post, and how to reach each one.

Before 2026-07-28 every panel had its own slash command whose whole job was
"put this panel in that channel" — seven commands, one shape, and exactly the kind
of admin plumbing CLAUDE.md keeps on the web. They collapse into one route
(``POST /api/panels/{key}/post``) reading this table.

They first landed on a single Channel Panels page, which put seven unrelated
controls in one place — an admin configuring the perk shop had to leave the
Sinks page to place the shop panel. Later the same day each control moved onto
the config page of the feature it belongs to (``host_page`` below); the page is
gone. The registry deliberately did *not* dissolve with it. Seven bespoke
endpoints would be seven chances to get the channel resolution, the bot-
permission check, or the option coercion subtly different, so the posting path
stayed one route and one table — only where the button is drawn changed.

Each cog exposes a uniform ``async post_*(guild, channel)`` method rather than
the route reaching into cog internals, so the registry stays plain data: a key,
a place to find the method, and the copy the dashboard renders. That uniformity
is the point — the panels are otherwise a mix of ``StickyPanel`` instances, a
module-level helper, and one that built its message inline.

Deliberately *not* auto-posted on boot, unlike the DM request panel. That one is
the only route to a member's DM settings, so a guild whose admin never pressed
the button would have no surface at all. These all sit alongside something that
still works — ``/ticket open``, ``/guess submit``, ``/voice …``, ``/bank wallet``,
or the Grant Audit report page — so a guild that never posts one loses
discoverability, not capability. Posting into a channel unasked is a bigger
imposition than that.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PanelOption:
    """A setting a panel needs before it can be posted.

    Only the grant-audit card has any: it renders one grant role at one minimum
    level, and those are a genuine choice rather than configuration living
    somewhere else. Declared here so the dashboard renders the right control and
    the route knows what to accept, instead of every panel with settings growing
    its own endpoint.

    ``kind`` is ``"grant_role"`` (choices resolved per-guild from grant-role
    config) or ``"int"``.
    """

    name: str
    label: str
    kind: str
    default: str | int
    hint: str = ""
    minimum: int | None = None


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
    #: Dashboard page that renders this panel's post control. Since 2026-07-28
    #: each control sits on the config page for the feature it belongs to
    #: rather than on one shared page, and this is where. Enforced by
    #: tests/test_panel_registry.py, which checks that page actually mounts it.
    host_page: str
    #: Settings the poster needs. Empty for panels that render themselves.
    options: tuple[PanelOption, ...] = ()


PANEL_SPECS: tuple[PanelSpec, ...] = (
    PanelSpec(
        # One spec where there were two ("economy-guide" and
        # "economy-leaderboard"): the panels merged on 2026-08-18.
        key="economy-panel",
        label="Economy Panel",
        description=(
            "The guild's economy panel — today's pulse, top earners, quest and "
            "community-goal progress, repainting itself after economy activity. "
            "Its buttons carry the rest: a member's own quests and wallet, the "
            "how-it-works guide, and the 🔔 opt-in for economy DMs. Sticks to "
            "the bottom of its channel."
        ),
        cog="EconomyCog",
        method="post_economy_panel",
        host_page="economy-config",
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
        # Settings, not Sinks: all three economy panels are placed in one
        # sitting when the economy is set up, and Sinks is about prices.
        host_page="economy-config",
    ),
    PanelSpec(
        key="economy-bounty",
        label="Bounty Board",
        description=(
            "The Bounty Board hub — explains how bounties work, lists the open "
            "ones with their pots, and carries the Post a bounty and Chip in "
            "buttons. Must go in the bounty board channel set above; it keeps "
            "itself at the bottom of it."
        ),
        cog="EconomyCog",
        method="post_bounty_panel",
        host_page="economy-config",
    ),
    PanelSpec(
        key="voice-control",
        label="Voice Control Owner Panel",
        description=(
            "The persistent owner-control panel for temporary voice channels. "
            "Goes to the Control Channel set above; it keeps itself at the bottom "
            "of that channel, so this is a once-per-setup action."
        ),
        cog="VoiceMasterCog",
        method="post_control_panel",
        host_page="voice-activity",
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
        host_page="config-guess",
    ),
    PanelSpec(
        key="grant-audit",
        label="Grant Audit Card",
        description=(
            "Who is at or past the level threshold but still missing the grant "
            "role. Posts a card that refreshes itself hourly; delete the message "
            "to retire it."
        ),
        cog="RoleGrantCog",
        method="post_audit_card",
        host_page="grant-audit",
        options=(
            PanelOption(
                name="role_key",
                label="Grant role",
                kind="grant_role",
                default="nsfw",
                hint="Which configured grant role the card audits.",
            ),
            PanelOption(
                name="min_level",
                label="Minimum level",
                kind="int",
                default=5,
                minimum=1,
                hint="Members at or above this level are expected to hold the role.",
            ),
        ),
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
        host_page="mod-tickets",
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
