"""Parsers that turn banked external-bot messages into economy payouts.

Pure functions over the raw ``embeds`` dicts (``embed.to_dict()``) the collector
banks — no DB, no Discord I/O — so they're trivially testable against real
``/games track sample`` dumps and re-runnable on the full history.

Currently: Gamebot Cards Against Humanity. A CAH game is a run of messages in
one channel ending in a *Game over!* embed. Players always render as real
mentions (``<@id>``), so rosters and winners are reliable:

* *Current Standings* / *Submission status* embeds → the roster (``<@id>: N``
  and ``✅ <@id> Submitted!``).
* *Game over!* embed → the winner (``<@id> is the winner!``).
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

# <@123> / <@!123>, tolerant of the nickname bang.
_MENTION_SCORE = re.compile(r"<@!?(\d+)>\s*:\s*\d+")
_SUBMITTED = re.compile(r"<@!?(\d+)>\s+Submitted")
_WINNER = re.compile(r"<@!?(\d+)>\s+is the winner")

_GAME_OVER_TITLE = "Game over!"


def _embed_texts(embeds: Sequence[Mapping[str, Any]]):
    """Yield (title, description) for each embed dict, blanks coerced to ''."""
    for e in embeds:
        if isinstance(e, Mapping):
            yield str(e.get("title") or ""), str(e.get("description") or "")


def players_from_standings(embeds: Sequence[Mapping[str, Any]]) -> set[int]:
    """Member ids from a *Current Standings* embed (``<@id>: N`` lines)."""
    out: set[int] = set()
    for _title, desc in _embed_texts(embeds):
        out.update(int(m) for m in _MENTION_SCORE.findall(desc))
    return out


def players_from_submissions(embeds: Sequence[Mapping[str, Any]]) -> set[int]:
    """Member ids from a *Submission status* embed (``✅ <@id> Submitted!``)."""
    out: set[int] = set()
    for _title, desc in _embed_texts(embeds):
        out.update(int(m) for m in _SUBMITTED.findall(desc))
    return out


def winner_from_game_over(embeds: Sequence[Mapping[str, Any]]) -> int | None:
    """The winner's id from a *Game over!* embed, or None if not one."""
    for _title, desc in _embed_texts(embeds):
        m = _WINNER.search(desc)
        if m:
            return int(m.group(1))
    return None


def is_game_over(embeds: Sequence[Mapping[str, Any]]) -> bool:
    """True when these embeds are Gamebot's end-of-game announcement."""
    for title, desc in _embed_texts(embeds):
        if title.strip() == _GAME_OVER_TITLE and "is the winner" in desc:
            return True
    return False


def current_game_window(
    parsed: Sequence[Mapping[str, Any]], over_index: int
) -> list[Mapping[str, Any]]:
    """Slice one game's messages: from just after the previous *Game over!* up
    to and including the one at ``over_index``.

    ``parsed`` is the channel's banked messages oldest-first, each a mapping with
    an ``embeds`` list. Bounding on the previous game-over keeps a busy channel's
    back-to-back games from bleeding rosters into each other.
    """
    start = 0
    for i in range(over_index - 1, -1, -1):
        if is_game_over(parsed[i].get("embeds") or []):
            start = i + 1
            break
    return list(parsed[start : over_index + 1])


def extract_cah_game(window: Sequence[Mapping[str, Any]]) -> tuple[set[int], int | None]:
    """(roster, winner) for one game's window of messages.

    Roster is the union of everyone seen in standings/submission embeds across
    the game (so a player who left before the final standings is still counted).
    The winner is always folded into the roster — they plainly played.
    """
    roster: set[int] = set()
    winner: int | None = None
    for msg in window:
        embeds = msg.get("embeds") or []
        roster |= players_from_standings(embeds)
        roster |= players_from_submissions(embeds)
        w = winner_from_game_over(embeds)
        if w is not None:
            winner = w
    if winner is not None:
        roster.add(winner)
    return roster, winner
