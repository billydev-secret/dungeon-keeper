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
# Same title, read from the front: everything before "is starting" is the host's
# username (which can contain dots, underscores and spaces).
_START_HOST = re.compile(r"^(?P<host>.+?)\s+is starting an? .+ game!\s*$", re.IGNORECASE)
# Gamebot's own game names → our sub-game key. A name that isn't here is a game
# we don't parse (Chess, Othello, Poker, Survey Says, Wisecracks, …); it still
# bounds a window, but pays nobody.
_START_GAMES: dict[str, str] = {
    "cards against humanity": GAME_CAH,
    "cards against humanity: family edition": GAME_CAH,
    "connect 4": GAME_CONNECT4,
    "anagrams": GAME_ANAGRAMS,
}

# The reverse direction, for anything that has to *show* a Gamebot game to a
# member (Event Echo's main-chat announcement). Kept here because this module
# already owns Gamebot's vocabulary — a rename lands in one file rather than
# in a feature that has never heard of Gamebot. Canonical rather than derived
# from `_START_GAMES`, whose CAH key has two spellings.
GAME_LABELS: dict[str, str] = {
    GAME_CAH: "Cards Against Humanity",
    GAME_CONNECT4: "Connect 4",
    GAME_ANAGRAMS: "Anagrams",
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


def host_from_lobby(embeds: Sequence[Mapping[str, Any]]) -> str | None:
    """The host's Discord **username** from a Gamebot lobby embed title.

    Gamebot names whoever started the game in the lobby title itself
    ("efficientpanic is starting a Cards Against Humanity game!") — by
    username, not mention, so the caller resolves it by name the way the
    Anagrams and Cat Bot paths do. This is the only place an external game
    identifies its host, which is why external games paid no host bounty at
    all until 2026-07-26.
    """
    for title, _desc in _embed_texts(embeds):
        m = _START_HOST.match(title.strip())
        if m:
            return _unescape_markdown(m.group("host").strip())
    return None


def host_from_window(window: Sequence[Mapping[str, Any]]) -> str | None:
    """The host username for a whole game window, from its lobby embed."""
    for msg in window:
        host = host_from_lobby(msg.get("embeds") or [])
        if host:
            return host
    return None


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

# Rarity → tier → coins (defaults; per-guild EconSettings.catcatch_coins_*
# override these — the cog passes the guild's table into parse_cat_catch).
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


def rarity_coins(rarity: str, tier_coins: Mapping[str, int] | None = None) -> int:
    """Coins for catching a cat of ``rarity`` (unknown rarities fall to common).

    ``tier_coins`` overrides the default table per guild (the six
    ``EconSettings.catcatch_coins_*`` dials); missing tiers fall back to the
    shipped defaults so a partial mapping can't zero a tier by accident."""
    tier = _RARITY_TIER.get(rarity.lower(), _DEFAULT_TIER)
    if tier_coins is not None:
        return int(tier_coins.get(tier, _TIER_COINS[tier]))
    return _TIER_COINS[tier]


@dataclass(frozen=True)
class CatCatch:
    """One resolved Cat Bot catch. ``coins`` already folds in the blessed×2."""

    username: str
    rarity: str
    doubled: bool
    coins: int


def parse_cat_catch(
    content: str, tier_coins: Mapping[str, int] | None = None
) -> CatCatch | None:
    """Extract (catcher username, rarity, doubled, coins) from a catch, or None.

    Only an *individual* catch parses — spawns ("has appeared", no "cought") and
    the bonus blurb ("Anyone who cought this cat…", where "cought" isn't next to
    the emoji) return None, so they never pay. ``tier_coins`` is the guild's
    dial table (see :func:`rarity_coins`).
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
        coins = rarity_coins(rarity, tier_coins) * (2 if doubled else 1)
        return CatCatch(username=username, rarity=rarity, doubled=doubled, coins=coins)
    return None


# ── Wordle (kind='wordle') ───────────────────────────────────────────────────
#
# The Wordle bot posts one self-contained daily digest per group — no embeds,
# no lobby, nothing to scan backwards for:
#
#     **Your group is on a 9 day streak!** 🔥 Here are yesterday's results:
#     👑 3/6: <@490886726076727296>
#     4/6: <@203866639005908992>
#     X/6: <@1069501326184153088>
#
# 👑 marks the best line, `N/6` is "solved in N guesses" and `X/6` is a fail.
# A line can carry several players (ties are common), so there are usually
# several winners. Scoring is **inverted** relative to CAH/Anagrams — fewer
# guesses is better — so it's flipped to a plain "higher is better" score here
# and the generic score-proportional payout does the rest.
#
# Players the bot couldn't mention render as a bare "@Name" instead, and at
# least one real case has a space in it ("@communal potato"), which makes the
# token boundary ambiguous. Those are reported for logging and never guessed
# at — the same posture as an unresolvable Cat Bot catcher.

WORDLE_MAX_GUESSES = 6

_WORDLE_RESULTS_MARK = "results:"
_WORDLE_LINE = re.compile(
    r"^(?P<crown>\U0001F451\s*)?(?P<guesses>[1-6X])/6:\s*(?P<players>.+)$"
)


@dataclass(frozen=True)
class WordleResults:
    """One day's group digest. Scores are higher-is-better (a 1/6 scores
    ``WORDLE_MAX_GUESSES``, a 6/6 scores 1, and a failed X/6 scores 0 — they
    played, so they're in the roster, they just earn no coins).

    Players come in two flavours because Wordle only mentions some of them:
    ``scores``/``winners`` are keyed by member id, ``named_scores``/
    ``named_winners`` by the bare ``@Name`` the bot printed instead. The caller
    resolves the names against the guild, exactly as the Anagrams and Cat Bot
    paths do — roughly a fifth of the observed result lines need it.
    """

    scores: dict[int, int]
    named_scores: dict[str, int]
    winners: frozenset[int]
    named_winners: frozenset[str]


def wordle_score(guesses: str) -> int:
    """Invert a ``N/6`` guess count into a higher-is-better score."""
    if guesses.upper() == "X":
        return 0
    return WORDLE_MAX_GUESSES + 1 - int(guesses)


def parse_wordle_results(content: str) -> WordleResults | None:
    """Parse a Wordle daily digest, or None if this isn't one.

    Only the digest parses — the bot's chatter ("<name> is playing") has no
    result lines and returns None, so it never pays.
    """
    if not content or _WORDLE_RESULTS_MARK not in content:
        return None
    scores: dict[int, int] = {}
    named_scores: dict[str, int] = {}
    winners: set[int] = set()
    named_winners: set[str] = set()
    for line in content.splitlines():
        m = _WORDLE_LINE.match(line.strip())
        if not m:
            continue
        score = wordle_score(m.group("guesses"))
        crowned = bool(m.group("crown"))
        players = m.group("players")
        for raw in _MENTION.findall(players):
            uid = int(raw)
            scores[uid] = score
            if crowned:
                winners.add(uid)
        # Whatever is left once the real mentions are stripped is one or more
        # players the bot printed as plain text. Split on the '@' rather than on
        # whitespace: display names can contain spaces ("@communal potato"), so
        # tokenising would shred them, while several names on one line always
        # arrive as "@A @B".
        leftover = _MENTION.sub("", players)
        for chunk in leftover.split("@")[1:]:
            name = chunk.strip()
            if not name:
                continue
            named_scores[name] = score
            if crowned:
                named_winners.add(name)
    if not scores and not named_scores:
        return None
    return WordleResults(
        scores=scores,
        named_scores=named_scores,
        winners=frozenset(winners),
        named_winners=frozenset(named_winners),
    )


# ── Co-ordle (kind='coordle') ────────────────────────────────────────────────
#
# Co-ordle is a *co-operative* word puzzle: one 6-letter word per hourly round,
# which the whole channel solves together. Its board embed is titled
# "Co-ordle for <t:1785103200:f>" and each filled row is one player's guess:
#
#     **`1.`** <:green_s:…><:gray_p:…>… <@!1069501326184153088> **+4**
#     **`2.`** <:white_square:…>×6                       (an unplayed row)
#
# Two things make it unlike every other tracked game:
#
# * **A new message is posted per guess**, each showing the whole board so far
#   — so the *last* board of a round is the final one, and the payout can't be
#   keyed on a message id without paying every guess. It's keyed on the round's
#   own scheduled timestamp from the title instead (``coordle_game_key``),
#   which is the round's real identity. Those values (~1.7e9) can't collide
#   with Discord snowflakes (~1.5e18) in the shared payout ledger.
# * **There is no terminal message.** "This Co-ordle has ended" is only a
#   rejection sent to a late guesser, never a broadcast. Finality is read off
#   the board itself: a row of six greens means solved, and a board with no
#   unplayed rows left is exhausted. A round that simply times out with rows
#   to spare stays 'open' and never pays — 16 of 1887 rounds in the observed
#   history, the accepted cost of having no end-of-round signal to listen for.
#
# Scores are the inline ``**+N**``, or ``**+N (+M)**`` where a bonus applies —
# in which case the player earned **N+M**. Checked against the bot's own
# cumulative leaderboard across consecutive snapshots: N+M reproduced the
# leaderboard delta for 79% of player-rounds and N alone for 0.5% (the
# remainder being rounds clipped by a snapshot boundary or by its top-10 cut).

_COORDLE_TITLE_MARK = "Co-ordle for "
_COORDLE_GAME_TS = re.compile(r"<t:(\d+):")
_COORDLE_ROW_MARK = "**`"
_COORDLE_CELL = re.compile(r"<a?:(green|yellow|gray|white)_(\w+?):\d+>")
_COORDLE_SCORE = re.compile(
    r"<@!?(\d+)>\s*\*\*\+(\d+)(?:\s*\(\+(\d+)\))?\*\*"
)

# Board states. Only the first two are final and therefore payable.
COORDLE_SOLVED = "solved"
COORDLE_EXHAUSTED = "exhausted"
COORDLE_OPEN = "open"
COORDLE_EMPTY = "empty"


def is_coordle_board(embeds: Sequence[Mapping[str, Any]]) -> bool:
    """True for a Co-ordle round board (not its leaderboard or rules embed)."""
    return any(
        title.startswith(_COORDLE_TITLE_MARK) for title, _desc in _embed_texts(embeds)
    )


def coordle_game_key(embeds: Sequence[Mapping[str, Any]]) -> int | None:
    """The round's scheduled unix timestamp — its stable identity across the
    many board messages one round posts. Used as the payout ledger key."""
    for title, _desc in _embed_texts(embeds):
        if title.startswith(_COORDLE_TITLE_MARK):
            m = _COORDLE_GAME_TS.search(title)
            if m:
                return int(m.group(1))
    return None


def extract_coordle_game(
    embeds: Sequence[Mapping[str, Any]]
) -> tuple[dict[int, int], int | None, str]:
    """``({member_id: points}, winner, state)`` from one Co-ordle board.

    ``winner`` is whoever played the solving row — the guess that came back all
    green — or None if the round wasn't solved. ``state`` is one of the
    ``COORDLE_*`` constants; only *solved* and *exhausted* are final.
    """
    scores: dict[int, int] = {}
    winner: int | None = None
    solved = False
    filled = unplayed = 0
    for title, desc in _embed_texts(embeds):
        if not title.startswith(_COORDLE_TITLE_MARK):
            continue
        for line in desc.splitlines():
            if not line.strip().startswith(_COORDLE_ROW_MARK):
                continue
            cells = _COORDLE_CELL.findall(line)
            if not cells:
                continue
            if all(colour == "white" and name == "square" for colour, name in cells):
                unplayed += 1
                continue
            filled += 1
            row_solved = all(colour == "green" for colour, _name in cells)
            solved = solved or row_solved
            m = _COORDLE_SCORE.search(line)
            if m:
                uid = int(m.group(1))
                points = int(m.group(2)) + (int(m.group(3)) if m.group(3) else 0)
                scores[uid] = scores.get(uid, 0) + points
                if row_solved:
                    winner = uid
    if solved:
        state = COORDLE_SOLVED
    elif filled == 0:
        state = COORDLE_EMPTY
    elif unplayed == 0:
        state = COORDLE_EXHAUSTED
    else:
        state = COORDLE_OPEN
    return scores, winner, state
