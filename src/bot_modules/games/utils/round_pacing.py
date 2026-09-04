"""Pacing and endings shared by the three reactive round games.

Would You Rather, Never Have I Ever and Most Likely To are the same shape: a
round opens, the room votes, someone presses **⏭️ Next**, repeat. Until
2026-09-04 that "someone" was the host or a mod and nothing else ever moved
the game along — no round timer, no round cap, no End button (vote-games-52,
vote-games-53, discovery-1, discovery-3). A host who looked away left a board
blocking the channel until ``/games end`` or the 24-hour sweep, and a
scheduled launch whose creator wasn't there stalled on round 1 (platform-23).

This module holds the rules the three cogs share so they cannot drift:

* :func:`resolve_pacing` — the per-launch ``round_seconds`` / ``max_rounds``
  pair: a slash argument or schedule option wins, else the dashboard's
  per-server default, else the built-in defaults (host-paced, ten rounds).
  ``0`` seconds means the host paces; ``0`` rounds means the game runs until
  ended.
* :func:`round_cap_reached` — whether the round that just closed was the
  last one.
* :func:`voter_may_advance` — the **scheduled-game unlock**: when the launch
  had no live host at the keyboard (a schedule, or feature rotation's
  host ``0``), anyone who voted may press Next once the round has been open
  for the round timer — or, host-paced, for :data:`SCHEDULED_NEXT_UNLOCK_SECONDS`.
* :class:`RoundPacing` — the per-round state a view carries (opened-at,
  the ``asyncio.Event`` Next sets, the timer task) and
  :func:`RoundPacing.start_timer`, which reuses Two Truths & a Lie's
  ``asyncio.wait_for`` pattern: the round closes itself when time runs out
  and Next stays an early skip.
* :func:`has_game_host_role` — the configured Game Host role
  (``games_editor_role``) unlocks Next and End the way it unlocks
  ``/games join`` for other players.

Nothing here sends anything; the cogs own the Discord side.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from bot_modules.core.utils import is_host_or_mod

log = logging.getLogger(__name__)

ADVANCE_DENIED = "❌ Only the host, a mod, or a Game Host can advance."
END_DENIED = "❌ Only the host, a mod, or a Game Host can end the game."

DEFAULT_MAX_ROUNDS = 10
MAX_ROUNDS_CAP = 50
MAX_ROUND_SECONDS = 300
# A scheduled, host-paced round: how long before any voter may press Next.
SCHEDULED_NEXT_UNLOCK_SECONDS = 90

# Reasons stamped into the archived payload by the three games' endings.
REASON_HOST_ENDED = "ended"
REASON_ROUND_CAP = "round_cap"
REASON_EXPIRED = "expired"


TIMER_FIELD_NAME = "⏱️ Next Round"


def timer_field_value(epoch: int) -> str:
    """The live countdown a timed round shows (a Discord relative timestamp)."""
    return f"Auto-advances <t:{int(epoch)}:R>"


def waiting_notice(pose_label: str, noun: str) -> str:
    """The round embed's body while the bank had nothing to serve
    (vote-games-50): the round waits for a posed prompt instead of ending."""
    return f"⏳ No {noun} yet — press **{pose_label}** to submit one and the round starts."


def clamp_round_seconds(value: Any) -> int:
    """Seconds per round, 0–300; anything unreadable is host-paced (0)."""
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(seconds, MAX_ROUND_SECONDS))


def clamp_max_rounds(value: Any, default: int = DEFAULT_MAX_ROUNDS) -> int:
    """Round cap, 0–50; ``None`` or unreadable falls back to *default*.
    ``0`` is "no cap" — the game runs until someone ends it."""
    if value is None:
        return default
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(rounds, MAX_ROUNDS_CAP))


def resolve_pacing(options: dict | None, game_opts: dict | None) -> tuple[int, int]:
    """``(round_seconds, max_rounds)`` for one launch.

    *options* is what the door passed — the slash arguments or the schedule
    row's option bag — and *game_opts* the dashboard's per-server defaults
    (``get_game_options``). A launch option that is present (even ``0``)
    wins over the dashboard default, which wins over the built-ins.
    """
    options = options or {}
    game_opts = game_opts or {}
    seconds = options.get("round_seconds")
    if seconds is None:
        seconds = game_opts.get("round_seconds", 0)
    rounds = options.get("max_rounds")
    if rounds is None:
        rounds = game_opts.get("max_rounds", DEFAULT_MAX_ROUNDS)
    return clamp_round_seconds(seconds), clamp_max_rounds(rounds)


def is_scheduled_launch(options: dict | None, host_id: int | None) -> bool:
    """A launch with nobody at the keyboard.

    The dashboard scheduler stamps ``scheduled: True`` on its option bag;
    feature rotation launches as host ``0``. Both need the voter unlock,
    since the "host" of such a game may never see the board.
    """
    if (options or {}).get("scheduled"):
        return True
    return not host_id


def round_cap_reached(round_num: int, max_rounds: int) -> bool:
    """True when the round numbered *round_num* was the last allowed one."""
    return max_rounds > 0 and int(round_num) >= int(max_rounds)


def advance_at(opened_at: float | None, round_seconds: int) -> int | None:
    """Unix epoch at which a timed round closes itself, or None if host-paced."""
    if not opened_at or round_seconds <= 0:
        return None
    return int(opened_at + round_seconds)


def seconds_left(opened_at: float | None, round_seconds: int, *, now: float | None = None) -> float | None:
    """Seconds until a timed round closes (never negative); None if host-paced.

    Used after a restart to resume the timer for the remainder rather than a
    full fresh window.
    """
    if not opened_at or round_seconds <= 0:
        return None
    now = time.time() if now is None else now
    return max(0.0, float(opened_at) + round_seconds - now)


def voter_may_advance(
    *,
    scheduled: bool,
    has_voted: bool,
    opened_at: float | None,
    round_seconds: int,
    now: float | None = None,
) -> bool:
    """May a non-host, non-mod voter press Next?

    Only on a scheduled launch, only if they voted this round, and only once
    the round has been open long enough: the round timer when one is set,
    otherwise :data:`SCHEDULED_NEXT_UNLOCK_SECONDS`. A waiting round (no
    ``opened_at``) never unlocks — there is nothing to advance past.
    """
    if not scheduled or not has_voted or not opened_at:
        return False
    now = time.time() if now is None else now
    window = round_seconds if round_seconds > 0 else SCHEDULED_NEXT_UNLOCK_SECONDS
    return now - float(opened_at) >= window


def voter_unlock_at(opened_at: float | None, round_seconds: int) -> int | None:
    """Epoch at which :func:`voter_may_advance` starts saying yes."""
    if not opened_at:
        return None
    window = round_seconds if round_seconds > 0 else SCHEDULED_NEXT_UNLOCK_SECONDS
    return int(float(opened_at) + window)


async def has_game_host_role(db, guild_id: int | None, role_ids: Iterable[int]) -> bool:
    """Does one of *role_ids* hold the guild's configured Game Host role?"""
    if not guild_id:
        return False
    try:
        row = await db.fetchone(
            "SELECT role_id FROM games_editor_role WHERE guild_id = ?", (int(guild_id),),
        )
    except Exception:
        log.exception("game host role lookup failed for guild %s", guild_id)
        return False
    if not row:
        return False
    return int(row["role_id"]) in {int(r) for r in role_ids}


def advance_refusal(
    *,
    scheduled: bool,
    has_voted: bool,
    opened_at: float | None,
    round_seconds: int,
) -> str:
    """Why a member may not press Next right now — the ephemeral ❌ line.

    On a scheduled game the line says what *would* unlock it: vote, or wait
    for the unlock moment (a live Discord timestamp).
    """
    if not scheduled or not opened_at:
        return ADVANCE_DENIED
    if not has_voted:
        return (
            "❌ Vote first — on a scheduled game anyone who has voted can press "
            "Next once the round has been open long enough."
        )
    unlock = voter_unlock_at(opened_at, round_seconds)
    return f"❌ Not yet — anyone who voted can press Next <t:{unlock}:R>."


async def may_control(interaction: Any, host_id: int, db) -> bool:
    """Host, mod, or holder of the configured Game Host role.

    The gate on End Game and on Next's first door; ``/games join`` already
    lets that role move other players, so it runs the board too.
    """
    if is_host_or_mod(interaction, host_id):
        return True
    return await has_game_host_role(
        db, getattr(interaction, "guild_id", None), member_role_ids(interaction.user),
    )


async def advance_check(
    interaction: Any, *, host_id: int, db, pacing: "RoundPacing", has_voted: bool,
) -> str | None:
    """None when the presser may advance the round, else the refusal line."""
    if await may_control(interaction, host_id, db):
        return None
    if voter_may_advance(
        scheduled=pacing.scheduled, has_voted=has_voted,
        opened_at=pacing.opened_at, round_seconds=pacing.round_seconds,
    ):
        return None
    return advance_refusal(
        scheduled=pacing.scheduled, has_voted=has_voted,
        opened_at=pacing.opened_at, round_seconds=pacing.round_seconds,
    )


def member_role_ids(user: Any) -> list[int]:
    """The role ids on an interaction's user (empty outside a guild)."""
    return [int(r.id) for r in getattr(user, "roles", None) or [] if getattr(r, "id", None)]


class RoundPacing:
    """The per-round pacing state a round view carries.

    ``advanced`` is the event the round's advance path sets — Next, the
    timer, End, and ``force_end_active_game`` (which pokes it through the
    view's ``_advanced_event`` alias). ``start_timer`` waits on it with a
    timeout and fires *on_timeout* when the round runs out first; the task is
    kept so a recovered or ended view can cancel it.
    """

    def __init__(
        self,
        *,
        round_seconds: Any = 0,
        max_rounds: Any = DEFAULT_MAX_ROUNDS,
        scheduled: bool = False,
        opened_at: float | None = None,
    ) -> None:
        self.round_seconds = clamp_round_seconds(round_seconds)
        self.max_rounds = clamp_max_rounds(max_rounds)
        self.scheduled = bool(scheduled)
        self.opened_at = opened_at
        self.advanced = asyncio.Event()
        self.timer_task: asyncio.Task | None = None

    @property
    def timed(self) -> bool:
        return self.round_seconds > 0

    def advance_at(self) -> int | None:
        return advance_at(self.opened_at, self.round_seconds)

    def open(self, now: float | None = None) -> float:
        """Mark the round as open (a waiting round opens when its prompt lands)."""
        self.opened_at = time.time() if now is None else now
        return self.opened_at

    def start_timer(
        self,
        on_timeout: Callable[[], Awaitable[None]],
        *,
        seconds: float | None = None,
    ) -> asyncio.Task | None:
        """Close the round on a timer unless Next (or End) gets there first.

        *seconds* overrides the wait — recovery passes the remainder of the
        window. Returns the task, or None when the round is host-paced.
        """
        if not self.timed and seconds is None:
            return None
        wait = self.round_seconds if seconds is None else max(0.0, float(seconds))

        async def _run() -> None:
            try:
                await asyncio.wait_for(self.advanced.wait(), timeout=wait)
            except asyncio.TimeoutError:
                try:
                    await on_timeout()
                except Exception:
                    log.exception("round timer: auto-advance failed")

        self.cancel_timer()
        self.timer_task = asyncio.create_task(_run())
        return self.timer_task

    def cancel_timer(self) -> None:
        task = self.timer_task
        self.timer_task = None
        if task is not None and not task.done():
            task.cancel()
