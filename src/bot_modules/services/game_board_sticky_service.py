"""Per-guild dial: keep a live game's board pinned to the channel's bottom.

Off by default (ship-dark). When a guild turns it on, a live game's board —
today, Mt. Rushmore Draft's draft board — re-posts itself to the bottom of
the channel as chat buries it, for the duration of that game, then goes
dormant the moment the game retires its board. One dial covers every game
that wires itself up to it; see ``docs/plans/sticky-panel-extraction.md`` for
the site survey and which games have adopted it so far.

Follows the same shape as ``advisor_service``'s ``ADVISOR_CONTEXT_KEY``: a
single boolean stored in the shared ``config`` table, scoped per guild.
"""

from __future__ import annotations

import sqlite3

from bot_modules.core.db_utils import get_config_value, parse_bool, set_config_value

#: Off by default — a merge (and even a restart) changes nothing until an
#: admin ticks this on Games Global Config.
BOARD_STICKY_KEY = "games_board_sticky_enabled"


def get_board_sticky_enabled(conn: sqlite3.Connection, guild_id: int = 0) -> bool:
    """Whether live game boards should stay pinned to the channel's bottom."""
    return parse_bool(get_config_value(conn, BOARD_STICKY_KEY, "0", guild_id), False)


def set_board_sticky_enabled(
    conn: sqlite3.Connection, enabled: bool, guild_id: int = 0
) -> None:
    set_config_value(conn, BOARD_STICKY_KEY, "1" if enabled else "0", guild_id)
