"""Parsers that turn banked external-bot messages into economy payouts.

Pure functions over the raw ``embeds`` dicts (``embed.to_dict()``) the collector
banks — no DB, no Discord I/O — so they're trivially testable against real
``/games track sample`` dumps and re-runnable on the full history.

Currently: Gamebot, which hosts many games in the same watched channel —
Cards Against Humanity, Connect 4 and Anagrams so far (kind='gamebot' covers
all of them; ``identify_game`` tells them apart). Each is a run of messages in
one channel that opens with a *"<host> is starting a <Game> game!"* lobby embed
and closes with a *Game over!* embed.

**Everything here is a pure function of the banked message list** — no game
registry, nothing tracked while a game is in flight. A finished game is
reconstructed by scanning *backwards* from its terminal message, which is
unavoidable: the *Game over!* embed carries only the winner's mention, never
the roster or the scores.

* CAH — *Current Standings* embeds → each player's running score
  (``<@id>: N``). Later standings supersede earlier ones (the count is
  cumulative, not incremental), so only the last one before *Game over!*
  matters. *Submission status* embeds (``✅ <@id> Submitted!``) fold in
  players seen before any standings post, at score 0. *Game over!* →
  ``<@id> is the winner!``.
* Connect 4 — roster from the join phase (see ``players_from_join_phase``).
  *Game over!* → ``<@id> has won!`` (a draw's exact wording is unconfirmed —
  no real sample yet — so an unrecognised finish just pays participation, no
  winner).
* Anagrams — a *Scoreboard* embed whose **field names** carry the scores as
  ``"<username> - N POINTS"``. Unlike the other two, players are named by
  Discord *username*, not mention, so the caller resolves them the same way
  the Cat Bot path does. *Game over!* → ``<@id> is the winner!``.

Telling the sub-games apart is done from the **lobby embed**, not the terminal:
CAH and Anagrams share the *exact* same ``<@id> is the winner!`` phrasing, so a
terminal message can never distinguish them (it was mis-attributing every
Anagrams game to CAH until 2026-07-26). The lobby embed names the game
outright, so ``current_game_window`` stops the backward scan there and
``identify_game`` reads the type off it. A lobby for a game we don't parse
(Chess, Poker, …) yields ``None`` rather than a wrong guess, so it pays nobody.
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
_RECAP_TITLE = "Time's up!"
_JOINED_FIELD = "Joined Players"
_STANDINGS_TITLE = "Current Standings"
_SCOREBOARD_TITLE = "Scoreboard"
# An Anagrams Scoreboard field name: "efficientpanic - 900 POINTS". The trailing
# "Pangram" field has no score and is skipped by the same pattern.
_SCORE_FIELD = re.compile(r"^(.+?)\s+-\s+(\d+)\s+POINTS$")

_GAME_OVER_TITLE = "Game over!"

# ── sub-games ────────────────────────────────────────────────────────────────
#
# Gamebot's lobby embed — "<host> is starting a <Game> game!" — is the only
# place a game names itself. Everything downstream (window bounding, payout
# dispatch) keys off it rather than off the terminal message's wording.
GAME_CAH = "cah"
GAME_CONNECT4 = "connect4"
GAME_ANAGRAMS = "anagrams"

_START_TITLE = re.compile(r"\bis starting an? (.+?) game!", re.IGNORECASE)
# Gamebot's own game names → our sub-game key. A name that isn't here is a game
# we don't parse (Chess, Othello, Poker, Survey Says, Wisecracks, …); it still
# bounds a window, but pays nobody.
_START_GAMES: dict[str, str] = {
    "cards against humanity": GAME_CAH,
    "cards against humanity: family edition": GAME_CAH,
    "connect 4": GAME_CONNECT4,
    "anagrams": GAME_ANAGRAMS,
}

# A lobby that timed out without enough players. Gamebot still posts a *Game
# over!* for it, but no game was played, so it must pay nobody.
_ABANDONED_MARK = "Not enough players joined the game!"


def _embed_texts(embeds: Sequence[Mapping[str, Any]]):
    """Yield (title, description) for each embed dict, blanks coerced to ''."""
    for e in embeds:
        if isinstance(e, Mapping):
            yield str(e.get("title") or ""), str(e.get("description") or "")


def players_from_standings(embeds: Sequence[Mapping[str, Any]]) -> set[int]:
    """Member ids from a *Current Standings* embed (``<@id>: N`` lines)."""
    return set(scores_from_standings(embeds))


def scores_from_standings(embeds: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    """``{member_id: score}`` from a *Current Standings* embed.

    Gated on the embed title so a stray ``<@id>: N`` elsewhere in a game's
    chatter can't invent a player or a score.
    """
    out: dict[int, int] = {}
    for title, desc in _embed_texts(embeds):
        if title.strip() != _STANDINGS_TITLE:
            continue
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


def is_game_start(embeds: Sequence[Mapping[str, Any]]) -> bool:
    """True for any Gamebot lobby embed (``<host> is starting a <Game> game!``),
    including games we don't parse."""
    return any(_START_TITLE.search(title) for title, _desc in _embed_texts(embeds))


def game_from_start(embeds: Sequence[Mapping[str, Any]]) -> str | None:
    """The sub-game key a lobby embed names, or None.

    None means either "not a lobby embed" or "a Gamebot game we don't parse" —
    both of which must pay nobody, so the caller doesn't need to tell them
    apart. Use ``is_game_start`` when the distinction matters.
    """
    for title, _desc in _embed_texts(embeds):
        m = _START_TITLE.search(title)
        if m:
            return _START_GAMES.get(m.group(1).strip().lower())
    return None


def current_game_window(
    parsed: Sequence[Mapping[str, Any]], over_index: int
) -> list[Mapping[str, Any]]:
    """Slice one game's messages, ending at the *Game over!* at ``over_index``.

    ``parsed`` is the channel's banked messages oldest-first, each a mapping
    with an ``embeds`` list. The scan walks backwards and stops at whichever
    boundary it meets first:

    * this game's own **lobby embed** (kept — it names the game and carries the
      joined roster), or
    * the **previous game's terminal** (excluded), for a game whose lobby has
      aged out of the banked slice.

    Anchoring on the lobby is what makes a busy channel safe: the window holds
    exactly one game's messages, so neither rosters nor scores can bleed across
    games, and ``identify_game`` gets an unambiguous type to dispatch on.
    """
    start = 0
    for i in range(over_index - 1, -1, -1):
        embeds = parsed[i].get("embeds") or []
        if is_game_start(embeds):
            start = i
            break
        if is_terminal(embeds):
            start = i + 1
            break
    return list(parsed[start : over_index + 1])


def identify_game(window: Sequence[Mapping[str, Any]]) -> str | None:
    """Which sub-game a window is, or None if it's one we don't pay out.

    Reads the lobby embed, which names the game outright — the terminal message
    can't, since CAH and Anagrams share the identical ``<@id> is the winner!``
    wording. Falls back to the window's shape only when no lobby is present
    (history truncated before the game began).
    """
    for msg in window:
        embeds = msg.get("embeds") or []
        if is_game_start(embeds):
            return game_from_start(embeds)
    return _infer_game(window)


def _infer_game(window: Sequence[Mapping[str, Any]]) -> str | None:
    """Best-effort type for a lobby-less window, from the embeds it does have."""
    saw_winner = False
    for msg in window:
        embeds = msg.get("embeds") or []
        for title, _desc in _embed_texts(embeds):
            if title.strip() == _STANDINGS_TITLE:
                return GAME_CAH
            if title.strip() == _SCOREBOARD_TITLE:
                return GAME_ANAGRAMS
        if winner_from_connect4_over(embeds) is not None:
            return GAME_CONNECT4
        saw_winner = saw_winner or is_game_over(embeds)
    # Nothing but a bare "<@id> is the winner!" to go on. CAH is by far the
    # more common of the two games that word it that way, and an Anagrams game
    # always posts its Scoreboard in the same window (caught above) — so this
    # only misfires on history truncated to the terminal message itself.
    return GAME_CAH if saw_winner else None


def is_abandoned(window: Sequence[Mapping[str, Any]]) -> bool:
    """True when the lobby timed out without enough players.

    Gamebot posts a *Game over!* for an abandoned lobby just as it does for a
    real game, so without this check a game nobody played still pays its
    would-be roster.
    """
    for msg in window:
        for _title, desc in _embed_texts(msg.get("embeds") or []):
            if _ABANDONED_MARK in desc:
                return True
    return False


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


def players_from_join_phase(embeds: Sequence[Mapping[str, Any]]) -> set[int]:
    """Member ids from a game's lobby — shared by every Gamebot sub-game, which
    all use the identical join phase:

    * the **Joined Players** field on the ``<host> is starting a <Game> game!``
      embed (plus the host mentioned in its description), and
    * the *Time's up!* recap's ``<@id>, <@id> joined the game!`` description.

    Both reads are gated on their embed's title. The **Joined Players** field
    used to be read off *any* embed, so an abandoned Cards Against Humanity
    lobby fed its roster straight into the Connect 4 payout.
    """
    out: set[int] = set()
    for e in embeds:
        if not isinstance(e, Mapping):
            continue
        title = str(e.get("title") or "")
        desc = str(e.get("description") or "")
        if _START_TITLE.search(title):
            out.update(int(m) for m in _MENTION.findall(desc))
            for f in e.get("fields") or []:
                if isinstance(f, Mapping) and str(f.get("name") or "").strip() == _JOINED_FIELD:
                    out.update(int(m) for m in _MENTION.findall(str(f.get("value") or "")))
        elif title.strip() == _RECAP_TITLE and "joined the game" in desc:
            out.update(int(m) for m in _MENTION.findall(desc))
    return out


# Kept as the Connect 4-flavoured name the payout path grew up with; the join
# phase turned out to be identical across sub-games, so it just delegates.
players_from_connect4_start = players_from_join_phase


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
        roster |= players_from_join_phase(embeds)
        w = winner_from_connect4_over(embeds)
        if w is not None:
            winner = w
    if winner is not None:
        roster.add(winner)
    return roster, winner


# ── Gamebot Anagrams ─────────────────────────────────────────────────────────


def scores_from_scoreboard(embeds: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """``{username: points}`` from an Anagrams *Scoreboard* embed.

    The scores live in the **field names** (``"efficientpanic - 900 POINTS"``),
    with the words each player found in the value. The trailing *Pangram* field
    has no score and so doesn't match. Players are named by Discord username —
    not mention — so the caller resolves them to members by name, exactly as
    the Cat Bot payout does. Names are markdown-unescaped defensively; unlike
    Cat Bot's, Gamebot's arrive raw (``dozer_nation``, no backslash).
    """
    out: dict[str, int] = {}
    for e in embeds:
        if not isinstance(e, Mapping):
            continue
        if str(e.get("title") or "").strip() != _SCOREBOARD_TITLE:
            continue
        for f in e.get("fields") or []:
            if not isinstance(f, Mapping):
                continue
            m = _SCORE_FIELD.match(str(f.get("name") or "").strip())
            if m:
                out[_unescape_markdown(m.group(1).strip())] = int(m.group(2))
    return out


def extract_anagrams_game(
    window: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, int], int | None]:
    """``({username: points}, winner_id)`` for one Anagrams game's window.

    Note the asymmetry, which is Gamebot's not ours: the scoreboard names
    players by **username** while *Game over!* names the winner by **mention**.
    The caller resolves the usernames and folds the winner in by id.

    Anagrams reuses CAH's exact ``<@id> is the winner!`` wording, which is why
    the sub-game has to be identified from the lobby embed rather than here.
    """
    scores: dict[str, int] = {}
    winner: int | None = None
    for msg in window:
        embeds = msg.get("embeds") or []
        scores.update(scores_from_scoreboard(embeds))
        w = winner_from_game_over(embeds)
        if w is not None:
            winner = w
    return scores, winner


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
# The username arrives markdown-*escaped* (``tryingnewthingz\_0504``) in either
# order, and the payout resolves it by name, so the escapes must come off.

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
# Cat Bot markdown-escapes usernames before printing them, so an underscore
# arrives as ``\_`` (``tryingnewthingz\_0504``). The escape survives verbatim in
# reverse-cat lines too. Left in, the name never matches a real member and the
# catch silently pays nobody, so strip the backslash off the markdown characters
# Discord escapes (underscore is the one that actually occurs in usernames).
_MD_ESCAPE = re.compile(r"\\([_*~`|\\>])")


def _unescape_markdown(name: str) -> str:
    return _MD_ESCAPE.sub(r"\1", name)


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
        username = _unescape_markdown(username.strip(",.!?"))
        if not username or _CAT_EMOJI.match(username):
            continue
        rarity = rarity.lower()
        doubled = _BLESSED in content
        coins = rarity_coins(rarity) * (2 if doubled else 1)
        return CatCatch(username=username, rarity=rarity, doubled=doubled, coins=coins)
    return None
