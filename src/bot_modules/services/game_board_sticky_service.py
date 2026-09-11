"""Per-guild dial: keep a live game's board pinned to the channel's bottom.

Off by default (ship-dark). When a guild turns it on, a live game's board
re-posts itself to the bottom of the channel as chat buries it, for as long as
that board is live, then goes dormant the moment the game retires it. One dial
covers every game that wires itself up to it; see
``docs/plans/sticky-panel-extraction.md`` for the site survey and which games
have adopted it so far.

The dial itself follows ``advisor_service``'s ``ADVISOR_CONTEXT_KEY``: a single
boolean in the shared ``config`` table, scoped per guild.

This module also owns the **wiring** every adopting cog needs, so a third game
is a `build`-callback and two calls rather than a third copy of it: the dial
read, the board's location in ``games_active_games``, the ``StickyPanel``
factory, and the open/retire pair. Mt. Rushmore Draft and Name Your Price both
went through a hand-rolled copy of this first; the copies were identical apart
from the view's type name, which is what moved it here.

Two rules are load-bearing and easy to lose if this is ever re-inlined:

* ``build`` **raises** once a board is retired rather than returning ``None``.
  ``StickyPanel._place_locked`` does not check its build result, so ``None``
  would explode inside core; raising is the established adapter idiom (see
  ``economy_cog._build_auction_panel``).
* ``forget()`` is called at retire as well, because an ``on_message`` already
  in flight when the game ends can otherwise still read the panel's cached ids
  and schedule a restick that reaches the refusal above — which core logs as an
  ERROR traceback for a perfectly ordinary game ending. See
  ``auction_views._release_panel``, which does the same thing for the same
  reason.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Protocol, cast

import discord

from bot_modules.core.db_utils import get_config_value, parse_bool, set_config_value
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.games.utils.game_manager import update_game_message

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


class LiveBoardView(Protocol):
    """What a game's view must expose to be stuck to the channel bottom.

    Both adopters are ``discord.ui.View`` subclasses that already had all of
    this except the last two, which the sticky wiring adds.
    """

    game_id: str
    guild: discord.Guild | None
    #: Set by ``open_game_board`` when the dial is on, so the view's own
    #: redraw seam knows to route through the panel instead of a cached
    #: message that a repost may have orphaned.
    _panel: StickyPanel | None
    #: Set by ``retire_game_board``. Distinct from a game's own ``_closed``,
    #: which is set one *legitimate* render earlier (the disabled frame).
    _board_retired: bool

    def _build_embed(self) -> discord.Embed: ...


def _read_dial(bot: Any, guild_id: int) -> bool:
    with bot.ctx.open_db() as conn:
        return get_board_sticky_enabled(conn, guild_id)


async def board_sticky_enabled(bot: Any, guild: discord.Guild | None) -> bool:
    """Whether *guild* wants live boards stuck to the channel bottom.

    A ``None`` guild short-circuits before the DB read: ``StickyPanel.place``
    takes a non-optional ``discord.Guild`` and dereferences ``guild.id``
    immediately, so a ``None`` must never reach it. Callers read this once,
    when a board is first posted, so a mid-game flip of the dial never
    disturbs a board already running.
    """
    if guild is None:
        return False
    return await asyncio.to_thread(_read_dial, bot, guild.id)


def board_location(bot: Any, game_id: str) -> tuple[int, int]:
    """``StickyPanel.load_ids`` for one game: where its board is right now.

    Reads the *live* ``games_active_games`` row rather than a second store, so
    it can never drift from what ``update_game_message`` wrote or what the
    busy-check's jump link and ``/games end`` see. Synchronous because
    ``StickyPanel`` calls its id hooks off-thread; ``game_manager``'s own
    accessors are async, which is why this pair lives here rather than there.
    """
    with bot.ctx.open_db() as conn:
        row = conn.execute(
            "SELECT channel_id, message_id FROM games_active_games WHERE game_id = ?",
            (game_id,),
        ).fetchone()
    if row is None:
        return 0, 0
    return int(row["channel_id"] or 0), int(row["message_id"] or 0)


def set_board_location(
    bot: Any, game_id: str, channel_id: int, message_id: int
) -> None:
    """``StickyPanel.save_ids`` — the same table and columns
    ``update_game_message`` writes, so a repost keeps the jump link live."""
    with bot.ctx.open_db() as conn:
        conn.execute(
            "UPDATE games_active_games SET channel_id = ?, message_id = ? WHERE game_id = ?",
            (channel_id, message_id, game_id),
        )


def build_board_content(name: str, view: LiveBoardView) -> PanelContent:
    """``StickyPanel.build`` for a live board — or a refusal once it retires.

    Raising, not returning ``None``: see the module docstring's first rule.
    ``_board_retired`` is the flag this checks, never a game's own ``_closed``,
    which is set one *legitimate* render earlier (the disabled frame that
    still has to go out).
    """
    if view._board_retired:
        raise RuntimeError(f"{name} is retired")
    return PanelContent(embed=view._build_embed(), view=cast(discord.ui.View, view))


def make_board_panel(
    bot: Any, name: str, game_id: str, view: LiveBoardView
) -> StickyPanel:
    """A ``StickyPanel`` for one game's (or one round's) live board.

    One instance per live board, never one shared per guild: ``StickyPanel``
    is keyed per guild, but games run per channel and a guild can have several
    going at once.
    """

    def _load(_guild_id: int) -> tuple[int, int]:
        return board_location(bot, game_id)

    def _save(_guild_id: int, channel_id: int, message_id: int) -> None:
        set_board_location(bot, game_id, channel_id, message_id)

    async def _build(_guild: discord.Guild) -> PanelContent:
        return build_board_content(name, view)

    return StickyPanel(name, bot, load_ids=_load, save_ids=_save, build=_build)


async def open_game_board(
    bot: Any,
    db: Any,
    boards: dict[str, StickyPanel],
    name: str,
    game_id: str,
    view: LiveBoardView,
    channel: Any,
    guild: discord.Guild | None,
    msg: discord.Message,
) -> discord.Message:
    """Post a live board — sticky when the guild's dial is on, an in-place
    edit of *msg* otherwise (every adopter's pre-existing behaviour).

    No explicit delete of the message being replaced: ``board_location`` reads
    it straight out of the game row, so ``place()`` already treats it as "the
    old panel" and removes it *after* the new board is safely up. Deleting it
    first would invert that — a placement failure (the bot lost Send Messages,
    say) would leave the game with neither the old message nor a board.
    """
    # ``view`` is a Protocol so this module need not import either game's
    # view class; every adopter is a real ``discord.ui.View``.
    ui_view = cast(discord.ui.View, view)

    if guild is not None and channel is not None and await board_sticky_enabled(
        bot, guild
    ):
        panel = make_board_panel(bot, name, game_id, view)
        posted = await panel.place(guild, channel)
        if posted is not None:
            boards[game_id] = panel
            view._panel = panel
            return posted
        # Placement failed — the old message is untouched (place() never got
        # far enough to delete it). Fall through to the non-sticky path
        # rather than leave the game without a board.

    embed = view._build_embed()
    try:
        await msg.edit(embed=embed, view=ui_view)
        return msg
    except Exception:
        if channel is None:
            # Nothing left to try: no channel to post into and the edit just
            # failed. Let the caller's own error handling see it rather than
            # swallow it into a game with no board.
            raise
        new_msg = await channel.send(embed=embed, view=ui_view)
        await update_game_message(db, game_id, new_msg.id)
        return new_msg


def retire_game_board(
    bot: Any,
    boards: dict[str, StickyPanel],
    view: LiveBoardView,
    channel: Any = None,
) -> discord.PartialMessage | None:
    """Stop a board reposting, and refuse to render it again.

    Call this at *every* point the board's life ends — natural completion, a
    forced ``/games end``, a host's own End Game button — so the panel is
    released promptly rather than left for a straggling ``on_message`` to
    restick. Idempotent, and a no-op when the dial was never on.

    Returns wherever the board actually ended up, for callers that go on
    editing it (a sticky repost may have moved it since it was opened, so the
    caller's own local message variable can be stale). ``None`` when the dial
    was off, when *channel* is not given, or when the game row no longer names
    a message — in each case the caller's existing reference is still right.
    """
    view._board_retired = True
    panel = boards.pop(view.game_id, None)
    if panel is None:
        return None
    panel.cancel_all()
    # Drop the cached ids too, not just the pending restick — see the module
    # docstring's second rule.
    if view.guild is not None:
        panel.forget(view.guild.id)
    if channel is None:
        return None
    _channel_id, message_id = board_location(bot, view.game_id)
    if not message_id:
        return None
    return channel.get_partial_message(message_id)
