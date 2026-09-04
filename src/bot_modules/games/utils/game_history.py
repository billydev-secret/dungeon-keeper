"""One INSERT for ``games_game_history``, for the games that never had an
active row to archive.

``game_manager.end_game`` records a party game by moving its
``games_active_games`` row into ``games_game_history``. Risky Rolls and the
duel / group games keep their own state (``risky_active_rounds``,
``<game>_games``) and so never had a row for it to move — which left the
most-played formats on the server invisible to Play Statistics, ``/recap``,
the game-night session tracker and the Ping Response game-player join
(discovery-5, duels-party-122). They record straight into history with the
statement built here instead.

The statement carries its own exactly-once predicate on ``game_id``: the
duel terminal hook can fire more than once for one game (the sweep, the
resume path and a normal resolution all reach it), and a second fire must
not become a second row. Callers that could collide on integer ids across
tables namespace the id themselves (``"chicken:12"``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

HISTORY_INSERT_SQL = """
INSERT INTO games_game_history
    (game_id, game_type, channel_id, host_id, player_count, round_count,
     payload, started_at, guild_id)
SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
WHERE NOT EXISTS (SELECT 1 FROM games_game_history WHERE game_id = ?)
"""


def history_started_at(epoch: float) -> str:
    """Format an epoch float the way ``games_active_games.created_at`` is
    written (``CURRENT_TIMESTAMP``: UTC, ``YYYY-MM-DD HH:MM:SS``), so the
    dashboard parses every ``started_at`` the same way."""
    return datetime.fromtimestamp(float(epoch), UTC).strftime("%Y-%m-%d %H:%M:%S")


def history_insert(
    *,
    game_id: str,
    game_type: str,
    channel_id: int,
    host_id: int,
    player_count: int,
    round_count: int,
    payload: dict[str, Any],
    started_at: float,
    guild_id: int,
) -> tuple[str, tuple]:
    """Return ``(sql, params)`` recording one played game, once.

    ``started_at`` is an epoch float (what every self-stored game keeps);
    ``payload`` should carry a ``players`` list so ``game_roster`` can
    rebuild the roster for the unique-players count.
    """
    return HISTORY_INSERT_SQL, (
        str(game_id),
        game_type,
        int(channel_id),
        int(host_id),
        int(player_count),
        int(round_count),
        json.dumps(payload),
        history_started_at(started_at),
        int(guild_id),
        str(game_id),
    )
