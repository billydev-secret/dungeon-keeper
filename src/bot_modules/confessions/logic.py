"""Pure decision logic for the confessions cog.

Everything here takes plain Python values and returns plain Python values
so the unit tests don't need a running Discord client. The cog imports
these helpers and calls them in place of the inline branches that used
to live in modals and interaction listeners.

The DB-layer helpers (config CRUD, anon identity assignment, rate
limits) stay in ``bot_modules/services/confessions_service.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from bot_modules.services.confessions_service import (
    CONFESSION_HEADER_LENGTH,
    MAX_DISCORD_MESSAGE_LENGTH,
    MIN_REPLY_COOLDOWN_SECONDS,
    jump_link,
)

HELP_TEXT = (
    "**Confess** — posts your message anonymously in the confessions channel. "
    "Nobody, including staff, can see who sent it.\n\n"
    "Once a confession is posted, anyone can reply to it anonymously:\n\n"
    "**🎭 Reply Anonymously** — gives you a consistent identity in that thread. "
    "Your name and color stay the same across all your replies there.\n\n"
    "**🎲 Reply as Someone New** — gives you a one-time random identity for just "
    "that message. A fresh name and color every time you click it."
)


_YES_TOKENS = frozenset({"", "y", "yes", "true", "1", "on"})
_NO_TOKENS = frozenset({"n", "no", "false", "0", "off"})


def parse_notify_pref(raw: str | None) -> Optional[bool]:
    """Parse the modal's ``yes/no`` notify-preference textbox.

    Returns ``True`` for yes-ish values (including empty input — yes is the
    default), ``False`` for no-ish values, and ``None`` when the input is
    something else (so the caller can show a "use yes or no" error).
    """
    pref = str(raw or "").strip().lower()
    if pref in _YES_TOKENS:
        return True
    if pref in _NO_TOKENS:
        return False
    return None


def compute_confession_max_chars(cfg_max_chars: int) -> int:
    """Return the effective character cap for a new confession.

    The cog reserves ``CONFESSION_HEADER_LENGTH`` characters for the
    prefix Discord adds when we post the message, then clamps to the
    guild's configured ``max_chars``. Always returns at least 1.
    """
    return min(cfg_max_chars, max(1, MAX_DISCORD_MESSAGE_LENGTH - CONFESSION_HEADER_LENGTH))


def compute_reply_max_chars(cfg_max_chars: int) -> int:
    """Return the effective character cap for an anonymous reply.

    Replies don't carry the same prefix overhead as confessions, so the
    cap is just ``min(cfg.max_chars, MAX_DISCORD_MESSAGE_LENGTH)``.
    """
    return min(cfg_max_chars, MAX_DISCORD_MESSAGE_LENGTH)


def compute_reply_cooldown(cfg_cooldown_seconds: int) -> int:
    """Return the cooldown to apply between consecutive anonymous replies.

    Replies use half the configured confession cooldown but never less
    than ``MIN_REPLY_COOLDOWN_SECONDS``.
    """
    return max(MIN_REPLY_COOLDOWN_SECONDS, cfg_cooldown_seconds // 2)


@dataclass(frozen=True)
class ThreadRootInfo:
    """The resolved root-message metadata used when posting a reply."""

    root_message_id: int
    parent_author_id: int
    parent_notify_pref: int


def resolve_thread_root_info(
    thread_info: Optional[tuple[int, int, int]],
    *,
    fallback_parent_message_id: int,
    fallback_notify_op_on_reply: bool,
) -> ThreadRootInfo:
    """Normalize the ``get_thread_info`` result to a populated ``ThreadRootInfo``.

    When ``thread_info`` is ``None`` (no DB row for that message), we
    fall back to the message itself as the root, zero out the author id
    (so OP notifications won't fire), and use the guild's default
    notify-OP-on-reply preference.

    When the stored ``notify_original_author`` field is the legacy
    sentinel ``-1`` (anything outside ``{0, 1}``), we fall back to the
    guild's default — old rows didn't store per-author prefs.
    """
    if thread_info is None:
        return ThreadRootInfo(
            root_message_id=fallback_parent_message_id,
            parent_author_id=0,
            parent_notify_pref=1 if fallback_notify_op_on_reply else 0,
        )
    root_message_id, parent_author_id, parent_notify_pref = thread_info
    if parent_notify_pref not in (0, 1):
        parent_notify_pref = 1 if fallback_notify_op_on_reply else 0
    return ThreadRootInfo(
        root_message_id=root_message_id,
        parent_author_id=parent_author_id,
        parent_notify_pref=parent_notify_pref,
    )


def is_op_reply(
    *,
    ephemeral: bool,
    parent_author_id: int,
    replier_id: int,
) -> bool:
    """Return True if the replier is the original confessor.

    Ephemeral ("Reply as Someone New") replies never get the OP badge
    even when the same user clicks them, and a parent_author_id of 0
    (unknown / legacy) never matches.
    """
    return not ephemeral and parent_author_id > 0 and replier_id == parent_author_id


def should_notify_op(
    *,
    parent_author_id: int,
    replier_id: int,
    parent_notify_pref: int,
) -> bool:
    """Return True when the original poster should get a DM about this reply.

    Skips when there's no known parent author, when the replier IS the
    original poster (self-notification is noise), or when the parent
    opted out of notifications.
    """
    return (
        parent_author_id > 0
        and parent_author_id != replier_id
        and bool(parent_notify_pref)
    )


def build_dm_notification_text(
    *,
    guild_name: str,
    guild_id: int,
    reply_channel_id: int,
    reply_message_id: int,
    confession_channel_id: int,
    root_message_id: int,
) -> str:
    """Build the DM body the bot sends to the original confessor on reply."""
    return (
        f"Someone replied to your anonymous confession in **{guild_name}**.\n"
        f"Reply: {jump_link(guild_id, reply_channel_id, reply_message_id)}\n"
        f"Confession: {jump_link(guild_id, confession_channel_id, root_message_id)}"
    )


# ---------------------------------------------------------------------------
# Button custom-id parsing
# ---------------------------------------------------------------------------


ButtonKind = Literal[
    "new_confession",     # nc|<guild_id> — open the ConfessModal
    "reply",              # cr|<root_id> — open ReplyModal (persistent identity)
    "reply_new",          # crn|<root_id> — open ReplyModal (ephemeral identity)
    "reply_help",         # crh|<root_id> — show the help text ephemerally
    "legacy_reply",       # plain "cr" — inspect interaction.message for target
    "invalid",            # known-prefix but malformed — ephemeral error
    "ignore",             # not one of our custom_ids — early return
]


@dataclass(frozen=True)
class ButtonAction:
    """The decoded outcome of inspecting a component interaction custom_id.

    Cases:
      * ``ignore`` — custom_id isn't ours; the listener returns immediately.
      * ``new_confession`` — open the ConfessModal. ``guild_id`` carries the
        embedded guild from the launcher button.
      * ``reply`` / ``reply_new`` / ``reply_help`` — ``root_id`` carries
        the parsed root confession message id.
      * ``legacy_reply`` — the bare ``"cr"`` button from old posts; the
        cog needs to look at ``interaction.message`` directly.
      * ``invalid`` — the prefix was right but the id portion was missing
        or non-numeric; ``error`` is the user-facing message to send.
    """

    kind: ButtonKind
    root_id: Optional[int] = None
    guild_id: Optional[int] = None
    error: Optional[str] = None


def parse_button_custom_id(custom_id: object) -> ButtonAction:
    """Decode a component-interaction ``custom_id`` into a tagged action.

    Returns an ``ignore`` action for anything outside the
    ``nc|`` / ``cr`` / ``cr|`` / ``crn|`` / ``crh|`` family so unrelated
    component interactions in the same listener short-circuit cleanly.
    """
    if not isinstance(custom_id, str):
        return ButtonAction(kind="ignore")

    if custom_id.startswith("nc|"):
        parts = custom_id.split("|")
        if len(parts) != 2 or not parts[1].isdigit():
            return ButtonAction(kind="invalid", error="Invalid confession button.")
        return ButtonAction(kind="new_confession", guild_id=int(parts[1]))

    if custom_id.startswith("crh|"):
        parts = custom_id.split("|")
        if len(parts) != 2 or not parts[1].isdigit():
            return ButtonAction(kind="invalid", error="Invalid reply button.")
        return ButtonAction(kind="reply_help", root_id=int(parts[1]))

    if custom_id.startswith("crn|"):
        parts = custom_id.split("|")
        if len(parts) != 2 or not parts[1].isdigit():
            return ButtonAction(kind="invalid", error="Invalid reply button.")
        return ButtonAction(kind="reply_new", root_id=int(parts[1]))

    if custom_id.startswith("cr|"):
        parts = custom_id.split("|")
        if len(parts) != 2 or not parts[1].isdigit():
            return ButtonAction(kind="invalid", error="Invalid reply button.")
        return ButtonAction(kind="reply", root_id=int(parts[1]))

    if custom_id == "cr":
        return ButtonAction(kind="legacy_reply")

    return ButtonAction(kind="ignore")


# ---------------------------------------------------------------------------
# Component-shape inspection
# ---------------------------------------------------------------------------


def _iter_component_custom_ids(components) -> list[str]:
    """Yield every ``custom_id`` string nested inside a message components tree."""
    out: list[str] = []
    for row in components or []:
        for child in getattr(row, "children", []):
            cid = getattr(child, "custom_id", None)
            if isinstance(cid, str):
                out.append(cid)
    return out


def message_has_confess_launcher(components, guild_id: int) -> bool:
    """Return True when a message exposes the confess launcher button for this guild.

    Used to spot duplicate launcher posts that the cog needs to clean up
    after re-posting the canonical one.
    """
    target_id = f"nc|{guild_id}"
    return any(cid == target_id for cid in _iter_component_custom_ids(components))


def message_exposes_reply_buttons(components) -> bool:
    """Return True when a message exposes either anonymous-reply button.

    The legacy ``"cr"`` button fallback uses this to confirm the message
    being right-clicked is one of the bot's reply-capable posts.
    """
    return any(
        cid.startswith("cr|") or cid.startswith("crn|")
        for cid in _iter_component_custom_ids(components)
    )


def is_stale_interaction_error_code(code: object) -> bool:
    """Return True for Discord HTTP error codes meaning "interaction expired".

    ``40060`` — interaction already acknowledged. ``10062`` — unknown
    interaction. Both are silent-skip cases in the cog's broad exception
    handler.
    """
    return code in (40060, 10062)
