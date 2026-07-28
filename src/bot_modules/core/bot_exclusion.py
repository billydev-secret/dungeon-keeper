"""Single source of truth for keeping bot traffic out of dashboard metrics.

Bots are ~21% of stored message volume, so counting them makes every
message-volume number on the dashboard wrong by up to a fifth.  Metric queries
therefore exclude them *by default*; callers that genuinely want bot rows (a mod
debugging a spammy bot) pass ``include_bots=True``.

"Bot" means a row in ``known_users`` with ``is_bot=1``.  That table is the only
source consulted — it retains bots that have since left the guild, which a live
``guild.members`` scan would miss.

The clause is a correlated ``NOT IN`` subquery rather than a join so it can be
appended to an existing ``WHERE`` without disturbing the surrounding SQL's
column list, grouping, or row shape.
"""

from __future__ import annotations

_BOT_IDS_SUBQUERY = "SELECT user_id FROM known_users WHERE guild_id=? AND is_bot=1"


def bot_filter_clause(
    guild_id: int,
    *,
    column: str = "author_id",
    include_bots: bool = False,
) -> tuple[str, tuple[int, ...]]:
    """Return ``(sql_fragment, params)`` excluding bot-authored rows.

    The fragment begins with ``AND`` and is meant to be concatenated into an
    existing ``WHERE`` clause.  ``params`` must be spliced into the caller's
    parameter tuple *at the position matching where the fragment lands in the
    SQL* — placeholders are positional, so appending the fragment mid-query but
    the params at the end will silently mis-bind.

    ``column`` names the author column to test, qualified where the query uses a
    table alias (e.g. ``"m.author_id"``).  It is interpolated directly, so it
    must never come from user input.

    When ``include_bots`` is true this returns ``("", ())`` — the caller's SQL is
    left byte-identical, so the opt-in path costs nothing.
    """
    if include_bots:
        return "", ()
    return f" AND {column} NOT IN ({_BOT_IDS_SUBQUERY})", (guild_id,)


def bot_ids_subquery() -> str:
    """The bare ``SELECT`` of bot user ids, for queries that need it inline.

    Takes one parameter (``guild_id``).  Use this where the exclusion cannot be
    expressed as a trailing ``AND`` — for example inside a ``HAVING``, or when
    filtering a column that is not the author of the row being counted.
    """
    return _BOT_IDS_SUBQUERY
