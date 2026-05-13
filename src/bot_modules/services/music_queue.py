"""Music cog — per-guild queue state.

Pure data layer (no Discord, no I/O). Held in-memory by MusicCog and
serializable to stdlib types for future v2 persistence.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque


class LoopMode(str, Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


@dataclass
class GuildQueue:
    guild_id: int
    voice_channel_id: int | None = None
    text_channel_id: int | None = None
    tracks: Deque[Any] = field(default_factory=deque)
    current: Any = None
    loop_mode: LoopMode = LoopMode.OFF
    history: Deque[Any] = field(default_factory=lambda: deque(maxlen=50))
    now_playing_message_id: int | None = None
    requesters: dict[str, int] = field(default_factory=dict)
    autoplay_playlist_url: str | None = None

    def add(self, track: Any, requester_id: int | None = None) -> None:
        self.tracks.append(track)
        if requester_id is not None:
            self.requesters[_track_key(track)] = requester_id

    def add_many(self, tracks: list[Any], requester_id: int | None = None) -> None:
        for t in tracks:
            self.add(t, requester_id)

    def next(self) -> Any | None:
        """Return the next track to play according to loop mode.

        Mutates ``current`` and ``history`` as a side effect.
        """
        if self.loop_mode == LoopMode.TRACK and self.current is not None:
            return self.current

        if self.current is not None:
            self.history.append(self.current)
            if self.loop_mode == LoopMode.QUEUE:
                self.tracks.append(self.current)

        self.current = self.tracks.popleft() if self.tracks else None
        return self.current

    def skip(self) -> Any | None:
        """Force-advance regardless of TRACK loop (skip should bypass repeat)."""
        if self.current is not None:
            self.history.append(self.current)
            if self.loop_mode == LoopMode.QUEUE:
                self.tracks.append(self.current)
        self.current = self.tracks.popleft() if self.tracks else None
        return self.current

    def shuffle(self) -> None:
        items = list(self.tracks)
        random.shuffle(items)
        self.tracks = deque(items)

    def clear(self) -> None:
        self.tracks.clear()
        self.requesters.clear()

    def set_loop(self, mode: LoopMode) -> None:
        self.loop_mode = mode

    def peek(self, n: int = 10) -> list[Any]:
        return list(self.tracks)[:n]

    def requester_for(self, track: Any) -> int | None:
        return self.requesters.get(_track_key(track))


def _track_key(track: Any) -> str:
    """Stable identifier for a track for requester lookup.

    Uses wavelink Playable's identifier when available, else uri/title.
    """
    for attr in ("identifier", "uri", "title"):
        v = getattr(track, attr, None)
        if v:
            return str(v)
    return str(id(track))
