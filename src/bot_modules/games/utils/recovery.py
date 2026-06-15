"""Crash-recovery for party games.

When the bot restarts, ``bot.active_views`` is empty and discord.py's
persistent-view registry is bare, so every game that was mid-flight before the
restart is left with dead buttons (clicks fail with "interaction failed").

Recovery works like the launcher registry: each game cog registers an async
recoverer in ``bot.game_recoverers`` (keyed by ``game_type``, mirroring
``bot.game_launchers``). On startup this module walks every row in
``games_active_games`` and hands each one to its recoverer, which rebuilds the
current-phase view(s) from the persisted ``state``/``payload``, re-registers
them with discord.py (bound to the stored ``message_id``), and re-arms any
round timer.

A recoverer has the signature::

    async def recover(row, payload, channel, message) -> bool

where ``row`` is the ``games_active_games`` row, ``payload`` is the decoded
JSON payload, ``channel`` is the resolved anchor channel (never ``None`` when
the recoverer is called), and ``message`` is the anchor message fetched from
``row["message_id"]`` (``None`` if it was deleted). It returns truthy when the
game was successfully re-registered. Games whose anchor message is gone, whose
channel no longer resolves, or whose type has no registered recoverer are
skipped — never crashed.
"""

import logging

from bot_modules.games.utils.game_manager import get_game_payload, is_game_expired

log = logging.getLogger(__name__)


async def resolve_anchor(bot, channel_id, message_id):
    """Best-effort fetch of a game's channel and anchor message.

    Returns ``(channel, message)``. ``channel`` is ``None`` when the channel no
    longer resolves; ``message`` is ``None`` when it has no id or was deleted.
    """
    channel = None
    if channel_id:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(channel_id))
            except Exception:
                return None, None
    if channel is None:
        return None, None

    message = None
    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
        except Exception:
            message = None
    return channel, message


async def recover_active_games(bot):
    """Re-register views/timers for every in-flight game. Call once on startup.

    Safe to call before any recoverers are registered (unknown types are
    skipped) and resilient to per-game failures (one bad row never aborts the
    sweep). Expired games are left for the hourly cleanup loop to archive.
    """
    db = bot.games_db
    try:
        rows = await db.fetchall("SELECT * FROM games_active_games")
    except Exception:
        log.exception("games recovery: failed to read active games")
        return

    recoverers = getattr(bot, "game_recoverers", {})
    recovered = skipped = expired = no_channel = no_message = failed = 0

    for row in rows:
        game_id = row["game_id"]
        game_type = row["game_type"]
        try:
            if await is_game_expired(db, game_id):
                expired += 1  # the cleanup loop will archive it
                continue

            recover = recoverers.get(game_type)
            if recover is None:
                skipped += 1
                continue

            channel, message = await resolve_anchor(bot, row["channel_id"], row["message_id"])
            if channel is None:
                no_channel += 1
                continue
            if message is None:
                # Anchor message gone — nothing to re-attach buttons to.
                no_message += 1
                continue

            payload = await get_game_payload(db, game_id)
            ok = await recover(row, payload, channel, message)
            if ok:
                recovered += 1
            else:
                skipped += 1
        except Exception:
            failed += 1
            log.exception("games recovery: failed to recover %s game %s", game_type, game_id)

    log.info(
        "games recovery: %d recovered, %d skipped, %d expired, %d no-channel, "
        "%d no-message, %d failed (of %d active)",
        recovered, skipped, expired, no_channel, no_message, failed, len(rows),
    )
