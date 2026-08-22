"""Pure shaping for the ``/info`` panel.

The cog gathers rows and Discord state, hands them here as plain values, and
gets back the exact list of things to show and the exact list of buttons to
offer. No queries, no Discord objects, no side effects — which is the whole
point: every branch below (a feature the guild never configured, a member who
opted out, a cog that isn't loaded) is a table row in
``tests/test_member_info_logic.py`` rather than a mocked interaction.

Two rules are encoded here rather than in the view, because they are the ones
that would leak if a future edit got them wrong:

* A feature the guild has not configured produces **no row at all**. ``/info``
  must not advertise a feature that does not exist here, and must not let a
  member "join" something an admin never set up.
* An action is only ever offered when the *member* can act on it. The panel
  never renders a button whose flow would immediately refuse.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# ── Opt-in states ────────────────────────────────────────────────────────
#
# "unset" is deliberately distinct from "out". Someone who has never joined
# Pen Pals and someone who deliberately left it get different copy: the first
# is an invitation, the second is a decision the bot should not argue with.
STATE_IN = "in"
STATE_OUT = "out"
STATE_UNSET = "unset"

# ── Actions the panel can offer ──────────────────────────────────────────
ACTION_JOIN = "join"
ACTION_LEAVE = "leave"
ACTION_OPEN = "open"


@dataclass(frozen=True)
class FeatureState:
    """What the cog found out about one feature, for one member.

    ``configured`` is the guild-level question ("is this feature set up and
    enabled here?"); ``state`` is the member-level one. ``detail`` is optional
    extra copy the cog computed (e.g. the member's DM mode), appended to the
    row's own sentence.

    ``actionable`` exists for the case where a feature is configured and the
    member could in principle act, but the cog that owns the flow isn't
    loaded. The row still renders — the status is true and worth showing —
    but no button is offered, because pressing it would do nothing.
    """

    configured: bool = False
    state: str = STATE_UNSET
    detail: str = ""
    actionable: bool = True


@dataclass(frozen=True)
class OptInRow:
    """One line in the panel's opt-in section, plus its optional button."""

    key: str
    label: str
    emoji: str
    state: str
    text: str
    action: str | None = None
    action_label: str = ""

    @property
    def has_action(self) -> bool:
        return self.action is not None


@dataclass(frozen=True)
class _FeatureSpec:
    """Static copy for one feature: how each state reads, what each offers."""

    key: str
    label: str
    emoji: str
    in_text: str
    out_text: str
    unset_text: str
    in_action: tuple[str, str] | None = None
    out_action: tuple[str, str] | None = None
    unset_action: tuple[str, str] | None = None


# Order here is the order rows appear in the embed. Social opt-ins first
# (the ones a member is most likely to want to change), then the settings-ish
# ones, then no-contact last — it is not an opt-in and reads as a footer.
_FEATURE_SPECS: tuple[_FeatureSpec, ...] = (
    _FeatureSpec(
        key="pen_pals",
        label="Pen Pals",
        emoji="✉️",
        in_text="You're in the pool — you'll be matched with someone.",
        out_text="You've left the pool and won't be matched.",
        unset_text="Not joined. Get paired with someone for a 24-hour chat.",
        in_action=(ACTION_LEAVE, "Leave Pen Pals"),
        out_action=(ACTION_JOIN, "Join Pen Pals"),
        unset_action=(ACTION_JOIN, "Join Pen Pals"),
    ),
    _FeatureSpec(
        key="whispers",
        label="Whispers",
        emoji="🤫",
        in_text="Opted in — you can send and receive whispers.",
        out_text="Opted out — you can't send or receive whispers.",
        unset_text="Not opted in — you can't send or receive whispers.",
        in_action=(ACTION_LEAVE, "Leave Whispers"),
        out_action=(ACTION_JOIN, "Join Whispers"),
        unset_action=(ACTION_JOIN, "Join Whispers"),
    ),
    _FeatureSpec(
        key="guess",
        label="Guess pool",
        emoji="🎭",
        in_text="You're in the pool.",
        out_text="You're not in the pool.",
        unset_text="You're not in the pool.",
        in_action=(ACTION_LEAVE, "Leave Guess"),
        out_action=(ACTION_JOIN, "Join Guess"),
        unset_action=(ACTION_JOIN, "Join Guess"),
    ),
    _FeatureSpec(
        key="dm_mode",
        label="DMs",
        emoji="📬",
        # Every state reads the same: the mode itself arrives as ``detail``.
        # These are not empty strings, deliberately — a row whose only text is
        # the caller-supplied detail renders blank the moment that lookup
        # fails, and a blank row is worse than a generic one.
        in_text="Who can DM you through the bot.",
        out_text="Who can DM you through the bot.",
        unset_text="Who can DM you through the bot.",
        in_action=(ACTION_OPEN, "DM settings"),
        out_action=(ACTION_OPEN, "DM settings"),
        unset_action=(ACTION_OPEN, "DM settings"),
    ),
    _FeatureSpec(
        key="wellness",
        label="Wellness check-ins",
        emoji="🌱",
        in_text="Opted in.",
        out_text="Opted out.",
        unset_text="Not set up.",
        in_action=(ACTION_OPEN, "Wellness settings"),
        out_action=(ACTION_OPEN, "Wellness setup"),
        unset_action=(ACTION_OPEN, "Wellness setup"),
    ),
    _FeatureSpec(
        key="birthday",
        label="Birthday",
        emoji="🎂",
        in_text="On file — it'll be announced on the day.",
        out_text="Not on file.",
        unset_text="Not on file.",
        in_action=(ACTION_LEAVE, "Remove birthday"),
        out_action=(ACTION_JOIN, "Set birthday"),
        unset_action=(ACTION_JOIN, "Set birthday"),
    ),
    _FeatureSpec(
        key="no_contact",
        label="No-contact list",
        emoji="🚫",
        # No count, ever. `/nocontact list` hides entries the *other* party
        # created against you (no_contact_cog.is_visible_to); a number here
        # computed from the raw table would leak precisely what that filter
        # exists to hide, and a number computed after filtering would still
        # differ from the unfiltered truth in a way a curious member could
        # difference against other surfaces. The button opens the filtered
        # view and this line says nothing numeric.
        in_text="Private, and never revealed to the other person.",
        out_text="Private, and never revealed to the other person.",
        unset_text="Private, and never revealed to the other person.",
        in_action=(ACTION_OPEN, "My no-contact list"),
        out_action=(ACTION_OPEN, "My no-contact list"),
        unset_action=(ACTION_OPEN, "My no-contact list"),
    ),
)

_SPEC_BY_KEY = {spec.key: spec for spec in _FEATURE_SPECS}


def _action_for(spec: _FeatureSpec, state: str) -> tuple[str, str] | None:
    if state == STATE_IN:
        return spec.in_action
    if state == STATE_OUT:
        return spec.out_action
    return spec.unset_action


def _text_for(spec: _FeatureSpec, state: str) -> str:
    if state == STATE_IN:
        return spec.in_text
    if state == STATE_OUT:
        return spec.out_text
    return spec.unset_text


def build_optin_rows(states: Mapping[str, FeatureState]) -> list[OptInRow]:
    """Turn per-feature findings into the panel's rows, in display order.

    Unconfigured features are dropped entirely (see the module docstring).
    A feature absent from ``states`` is treated as unconfigured, so a cog that
    failed to load degrades to "not shown" rather than to a broken row.
    """
    rows: list[OptInRow] = []
    for spec in _FEATURE_SPECS:
        found = states.get(spec.key)
        if found is None or not found.configured:
            continue

        text = _text_for(spec, found.state)
        if found.detail:
            text = f"{found.detail} — {text}" if text else found.detail

        action = _action_for(spec, found.state) if found.actionable else None
        rows.append(
            OptInRow(
                key=spec.key,
                label=spec.label,
                emoji=spec.emoji,
                state=found.state,
                text=text,
                action=action[0] if action else None,
                action_label=action[1] if action else "",
            )
        )
    return rows


# ── Account & activity ───────────────────────────────────────────────────


@dataclass(frozen=True)
class AccountFacts:
    """The member-safe half of what ``/modinfo`` shows about a member."""

    account_age_days: int
    created_ts: int
    joined_ts: int | None
    role_names: Sequence[str] = field(default_factory=tuple)
    level: int | None = None
    total_xp: float = 0.0
    xp_by_source: Mapping[str, float] = field(default_factory=dict)
    msgs_30d: int = 0
    top_channels: Sequence[tuple[int, int]] = field(default_factory=tuple)
    last_seen_ts: float | None = None


def visible_top_channels(
    rows: Sequence[Mapping[str, object]],
    viewable_channel_ids: set[int],
    *,
    limit: int = 3,
) -> list[tuple[int, int]]:
    """Drop channels the member can no longer see, then take the top ``limit``.

    Your own message counts are yours, but the *channel name* isn't: a channel
    you were removed from (or one that went private) would otherwise be named
    back at you by your own stats page. Filtering happens here, on ids the cog
    read from live Discord state, so the query stays a plain top-N.
    """
    out: list[tuple[int, int]] = []
    for row in rows:
        channel_id = int(row["channel_id"])  # type: ignore[arg-type]
        if channel_id not in viewable_channel_ids:
            continue
        out.append((channel_id, int(row["cnt"])))  # type: ignore[arg-type]
        if len(out) >= limit:
            break
    return out


def displayable_roles(
    role_names: Sequence[str], *, limit: int = 12
) -> tuple[list[str], int]:
    """The member's roles for display, plus how many were cut.

    Returns ``(names, overflow)`` so the embed can render "+N more" without
    re-deriving the cut. Caller passes roles already ordered highest-first and
    with ``@everyone`` removed — ordering is Discord's business, not ours.
    """
    kept = list(role_names[:limit])
    return kept, max(0, len(role_names) - len(kept))
