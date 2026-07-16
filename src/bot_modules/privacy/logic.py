"""Pure helpers for the data-deletion flow — no Discord, no DB.

These functions exist so the bookkeeping inside ``_delete_discord_messages``
can be unit-tested without standing up a fake guild. The cog still owns the
network calls (``channel.delete_messages``, ``fetch_message``, etc.) —
everything testable about *what to delete* and *how to report progress*
lives here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import discord

# Discord's bulk-delete API only accepts messages younger than 14 days; older
# messages have to be deleted one at a time. The partition helper below uses
# this to split a per-channel batch into "recent" (bulk-eligible) and "old"
# (single-shot) lists.
FOURTEEN_DAYS = timedelta(days=14)

# Default width of the textual progress bar shown while deletion runs.
_DEFAULT_BAR_WIDTH = 20

# Deletion scope. "all" is the original erasure: every message goes and the
# account's DB records are purged. "media"/"text" are *scrubs* — they remove
# only that slice of the member's Discord messages and leave XP, activity and
# profile intact, so clearing your photos doesn't cost you your level.
MODE_ALL = "all"
MODE_MEDIA = "media"
MODE_TEXT = "text"

PARTIAL_MODES = frozenset({MODE_MEDIA, MODE_TEXT})

# Discord auto-generates an embed for any link a member posts, so embed
# presence alone doesn't mean media. Only these types are real media; a
# link preview ("link"/"article"/"rich") leaves the message plain text.
_MEDIA_EMBED_TYPES = frozenset({"image", "video", "gifv"})


def message_has_media(message: object) -> bool:
    """True when *message* carries real media rather than plain text.

    Counts uploads (attachments, stickers) and media embeds. A posted image
    *URL* has no attachment but does produce an ``image``-type embed, so it
    counts too — from the member's point of view they posted a picture either
    way. A bare link with an article/rich preview does not.
    """
    if getattr(message, "attachments", None):
        return True
    if getattr(message, "stickers", None):
        return True
    for embed in getattr(message, "embeds", None) or ():
        if getattr(embed, "type", None) in _MEDIA_EMBED_TYPES:
            return True
    return False


def clears_account_data(mode: str) -> bool:
    """True when *mode* should purge XP/activity/profile alongside messages.

    Only the full erasure does. Guarding on this (rather than on the command)
    keeps a scrub from silently wiping an account that asked to keep it.
    """
    return mode not in PARTIAL_MODES


def message_matches_mode(message: object, mode: str) -> bool:
    """True when *message* is in scope for *mode*.

    Used as the scan predicate, so an out-of-scope message is never collected
    and therefore never deleted.
    """
    if mode == MODE_MEDIA:
        return message_has_media(message)
    if mode == MODE_TEXT:
        return not message_has_media(message)
    return True


def is_forum_thread(channel: object) -> bool:
    """Return True if *channel* is a forum-thread (a Thread under a ForumChannel).

    Forum-thread OPs (where ``message_id == channel_id``) need special handling:
    deleting them would nuke the whole thread, so the cog re-posts a tombstone
    under the bot. The cog calls this to decide whether to take that branch.
    """
    if not isinstance(channel, discord.Thread):
        return False
    return isinstance(channel.parent, discord.ForumChannel)


def group_messages_by_channel(
    msg_rows: list[tuple[int, int]],
) -> dict[int, list[int]]:
    """Bucket ``(message_id, channel_id)`` pairs into ``{channel_id: [msg_ids]}``.

    The scan in ``find_user_messages`` returns a flat list per guild because
    that's the cheapest shape over the discord.py iteration API. The cog
    then needs them grouped per-channel so each channel is fetched at most
    once and its archive state managed in a single block.
    """
    by_channel: dict[int, list[int]] = {}
    for message_id, channel_id in msg_rows:
        by_channel.setdefault(channel_id, []).append(message_id)
    return by_channel


def partition_by_bulk_delete_window(
    message_ids: list[int],
    now: datetime | None = None,
) -> tuple[list[int], list[int]]:
    """Split message IDs by the 14-day bulk-delete cutoff.

    Returns ``(recent, old)`` where:
      - ``recent`` is bulk-delete eligible (Discord allows up to 100 per call).
      - ``old`` must be deleted one at a time.

    The split uses ``discord.utils.snowflake_time`` so the result is purely
    a function of the message ID — no need to fetch any Discord state.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - FOURTEEN_DAYS

    recent: list[int] = []
    old: list[int] = []
    for mid in message_ids:
        if discord.utils.snowflake_time(mid) > cutoff:
            recent.append(mid)
        else:
            old.append(mid)
    return recent, old


def chunk_for_bulk_delete(message_ids: list[int], chunk_size: int = 100) -> list[list[int]]:
    """Chunk message IDs into lists no longer than ``chunk_size``.

    Discord caps ``channel.delete_messages`` at 100 IDs per request. The
    cog uses 100; the param is exposed so tests can use a smaller cap
    without hand-crafting 100-element fixtures.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [
        message_ids[i : i + chunk_size]
        for i in range(0, len(message_ids), chunk_size)
    ]


def render_progress_bar(done: int, total: int, *, width: int = _DEFAULT_BAR_WIDTH) -> str:
    """Render a textual progress bar of the form ``[████░░░░] 12/40``.

    Matches the format used in ``_run_deletion`` so the cog can call this
    directly. A ``total`` of zero renders a full bar (defensive — there's
    nothing to delete, so the run is effectively complete).
    """
    if width <= 0:
        raise ValueError("width must be positive")
    if total <= 0:
        filled = width
    else:
        filled = round(width * done / total)
        # round() can overshoot when done > total (e.g. failed + replaced + deleted
        # being summed double-counts a single message). Clamp to avoid negative
        # padding lengths producing visually broken bars.
        filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    return f"`[{bar}]` {done}/{total}"


def render_scan_status(done: int, total: int, found: int) -> str:
    """Format the "Scanning the server…" status string.

    Extracted so the wording can be tested without mocking out the entire
    interaction edit path in the cog.
    """
    return (
        f"Scanning the server for your messages — channel **{done}/{total}** "
        f"(**{found}** found so far)…"
    )


def mode_noun(mode: str) -> str:
    """The member-facing word for what *mode* targets."""
    if mode == MODE_MEDIA:
        return "images & files"
    if mode == MODE_TEXT:
        return "text messages"
    return "messages"


def render_confirm_prompt(
    *,
    mode: str,
    keep_messages: bool,
    subject: str | None = None,
) -> str:
    """Copy shown immediately before the irreversible click.

    *subject* is a mention when a mod is acting on someone else, and None when
    the member is acting on themselves (which switches the copy to "your").

    This is the moment consent is given, so it states the real scope. In
    particular ``keep_messages`` (``/delete_me``) leaves the server's own copy
    of the messages in place: that is deliberate — staff need it — but the
    member is told here rather than discovering it in the summary afterwards.
    """
    who = "your" if subject is None else f"{subject}'s"
    whose = "Your" if subject is None else f"{subject}'s"
    they = "you" if subject is None else subject

    if not clears_account_data(mode):
        noun = mode_noun(mode)
        kept = "text messages" if mode == MODE_MEDIA else "images & files"
        detail = (
            "every message with an attachment, sticker, or embedded image/video"
            if mode == MODE_MEDIA
            else "every message that carries no attachment or media"
        )
        return (
            f"⚠️ **This will delete {who} {noun} from Discord** — {detail}.\n\n"
            f"{whose} {kept}, XP, activity and profile stay exactly as they are.\n\n"
            "This cannot be undone. Are you sure?"
        )

    lines = [
        f"⚠️ **This will delete all {who} messages from Discord** and clear "
        f"{who} XP, activity history, and profile data."
    ]
    if keep_messages:
        lines.append(
            f"The server keeps its own copy of {who} messages for moderation — "
            f"this removes them from Discord, not from those records."
        )
    else:
        lines.append(
            f"The server's own stored copy of {who} messages is erased too — "
            f"nothing about {they} is kept."
        )
    lines.append("This cannot be undone. Are you sure?")
    return "\n\n".join(lines)


def confirm_button_label(mode: str, *, self_service: bool = True) -> str:
    """Label for the danger button — names the real scope, not "everything".

    *self_service* is False when a mod is acting on someone else, which swaps
    "my" for "their".
    """
    owner = "my" if self_service else "their"
    if mode == MODE_MEDIA:
        return f"Yes, delete {owner} images & files"
    if mode == MODE_TEXT:
        return f"Yes, delete {owner} text messages"
    return "Yes, delete everything"


def render_deletion_summary(
    *,
    deleted: int,
    failed: int,
    replaced: int,
    keep_messages: bool,
    mode: str = MODE_ALL,
) -> str:
    """Build the final "All done. Here's what was removed:" report.

    The cog's user-facing copy distinguishes between "we kept your archive
    locally" (``/delete_me``, ``keep_messages=True``) and "everything cleared"
    (``/delete_user``). Returning the same string the cog would render makes
    this trivially snapshot-testable.

    A partial *mode* says plainly that the account was left alone — claiming
    "server-side data cleared" there would be a lie, and this line is the only
    report the member gets.
    """
    if not clears_account_data(mode):
        lines = [
            "All done. Here's what was removed:",
            f"{mode_noun(mode).capitalize()} deleted from Discord: **{deleted}**",
            "Your XP, activity and profile: **left alone** — you cleared "
            f"only your {mode_noun(mode)}.",
        ]
    else:
        archive_note = " (your message archive is preserved)" if keep_messages else ""
        lines = [
            "All done. Here's what was removed:",
            f"Discord messages deleted: **{deleted}**",
            f"Server-side data (XP, activity, profile): **cleared**{archive_note}",
        ]
    if replaced:
        lines.append(f"Forum posts replaced with tombstone: **{replaced}**")
    if failed:
        lines.append(f"Messages that couldn't be deleted (no access): **{failed}**")
    return "\n".join(lines)


def render_empty_summary(*, keep_messages: bool, mode: str = MODE_ALL) -> str:
    """Return the "no messages found" summary string.

    Used when the scan turns up nothing on Discord. For a full erasure the DB
    purge still runs, so the copy reports it; a partial mode had nothing to do
    at all and says so rather than implying data was cleared.
    """
    if not clears_account_data(mode):
        return (
            f"All done. No {mode_noun(mode)} found in any channel I can read. "
            "Nothing else was touched."
        )
    archive_note = " (your message archive is preserved)" if keep_messages else ""
    return (
        "All done. No messages found in any channel I can read. "
        f"Server-side data (XP, activity, profile): **cleared**{archive_note}."
    )


def should_throttle(
    last_update: float,
    now: float,
    *,
    done: int,
    total: int,
    interval: float,
) -> bool:
    """Return True if a progress update should be skipped to respect rate limits.

    Discord rate-limits ``edit_original_response`` aggressively. The cog
    coalesces updates so at most one fires per ``interval`` seconds — but
    always lets the final update through (``done >= total``) so the user
    sees the completion state. Both the scan and delete phases use the same
    pattern with different intervals (2.0s and 1.5s respectively).
    """
    if done >= total:
        return False
    return (now - last_update) < interval
