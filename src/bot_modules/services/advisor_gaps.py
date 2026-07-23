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
from dataclasses import dataclass

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


def scan_guild(conn: sqlite3.Connection, guild_id: int) -> list[FeatureGap]:
    """Classify every registered feature for one guild, best-suggestion first."""
    values = _load_config(conn, guild_id)
    gaps = [classify_feature(f, values) for f in FEATURES]
    gaps.sort(key=lambda g: (STATUS_ORDER.index(g.status), g.effort, g.feature.label))
    return gaps


def suggestions(conn: sqlite3.Connection, guild_id: int, limit: int = 3) -> list[FeatureGap]:
    """The top few features worth setting up next."""
    return [g for g in scan_guild(conn, guild_id) if g.is_gap][: max(0, limit)]


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
    """
    from bot_modules.core.db_utils import open_db
    from bot_modules.services.advisor_context import can_see_config

    if member is not None and not can_see_config(member):
        return "Not available: only server admins can review setup gaps."
    try:
        with open_db(db_path) as conn:
            gaps = scan_guild(conn, guild_id)
    except Exception:
        log.exception("gap scan failed for guild %s", guild_id)
        return "Couldn't check the server's setup just now — suggest the dashboard."
    return format_gap_report(gaps)
