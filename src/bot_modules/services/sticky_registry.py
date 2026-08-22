"""Which channel each sticky panel occupies, for everything that posts into one.

Discord has one bottom slot per channel, so two sticky panels in one channel
take turns being second — a delete-and-repost fight that is intermittent at
best and, when both panels re-stick under *bot* messages, an indefinite storm
(``docs/reviews/2026-08-06-sticky-panel-machinery.md``, F1). Nothing at
placement time can fix that; the only place it is legible is *before* the
second thing is posted, while an admin or a mod is still choosing a channel.

This table is what makes that check possible without coupling the cogs to each
other at runtime: every panel's channel is reachable from a plain config read,
so one connection answers "who is already in this channel" for all of them.

It started inside ``economy_auction_service`` knowing only the four economy and
casino panels, with a docstring conceding the rest were "not worth four
cross-cog imports" — which meant ``/bank auction start`` and the dashboard's
panel buttons happily posted into a channel held by pen pals, DM perms, Voice
Control, a todo board, the Survivor panel or the Guess Who prompt with no
warning at all. The Survivor panel is the sharp one: it re-sticks under bot
messages, so sharing with it is the *blocking* case, and it was invisible.

**Deliberately absent: panels with a lifecycle.** The auction card and the
mahjong table panels are posted per-event and retire themselves, so "the
channel this feature's panel lives in" is not a property of the config — it is
a property of a round that may not exist. They consult this table rather than
appearing in it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from bot_modules.core.db_utils import get_config_value


@dataclass(frozen=True)
class StickyResident:
    """A sticky panel already holding a channel's bottom slot."""

    #: How to name it to a mod ("the casino hub panel").
    name: str
    #: Whether that panel re-sticks under *bot* messages
    #: (``StickyPanel(restick_on_bot=True)``). This is what decides whether
    #: something else may share the channel at all. A resident that only moves
    #: under *human* messages trades places with the newcomer intermittently
    #: and visibly — tolerable, so it warns. A resident that moves under bot
    #: messages re-takes the bottom after every render, so the newcomer is
    #: buried reliably and silently and nothing anyone does in the channel can
    #: keep it in view — so it blocks.
    restick_on_bot: bool


def _economy_panel_channel(conn: sqlite3.Connection, guild_id: int) -> int:
    from bot_modules.services.economy_service import (  # noqa: PLC0415
        load_econ_settings,
    )

    return int(load_econ_settings(conn, guild_id).guide_channel_id or 0)


def _economy_shop_channel(conn: sqlite3.Connection, guild_id: int) -> int:
    from bot_modules.services.economy_service import (  # noqa: PLC0415
        load_econ_settings,
    )

    return int(load_econ_settings(conn, guild_id).shop_channel_id or 0)


def _economy_bounty_channel(conn: sqlite3.Connection, guild_id: int) -> int:
    from bot_modules.services.economy_service import (  # noqa: PLC0415
        load_econ_settings,
    )

    return int(load_econ_settings(conn, guild_id).bounty_channel_id or 0)


def _casino_channel(conn: sqlite3.Connection, guild_id: int) -> int:
    from bot_modules.services.casino_service import (  # noqa: PLC0415
        load_casino_settings,
    )

    return int(load_casino_settings(conn, guild_id).panel_channel_id or 0)


def _pen_pals_channel(conn: sqlite3.Connection, guild_id: int) -> int:
    row = conn.execute(
        "SELECT panel_channel_id FROM pen_pals_config WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    return int(row["panel_channel_id"] or 0) if row is not None else 0


def _dm_perms_channel(conn: sqlite3.Connection, guild_id: int) -> int:
    row = conn.execute(
        "SELECT panel_channel_id FROM dm_panel_settings WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    return int(row["panel_channel_id"] or 0) if row is not None else 0


def _voice_control_channel(conn: sqlite3.Connection, guild_id: int) -> int:
    return int(
        get_config_value(conn, "voice_master_panel_channel_id", "0", guild_id) or 0
    )


def _guess_prompt_channel(conn: sqlite3.Connection, guild_id: int) -> int:
    from bot_modules.services.guess_repo import get_guess_config  # noqa: PLC0415

    config = get_guess_config(conn, guild_id)
    # Same legacy fallback the cog's own ``_panel_ids`` carries: guilds whose
    # prompt predates ``guess_prompt_channel_id`` have a message id and no
    # channel id, and their prompt is in the Guess channel.
    return int(
        config.prompt_channel_id
        or (config.guess_channel_id if config.prompt_message_id else 0)
    )


def _todo_board_channel(chores: bool) -> Callable[[sqlite3.Connection, int], int]:
    def read(conn: sqlite3.Connection, guild_id: int) -> int:
        from bot_modules.services.todo_service import (  # noqa: PLC0415
            BOARD_ALL,
            BOARD_CHORES,
            get_board,
        )

        kind = BOARD_CHORES if chores else BOARD_ALL
        return int(get_board(conn, guild_id, kind).channel_id or 0)

    return read


def _survivor_channel(conn: sqlite3.Connection, guild_id: int) -> int:
    """The season's configured channel, not where the panel was last posted.

    ``panel_ids`` answers "where is it now", which is empty until the first
    post — and a Survivor panel that hasn't landed yet is exactly when this
    matters: something else gets placed in that channel unopposed, and the
    Wednesday repost then buries it. The configured channel is where the panel
    is going to live, which is the same rule the bounty hub already follows
    (it keys off the board channel, not its last placement). Falls back to the
    recorded location for the window after an admin repoints the channel and
    before the next repost moves the panel off the old one.
    """
    from bot_modules.services.survivor_service import (  # noqa: PLC0415
        get_active_season,
    )

    season = get_active_season(conn, guild_id)
    if season is None:
        return 0
    config = season["config"]
    return int(
        config.get("channel_id") or config.get("announcement_channel_id") or 0
    )


#: Every sticky panel a plain config read can find, as
#: ``(key, name, restick_on_bot, resolver)``.
#:
#: ``key`` is stable so a caller can exclude *itself* when asking who else is
#: in a channel (see ``bot_chasing_resident``), and it matches the panel's
#: ``PanelSpec.key`` in ``panel_registry`` wherever the dashboard can post it —
#: that is what lets ``routes/panels.py`` run this check for any panel without
#: a translation table.
_STICKY_PANELS: tuple[tuple[str, str, bool, Callable[..., int]], ...] = (
    # One entry where there were two: the guide and leaderboard panels merged
    # on 2026-08-18, and the survivor lives on the guide's ids.
    ("economy-panel", "the economy panel", False, _economy_panel_channel),
    ("economy-shop", "the shop panel", False, _economy_shop_channel),
    # The bounty hub sits in the board channel itself, so that is the
    # collision — not where the panel was last recorded as posted. It
    # re-sticks under bot messages so it stays below its own cards, which
    # also means it out-competes anything else here.
    ("economy-bounty", "the bounty board panel", True, _economy_bounty_channel),
    ("casino", "the casino hub panel", True, _casino_channel),
    ("pen-pals", "the pen pals panel", False, _pen_pals_channel),
    ("dm-perms", "the DM request panel", False, _dm_perms_channel),
    ("voice-control", "the Voice Control owner panel", False, _voice_control_channel),
    ("guess-prompt", "the Guess Who prompt", False, _guess_prompt_channel),
    ("todo-board", "the todo board", False, _todo_board_channel(chores=False)),
    ("todo-chores", "the chore board", False, _todo_board_channel(chores=True)),
    # restick_on_bot: the Reckoning and last-call posts are the panel's own
    # main buriers, so it follows them down — and blocks anything else here.
    ("survivor", "the Survivor panel", True, _survivor_channel),
)


_STICKY_KEYS = frozenset(key for key, _name, _on_bot, _resolve in _STICKY_PANELS)


def is_sticky_panel(key: str) -> bool:
    """Whether this panel keeps itself at the bottom of its channel.

    The collision rules are about two panels contesting one bottom slot, so a
    caller placing something that does *not* re-stick — the support ticket
    panel, the grant-audit card — has no contest to lose and must not be
    refused. Being scrolled up is ordinary Discord, not a fight.
    """
    return key in _STICKY_KEYS


def panel_channels(
    conn: sqlite3.Connection, guild_id: int
) -> dict[str, tuple[int, str, bool]]:
    """``key -> (channel_id, name, restick_on_bot)`` for every configured panel.

    Panels with no channel configured are absent rather than present with a
    zero, so a caller can never match "unconfigured" against "channel 0".
    """
    out: dict[str, tuple[int, str, bool]] = {}
    for key, name, on_bot, resolve in _STICKY_PANELS:
        channel_id = resolve(conn, guild_id)
        if channel_id:
            out[key] = (channel_id, name, on_bot)
    return out


def _merge(residents: list[tuple[str, bool]]) -> StickyResident | None:
    """Fold every panel in one channel into a single resident.

    Merged, not overwritten. This used to build its dict by comprehension, so
    a channel hosting two panels reported only whichever came last in the
    table — a mod warned about a shared channel was told about one of the two
    things they were sharing it with. ``restick_on_bot`` is an OR: one
    bot-chasing panel is enough to bury a newcomer.
    """
    if not residents:
        return None
    return StickyResident(
        name=" and ".join(name for name, _ in residents),
        restick_on_bot=any(on_bot for _, on_bot in residents),
    )


def resident_in(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    *,
    excluding: str | None = None,
) -> StickyResident | None:
    """What already holds this channel's bottom slot, if anything.

    ``excluding`` is the asking panel's own key, for a caller deciding whether
    to *re*-post itself somewhere: a panel already recorded in its destination
    would otherwise always find itself and refuse.
    """
    return _merge(
        [
            (name, on_bot)
            for key, (cid, name, on_bot) in panel_channels(conn, guild_id).items()
            if cid == channel_id and key != excluding
        ]
    )


def sticky_panel_channels(
    conn: sqlite3.Connection, guild_id: int
) -> dict[int, StickyResident]:
    """Channels that already host a sticky panel → what is sitting there.

    Rather than couple the cogs at runtime, callers ask this while a mod or an
    admin is still choosing a channel — see ``_sticky_check`` in
    ``economy/auction_views.py`` for the block/warn split ``restick_on_bot``
    feeds, and ``routes/panel_posting.py`` for the same split on the dashboard.
    """
    by_channel: dict[int, list[tuple[str, bool]]] = {}
    for channel_id, name, on_bot in panel_channels(conn, guild_id).values():
        by_channel.setdefault(channel_id, []).append((name, on_bot))
    merged = {cid: _merge(entries) for cid, entries in by_channel.items()}
    return {cid: resident for cid, resident in merged.items() if resident is not None}


def bot_chasing_resident(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, *, excluding: str
) -> str | None:
    """Name of *another* bot-chasing sticky panel already in ``channel_id``.

    Two panels with ``restick_on_bot`` in one channel is the configuration that
    made them re-post each other indefinitely (2026-08-06 review, F1).
    ``core.sticky`` now refuses to chase another panel's placement so it can no
    longer storm, but the two still trade the bottom slot on every trigger and
    one of them is always the buried one — so the posting paths refuse it.

    ``excluding`` is the asking panel's own key: the bounty hub lives in the
    bounty channel by definition, so it would otherwise always find itself.
    """
    for key, (cid, name, on_bot) in panel_channels(conn, guild_id).items():
        if key != excluding and on_bot and cid == channel_id:
            return name
    return None
