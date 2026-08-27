"""What this server *isn't* using — the data behind Billy-bot's setup help.

Asking Billy-bot a question requires already knowing the feature exists, which
is exactly what an admin who's never opened the Chat Revive panel doesn't know.
This module answers the question they can't phrase: "what am I missing?"

The DB alone can't answer it — an absent row and a key that was never a setting
look identical. ``settings_registry`` supplies the list of things that *could*
be configured; this compares it against what is, and classifies each feature:

``ready_but_off``
    Every required setting is filled in, but the feature's on/off key is off.
    The best kind of suggestion: all the work is already done.
``partial``
    Some required settings are set, others aren't. Half-built, probably
    abandoned mid-setup, and currently doing nothing.
``unconfigured``
    Nothing is set. Either deliberately unwanted or never discovered — this
    module can't tell which, so suggestions stay suggestions.
``configured``
    Set up and on. Not reported as a gap.

Reads are guild-scoped with the same legacy ``guild_id = 0`` fallback the rest
of the config layer uses, so a server configured before per-guild keys existed
doesn't show up as one giant gap.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, replace

from bot_modules.services.settings_registry import FEATURES, Feature, Setting

log = logging.getLogger(__name__)

# Worst-first: a suggestion is more useful the less work it implies.
STATUS_ORDER = ("ready_but_off", "partial", "unconfigured", "configured")
_GAP_STATUSES = frozenset({"ready_but_off", "partial", "unconfigured"})


@dataclass(frozen=True)
class FeatureGap:
    """One feature's setup state on one guild.

    ``missing``/``present`` describe *wiring* — the required settings other than
    the on/off switch. Whether the switch is flipped is carried by ``status``
    and ``switch_on`` instead, so a fully-wired feature that's merely switched
    off doesn't report its own toggle as a missing setting.
    """

    feature: Feature
    status: str
    #: Required non-switch settings with nothing usable stored.
    missing: tuple[Setting, ...]
    #: Required non-switch settings that are filled in.
    present: tuple[Setting, ...]
    #: Switch state, or None when the feature has no on/off key.
    switch_on: bool | None = None
    #: The server has decided not to use this feature. Guild-level and
    #: permanent until an admin restores it; suggestions hide these, the full
    #: scan still reports them so nothing goes silently missing.
    dismissed: bool = False

    @property
    def is_gap(self) -> bool:
        return self.status in _GAP_STATUSES

    @property
    def effort(self) -> int:
        """How many settings still need a value before the feature works."""
        return len(self.missing)


def _load_config(conn: sqlite3.Connection, guild_id: int) -> dict[str, str]:
    """Every config value visible to this guild, guild-specific winning over 0."""
    values: dict[str, str] = {}
    try:
        # guild_id=0 first so the guild's own rows overwrite the legacy fallback.
        for gid in (0, guild_id):
            for row in conn.execute(
                "SELECT key, value FROM config WHERE guild_id = ?", (gid,)
            ):
                if row["value"] is not None:
                    values[str(row["key"])] = str(row["value"])
    except sqlite3.Error:
        log.exception("gap scan: config read failed for guild %s", guild_id)
    return values


def classify_feature(feature: Feature, values: dict[str, str]) -> FeatureGap:
    """Bucket one feature by how much of its required setup exists."""
    enable = feature.enable_key
    # The enable key is judged separately from the wiring, so a fully-wired but
    # switched-off feature reads as the cheap win it is rather than as "partial".
    switch_on: bool | None = None
    if enable is not None:
        enable_setting = next((s for s in feature.settings if s.key == enable), None)
        if enable_setting is not None:
            switch_on = enable_setting.is_set(values.get(enable))

    wiring = tuple(s for s in feature.required_settings() if s.key != enable)
    missing = tuple(s for s in wiring if not s.is_set(values.get(s.key)))
    present = tuple(s for s in wiring if s not in missing)

    if not missing:
        status = "ready_but_off" if switch_on is False else "configured"
    elif not present:
        status = "unconfigured"
    else:
        status = "partial"

    return FeatureGap(feature, status, missing, present, switch_on)


# ---------------------------------------------------------------------------
# Dismissal — "this server has decided not to use that"
# ---------------------------------------------------------------------------
#
# A gap is recomputed on every load, so without this the same three rows came
# back forever and the features behind them never surfaced. Dismissal is keyed
# by guild alone: it records what the *server* wants, not what one admin is
# tired of seeing, so it holds for every admin and needs no per-user column
# (and therefore no data_register row).


def dismissed_slugs(conn: sqlite3.Connection, guild_id: int) -> set[str]:
    """Feature slugs this guild has cleared. Unknown slugs are ignored."""
    known = {f.slug for f in FEATURES}
    try:
        rows = conn.execute(
            "SELECT feature_key FROM setup_suggestion_dismissals WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    except sqlite3.Error:
        # A table this young may be missing on a DB that hasn't migrated yet.
        # Suggestions are advisory: degrade to "nothing dismissed" rather than
        # taking the tile down.
        log.exception("gap scan: dismissal read failed for guild %s", guild_id)
        return set()
    # A row for a feature that has since been renamed or removed is inert
    # rather than an error — it simply matches nothing.
    return {str(r[0]) for r in rows} & known


def dismiss(conn: sqlite3.Connection, guild_id: int, slug: str, *, now: float | None = None) -> bool:
    """Clear one suggestion for this guild. False if the slug isn't a feature."""
    if slug not in {f.slug for f in FEATURES}:
        return False
    conn.execute(
        "INSERT OR REPLACE INTO setup_suggestion_dismissals "
        "(guild_id, feature_key, dismissed_at) VALUES (?, ?, ?)",
        (guild_id, slug, now if now is not None else time.time()),
    )
    return True


def restore(conn: sqlite3.Connection, guild_id: int, slug: str) -> bool:
    """Bring a dismissed suggestion back. False if the slug isn't a feature."""
    if slug not in {f.slug for f in FEATURES}:
        return False
    conn.execute(
        "DELETE FROM setup_suggestion_dismissals WHERE guild_id = ? AND feature_key = ?",
        (guild_id, slug),
    )
    return True


def scan_guild(conn: sqlite3.Connection, guild_id: int) -> list[FeatureGap]:
    """Classify every registered feature for one guild, best-suggestion first.

    Dismissed features are *marked*, not dropped: this is the full picture, and
    an admin (or Billy-bot) asking "what am I missing?" should still be told a
    feature exists and that the server chose to pass on it.
    """
    values = _load_config(conn, guild_id)
    cleared = dismissed_slugs(conn, guild_id)
    gaps = [
        replace(classify_feature(f, values), dismissed=f.slug in cleared)
        for f in FEATURES
    ]
    gaps.sort(key=lambda g: (STATUS_ORDER.index(g.status), g.effort, g.feature.label))
    return gaps


def suggestions(
    conn: sqlite3.Connection,
    guild_id: int,
    limit: int = 3,
    *,
    include_dismissed: bool = False,
) -> list[FeatureGap]:
    """The top few features worth setting up next.

    ``include_dismissed`` is for the manage view, which has to show a cleared
    row before it can offer to restore it. The tile never asks for them.
    """
    gaps = [g for g in scan_guild(conn, guild_id) if g.is_gap]
    if not include_dismissed:
        gaps = [g for g in gaps if not g.dismissed]
    return gaps[: max(0, limit)]


# ---------------------------------------------------------------------------
# Rendering — the tool result Billy-bot reads
# ---------------------------------------------------------------------------

_STATUS_BLURB = {
    "ready_but_off": "fully set up but switched OFF — just needs turning on",
    "partial": "half set up — some required settings are still empty",
    "unconfigured": "not set up at all",
}

_MAX_REPORT_CHARS = 4000


def format_gap_report(gaps: list[FeatureGap], *, include_configured: bool = False) -> str:
    """Render a scan as text for the model.

    Each gap names the feature, what it gives the server, what's missing (by
    key, so the model can propose values for them), and which panel owns it.
    """
    lines: list[str] = []
    reported = [g for g in gaps if g.is_gap or include_configured]
    if not reported:
        return "Every feature I track is already set up on this server."

    for gap in reported:
        f = gap.feature
        if gap.status == "configured":
            lines.append(f"- {f.label}: set up and running.")
            continue
        blurb = _STATUS_BLURB[gap.status]
        if gap.status == "unconfigured" and gap.switch_on:
            # Saying "not set up at all" about a feature whose switch someone
            # deliberately flipped reads wrong — it was started, not ignored.
            blurb = "switched on, but nothing is wired up behind it yet"
        if gap.dismissed:
            # Marked, not hidden: the model shouldn't keep pushing a feature the
            # server has passed on, but it also shouldn't pretend the feature
            # doesn't exist when an admin asks what they're missing.
            blurb += " — the server has dismissed this suggestion"
        lines.append(f"- {f.label} — {blurb}")
        lines.append(f"    What it does: {f.blurb}")
        if gap.missing:
            needed = ", ".join(f"{s.key} ({s.label})" for s in gap.missing)
            lines.append(f"    Still needs: {needed}")
        if gap.present:
            done = ", ".join(s.label for s in gap.present)
            lines.append(f"    Already set: {done}")
        lines.append(f"    Panel: {f.panel}")
        if f.extra_panel_only:
            lines.append(
                f"    Dashboard-only extras: {', '.join(f.extra_panel_only)}"
            )

    text = "\n".join(lines)
    if len(text) > _MAX_REPORT_CHARS:
        text = text[:_MAX_REPORT_CHARS].rsplit("\n", 1)[0] + "\n(…more not shown)"
    return text


def fetch_setup_gaps(db_path, guild_id: int, member=None) -> str:
    """Handler behind Billy-bot's ``find_setup_gaps`` tool.

    Admin-gated like the settings reads: knowing exactly which features a server
    hasn't set up is reconnaissance a regular member has no business getting.
    Returns model-readable text in every case, errors included.

    Fails **closed** on an unresolved member: ``can_see_config(None)`` is False,
    matching ``fetch_feature_settings`` and ``validate_config_change``'s
    ``is_admin=False`` default. It used to skip the check entirely when
    ``member`` was None — unreachable, since both surfaces build the tool only
    for a resolved admin, but the wrong default for the one gate in this module.
    """
    from bot_modules.core.db_utils import open_db
    from bot_modules.services.advisor_context import can_see_config

    if not can_see_config(member):
        return "Not available: only server admins can review setup gaps."
    try:
        with open_db(db_path) as conn:
            gaps = scan_guild(conn, guild_id)
    except Exception:
        log.exception("gap scan failed for guild %s", guild_id)
        return "Couldn't check the server's setup just now — suggest the dashboard."
    return format_gap_report(gaps)
