"""Pure-logic helpers extracted from ``cogs/music_cog.py``.

The cog stays the Discord-glue surface (slash commands, event listeners,
View/Modal classes, wavelink/voice calls). Anything that takes plain
Python in and returns plain Python out -- URL classification, queue
pagination math, idle-disconnect gating, message/embed assembly --
lives here so it's unit-testable without spinning up Discord or
Lavalink.

Sibling services (``music_queue``, ``music_settings``,
``music_now_playing``, ``spotify_resolver``, ``lavalink_manager``) are
untouched -- this package is a thin layer of new sibling logic on top.
"""

from __future__ import annotations
