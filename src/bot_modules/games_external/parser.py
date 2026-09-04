"""Parsers that turn banked external-bot messages into economy payouts.

Pure functions over the raw ``embeds`` dicts (``embed.to_dict()``) the collector
banks — no DB, no Discord I/O — so they're trivially testable against real
``/games track sample`` dumps and re-runnable on the full history.

Currently: Gamebot, which hosts many games in the same watched channel —
Cards Against Humanity, Connect 4 and Anagrams so far (kind='gamebot' covers
all of them; ``identify_game`` tells them apart). Each is a run of messages in
one channel that opens with a *"<host> is starting a <Game> game!"* lobby embed
and closes with a terminal embed (*Game over!* before 2026-08-15, *Final
scores* after — see the CAH bullet below).

**Everything here is a pure function of the banked message list** — no game
registry, nothing tracked while a game is in flight. A finished game is
reconstructed by scanning *backwards* from its terminal message, which is
unavoidable: the *Game over!* embed carries only the winner's mention, never
the roster or the scores.

* CAH — standings → each player's running score (``<@id>: N``). Later
  standings supersede earlier ones (the count is cumulative, not incremental),
  so only the last one in the window matters. Submission embeds fold in
  players seen before any standings post, at score 0.

  Gamebot rewrote every one of those strings on **2026-08-15** and payouts
  stopped dead (last ``gamebot_cah`` row: 08-14), so both vocabularies are
  understood — the guild can have a pre-update game still in flight:

  - **standings** — was a *Current Standings* embed with ``<@id>: 3`` in its
    description; is now a *Standings* **field** hung off *Round winner* and
    *Final scores*, with the score bolded (``<@id>: **3**``).
  - **submissions** — was ``✅ <@id> Submitted!``; is now a *Submissions*
    field listing a bare ``✅ <@id>`` (or ``⬜ <@id>``, still to play).
  - **finish** — was *Game over!* + ``<@id> is the winner!``; is now
    *Final scores*, which declares no winner at all.
  - **lobby roster** — was a *Joined Players* field; is now *Players (6/12)*.

  The new format never names a winner, so it's derived as the top score.
  That can tie, and every tied leader wins (``extract_cah_game`` returns a
  *list*) — the payout faucet already takes a collection, since Wordle ties
  the same way.
* Connect 4 — roster from the join phase (see ``players_from_join_phase``).
  *Game over!* → ``<@id> has won!`` (a draw's exact wording is unconfirmed —
  no real sample yet — so an unrecognised finish just pays participation, no
  winner).
* Anagrams — a *Scoreboard* embed. Before 2026-08-15 its **field names**
  carried the scores as ``"<username> - N POINTS"``; since the rewrite the
  fields are empty and the **description** lists ``**DisplayName** — N
  points`` per player (payouts were silently dead for a week over exactly
  that — three real games unpaid, photo-external-99). Either way players are
  *named*, not mentioned, so the caller resolves them by name the way the Cat
  Bot path does. *Game over!* → ``<@id> is the winner!`` (old) or
  ``<@id> wins!`` (new).
* Survey Says and Wisecracks — a *Final scores* embed whose **description**
  lists ``**DisplayName**: N points`` per player. Survey Says adds a
  ``<@id> reached 5 points!`` line naming the winner; Wisecracks declares
  nobody and the winner is the top score once the names resolve. Survey Says
  also posts a *Game over!* (``<@id> wins!``) 0.7s after its *Final scores*,
  which bounds into a window of its own and pays nobody (see ``_infer_game``).

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
# "<@123>: 3" (old) and "<@123>: **3**" (new — the score is bolded).
_STANDINGS_ENTRY = re.compile(r"<@!?(\d+)>\s*:\s*\*{0,2}(\d+)\*{0,2}")
_SUBMITTED = re.compile(r"<@!?(\d+)>\s+Submitted")
# "<@123> is the winner!" (through 2026-08-14) / "<@123> wins!" (since).
_WINNER = re.compile(r"<@!?(\d+)>\s+(?:is the winner|wins!)")
_C4_WON = re.compile(r"<@!?(\d+)>\s+has won")
_RECAP_TITLE = "Time's up!"
_JOINED_FIELD = "Joined Players"
# The same lobby roster field, renamed and given a live count on 2026-08-15:
# "Joined Players" → "Players (6/12)".
_PLAYERS_FIELD = re.compile(r"^Players(\s*\(\d+/\d+\))?$")
_STANDINGS_TITLE = "Current Standings"
# Post-2026-08-15 the scores moved out of their own embed and into a field
# hung off *Round winner* and *Final scores*.
_STANDINGS_FIELD = "Standings"
_SUBMISSIONS_FIELD = "Submissions"
_SCOREBOARD_TITLE = "Scoreboard"
# An Anagrams Scoreboard field name: "efficientpanic - 900 POINTS". The trailing
# "Pangram" field has no score and is skipped by the same pattern.
_SCORE_FIELD = re.compile(r"^(.+?)\s+-\s+(\d+)\s+POINTS$")
# The same score, post-2026-08-15, as a description line: "**EP** — 1700 points".
# Anchored on the line so the trailing "**Skipped words:** COLOGNE" (no score)
# and the found-words line under each player never match.
_SCORE_LINE = re.compile(r"^\*\*(.+?)\*\*\s+[—–-]\s+(\d+)\s+points?\s*$", re.MULTILINE)
# Survey Says / Wisecracks *Final scores* line: "**EP**: 5 points". The
# per-round *Results* embeds carry the same line under a ✅/❌ prefix, which
# the line anchor rejects — and the read is gated on the title anyway.
_NAMED_FINAL_SCORE = re.compile(r"^\*\*(.+?)\*\*:\s+(\d+)\s+points?\s*$", re.MULTILINE)
# Survey Says' winner line inside *Final scores*: "<@123> reached 5 points!".
_REACHED = re.compile(r"<@!?(\d+)>\s+reached\s+\d+\s+points")

_GAME_OVER_TITLE = "Game over!"
# The post-2026-08-15 CAH finish. Unlike *Game over!* it declares no winner —
# it is just the last standings under a header — so the winner is derived.
# Survey Says and Wisecracks finish under the same title with named lines in
# the description instead of a *Standings* field.
_FINAL_SCORES_TITLE = "Final scores"
# Gamebot dropping a game mid-run. Seen once (2026-08-16), 1.4s *after* a
# perfectly good *Final scores* on a game that had already been won, so it is
# not on its own evidence of an abandoned game: it ends a window, and whatever
# standings that window holds are paid. A window containing only this message
# (the crash-after-a-clean-finish case) has no standings and so pays nobody,
# which is what stops the same game being paid twice.
_CRASHED_TITLE = "Something went wrong"

# ── sub-games ────────────────────────────────────────────────────────────────
#
# Gamebot's lobby embed — "<host> is starting a <Game> game!" — is the only
# place a game names itself. Everything downstream (window bounding, payout
# dispatch) keys off it rather than off the terminal message's wording.
GAME_CAH = "cah"
GAME_CONNECT4 = "connect4"
GAME_ANAGRAMS = "anagrams"
GAME_SURVEY_SAYS = "survey_says"
GAME_WISECRACKS = "wisecracks"

_START_TITLE = re.compile(r"\bis starting an? (.+?) game!", re.IGNORECASE)
# Same title, read from the front: everything before "is starting" is the host's
# username (which can contain dots, underscores and spaces).
_START_HOST = re.compile(r"^(?P<host>.+?)\s+is starting an? .+ game!\s*$", re.IGNORECASE)
# Gamebot's own game names → our sub-game key. A name that isn't here is a game
# we don't parse (Chess, Othello, Poker, …); it still bounds a window, but pays
# nobody.
_START_GAMES: dict[str, str] = {
    "cards against humanity": GAME_CAH,
    "cards against humanity: family edition": GAME_CAH,
    "connect 4": GAME_CONNECT4,
    "anagrams": GAME_ANAGRAMS,
    "survey says": GAME_SURVEY_SAYS,
    "wisecracks": GAME_WISECRACKS,
}
# The sub-games whose finish is a *Final scores* embed of named lines
# ("**Name**: N points") — one extractor serves both.
NAMED_SCORE_GAMES: frozenset[str] = frozenset({GAME_SURVEY_SAYS, GAME_WISECRACKS})

# The reverse direction, for anything that has to *show* a Gamebot game to a
# member (Event Echo's main-chat announcement). Kept here because this module
# already owns Gamebot's vocabulary — a rename lands in one file rather than
# in a feature that has never heard of Gamebot. Canonical rather than derived
# from `_START_GAMES`, whose CAH key has two spellings.
GAME_LABELS: dict[str, str] = {
    GAME_CAH: "Cards Against Humanity",
    GAME_CONNECT4: "Connect 4",
    GAME_ANAGRAMS: "Anagrams",
    GAME_SURVEY_SAYS: "Survey Says",
    GAME_WISECRACKS: "Wisecracks",
}

# A lobby that timed out without enough players. Gamebot still posts a *Game
# over!* for it, but no game was played, so it must pay nobody.
_ABANDONED_MARK = "Not enough players joined the game!"


def _embed_texts(embeds: Sequence[Mapping[str, Any]]):
    """Yield (title, description) for each embed dict, blanks coerced to ''."""
    for e in embeds:
        if isinstance(e, Mapping):
            yield str(e.get("title") or ""), str(e.get("description") or "")


def _embed_fields(embeds: Sequence[Mapping[str, Any]]):
    """Yield (field_name, field_value) for every field across these embeds."""
    for e in embeds:
        if not isinstance(e, Mapping):
            continue
        for f in e.get("fields") or []:
            if isinstance(f, Mapping):
                yield str(f.get("name") or "").strip(), str(f.get("value") or "")


def players_from_standings(embeds: Sequence[Mapping[str, Any]]) -> set[int]:
    """Member ids from a standings post (``<@id>: N`` lines)."""
    return set(scores_from_standings(embeds))


def scores_from_standings(embeds: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    """``{member_id: score}`` from a standings post, either format.

    Old: a *Current Standings* embed with the entries in its description.
    New (2026-08-15+): a *Standings* **field** hung off *Round winner* or
    *Final scores*, with the score bolded.

    Both reads stay gated — on the embed title before, on the field name now —
    so a stray ``<@id>: N`` in a game's ordinary chatter can't invent a player
    or a score.
    """
    out: dict[int, int] = {}
    for title, desc in _embed_texts(embeds):
        if title.strip() == _STANDINGS_TITLE:
            for m in _STANDINGS_ENTRY.finditer(desc):
                out[int(m.group(1))] = int(m.group(2))
    for name, value in _embed_fields(embeds):
        if name == _STANDINGS_FIELD:
            for m in _STANDINGS_ENTRY.finditer(value):
                out[int(m.group(1))] = int(m.group(2))
    return out


def players_from_submissions(embeds: Sequence[Mapping[str, Any]]) -> set[int]:
    """Member ids who submitted a card this round, either format.

    Old: a *Submission status* embed, ``✅ <@id> Submitted!`` in the
    description. New: a *Submissions* field on the *Play your card* embed,
    which dropped the word and lists a bare ``✅ <@id>`` per line — so the new
    read has to be gated on the field name, there being nothing else in the
    text to key on.

    Either way this is only ever additive at score 0: the round czar never
    appears (they're judging, not submitting), and standings cover everyone
    the moment the first round resolves.
    """
    out: set[int] = set()
    for _title, desc in _embed_texts(embeds):
        out.update(int(m) for m in _SUBMITTED.findall(desc))
    for name, value in _embed_fields(embeds):
        if name == _SUBMISSIONS_FIELD:
            out.update(int(m) for m in _MENTION.findall(value))
    return out


def winner_from_game_over(embeds: Sequence[Mapping[str, Any]]) -> int | None:
    """The winner's id from a *Game over!* embed, or None if not one."""
    for _title, desc in _embed_texts(embeds):
        m = _WINNER.search(desc)
        if m:
            return int(m.group(1))
    return None


def is_game_over(embeds: Sequence[Mapping[str, Any]]) -> bool:
    """True when these embeds are a score-carrying game's finish — as opposed
    to Connect 4's ``has won!`` *Game over!*.

    *Final scores* is CAH's finish since 2026-08-15 **and** Survey Says' /
    Wisecracks' (named lines instead of a *Standings* field); *Game over!*
    with a winner line is CAH's old finish and Anagrams' (either phrasing).
    Which game it was is the lobby's job (``identify_game``); this only says
    "somebody won something here". Deliberately does **not** cover *Something
    went wrong*: that ends a window without being evidence a game happened
    in it.
    """
    for title, desc in _embed_texts(embeds):
        title = title.strip()
        if title == _FINAL_SCORES_TITLE:
            return True
        if title == _GAME_OVER_TITLE and _WINNER.search(desc):
            return True
    return False


def is_terminal(embeds: Sequence[Mapping[str, Any]]) -> bool:
    """True for any Gamebot message that ends a game's run.

    Used both to bound a game's message window and, in the cog, as the trigger
    to pay one out. Three titles qualify:

    * *Game over!* — every sub-game's finish before 2026-08-15, and still
      Connect 4's as far as we know (no post-update sample exists).
    * *Final scores* — CAH's finish from 2026-08-15, and Survey Says' and
      Wisecracks' (a named-lines variant of the same title).
    * *Something went wrong* — Gamebot dropping a run. Terminal so that a game
      which dies mid-way still pays out the rounds that were actually played,
      and so that one trailing after a clean *Final scores* is bounded into a
      window of its own rather than re-paying the game before it.

    Telling *a* game apart from the next one only needs this; telling *which*
    game it was is the lobby's job (``identify_game``).
    """
    for title, _desc in _embed_texts(embeds):
        if title.strip() in (_GAME_OVER_TITLE, _FINAL_SCORES_TITLE, _CRASHED_TITLE):
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
    start, _bounded = find_window_start(parsed, over_index)
    return list(parsed[start : over_index + 1])


def find_window_start(
    parsed: Sequence[Mapping[str, Any]], over_index: int
) -> tuple[int, bool]:
    """``(start_index, bounded)`` for the game ending at ``over_index``.

    ``bounded`` is False when the scan ran off the front of ``parsed`` without
    meeting a lobby or a previous terminal — the slice the caller fetched may
    simply be too short for a long game (the largest real one is 214 Gamebot
    messages; photo-external-110), and it should fetch further back before
    trusting the window.
    """
    for i in range(over_index - 1, -1, -1):
        embeds = parsed[i].get("embeds") or []
        if is_game_start(embeds):
            return i, True
        if is_terminal(embeds):
            return i + 1, True
    return 0, False


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
    saw_old_winner = False
    for msg in window:
        embeds = msg.get("embeds") or []
        for title, _desc in _embed_texts(embeds):
            if title.strip() == _STANDINGS_TITLE:
                return GAME_CAH
            if title.strip() == _SCOREBOARD_TITLE:
                return GAME_ANAGRAMS
        # The new format has no standings embed of its own — the scores hang
        # off *Round winner* and *Final scores* as a field, and only CAH posts
        # one. A *Final scores* WITHOUT it is Survey Says' or Wisecracks',
        # which can't be told apart without the lobby — so it falls through
        # to None and pays nobody rather than guessing.
        if any(name == _STANDINGS_FIELD for name, _v in _embed_fields(embeds)):
            return GAME_CAH
        if winner_from_connect4_over(embeds) is not None:
            return GAME_CONNECT4
        for _title, desc in _embed_texts(embeds):
            saw_old_winner = saw_old_winner or "is the winner" in desc
    # Nothing but a bare "<@id> is the winner!" to go on. That is the
    # pre-2026-08-15 phrasing, when CAH was by far the more common of the two
    # games using it, and an Anagrams game always posts its Scoreboard in the
    # same window (caught above) — so this only misfires on history truncated
    # to the terminal message itself. The new "<@id> wins!" is never CAH
    # (CAH finishes on *Final scores* now): it is Anagrams' or the trailing
    # Game over! Survey Says posts after its own *Final scores*, so a bare one
    # is None.
    return GAME_CAH if saw_old_winner else None


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


def payable_game(window: Sequence[Mapping[str, Any]]) -> str | None:
    """The sub-game key when this window would be paid, else None.

    Mirrors the cog's own skip logic so the dashboard's "unpaid finishes"
    health count (photo-external-100) flags only windows the live path would
    actually credit: an abandoned lobby, a game we don't parse, a *Something
    went wrong* trailing a clean finish, a solo Anagrams run that scored
    nobody, or the *Game over!* Survey Says posts after its *Final scores*
    all pay nobody by design and so are not "unpaid".
    """
    if is_abandoned(window):
        return None
    game = identify_game(window)
    if game is None:
        return None
    if game == GAME_CAH:
        scores, _winners = extract_cah_game(window)
        return game if scores else None
    if game == GAME_CONNECT4:
        roster, _winner = extract_connect4_game(window)
        return game if roster else None
    if game == GAME_ANAGRAMS:
        named, winner = extract_anagrams_game(window)
        return game if named or winner is not None else None
    if game in NAMED_SCORE_GAMES:
        named, _winner = extract_named_scores_game(window)
        return game if named else None
    return None


def extract_cah_game(
    window: Sequence[Mapping[str, Any]]
) -> tuple[dict[int, int], list[int]]:
    """(scores, winners) for one game's window of messages.

    ``scores`` reflects the *last* standings post in the window (each one is a
    full cumulative snapshot, so later ones supersede earlier ones rather than
    merging with them). A player only seen submitting before the first
    standings post is folded in at 0, and so is a declared winner if they're
    otherwise absent — they plainly played.

    ``winners`` is a list because the new format stopped declaring a winner:

    * Through 2026-08-14 Gamebot said ``<@id> is the winner!`` outright, and
      that declaration is taken as-is when present — one name, always.
    * From 2026-08-15 it just prints *Final scores* and stops, so the winner
      is whoever tops the last standings. CAH is first-to-N, so a game that
      finishes has a unique leader; a tie only turns up in a game Gamebot
      dropped part-way, and then **every** tied leader wins. That's Billy's
      call (2026-08-16) and it costs nothing to honour — the payout faucet
      already accepts a collection of winner ids, since Wordle ties routinely.

    Empty when there's nothing to go on: no declaration and no scores, or an
    all-zero standings where nobody actually won a round.
    """
    scores: dict[int, int] = {}
    declared: int | None = None
    for msg in window:
        embeds = msg.get("embeds") or []
        scores.update(scores_from_standings(embeds))
        for uid in players_from_submissions(embeds):
            scores.setdefault(uid, 0)
        w = winner_from_game_over(embeds)
        if w is not None:
            declared = w
    if declared is not None:
        scores.setdefault(declared, 0)
        return scores, [declared]
    top = max(scores.values(), default=0)
    if top <= 0:
        return scores, []
    return scores, sorted(uid for uid, n in scores.items() if n == top)


# ── Gamebot Connect 4 ─────────────────────────────────────────────────────────


def players_from_join_phase(embeds: Sequence[Mapping[str, Any]]) -> set[int]:
    """Member ids from a game's lobby — shared by every Gamebot sub-game, which
    all use the identical join phase:

    * the roster field on the ``<host> is starting a <Game> game!`` embed —
      **Joined Players** before 2026-08-15, **Players (6/12)** after — plus
      the host mentioned in its description, and
    * the *Time's up!* recap's ``<@id>, <@id> joined the game!`` description.

    Both reads are gated on their embed's title. The roster field used to be
    read off *any* embed, so an abandoned Cards Against Humanity lobby fed its
    roster straight into the Connect 4 payout.
    """
    out: set[int] = set()
    for e in embeds:
        if not isinstance(e, Mapping):
            continue
        title = str(e.get("title") or "")
        desc = str(e.get("description") or "")
        if _START_TITLE.search(title):
            out.update(int(m) for m in _MENTION.findall(desc))
            for name, value in _embed_fields([e]):
                if name == _JOINED_FIELD or _PLAYERS_FIELD.match(name):
                    out.update(int(m) for m in _MENTION.findall(value))
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
    """``{name: points}`` from an Anagrams *Scoreboard* embed, either format.

    Old (through 2026-08-14): the scores live in the **field names**
    (``"efficientpanic - 900 POINTS"``), with the words each player found in
    the value; the trailing *Pangram* field has no score and so doesn't match.
    New: the fields are empty and the **description** lists ``**EP** — 1700
    points`` per player, each followed by their words (or "No words
    submitted."), with a scoreless ``**Skipped words:**`` line at the end.
    Players are *named* either way — username before, display name after —
    never mentioned, so the caller resolves them to members by name, exactly
    as the Cat Bot payout does. Names are markdown-unescaped defensively;
    unlike Cat Bot's, Gamebot's arrive raw (``dozer_nation``, no backslash).
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
        for m in _SCORE_LINE.finditer(str(e.get("description") or "")):
            out[_unescape_markdown(m.group(1).strip())] = int(m.group(2))
    return out


def extract_anagrams_game(
    window: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, int], int | None]:
    """``({username: points}, winner_id)`` for one Anagrams game's window.

    Note the asymmetry, which is Gamebot's not ours: the scoreboard names
    players by **username** while *Game over!* names the winner by **mention**.
    The caller resolves the usernames and folds the winner in by id.

    Anagrams reused CAH's exact ``<@id> is the winner!`` wording (and now
    shares ``<@id> wins!`` with Survey Says), which is why the sub-game has to
    be identified from the lobby embed rather than here.
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


# ── Gamebot Survey Says / Wisecracks ─────────────────────────────────────────


def scores_from_final_scores(embeds: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """``{display_name: points}`` from a named-lines *Final scores* embed.

    Gated on the title: the per-round *Results* embeds list the same running
    scores under a ✅/❌ prefix, and only the final one is the game's outcome.
    CAH's *Final scores* has no such lines (its scores are a *Standings*
    field of mentions) and reads as empty here.
    """
    out: dict[str, int] = {}
    for title, desc in _embed_texts(embeds):
        if title.strip() != _FINAL_SCORES_TITLE:
            continue
        for m in _NAMED_FINAL_SCORE.finditer(desc):
            out[_unescape_markdown(m.group(1).strip())] = int(m.group(2))
    return out


def extract_named_scores_game(
    window: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, int], int | None]:
    """``({display_name: points}, declared_winner_id)`` for a Survey Says or
    Wisecracks window.

    The scores are the **last** *Final scores* in the window. The winner is
    the ``<@id> reached N points!`` line Survey Says appends to it (or a
    ``<@id> wins!`` *Game over!* in the same window); Wisecracks declares
    nobody, so ``None`` means "derive the top scorer once the names resolve" —
    the caller does that, since a name that matches no member can't win.
    """
    scores: dict[str, int] = {}
    winner: int | None = None
    for msg in window:
        embeds = msg.get("embeds") or []
        found = scores_from_final_scores(embeds)
        if found:
            scores = found
        for title, desc in _embed_texts(embeds):
            if title.strip() == _FINAL_SCORES_TITLE:
                m = _REACHED.search(desc)
                if m:
                    winner = int(m.group(1))
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
