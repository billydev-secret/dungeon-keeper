"""Pure decision logic for the Story Builder (Exquisite Corpse) cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these from inside
its button callbacks, slash command, and the ``_run_story`` async loop;
the Discord glue (sending the message, persisting via ``modify_payload``)
stays in the cog.

High-leverage pieces:

* :func:`clamp_max_sentences` — bounds the slash command's
  ``max_sentences`` argument (2..30).
* :func:`resolve_starter` — supplies the default opening sentence when
  the host leaves the ``starter`` argument blank.
* :func:`add_player` / :func:`remove_player` — the join-lobby
  ``modify_payload`` closures become one-liners that delegate here.
* :func:`build_turn_order` — shuffles the writers; ``rng`` is injected
  so tests can pin the order.
* :func:`pick_current_player` — modular index lookup, so a turn loop
  that has gone past the last writer wraps back to the first.
* :func:`build_context` — what the modal shows in the "context" field:
  the last sentence (``blind`` visibility) or the whole story so far
  (``full`` visibility).
* :func:`append_sentence` — pushes a writer's submission onto the
  payload's ``sentences`` list.
* :func:`should_end_after_skip` — predicate the run loop checks each
  time a player is skipped; once every writer in the rotation has
  skipped, the story ends.
* :func:`assemble_story_text` — joins all sentence texts into the
  embed description string, escaping markdown and applying the 4090-char
  truncation budget (Discord embed description limit).
* :func:`build_attribution_lines` — one rendered ``**Name:** *text*``
  line per sentence; ``name_resolver`` maps author uids to display names
  so the cog can pass its guild-aware closure and tests can pass a dict.
* :func:`chunk_attribution_lines` — groups the rendered lines into field
  values bounded by the 1024-char per-field limit, so the reveal embed
  can spill into "pt. 2" / "pt. 3" fields.
"""

from __future__ import annotations

import random
from typing import Any, Callable

import discord

DEFAULT_STARTER: str = "Once upon a time, in a place no one quite remembered..."

# Discord embed description hard limit is 4096; cog historically uses 4090
# with a 3-char ellipsis budget (truncates to 4087 + "…").
_DESCRIPTION_BUDGET: int = 4090
_DESCRIPTION_TRUNC_TO: int = 4087

# Discord embed field-value hard limit.
_FIELD_VALUE_LIMIT: int = 1024


def clamp_max_sentences(n: int) -> int:
    """Clamp ``max_sentences`` to the cog's allowed range (2..30).

    Matches ``min(max(n, 2), 30)`` in the slash command body — exposed
    here so tests can pin the bounds and other entry points (web, /story
    variants) can reuse the same rule.
    """
    return min(max(n, 2), 30)


def resolve_starter(starter: str | None) -> str:
    """Return the opening sentence — host's text, or the default.

    Blank / ``None`` falls back to :data:`DEFAULT_STARTER` so the story
    always has something to riff off in sentence #2.
    """
    if not starter:
        return DEFAULT_STARTER
    return starter


def add_player(payload: dict[str, Any], user_id: int) -> None:
    """Append ``user_id`` to the payload's ``players`` list if absent.

    Mutates ``payload`` in place — the cog's join button's
    ``modify_payload`` closure delegates here.
    """
    players: list[int] = payload.setdefault("players", [])
    if user_id not in players:
        players.append(user_id)


def remove_player(payload: dict[str, Any], user_id: int) -> None:
    """Remove ``user_id`` from the payload's ``players`` list if present.

    Mutates ``payload`` in place — the cog's leave button's
    ``modify_payload`` closure delegates here.
    """
    players: list[int] = payload.setdefault("players", [])
    if user_id in players:
        players.remove(user_id)


def build_turn_order(
    players: list[int], rng: random.Random | None = None
) -> list[int]:
    """Return a shuffled copy of ``players`` for the writing rotation.

    ``rng`` is injected so tests can pin the order; defaults to the
    module ``random`` for production randomness. The returned list is a
    fresh object so mutating it doesn't perturb the caller's input.
    """
    chooser = rng if rng is not None else random
    order = list(players)
    chooser.shuffle(order)
    return order


def pick_current_player(turn_order: list[int], turn_index: int) -> int:
    """Look up the writer whose turn it is, wrapping modulo the rotation.

    The ``_run_story`` loop increments ``turn_index`` past the end of
    the list as turns advance; this helper handles the wrap so callers
    never have to think about it.
    """
    return turn_order[turn_index % len(turn_order)]


def build_context(sentences: list[dict[str, Any]], visibility: str) -> str:
    """Build the modal's "context" pre-fill for the current writer.

    * ``blind`` — only the most recent sentence is shown, so each writer
      riffs only off the previous beat (classic Exquisite Corpse).
    * Any other value (typically ``full``) — the entire story so far is
      shown, so writers can keep continuity.
    """
    if not sentences:
        return ""
    if visibility == "blind":
        return sentences[-1]["text"]
    return " ".join(s["text"] for s in sentences)


def append_sentence(
    payload: dict[str, Any], author_id: int | None, text: str
) -> list[dict[str, Any]]:
    """Append a new sentence dict to ``payload["sentences"]`` and return it.

    Mutates ``payload`` in place. ``author_id`` may be ``None`` for the
    starter line (rendered as "Narrator" in the reveal embed). Returns
    the updated list so the caller can pass it directly to
    ``update_game_payload`` without re-reading.
    """
    sentences: list[dict[str, Any]] = payload.setdefault("sentences", [])
    sentences.append({"author_id": author_id, "text": text})
    return sentences


def should_end_after_skip(consecutive_skips: int, num_writers: int) -> bool:
    """Return True iff every writer in the rotation has skipped in a row.

    When this trips the run loop ends the story early ("All writers
    were skipped — ending the story.") rather than spinning forever.
    """
    return consecutive_skips >= num_writers


def assemble_story_text(
    sentences: list[dict[str, Any]], max_len: int = _DESCRIPTION_BUDGET
) -> str:
    """Join all sentence texts into the reveal embed's description.

    Each sentence's stored text is escaped with
    ``discord.utils.escape_markdown`` (sentences are stored raw — see
    the cog) and joined with single spaces. If the result exceeds
    ``max_len``, it is truncated to ``max_len - 3`` characters plus an
    ellipsis so the embed stays under Discord's 4096-char description
    cap.
    """
    text = " ".join(discord.utils.escape_markdown(s["text"]) for s in sentences)
    if len(text) > max_len:
        text = text[: max_len - 3] + "…"
    return text


def build_attribution_lines(
    sentences: list[dict[str, Any]],
    name_resolver: Callable[[int], str],
) -> list[str]:
    """Render the per-sentence ``**Name:** *text*`` attribution lines.

    ``name_resolver`` maps a non-None author uid to a display name; the
    cog passes a guild-aware closure, tests pass a dict-backed lambda.
    A ``None`` author (the starter line) is attributed to "Narrator".
    Both the name and the text are escaped so user input never breaks
    markdown.
    """
    esc = discord.utils.escape_markdown
    lines: list[str] = []
    for s in sentences:
        author_id = s["author_id"]
        if author_id:
            name = esc(name_resolver(author_id))
        else:
            name = "Narrator"
        lines.append(f"**{name}:** *{esc(s['text'])}*")
    return lines


def chunk_attribution_lines(
    lines: list[str], max_field_len: int = _FIELD_VALUE_LIMIT
) -> list[list[str]]:
    """Group rendered lines into per-field chunks under the field-len cap.

    Each chunk's joined-with-newlines length stays at or under
    ``max_field_len`` (1024 — Discord embed field-value limit). The
    caller renders each chunk as one embed field, optionally labelled
    "(pt. N)" when there's more than one chunk. Mirrors the per-line
    accumulator in the cog's ``_reveal_story`` so the truncation
    behavior is preserved.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        # Match the cog's accumulator exactly: ``chunk_len + len(line) + 1``
        # (the +1 is the newline join separator).
        if current_len + len(line) + 1 > max_field_len and current:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append(current)
    return chunks
