from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_ZERO_WIDTH = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")
_DANGEROUS_PREFIX = re.compile(r"^[@#/]")
_EVERYONE_HERE = re.compile(r"\b(everyone|here)\b", re.IGNORECASE)

DEFAULT_NICK_DENYLIST: list[str] = [
    r"\bn[i1]gg[ae3]r\b",
    r"\bf[a@]gg[o0]t\b",
    r"\br[e3]t[a@]rd\b",
]


@dataclass
class FilterResult:
    ok: bool
    value: str
    reason: str | None


def _clean(text: str) -> str:
    text = _ZERO_WIDTH.sub("", text)
    # NFKC (compatibility) folds fullwidth/homoglyph/combining forms so the
    # denylist can't be bypassed with lookalike Unicode.
    return unicodedata.normalize("NFKC", text).strip()


def _extra_word_pattern(word: str) -> str:
    """A configured banned word as a literal, whole-word regex.

    The word is escaped, so admin-typed punctuation (``c++``) bans that literal
    text instead of exploding (``re.error``) or silently never matching. The
    boundary guards are added only on the ends that are word characters, which
    is what keeps a short entry from swallowing longer words it merely sits
    inside — "ass" must not block "class" or "Cassandra" — while an entry like
    ``c++`` still matches, its ``+`` needing no boundary at all.
    """
    lead = r"(?<!\w)" if _is_word_char(word[:1]) else ""
    tail = r"(?!\w)" if _is_word_char(word[-1:]) else ""
    return f"{lead}{re.escape(word)}{tail}"


def _is_word_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch == "_")


def _denylist_hit(cleaned: str, denylist: list[str] | None) -> bool:
    """True when *cleaned* trips the built-in denylist or a configured word.

    The built-in entries above are regexes. The configured extras are not: they
    come from the "Extra Banned Words" box on each duel game's dashboard panel,
    so they are matched literally and as whole words (see
    ``_extra_word_pattern``).
    """
    if any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in DEFAULT_NICK_DENYLIST):
        return True
    return any(
        re.search(_extra_word_pattern(word.strip()), cleaned, re.IGNORECASE)
        for word in (denylist or [])
        if word.strip()
    )


def contains_disallowed_content(text: str, denylist: list[str] | None = None) -> bool:
    """True if the cleaned text matches the slur/abuse denylist.

    Reusable for free-text fields beyond nicknames/stakes (e.g. game question
    prompts, confessions) so they share one guard.
    """
    return _denylist_hit(_clean(text), denylist)


def validate_nickname(
    raw: str,
    *,
    max_length: int = 32,
    denylist: list[str] | None = None,
    admin_display_names: list[str] | None = None,
    all_member_display_names: list[str] | None = None,
) -> FilterResult:
    """Full nickname filter pipeline. Returns FilterResult(ok, cleaned_value, reason)."""
    cleaned = _clean(raw)
    if not cleaned:
        return FilterResult(ok=False, value=raw, reason="Nickname cannot be blank after cleaning.")
    if len(cleaned) > max_length:
        return FilterResult(
            ok=False, value=raw, reason=f"Nickname must be {max_length} characters or fewer."
        )
    if _denylist_hit(cleaned, denylist):
        return FilterResult(ok=False, value=raw, reason="Nickname contains disallowed content.")
    if _DANGEROUS_PREFIX.search(cleaned):
        return FilterResult(
            ok=False, value=raw, reason="Nickname cannot start with @, #, or /."
        )
    if _EVERYONE_HERE.search(cleaned):
        return FilterResult(
            ok=False, value=raw, reason="Nickname cannot contain 'everyone' or 'here'."
        )
    for name in admin_display_names or []:
        if cleaned.lower() == name.lower():
            return FilterResult(
                ok=False, value=raw, reason="Nickname impersonates a server admin or mod."
            )
    for name in all_member_display_names or []:
        if cleaned.lower() == name.lower():
            return FilterResult(
                ok=False,
                value=raw,
                reason="That display name is already taken by a server member.",
            )
    return FilterResult(ok=True, value=cleaned, reason=None)


def validate_stakes(
    raw: str,
    *,
    max_length: int = 200,
    denylist: list[str] | None = None,
) -> FilterResult:
    """Lighter filter for stakes text — strip, length, and denylist only."""
    cleaned = _clean(raw)
    if len(cleaned) > max_length:
        return FilterResult(
            ok=False, value=raw, reason=f"Stakes must be {max_length} characters or fewer."
        )
    if _denylist_hit(cleaned, denylist):
        return FilterResult(
            ok=False, value=raw, reason="Stakes text contains disallowed content."
        )
    return FilterResult(ok=True, value=cleaned, reason=None)


# A coin wager with no custom stakes text and no rename is its own stake: the
# pot replaces the nickname forfeit. Recorded as the game's stakes_text at
# creation so every embed has something to render.
WAGER_STAKES_TEXT = "Coins on the line — winner takes the pot."

#: What the "📋 Stakes" line says when the loser's nickname is on the table
#: alongside something else. A nickname-only game keeps ``stakes_text = None``
#: and each cog's own fallback wording, exactly as before.
NICK_STAKES_LINE = "🏷️ Loser surrenders their nickname for 24 hours."


def resolve_nick_stake(
    stakes_text: str | None, wager: int | None, nickname: bool | None
) -> bool:
    """Whether this game renames the loser.

    ``nickname`` is the challenger's explicit choice; ``None`` means they said
    nothing, in which case a game with no other stake on it is a nickname game
    (that is the historic default and what "just challenge someone" should
    still mean) and a game that already has coins or custom stakes riding on it
    is not.

    The point of the flag is that the three stakes are now independent. Before
    it, nickname mode was inferred as ``stakes_text is None``, so naming any
    other stake silently cancelled the rename — a Pressure Cooker game staked
    as "24 hour nickname change" *plus* 500 coins offered nobody a rename
    button, and the players spent the aftermath asking where it was ("It
    didn't give me the option!", game night 2026-08-21).
    """
    if nickname is not None:
        return bool(nickname)
    return stakes_text is None and wager is None


def resolve_stakes_text(
    stakes_text: str | None,
    wager: int | None,
    nick_stake: bool | None = None,
    wager_line: str | None = None,
) -> str | None:
    """The stakes string to persist for a game — one line per live stake.

    A nickname-only game (nothing else staked) still persists ``None``, which
    keeps every cog's own fallback wording and every pre-flag row rendering
    exactly as it did. Anything else composes: the challenger's own text
    first, then the wager, then the nickname forfeit, so all of a game's
    stakes are visible on every embed that renders this field rather than the
    coins only turning up at settlement ("Oh there were 2 stakes 👀").

    ``wager_line`` lets the caller pass a line already formatted in the
    guild's currency vocabulary; without it the amount is rendered plainly.
    ``nick_stake`` defaults to the legacy inference so existing callers that
    don't know about the flag behave as before.
    """
    if nick_stake is None:
        nick_stake = stakes_text is None and wager is None
    lines: list[str] = []
    if stakes_text:
        lines.append(stakes_text)
    if wager is not None:
        lines.append(wager_line or f"💰 **{wager:,}** each — winner takes the pot.")
    if nick_stake:
        lines.append(NICK_STAKES_LINE)
    if not lines:
        return None
    if lines == [NICK_STAKES_LINE]:
        # Plain nickname duel: leave it null so nothing about the oldest and
        # most common shape of game changes.
        return None
    return "\n".join(lines)


def game_is_nick_stake(game: object) -> bool:
    """Whether this game's loser gets renamed.

    The explicit ``nick_stake`` flag decides it. When the flag is off *and*
    nothing else is staked either, the game is still a nickname duel: a duel
    with no stake at all isn't a thing, that has always been the default, and
    it is what keeps rows written before migration 177 — and rows created
    straight through the db helper — meaning what they meant.
    """
    if getattr(game, "nick_stake", None):
        return True
    return getattr(game, "stakes_text", None) is None
