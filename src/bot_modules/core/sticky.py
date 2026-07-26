"""Shared rules for panels that stay pinned to the bottom of a channel.

Discord has no reorder API, so a "sticky" panel keeps its position by being
deleted and re-posted whenever a member posts beneath it. Several features do
this (the economy guide / shop / leaderboard panels, the todo board); the
predicate below is the part they all agree on.

Lived in ``bot_modules/economy/guide.py`` until the todo board became the
fourth caller — a moderation feature importing from the economy package to
decide whether to re-stick a panel was the wrong dependency. ``guide`` still
re-exports it so existing economy call sites are unaffected.
"""

from __future__ import annotations


def should_restick_guide(
    *,
    message_channel_id: int,
    message_id: int,
    panel_channel_id: int,
    panel_message_id: int,
) -> bool:
    """Whether a new message should push a sticky panel back to the bottom.

    Bot messages are filtered out by the caller before we get here
    (re-sticking under our own repost self-loops), so this predicate only ever
    sees member activity; the message-id guard below stays as a belt-and-braces
    skip of the panel itself.
    """
    if not panel_channel_id or not panel_message_id:
        return False  # no panel posted yet
    if message_channel_id != panel_channel_id:
        return False  # activity in some other channel
    return message_id != panel_message_id  # skip our own panel
