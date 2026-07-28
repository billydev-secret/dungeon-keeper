"""Decide what a listing bot's message means: a bump, a refusal, or neither.

Listing bots deliver their confirmations three different ways, and a detector
has to read all of them:

* plain message content (Discadia's refusals)
* a classic embed (DISBOARD, Discodus)
* Components V2 — an empty ``content``, no embeds, and the visible text nested
  in ``text_display`` components (DH Bump, and Discadia's *successes*)

A refused bump usually echoes the success wording closely enough that a naive
substring match counts it as a bump and resets the timer, so ``classify``
checks the failure pattern first and lets it veto.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

Outcome = Literal["success", "failure"]

# Components V2 nests container → section → text_display. The cap is a guard
# against a malformed or cyclic tree, not a real structural limit.
_MAX_COMPONENT_DEPTH = 12


def component_texts(components: Iterable[Any], _depth: int = 0) -> list[str]:
    """Every ``text_display`` string in a component tree, outermost first.

    Walks duck-typed nodes — ``content`` is unique to ``TextDisplay``, and
    ``children``/``accessory`` are the only nesting attributes discord.py
    exposes on ``ActionRow``, ``Container`` and ``SectionComponent``.
    """
    found: list[str] = []
    if _depth > _MAX_COMPONENT_DEPTH:
        return found

    for node in components or ():
        content = getattr(node, "content", None)
        if isinstance(content, str) and content:
            found.append(content)

        children = getattr(node, "children", None)
        if children:
            found.extend(component_texts(children, _depth + 1))

        accessory = getattr(node, "accessory", None)
        if accessory is not None:
            found.extend(component_texts([accessory], _depth + 1))

    return found


def _embed_texts(embeds: Iterable[Any]) -> list[str]:
    """Every matchable string on an embed.

    Discodus reuses one title ("DISCODUS: Discord Server Booster") for both
    its bump and its refusal, so the title alone can never separate them — but
    it is still worth matching on, since other bots put the outcome there.
    """
    found: list[str] = []
    for embed in embeds or ():
        for attr in ("title", "description"):
            value = getattr(embed, attr, None)
            if isinstance(value, str) and value:
                found.append(value)

        for proxy_attr in ("footer", "author"):
            proxy = getattr(embed, proxy_attr, None)
            text = getattr(proxy, "text", None) or getattr(proxy, "name", None)
            if isinstance(text, str) and text:
                found.append(text)

        for field in getattr(embed, "fields", None) or ():
            for attr in ("name", "value"):
                value = getattr(field, attr, None)
                if isinstance(value, str) and value:
                    found.append(value)

    return found


def message_text(message: Any) -> str:
    """All text a detector may match on, lowercased and newline-joined.

    Takes a duck-typed message (``content``, ``embeds``, ``components``) so the
    matching rules can be tested without standing up Discord objects.
    """
    parts: list[str] = []

    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        parts.append(content)

    parts.extend(_embed_texts(getattr(message, "embeds", None) or ()))
    parts.extend(component_texts(getattr(message, "components", None) or ()))

    return "\n".join(parts).lower()


def classify(
    text: str,
    *,
    detector_pattern: str,
    failure_pattern: str,
) -> Outcome | None:
    """Classify already-lowercased message text for one site's detector.

    An empty ``detector_pattern`` means "anything this bot posts is a bump" —
    the setting every live guild was configured with, and the reason the
    failure veto has to be evaluated first.
    """
    if failure_pattern and failure_pattern.lower() in text:
        return "failure"
    if not detector_pattern or detector_pattern.lower() in text:
        return "success"
    return None


def match_site(
    sites: Sequence[Mapping[str, Any]],
    author_id: int,
    text: str,
) -> tuple[str, Outcome] | None:
    """First configured site whose detector bot posted this, and what it means.

    Returns ``None`` when no site claims the message. A site whose detector bot
    matches but whose *success* pattern does not is skipped, so two sites can
    share one bot and be told apart by their patterns.

    A ``failure`` ends the scan rather than skipping to the next site. When two
    sites share a detector bot this makes the outcome depend on row order, which
    is the deliberate trade: stopping can at worst miss recording a real bump
    (the timer stays stale and the role is pinged again later), whereas
    continuing would let a site with an empty — "match anything" — pattern claim
    a message another site just identified as a refusal, resetting a cooldown
    that never started. Under-recording is recoverable; over-recording is the
    bug this veto exists to prevent.
    """
    for site in sites:
        if author_id != site["detector_bot_id"]:
            continue
        outcome = classify(
            text,
            detector_pattern=site["detector_pattern"] or "",
            failure_pattern=site["failure_pattern"] or "",
        )
        if outcome is None:
            continue
        return site["site_name"], outcome
    return None
