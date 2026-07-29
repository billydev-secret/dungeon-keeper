"""Recover a game's roster from its stored payload.

A game's payout rides on ``end_game(bot=…, player_ids=…)``, and each cog builds
that roster from locals it has in hand at its own completion site. The two paths
that end a game from *outside* it — the 24-hour sweep and ``force_end_active_game``
(``/games end``, ``/games config game-end``) — have no such locals; all they have
is the payload row. This module reconstructs the roster from that payload, one
extractor per game type, each mirroring what its cog passes as ``player_ids``.

Keeping the extractors here rather than on the cogs is deliberate: the callers
are ``game_manager`` and ``expiry_service``, and importing a cog from either
would invert the dependency (cogs import *them*).

An unlisted game type returns an empty roster and so pays nobody. That is the
correct default in both directions: ffa banner posts and Photo Challenge have no
joined roster to credit, and a type whose roster genuinely can't be rebuilt from
the payload should stay unpaid rather than be paid a guess.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

log = logging.getLogger(__name__)


def _ints(values: Iterable[Any] | None) -> list[int]:
    """Coerce, drop junk, and de-duplicate — payload ids round-trip as strings."""
    out: list[int] = []
    for raw in values or []:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if uid not in out:
            out.append(uid)
    return out


# ── per-type extractors ───────────────────────────────────────────────────────
# Each returns (player_ids, round_count) and mirrors the cited completion site.

def _traditional(p: dict) -> tuple[list[int], int]:
    """games_traditional_cog._do_close — everyone who opted into a category."""
    return _ints(p.get("participants")), len(p.get("asked") or {})


def _clapback(p: dict) -> tuple[list[int], int]:
    """games_clapback_cog:1125 — the joined player list."""
    return _ints(p.get("players")), len(p.get("round_history") or [])


def _compliment(p: dict) -> tuple[list[int], int]:
    """games_compliment_cog:147 — the join pool."""
    return _ints(p.get("participants")), 0


def _mfk(p: dict) -> tuple[list[int], int]:
    """games_mfk_cog:170 — the join pool."""
    return _ints(p.get("participants")), 0


def _story(p: dict) -> tuple[list[int], int]:
    """games_story_cog:490 — the writer list."""
    return _ints(p.get("players")), 0


def _legitlibs(p: dict) -> tuple[list[int], int]:
    """legitlibs classic/quiplash — payload["players"], one scored round."""
    return _ints(p.get("players")), 1


def _rushmore(p: dict) -> tuple[list[int], int]:
    """games_rushmore_cog:1275 — draft_view.players is seeded from the payload."""
    return _ints(p.get("players")), len(p.get("rounds") or {})


def _ttl(p: dict) -> tuple[list[int], int]:
    """games_ttl_cog:596 — subjects whose rounds were revealed.

    Mirrors ``played_ids_from_payload``: the explicit ``played`` list, falling
    back to ``scores`` keys for payloads written before that list existed.
    """
    played = p.get("played")
    if played is None:
        played = list(p.get("scores") or {})
    return _ints(played), len(_ints(played))


def _nhie(p: dict) -> tuple[list[int], int]:
    """games_nhie_cog:489 — ``lives`` keeps eliminated players at 0 hp, so it is
    the full roster; a survivors-only set would drop an eliminated winner.
    """
    return _ints(p.get("lives") or {}), 0


def _ama(p: dict) -> tuple[list[int], int]:
    """games_ama_cog:808 — askers plus the hot seats who answered them.

    ``asker_id`` 0 is the AI idle-question sentinel and never counts.
    """
    questions = p.get("questions") or []
    roster: list[Any] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        for key in ("asker_id", "hot_seat_id"):
            val = q.get(key, 0)
            try:
                if int(val) > 0:
                    roster.append(val)
            except (TypeError, ValueError):
                continue
    return _ints(roster), len(questions)


def _hottakes(p: dict) -> tuple[list[int], int]:
    """games_hottakes_cog:494 — voters plus take authors.

    The winning take's author may never have voted, so a voters-only roster
    would drop their bonus.
    """
    results = p.get("results") or []
    roster: list[Any] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        roster.extend(r.get("voters") or [])
        if r.get("author") is not None:
            roster.append(r["author"])
    return _ints(roster), len(results)


def _fantasies(p: dict) -> tuple[list[int], int]:
    """Entry authors plus everyone who voted either way on an entry.

    Fantasies has no paying completion site of its own — every end path is a
    bare ``end_game`` — so this roster is what makes an ended game pay at all.
    """
    results = p.get("results") or []
    roster: list[Any] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("author") is not None:
            roster.append(r["author"])
        roster.extend(r.get("same_votes") or [])
        roster.extend(r.get("nope_votes") or [])
    return _ints(roster), len(results)


def _wyr(p: dict) -> tuple[list[int], int]:
    """games_wyr_cog._voter_roster — everyone who voted a or b in any round."""
    rounds = p.get("rounds") or {}
    roster: list[Any] = []
    for rd in rounds.values():
        if not isinstance(rd, dict):
            continue
        roster.extend(rd.get("a") or [])
        roster.extend(rd.get("b") or [])
    return _ints(roster), len(rounds)


def _mlt(p: dict) -> tuple[list[int], int]:
    """games_mlt_cog._voter_roster — everyone who cast a vote in any round.

    Deliberately not the survivors-only ``players`` list, which would drop
    members who voted for several rounds and then left.
    """
    rounds = p.get("rounds") or {}
    roster: list[Any] = []
    for rd in rounds.values():
        if isinstance(rd, dict):
            roster.extend(rd.get("votes") or {})
    return _ints(roster), len(rounds)


def _price(p: dict) -> tuple[list[int], int]:
    """games_price_cog:1066 — every uid that submitted a price in any round.

    Mirrors ``_player_prices_from_rounds``: round payloads store ``prices`` as
    ``{str(uid): amount}``.
    """
    rounds = p.get("rounds") or {}
    roster: list[Any] = []
    for rd in rounds.values():
        if isinstance(rd, dict):
            roster.extend(rd.get("prices") or {})
    return _ints(roster), len(rounds)


_EXTRACTORS: dict[str, Callable[[dict], tuple[list[int], int]]] = {
    "traditional": _traditional,
    "clapback": _clapback,
    "compliment": _compliment,
    "mfk": _mfk,
    "story": _story,
    "legitlibs": _legitlibs,
    "rushmore": _rushmore,
    "ttl": _ttl,
    "nhie": _nhie,
    "ama": _ama,
    "hottakes": _hottakes,
    "fantasies": _fantasies,
    "wyr": _wyr,
    "mlt": _mlt,
    "price": _price,
}

# Types with no joined roster, listed so their absence above reads as a decision
# rather than an oversight: ffa posts a prompt card (banner mode ends at launch),
# photo is post-based and paid by the economy's own photo_post trigger.
NO_ROSTER_TYPES = frozenset({"ffa", "photo"})


def roster_from_payload(game_type: str, payload: dict | None) -> tuple[list[int], int]:
    """Return ``(player_ids, round_count)`` rebuilt from a game's stored payload.

    Never raises: a malformed payload costs its own game a roster, not the whole
    sweep it was found in.
    """
    extractor = _EXTRACTORS.get(game_type)
    if extractor is None:
        return [], 0
    try:
        return extractor(payload or {})
    except Exception:
        log.exception("roster extraction failed for game_type %s", game_type)
        return [], 0
