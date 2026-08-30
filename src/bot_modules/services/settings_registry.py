"""A declarative inventory of the config settings Billy-bot may reason about.

Dungeon Keeper has ~240 distinct keys in the shared ``config`` KV table spread
across ~138 dashboard panels. Two jobs need to know more about a key than "what
string is stored under it":

* **Writes.** ``advisor_actions.validate_config_change`` used to infer a key's
  shape from its *currently stored value*, which meant it could only touch keys
  that already had a row. A brand-new feature has no rows, so the one case that
  matters for adoption — "turn this on for me" — was the one case it refused.
  With a schema, shape comes from the registry and an unset key is writable.
* **Gap detection.** "What isn't this server using?" can't be answered from the
  DB alone: an absent row and a never-existed key look identical. The registry
  is what makes absence meaningful (see ``advisor_gaps``).

This file is **hand-authored on purpose**. It is a curated list of what an admin
should be nudged toward, not a dump of every key — an auto-derived inventory
would be mostly noise (cursors, message ids, cache hashes) and would happily
expose keys nobody vetted for model access.

Two safety rules govern entries:

* ``writable`` is **opt-in per setting**. The human Apply gate in
  ``advisor_actions`` is the only thing between a prompt-injected channel topic
  or server doc and a config write, so widening what model output can propose is
  deliberate, never a default.
* ``admin_only`` raises the bar for settings that grant access or moderation
  authority (the jailed role, NSFW/veil access roles, who may mark Q&A answers),
  **or that route confidential data to a channel** (the whisper mod log, which
  names the sender behind an anonymous whisper; the jail/ticket transcript
  archive). The second clause was added 2026-08-06: the tier used to be defined
  by authority alone, which left two keys whose whole risk is disclosure sitting
  at the ordinary ``manage_guild`` bar. These are proposable, but only for an
  asker with full ``administrator`` — not merely ``manage_guild``, which is all
  the ordinary settings gate requires — and the check is re-run against whoever
  clicks Apply, not just whoever asked.
* Keys that define the **top-level permission boundary** (``admin_role_ids``,
  ``mod_role_ids``) or a **privacy default** (``message_storage_level``) are
  never writable at any confirmation level. Handing out admin, or widening what
  message data is retained, is not something an Apply button should be able to
  do — a mistaken click there is unrecoverable in a way the rest aren't.

Defaults are recorded only where a gap check needs them. Most live in each
feature's own service module, and duplicating them here would just create drift;
"is this configured?" leans on whether the ``required`` keys are set, not on an
exhaustive comparison against every default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Mirrors advisor_context / advisor_actions. A registry entry matching this is a
# bug in the registry, so it's asserted at import rather than filtered silently.
_SECRET_KEY_RE = re.compile(
    r"token|secret|refresh|password|passwd|api[_-]?key|webhook|oauth|credential",
    re.I,
)

# Never writable by the model regardless of the per-setting flag — enforced in
# _check_registry below. Deliberately short: everything else that grants access
# or authority is writable behind ``admin_only`` instead.
PRIVILEGE_KEYS: frozenset[str] = frozenset({
    "admin_role_ids",       # handing out admin
    "mod_role_ids",         # handing out moderation
    "message_storage_level",  # privacy default: what message data is retained
})

# Rows that still exist in `config` on live servers but that no code reads any
# more. They look exactly like real settings, so without this guard a future
# pass could reasonably add them — and then an admin would click Apply, the
# write would succeed, and nothing whatsoever would change. Worse than
# refusing.
#
#   nsfw/denizen/veteran_role_id — superseded by the `grant_roles` table
#                                  (see advisor_actions.validate_grant_role_change)
#   veil_*                       — Veil was renamed to Guess in migration 020
DEAD_KEYS: frozenset[str] = frozenset({
    "nsfw_role_id",
    "denizen_role_id",
    "veteran_role_id",
    "veil_role_id",
    "veil_channel_id",
})

KINDS = frozenset({"channel", "role", "bool", "int", "text"})


@dataclass(frozen=True)
class Setting:
    """One config KV key, described well enough to validate and to miss."""

    key: str
    label: str
    kind: str
    #: Part of the feature's minimum viable setup — if unset, the feature is
    #: not usable, and gap detection reports the feature as unconfigured.
    required: bool = False
    #: Whether Billy-bot may propose a change (still human-confirmed).
    writable: bool = False
    #: Requires full ``administrator`` to propose *and* to apply, rather than
    #: the ``manage_guild`` that ordinary settings accept. For anything that
    #: grants access or moderation authority.
    admin_only: bool = False
    #: Value meaning "not configured". Channels/roles use "0" by convention.
    default: str | None = None
    #: Inclusive bounds for ``kind == "int"``.
    minimum: int | None = None
    maximum: int | None = None
    #: Allowed values for ``kind == "text"`` when it's really an enum.
    choices: tuple[str, ...] | None = None
    #: One line the model can quote when explaining the setting.
    help: str = ""

    def is_set(self, raw: str | None) -> bool:
        """Whether a stored value counts as configured."""
        if raw is None:
            return False
        val = raw.strip()
        if not val:
            return False
        if self.kind in ("channel", "role") and val in ("0", "-1"):
            return False
        if self.kind == "bool":
            return val not in ("0", "false", "off", "no")
        if self.default is not None and val == self.default:
            return False
        return True


@dataclass(frozen=True)
class Feature:
    """A group of settings an admin turns on together."""

    slug: str
    label: str
    panel: str
    #: One sentence on what the feature gives the server — this is what a
    #: suggestion shows an admin who has never heard of it.
    blurb: str
    settings: tuple[Setting, ...]
    #: The on/off key, when the feature has one. Listed in ``settings`` too.
    enable_key: str | None = None
    #: Feature-table settings this feature also has, which the model can read
    #: (via get_server_settings) but never write. Named for honest answers.
    extra_panel_only: tuple[str, ...] = field(default_factory=tuple)

    def required_settings(self) -> tuple[Setting, ...]:
        return tuple(s for s in self.settings if s.required)


def _ch(key, label, *, required=False, writable=True, admin_only=False, help="") -> Setting:
    return Setting(key, label, "channel", required=required, writable=writable,
                   admin_only=admin_only, default="0", help=help)


def _role(key, label, *, required=False, writable=True, admin_only=False, help="") -> Setting:
    return Setting(key, label, "role", required=required, writable=writable,
                   admin_only=admin_only, default="0", help=help)


def _flag(key, label, *, required=False, writable=True, default="0", help="") -> Setting:
    return Setting(key, label, "bool", required=required, writable=writable,
                   default=default, help=help)


def _num(key, label, *, minimum=None, maximum=None, required=False,
         writable=True, default=None, help="") -> Setting:
    return Setting(key, label, "int", required=required, writable=writable,
                   default=default, minimum=minimum, maximum=maximum, help=help)


def _text(key, label, *, required=False, writable=True, choices=None, help="") -> Setting:
    return Setting(key, label, "text", required=required, writable=writable,
                   choices=choices, help=help)


FEATURES: tuple[Feature, ...] = (
    Feature(
        slug="welcome",
        label="Welcome messages",
        panel="Config → Welcome",
        blurb="Greets every new member with a message in a channel you pick.",
        settings=(
            _ch("welcome_channel_id", "Welcome channel", required=True,
                help="Where the welcome message is posted."),
            _text("welcome_message", "Welcome message",
                  help="Supports placeholders for the member's name."),
            _flag("welcome_ping_member", "Ping the new member"),
            # Ping-only: naming this role grants nothing.
            _role("welcome_ping_role_id", "Role to ping on join",
                  help="Left untouched, the bot makes a @Welcome Ping role "
                       "the first time it greets someone. Setting it to "
                       "(none) turns the ping off for good."),
            # Closed choice: enforcement matches only these exact strings —
            # anything else silently disables welcome messages.
            _text("welcome_trigger", "What triggers the welcome",
                  choices=("join", "verified")),
            _role("unverified_role_id", "Unverified role", admin_only=True,
                  help="Losing this role is what counts as passing the gate — "
                       "full admin only."),
        ),
    ),
    Feature(
        slug="goodbye",
        label="Leave messages",
        panel="Config → Welcome",
        blurb="Posts a note when someone leaves, so the room notices.",
        settings=(
            _ch("leave_channel_id", "Leave channel", required=True),
            _text("leave_message", "Leave message"),
        ),
    ),
    Feature(
        slug="birthdays",
        label="Birthdays",
        panel="Config → Birthdays",
        blurb="Members register a birthday and the bot celebrates it on the day.",
        settings=(
            _ch("birthday_channel_id", "Birthday announcement channel", required=True),
            _text("birthday_message", "Birthday message"),
            _flag("birthday_pin", "Pin the birthday post"),
        ),
    ),
    # The qa_* keys (QA Tracker, Dev → QA Tracker) are deliberately NOT here.
    # The entry they had described a fictional "Q&A rewards" feature, nudged
    # every unconfigured guild to set up what is dev tooling, and was a second
    # write path with 10-100x the panel's bounds — the 2026-08-29 IA audit's
    # duplicated-control finding. The panel (PUT /api/qa/settings) is the sole
    # writer; the advisor still *sees* the settings via the advisor_context
    # feature loader. Pinned by tests/test_settings_registry_contract.py.
    Feature(
        slug="greeting_watch",
        label="Greeting watch",
        panel="Config → Greeting watch",
        blurb="Nudges a greeter when a newcomer says hello and nobody replies.",
        enable_key="greeting_watch_enabled",
        settings=(
            _flag("greeting_watch_enabled", "Greeting watch on", required=True),
            _text("greeting_watch_channel_ids", "Watched channels", required=True,
                  writable=False, help="Set from the panel — it's a channel list."),
            _num("greeting_watch_window_minutes", "Minutes before nudging",
                 minimum=1, maximum=1440),
            _text("greeting_watch_extra_words", "Extra greeting words",
                  help="Comma-separated server-specific greetings matched at "
                       "the start of a message, e.g. server in-jokes."),
            _ch("greeter_chat_channel_id", "Fallback chat channel"),
            _role("greeter_role_id", "Greeter role", admin_only=True,
                  help="Who gets nudged to greet — full admin only."),
        ),
    ),
    Feature(
        slug="intake",
        label="Intake cards",
        panel="Config → Intake Cards",
        blurb="Posts a live welcome-checklist card in greeter chat for every newcomer.",
        enable_key="intake_enabled",
        settings=(
            _flag("intake_enabled", "Intake cards on", required=True),
            _ch("intake_channel_id", "Card channel", required=True,
                help="Where cards post; the greeting watch's fallback chat "
                     "channel (greeter_chat_channel_id) is used when unset. "
                     "Setting it here doesn't make a channel count as a "
                     "greeting place — that's resolved separately, from "
                     "greeter chat then the welcome channel."),
            _role("intake_verified_role_id", "Verified role", admin_only=True,
                  help="Role your verification bot grants on success (e.g. "
                       "Double Counter). Gaining it ticks the Verified step; "
                       "losing the unverified role does too."),
            _text("intake_completion_code", "Completion code",
                  help="Posting a welcome message containing this phrase and "
                       "@mentioning the newcomer completes their card."),
            _num("intake_stale_hours", "Hours before a stale-card nudge",
                 minimum=1, maximum=720),
            _ch("intake_reference_channel_id", "Procedure reference channel",
                writable=False,
                help="Set from the panel — its save is what syncs the "
                     "procedure blocks into the new channel; a bare config "
                     "write leaves the channel empty until someone re-saves."),
        ),
        extra_panel_only=("checklist step editor", "procedure reference blocks"),
    ),
    Feature(
        slug="rules_watch",
        label="Rules watch",
        panel="Config → Rules watch",
        blurb="Points members at the server guide when they ask a rules question.",
        enable_key="rules_watch_enabled",
        settings=(
            _flag("rules_watch_enabled", "Rules watch on", required=True),
            _ch("server_guide_channel_id", "Server guide channel", required=True),
        ),
    ),
    Feature(
        slug="inactivity",
        label="Inactivity prune",
        panel="Config → Inactivity",
        blurb="Flags members who've gone quiet so you can nudge or prune them.",
        enable_key="inactive_auto_sweep",
        settings=(
            _flag("inactive_auto_sweep", "Automatic sweep"),
            # Channel/role are set through POST /config/inactive/channel,
            # which creates the role and re-plumbs channel permissions; a
            # bare config write leaves the old channel visible to inactive
            # members forever, so neither is model-writable.
            _ch("inactive_channel_id", "Sleeper report channel", required=True,
                writable=False,
                help="Set from the panel — its setup flow moves the channel "
                     "permissions with it."),
            _role("inactive_role_id", "Role applied to inactive members",
                  writable=False,
                  help="Created and plumbed by the panel's setup flow — "
                       "panel only."),
            _num("inactive_threshold_days", "Days of silence before flagging",
                 minimum=1, maximum=3650),
            # 1-200 mirrors PUT /config/inactive's clamp; the enforcing
            # reader clamps only the lower bound.
            _num("inactive_sweep_cap", "Max members per sweep", minimum=1, maximum=200),
        ),
    ),
    Feature(
        slug="tickets",
        label="Support tickets",
        panel="Config → Tickets",
        blurb="Gives members a button that opens a private ticket channel for staff.",
        settings=(
            _ch("ticket_panel_channel_id", "Ticket panel channel", required=True),
            _num("ticket_category_id", "Ticket category", required=True, writable=False,
                 help="A category, not a text channel — set it from the panel."),
            _ch("transcript_channel_id", "Transcript archive channel",
                admin_only=True,
                help="Where closed tickets and jail transcripts are archived. "
                     "Pointing it at a channel members can read publishes them "
                     "— full admin only."),
            _flag("ticket_notify_on_create", "Notify staff on new tickets"),
        ),
    ),
    Feature(
        slug="jail",
        label="Jail",
        panel="Config → Jail",
        blurb="Quarantines a rule-breaker into a restricted channel instead of banning.",
        settings=(
            _num("jail_category_id", "Jail category", required=True, writable=False,
                 help="A category — set it from the panel."),
            _role("jailed_role_id", "Jailed role", required=True, admin_only=True,
                  help="Controls what a jailed member can reach — full admin only."),
        ),
    ),
    Feature(
        slug="whisper",
        label="Whisper",
        panel="Config → Whisper",
        blurb="Lets members send anonymous notes through the bot, with a mod log.",
        settings=(
            _ch("whisper_channel_id", "Whisper channel", required=True),
            _ch("whisper_log_channel_id", "Whisper mod log", admin_only=True,
                help="This log names the sender behind each anonymous whisper. "
                     "Pointing it at a channel members can read deanonymizes "
                     "the feature — full admin only."),
            _role("whisper_role_id", "Role allowed to whisper", admin_only=True,
                  help="Gates who can send anonymous notes — full admin only."),
        ),
    ),
    Feature(
        slug="voice_master",
        label="Voice Control",
        panel="Config → Voice Control",
        blurb="Members get their own temporary voice channel by joining a hub.",
        settings=(
            _ch("voice_master_hub_channel_id", "Hub voice channel", required=True,
                help="Joining this channel creates a personal room."),
            _num("voice_master_category_id", "Category for created rooms",
                 required=True, writable=False, help="A category — set it from the panel."),
            _ch("voice_master_control_channel_id", "Control panel channel"),
            _num("voice_master_max_per_member", "Rooms one member may own",
                 minimum=1, maximum=10),
            _flag("voice_master_post_inline_panel", "Post the control panel inline"),
        ),
        extra_panel_only=("access dial", "default bitrate", "saveable fields"),
    ),
    Feature(
        slug="logging",
        label="Logging channels",
        panel="Config → Logging",
        blurb="Sends moderation and join/leave activity to channels you can watch.",
        settings=(
            _ch("log_channel_id", "General log channel", required=True),
            _ch("mod_channel_id", "Moderation channel"),
            _ch("join_leave_log_channel_id", "Join/leave log"),
        ),
    ),
    Feature(
        slug="guess",
        label="Guess game",
        panel="Config → Guess",
        blurb="A rolling image-guessing game members can play in one channel.",
        settings=(
            _ch("guess_channel_id", "Guess channel", required=True),
            _role("guess_role_id", "Role pinged for new rounds"),
            _num("guess_guess_cooldown_seconds", "Seconds between guesses",
                 minimum=0, maximum=3600),
            _num("guess_inactivity_ping_hours", "Hours of silence before a nudge",
                 minimum=0, maximum=720),
        ),
    ),
    Feature(
        slug="starboard_bios",
        label="Member bios",
        panel="Config → Bios",
        blurb="Members write a profile the server can browse.",
        settings=(
            _ch("bios_channel_id", "Bios channel", required=True),
            # Bounds mirror routes/bios.py BiosConfigBody — pinned by
            # tests/test_settings_registry_contract.py. archive_grace is
            # SECONDS the wizard room stays open, not days: the old "days"
            # label made an advisor Apply an 86,400x error.
            _num("bios_questions_per_bio", "Questions per bio", minimum=1, maximum=10),
            _num("bios_archive_grace", "Seconds the bio wizard room stays open",
                 minimum=0, maximum=3600),
        ),
    ),
    Feature(
        slug="needle",
        label="Needle (unanswered threads)",
        panel="Config → Needle",
        blurb="Marks threads nobody has answered so they don't get lost.",
        settings=(
            _text("needle_default_reply", "Default reply", required=True),
            _text("needle_emoji_unanswered", "Unanswered marker"),
            _text("needle_emoji_locked", "Locked marker"),
            _text("needle_emoji_archived", "Archived marker"),
        ),
    ),
    Feature(
        slug="event_echo",
        label="Event Echo",
        panel="Config → Event Echo",
        blurb="Mirrors a starting game into main chat with a link to jump to it.",
        settings=(
            _ch("event_echo_channel_id", "Echo destination", required=True,
                help="Where echoes are posted. Unset means the feature is off."),
        ),
    ),
    Feature(
        slug="billy_bot",
        label="Billy-bot",
        panel="Config → Billy-bot",
        blurb="The AI helper behind /ask — it can use this server's own context.",
        settings=(
            _flag("advisor_server_context", "Use live server context"),
            _flag("advisor_config_tools", "On-demand settings lookup", default="1"),
        ),
    ),
)

FEATURES_BY_SLUG: dict[str, Feature] = {f.slug: f for f in FEATURES}
SETTINGS_BY_KEY: dict[str, Setting] = {s.key: s for f in FEATURES for s in f.settings}


def _check_registry() -> None:
    """Fail at import on a registry that violates its own safety rules."""
    seen: set[str] = set()
    for feature in FEATURES:
        for s in feature.settings:
            if s.kind not in KINDS:
                raise ValueError(f"{s.key}: unknown kind {s.kind!r}")
            if _SECRET_KEY_RE.search(s.key):
                raise ValueError(f"{s.key}: secret-shaped key must not be in the registry")
            if s.writable and s.key in PRIVILEGE_KEYS:
                raise ValueError(f"{s.key}: privilege key must never be model-writable")
            if s.admin_only and not s.writable:
                raise ValueError(f"{s.key}: admin_only is meaningless without writable")
            if s.key in DEAD_KEYS:
                raise ValueError(
                    f"{s.key}: nothing reads this key — writing it would be a no-op"
                )
            if s.key in seen:
                raise ValueError(f"{s.key}: listed in two features")
            seen.add(s.key)
        if feature.enable_key and feature.enable_key not in {
            s.key for s in feature.settings
        }:
            raise ValueError(f"{feature.slug}: enable_key not among its settings")


_check_registry()


def get_setting(key: str) -> Setting | None:
    """Look up one setting's schema by exact key."""
    return SETTINGS_BY_KEY.get((key or "").strip())


def writable_keys(*, is_admin: bool = True) -> frozenset[str]:
    """Every key Billy-bot may propose a change to for this asker.

    ``is_admin`` is full ``administrator``; a ``manage_guild``-only asker sees
    the same list minus the settings that grant access or authority.
    """
    return frozenset(
        k
        for k, s in SETTINGS_BY_KEY.items()
        if s.writable and (is_admin or not s.admin_only)
    )


def feature_for_key(key: str) -> Feature | None:
    """Which feature a key belongs to (for 'open this panel' answers)."""
    key = (key or "").strip()
    for feature in FEATURES:
        if any(s.key == key for s in feature.settings):
            return feature
    return None


def coerce_value(setting: Setting, raw: str) -> str:
    """Normalize a non-channel/role value against its schema.

    Channels and roles need the guild to resolve a name to an id, so they stay
    in ``advisor_actions``; everything else is decidable from the schema alone.
    Raises ``ValueError`` with a model-readable reason.
    """
    val = (raw or "").strip()
    if not val:
        raise ValueError("a value is required")
    if setting.kind == "bool":
        low = val.casefold()
        if low in ("1", "on", "true", "yes", "enable", "enabled"):
            return "1"
        if low in ("0", "off", "false", "no", "disable", "disabled"):
            return "0"
        raise ValueError(f"'{setting.key}' is an on/off setting — say on or off")
    if setting.kind == "int":
        try:
            num = int(val.replace(",", ""))
        except ValueError:
            raise ValueError(f"'{setting.key}' expects a whole number") from None
        if setting.minimum is not None and num < setting.minimum:
            raise ValueError(f"'{setting.key}' can't be below {setting.minimum}")
        if setting.maximum is not None and num > setting.maximum:
            raise ValueError(f"'{setting.key}' can't be above {setting.maximum}")
        return str(num)
    if setting.choices and val not in setting.choices:
        raise ValueError(
            f"'{setting.key}' must be one of: {', '.join(setting.choices)}"
        )
    return val
