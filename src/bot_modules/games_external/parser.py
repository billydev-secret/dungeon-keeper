"""Parsers that turn banked external-bot messages into economy payouts.

Pure functions over the raw ``embeds`` dicts (``embed.to_dict()``) the collector
banks — no DB, no Discord I/O — so they're trivially testable against real
``/games track sample`` dumps and re-runnable on the full history.

Currently: Gamebot, which hosts more than one game in the same watched
channel — Cards Against Humanity and Connect 4 so far (kind='gamebot' covers
both; the cog tells them apart per terminal message). Both are a run of
messages in one channel ending in a *Game over!* embed. Players always render
as real mentions (``<@id>``), so rosters, scores, and winners are reliable:

* CAH — *Current Standings* embeds → each player's running score
  (``<@id>: N``). Later standings supersede earlier ones (the count is
  cumulative, not incremental), so only the last one before *Game over!*
  matters. *Submission status* embeds (``✅ <@id> Submitted!``) fold in
  players seen before any standings post, at score 0. *Game over!* →
  ``<@id> is the winner!``.
* Connect 4 — the *"<host> is starting a Connect 4 game!"* embed's
  **Joined Players** field, and the *Time's up!* recap (``<@id>, <@id> joined
  the game!``), give the roster. *Game over!* → ``<@id> has won!`` (a draw's
  exact wording is unconfirmed — no real sample yet — so an unrecognised
  finish just pays participation, no winner).

Both games reuse the same *Game over!* title, so window-bounding (telling two
back-to-back games apart) is title-only (``is_terminal``); telling *which*
game a terminal message belongs to is the more specific check (``is_game_over``
for CAH's "is the winner" phrasing, else it's Connect 4).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# <@123> / <@!123>, tolerant of the nickname bang.
_MENTION = re.compile(r"<@!?(\d+)>")
_STANDINGS_ENTRY = re.compile(r"<@!?(\d+)>\s*:\s*(\d+)")
_SUBMITTED = re.compile(r"<@!?(\d+)>\s+Submitted")
_WINNER = re.compile(r"<@!?(\d+)>\s+is the winner")
_C4_WON = re.compile(r"<@!?(\d+)>\s+has won")
_C4_START_MARK = "is starting a Connect 4 game!"
_C4_RECAP_TITLE = "Time's up!"
_C4_JOINED_FIELD = "Joined Players"

_GAME_OVER_TITLE = "Game over!"


def _embed_texts(embeds: Sequence[Mapping[str, Any]]):
    """Yield (title, description) for each embed dict, blanks coerced to ''."""
    for e in embeds:
        if isinstance(e, Mapping):
            yield str(e.get("title") or ""), str(e.get("description") or "")


def players_from_standings(embeds: Sequence[Mapping[str, Any]]) -> set[int]:
    """Member ids from a *Current Standings* embed (``<@id>: N`` lines)."""
    return set(scores_from_standings(embeds))


def scores_from_standings(embeds: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    """``{member_id: score}`` from a *Current Standings* embed."""
    out: dict[int, int] = {}
    for _title, desc in _embed_texts(embeds):
        for m in _STANDINGS_ENTRY.finditer(desc):
            out[int(m.group(1))] = int(m.group(2))
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
    """True when these embeds are Gamebot's **CAH** end-of-game announcement."""
    for title, desc in _embed_texts(embeds):
        if title.strip() == _GAME_OVER_TITLE and "is the winner" in desc:
            return True
    return False


def is_terminal(embeds: Sequence[Mapping[str, Any]]) -> bool:
    """True for any Gamebot *Game over!* embed, CAH or Connect 4 alike.

    Used to bound a game's message window — both games share this title, so
    telling *a* game apart from the next one only needs the title; telling
    *which* game it was needs the more specific ``is_game_over`` (CAH).
    """
    for title, _desc in _embed_texts(embeds):
        if title.strip() == _GAME_OVER_TITLE:
            return True
    return False


def current_game_window(
    parsed: Sequence[Mapping[str, Any]], over_index: int
) -> list[Mapping[str, Any]]:
    """Slice one game's messages: from just after the previous *Game over!* up
    to and including the one at ``over_index``.

    ``parsed`` is the channel's banked messages oldest-first, each a mapping with
    an ``embeds`` list. Bounding on the previous game-over (of either game
    type sharing this channel) keeps a busy channel's back-to-back games from
    bleeding rosters into each other.
    """
    start = 0
    for i in range(over_index - 1, -1, -1):
        if is_terminal(parsed[i].get("embeds") or []):
            start = i + 1
            break
    return list(parsed[start : over_index + 1])


def extract_cah_game(
    window: Sequence[Mapping[str, Any]]
) -> tuple[dict[int, int], int | None]:
    """(scores, winner) for one game's window of messages.

    ``scores`` reflects the *last* Current Standings embed in the window
    (each one is a full cumulative snapshot, so later ones supersede earlier
    ones rather than merging with them). A player only seen submitting before
    the first standings post is folded in at 0, and so is the winner if
    they're otherwise absent — they plainly played.
    """
    scores: dict[int, int] = {}
    winner: int | None = None
    for msg in window:
        embeds = msg.get("embeds") or []
        scores.update(scores_from_standings(embeds))
        for uid in players_from_submissions(embeds):
            scores.setdefault(uid, 0)
        w = winner_from_game_over(embeds)
        if w is not None:
            winner = w
    if winner is not None:
        scores.setdefault(winner, 0)
    return scores, winner


# ── Gamebot Connect 4 ─────────────────────────────────────────────────────────


def players_from_connect4_start(embeds: Sequence[Mapping[str, Any]]) -> set[int]:
    """Member ids from the join phase: the **Joined Players** field on the
    "*host* is starting a Connect 4 game!" embed, and/or the *Time's up!*
    recap's "``<@id>, <@id> joined the game!``" description — either is
    sufficient, both list the same roster."""
    out: set[int] = set()
    for e in embeds:
        if not isinstance(e, Mapping):
            continue
        title = str(e.get("title") or "")
        if _C4_START_MARK in title or title.strip() == _C4_RECAP_TITLE:
            out.update(int(m) for m in _MENTION.findall(str(e.get("description") or "")))
        for f in e.get("fields") or []:
            if isinstance(f, Mapping) and str(f.get("name") or "").strip() == _C4_JOINED_FIELD:
                out.update(int(m) for m in _MENTION.findall(str(f.get("value") or "")))
    return out


def winner_from_connect4_over(embeds: Sequence[Mapping[str, Any]]) -> int | None:
    """The winner's id from a Connect 4 *Game over!* embed (``<@id> has won!``),
    or None — including for a draw, whose exact wording isn't confirmed yet."""
    for _title, desc in _embed_texts(embeds):
        m = _C4_WON.search(desc)
        if m:
            return int(m.group(1))
    return None


def extract_connect4_game(
    window: Sequence[Mapping[str, Any]]
) -> tuple[set[int], int | None]:
    """(roster, winner) for one Connect 4 game's window of messages.

    The winner is folded into the roster even if somehow absent from the join
    phase — they plainly played.
    """
    roster: set[int] = set()
    winner: int | None = None
    for msg in window:
        embeds = msg.get("embeds") or []
        roster |= players_from_connect4_start(embeds)
        w = winner_from_connect4_over(embeds)
        if w is not None:
            winner = w
    if winner is not None:
        roster.add(winner)
    return roster, winner


# ── Cat Bot (kind='catbot') ──────────────────────────────────────────────────
#
# Cat Bot catches are message *content* (no embeds). A catch names the catcher
# by their Discord *username* (not a mention) next to the rarity emoji, e.g.
#     efficientpanic cought <:wildcat:12…> Wild cat!!!!1!
# Reverse cats print the whole line reversed but keep the emoji token intact:
#     …cat Reverse <:reversecat:12…> cought ceilruxdealta
# so the catcher is always the non-emoji token adjacent to "cought". Rarity is
# read from the emoji name (``<:wildcat:…>`` → ``wild``), which is reliable in
# both orders. A "blessed … got doubled!" line means Cat Bot doubled the catch.

# Rarity → tier → coins (defaults; a future dashboard panel can override).
# Tapered against an earlier flatter table (3/8/20/50/120/300): a 75% cut on the
# lowest tier scaling linearly to 0% at the top, so common catches barely pay
# while the rare top-end keeps its pull.
_TIER_COINS: dict[str, int] = {
    "common": 1, "uncommon": 3, "rare": 11, "epic": 35, "mythic": 102, "divine": 300,
}
# The 22 cat types grouped into tiers. NB the *Rare* cat sits in the *uncommon*
# tier — the tier name and that cat's name collide but mean different things.
_RARITY_TIER: dict[str, str] = {
    "fine": "common", "nice": "common", "good": "common",
    "rare": "uncommon", "wild": "uncommon", "gremlin": "uncommon",
    "epic": "rare", "sus": "rare", "brave": "rare", "rickroll": "rare", "reverse": "rare",
    "superior": "epic", "trash": "epic", "legendary": "epic",
    "mythic": "mythic", "8bit": "mythic", "corrupt": "mythic", "professor": "mythic",
    "divine": "divine", "real": "divine", "ultimate": "divine", "egirl": "divine",
}
_DEFAULT_TIER = "common"

_CAT_EMOJI = re.compile(r"^<a?:(\w+?)cat:\d+>$", re.IGNORECASE)
_BLESSED = "blessed your catch and it got doubled"


def rarity_coins(rarity: str) -> int:
    """Coins for catching a cat of ``rarity`` (unknown rarities fall to common)."""
    tier = _RARITY_TIER.get(rarity.lower(), _DEFAULT_TIER)
    return _TIER_COINS[tier]


@dataclass(frozen=True)
class CatCatch:
    """One resolved Cat Bot catch. ``coins`` already folds in the blessed×2."""

    username: str
    rarity: str
    doubled: bool
    coins: int


def parse_cat_catch(content: str) -> CatCatch | None:
    """Extract (catcher username, rarity, doubled, coins) from a catch, or None.

    Only an *individual* catch parses — spawns ("has appeared", no "cought") and
    the bonus blurb ("Anyone who cought this cat…", where "cought" isn't next to
    the emoji) return None, so they never pay.
    """
    if not content or "cought" not in content:
        return None
    tokens = content.split()
    for i, tok in enumerate(tokens):
        if tok != "cought":
            continue
        before = tokens[i - 1] if i > 0 else ""
        after = tokens[i + 1] if i + 1 < len(tokens) else ""
        m_after, m_before = _CAT_EMOJI.match(after), _CAT_EMOJI.match(before)
        if m_after:            # normal: "{username} cought <:emoji> …"
            username, rarity = before, m_after.group(1)
        elif m_before:         # reverse: "… <:emoji> cought {username}"
            username, rarity = after, m_before.group(1)
        else:
            continue           # "Anyone who cought this cat" — not an individual catch
        username = username.strip(",.!?")
        if not username or _CAT_EMOJI.match(username):
            continue
        rarity = rarity.lower()
        doubled = _BLESSED in content
        coins = rarity_coins(rarity) * (2 if doubled else 1)
        return CatCatch(username=username, rarity=rarity, doubled=doubled, coins=coins)
    return None
