"""Music playlist — a watched channel becomes a rolling Spotify playlist.

Ported from OpenMusicBot (docs/plans/music-playlist-cog.md). Pure parsing and
matching live in ``music_playlist_logic``; the pipeline (resolve → dedupe →
write → trim) is the sibling service's job, and the cog is thin glue on top.
"""

from __future__ import annotations
