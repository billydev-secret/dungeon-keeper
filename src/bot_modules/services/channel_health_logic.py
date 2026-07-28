"""Is a configured channel still *usable*, or has Discord quietly broken it?

The dashboard already answers "what isn't set up?" (``advisor_gaps``). This
module answers the opposite and much sneakier question: **what is set up, looks
fine in the config table, and doesn't work?**

The motivating incident: the bios channel had ``@everyone`` denied
``view_channel`` with a single role overwrite granting it back. That role was
deleted, which silently takes its channel overwrite with it — no audit entry
names the channel, no config value changes, and the dashboard keeps happily
showing a correctly-configured channel id. The result was a members-only
feature whose channel, trigger button and posted output no member could see for
nine days, while the bot itself (holding Administrator) could still post there
perfectly.

The rules deliberately fire only on states that are wrong under *every* reading
of what a channel is for:

``missing``
    The stored id resolves to nothing. Always wrong.
``wrong_type``
    The channel exists but can't take messages (a category or voice channel
    saved into a "post here" setting). Always wrong.
``bot_cannot_post``
    The bot lacks view/send/embed. Always wrong for an output channel.
``nobody_can_view``
    Not one non-bot member can see it. Always wrong — a staff-only channel
    still has staff, so a legitimately restricted channel lands on a small
    number, never zero.

That last rule is the one worth defending. The obvious design — flag channels
only a *small share* of members can see — was measured against a live server
and would have flagged eleven channels, ten of them correct mod-only rooms, to
catch the one real fault. A share threshold cannot tell "locked down on
purpose" from "broken"; zero can, because nothing is deliberately configured
for an audience of nobody.

Pure and Discord-free: callers hand over a :class:`ChannelSnapshot` built from
whatever they have (``channel_health.snapshot_channel`` does it from a live
``discord.Guild``), so the rules are testable without touching Discord.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

IssueCode = Literal["missing", "wrong_type", "bot_cannot_post", "nobody_can_view"]
Severity = Literal["error", "warning"]

# Worst first — the order the dashboard lists them in. "missing" outranks the
# rest because a dangling id makes every other question unanswerable.
_CODE_ORDER: tuple[IssueCode, ...] = (
    "missing",
    "wrong_type",
    "bot_cannot_post",
    "nobody_can_view",
)


@dataclass(frozen=True)
class ChannelSnapshot:
    """One configured channel, resolved against live Discord state.

    ``total_humans`` is the number of non-bot members the caller could see when
    it built this. It exists so the visibility rule can tell "nobody can view
    this channel" apart from "I couldn't enumerate anybody" — an unpopulated
    member cache must not be reported to an admin as a broken channel.
    """

    key: str  # config key, e.g. "bios_channel_id"
    label: str  # human label, e.g. "Bios channel"
    panel: str  # where to fix it, e.g. "Config → Bios"
    channel_id: int
    exists: bool
    channel_name: str = ""
    #: False for categories, voice channels and anything else that can't take
    #: a message — only meaningful when ``exists``.
    accepts_messages: bool = True
    #: Non-bot members who can actually ``view_channel``. Only ever compared
    #: against zero, so a caller may stop counting once it finds one (see
    #: ``channel_health._human_viewers``) — treat a positive value as "some",
    #: not as a total.
    human_viewers: int = 0
    #: Non-bot members the caller enumerated. 0 ⇒ visibility is unknowable.
    total_humans: int = 0
    bot_can_view: bool = False
    bot_can_send: bool = False
    bot_can_embed: bool = False
    #: Only meaningful for settings that expect a category (see
    #: :func:`diagnose_category`) — a feature that carves out per-member
    #: channels needs somewhere to put them.
    is_category: bool = False
    bot_can_manage_channels: bool = False


@dataclass(frozen=True)
class ChannelIssue:
    """One problem with one configured channel, ready to render."""

    key: str
    label: str
    panel: str
    channel_id: int
    channel_name: str
    code: IssueCode
    severity: Severity
    message: str

    @property
    def sort_key(self) -> tuple[int, str]:
        return (_CODE_ORDER.index(self.code), self.key)


def _missing_bot_perms(snap: ChannelSnapshot) -> list[str]:
    """Which of the three perms needed to post an embed are absent."""
    return [
        name
        for ok, name in (
            (snap.bot_can_view, "View Channel"),
            (snap.bot_can_send, "Send Messages"),
            (snap.bot_can_embed, "Embed Links"),
        )
        if not ok
    ]


def diagnose_channel(snap: ChannelSnapshot) -> list[ChannelIssue]:
    """Every problem with one channel, worst first (empty when it's healthy).

    A missing channel short-circuits: the remaining checks would all be
    answering questions about a channel that isn't there.
    """

    def issue(code: IssueCode, message: str, severity: Severity = "error") -> ChannelIssue:
        return ChannelIssue(
            key=snap.key,
            label=snap.label,
            panel=snap.panel,
            channel_id=snap.channel_id,
            channel_name=snap.channel_name,
            code=code,
            severity=severity,
            message=message,
        )

    if not snap.exists:
        return [
            issue(
                "missing",
                "This points at a channel that no longer exists. Pick a new one "
                "or clear the setting.",
            )
        ]

    found: list[ChannelIssue] = []

    if not snap.accepts_messages:
        found.append(
            issue(
                "wrong_type",
                "This channel can't receive messages — it looks like a category "
                "or voice channel. Pick a text channel.",
            )
        )

    missing_perms = _missing_bot_perms(snap)
    if missing_perms:
        found.append(
            issue(
                "bot_cannot_post",
                "I can't post here — I'm missing "
                f"{', '.join(missing_perms)}. Grant them in the channel's "
                "permission settings.",
            )
        )

    # Guarded on total_humans: an empty member list means the caller couldn't
    # enumerate anyone, which is not evidence that the channel is hidden.
    if snap.total_humans > 0 and snap.human_viewers == 0:
        found.append(
            issue(
                "nobody_can_view",
                f"Not one of your {snap.total_humans} members can see this "
                "channel, so nothing posted here reaches anybody. Usually this "
                "means @everyone is denied View Channel and the role that "
                "granted it back was deleted.",
            )
        )

    found.sort(key=lambda i: i.sort_key)
    return found


def diagnose_category(snap: ChannelSnapshot) -> list[ChannelIssue]:
    """Rules for a setting that names a *category* to create channels under.

    Deliberately not :func:`diagnose_channel`: a category legitimately takes no
    messages and is usually invisible to members on purpose (the bios wizard
    hides each member's channel from everyone else), so both of those rules
    would be backwards here. What matters instead is that it is a category and
    that the bot may create channels inside it.
    """

    def issue(code: IssueCode, message: str) -> ChannelIssue:
        return ChannelIssue(
            key=snap.key,
            label=snap.label,
            panel=snap.panel,
            channel_id=snap.channel_id,
            channel_name=snap.channel_name,
            code=code,
            severity="error",
            message=message,
        )

    if not snap.exists:
        return [
            issue(
                "missing",
                "This points at a category that no longer exists. Pick a new "
                "one or clear the setting.",
            )
        ]
    if not snap.is_category:
        return [
            issue(
                "wrong_type",
                "This needs to be a category, not a channel — it's where each "
                "member's private channel gets created.",
            )
        ]
    if not snap.bot_can_manage_channels:
        return [
            issue(
                "bot_cannot_post",
                "I can't create channels in this category — I'm missing Manage "
                "Channels. Grant it in the category's permission settings.",
            )
        ]
    return []


def diagnose_all(snaps: list[ChannelSnapshot]) -> list[ChannelIssue]:
    """Diagnose many channels at once, worst-first across all of them."""
    issues: list[ChannelIssue] = []
    for snap in snaps:
        issues.extend(diagnose_channel(snap))
    issues.sort(key=lambda i: i.sort_key)
    return issues


@dataclass(frozen=True)
class GroupedIssue:
    """One fault on one channel, naming every setting that points at it.

    Several settings routinely share a channel (a casino room is both
    ``casino_channel_id`` and ``casino_panel_channel_id``; the bios room is
    both the output channel and the trigger channel). Listing the same broken
    channel once per setting reads as several faults when there is one, and one
    permission fix clears them all.
    """

    channel_id: int
    channel_name: str
    code: IssueCode
    severity: Severity
    message: str
    #: ``(key, label, panel)`` for each setting pointing at this channel.
    settings: tuple[tuple[str, str, str], ...]


def group_by_channel(issues: list[ChannelIssue]) -> list[GroupedIssue]:
    """Collapse per-setting issues into one row per (channel, fault)."""
    order: list[tuple[int, IssueCode]] = []
    grouped: dict[tuple[int, IssueCode], list[ChannelIssue]] = {}
    for issue in issues:
        ident = (issue.channel_id, issue.code)
        if ident not in grouped:
            grouped[ident] = []
            order.append(ident)
        grouped[ident].append(issue)

    out: list[GroupedIssue] = []
    for ident in order:
        members = grouped[ident]
        first = members[0]
        out.append(
            GroupedIssue(
                channel_id=first.channel_id,
                channel_name=first.channel_name,
                code=first.code,
                severity=first.severity,
                message=first.message,
                settings=tuple((m.key, m.label, m.panel) for m in members),
            )
        )
    out.sort(key=lambda g: (_CODE_ORDER.index(g.code), g.settings[0][0]))
    return out


def grouped_issue_to_dict(group: GroupedIssue) -> dict[str, object]:
    """JSON shape for the dashboard. See :func:`issue_to_dict` on snowflakes."""
    return {
        "channel_id": str(group.channel_id),
        "channel_name": group.channel_name,
        "code": group.code,
        "severity": group.severity,
        "message": group.message,
        "settings": [
            {"key": key, "label": label, "panel": panel}
            for (key, label, panel) in group.settings
        ],
    }


def issue_to_dict(issue: ChannelIssue) -> dict[str, object]:
    """JSON shape for the dashboard. ``channel_id`` is a string — snowflakes
    over 2^53 must never leave as bare numbers (see docs/web_testing.md)."""
    return {
        "key": issue.key,
        "label": issue.label,
        "panel": issue.panel,
        "channel_id": str(issue.channel_id),
        "channel_name": issue.channel_name,
        "code": issue.code,
        "severity": issue.severity,
        "message": issue.message,
    }


__all__ = [
    "ChannelIssue",
    "ChannelSnapshot",
    "GroupedIssue",
    "diagnose_all",
    "diagnose_category",
    "diagnose_channel",
    "group_by_channel",
    "grouped_issue_to_dict",
    "issue_to_dict",
]
