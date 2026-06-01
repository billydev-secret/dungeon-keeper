"""Pure decision logic for the games-config admin cog.

The cog handles per-server settings for the games cluster: which channels
allow games, where audit logs go, who has web portal access, plus
read-only views into the active game in a channel. The Discord glue
(interaction objects, permission checks, db calls) lives in the cog;
this module is the row-and-string transforms it delegates to so they
can be tested without spinning up Discord.

Two callable types worth flagging:

* ``ChannelResolver`` — the cog passes ``guild.get_channel`` so we don't
  need a real Guild to test the "mention if resolvable, else fallback"
  branch.
* ``rows`` — generally a sequence of mapping-like objects (sqlite3.Row
  or plain dict). Tests use dicts so we don't need an open DB.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

#: Callable signature for ``guild.get_channel``. Returns the channel
#: object (which only needs a ``.mention`` attribute) or ``None``.
ChannelResolver = Callable[[int], Any]


def format_allowed_channels(
    rows: Sequence[Sequence[Any]],
    resolver: ChannelResolver,
) -> str:
    """Render the ``/games list-channels`` body.

    Each row is expected to expose its first column as the channel id
    (matching ``SELECT channel_id FROM games_allowed_channels``). For
    each id we try ``resolver(id)`` — if the channel is still in the
    guild we use ``.mention``, otherwise we fall back to a literal
    ``<#id>`` link so deleted channels still render usefully.

    Returns the empty-state hint when ``rows`` is empty so the caller
    can drop the result straight into an embed.
    """
    if not rows:
        return "No game channels configured yet. Use `/games allow-channel`."
    mentions: list[str] = []
    for row in rows:
        channel_id = row[0]
        channel = resolver(channel_id)
        if channel is not None and hasattr(channel, "mention"):
            mentions.append(channel.mention)
        else:
            mentions.append(f"<#{channel_id}>")
    return "\n".join(mentions)


def format_portal_access_list(rows: Sequence[Sequence[Any]]) -> str:
    """Render the ``/games portal-list`` body.

    Each row is ``(user_id, granted_by, granted_at)`` — the third
    column is unused in the rendered text but kept in the SELECT so
    the ORDER BY clause is valid. Empty input returns the hint.
    """
    if not rows:
        return "No users have been granted portal access. Use `/games portal-grant`."
    return "\n".join(f"<@{row[0]}> — granted by <@{row[1]}>" for row in rows)


def describe_active_game(row: Mapping[str, Any] | None) -> tuple[str, str]:
    """Return ``(title, description)`` for the ``/games game-status`` embed.

    The cog hands us the row from ``games_active_games`` (or ``None``
    when no game is running). We build both the title and the body
    text here so the cog's branch is just an embed construction.
    """
    if not row:
        return (
            "No Active Game",
            "There's no game running in this channel.",
        )
    return (
        "Active Game",
        (
            f"**Type:** {row['game_type']}\n"
            f"**State:** {row['state']}\n"
            f"**Host:** <@{row['host_id']}>\n"
            f"**Game ID:** `{row['game_id']}`"
        ),
    )


def describe_force_end(game_type: str) -> str:
    """Body text for the ``/games game-end`` success embed."""
    return f"The **{game_type}** game has been ended by an admin/mod."


def audit_channel_change(
    channel_id: int | None,
) -> tuple[str, str]:
    """Return ``(title, description)`` for the audit-channel set/clear embed.

    Pass ``channel.id`` to set, ``None`` to clear. Keeping this in
    logic makes the cog's branch a single dict lookup instead of two
    parallel embed constructions.
    """
    if channel_id is None:
        return (
            "✅ Audit Channel Cleared",
            "Anonymous submission logging has been disabled.",
        )
    return (
        "✅ Audit Channel Set",
        f"Anonymous submissions will be logged to <#{channel_id}>.",
    )


def has_admin_permissions(perms: Any) -> bool:
    """Return True if the given guild_permissions grant admin.

    Pulled out so the predicate test doesn't need a real Interaction.
    """
    return bool(getattr(perms, "administrator", False))


def has_mod_or_admin_permissions(perms: Any) -> bool:
    """Return True if perms grant admin, manage_guild, or manage_channels.

    Matches the cog's ``is_mod_or_admin`` rule: any one of the three
    elevated perms qualifies a user to run mod-tier game commands.
    """
    if not perms:
        return False
    return bool(
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_guild", False)
        or getattr(perms, "manage_channels", False)
    )


def channel_ids_from_rows(rows: Iterable[Sequence[Any]]) -> list[int]:
    """Project a sequence of channel-id rows down to a plain int list.

    Convenience wrapper used by callers that need the IDs as a list
    (e.g. for set membership) rather than the rendered string output
    of :func:`format_allowed_channels`.
    """
    return [int(row[0]) for row in rows]
