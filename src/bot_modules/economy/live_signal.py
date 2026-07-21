"""Dirty-guild signal for the live leaderboard refresh.

Import-free on purpose: the producers are sync service code deep inside
transactions (``apply_credit``, community bumps, dashboard progress edits)
and the consumer is the async ``leaderboard_live_loop`` — neither side may
drag the other's dependencies in. State is process-local by design: a signal
lost to a restart costs at most one hour, because the hourly economy tick
refreshes every panel regardless.

A mark made inside a transaction that later rolls back is fine — it causes
one refresh that repaints the same panel. The debounce lives with the
consumer (:func:`take_ready`): a guild is only handed out when its last
hand-out is at least ``min_interval`` seconds old, and it stays pending
until then, so a burst of activity coalesces into a single edit.
"""

from __future__ import annotations

_pending: set[int] = set()
_last_taken: dict[int, float] = {}


def mark_dirty(guild_id: int) -> None:
    """Flag a guild's leaderboard panel as stale. Cheap; call freely."""
    _pending.add(int(guild_id))


def take_ready(now: float, min_interval: float) -> list[int]:
    """Pop the guilds due for a refresh; the rest keep waiting.

    A popped guild's clock restarts even if the caller's refresh then fails —
    the next mark re-queues it, and the hourly tick is the backstop.
    """
    ready = [
        gid
        for gid in _pending
        if now - _last_taken.get(gid, 0.0) >= min_interval
    ]
    for gid in ready:
        _pending.discard(gid)
        _last_taken[gid] = now
    return ready


def pending_count() -> int:
    """How many guilds are waiting (introspection/tests)."""
    return len(_pending)


def reset() -> None:
    """Clear all state — test isolation only."""
    _pending.clear()
    _last_taken.clear()
