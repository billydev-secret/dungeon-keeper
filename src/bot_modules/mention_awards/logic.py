"""Pure matching for Mention Awards: condition chips → pay the member tagged.

A rule is a **channel**, an **amount**, and a list of **conditions** — chips
on the dashboard — every one of which must match (AND). The event it
recognises is one message:

    @Hot Seat your turn @turbodog8 ! Let's all find out more about him!

When a message in the rule's channel satisfies every chip and @-mentions
exactly one member, that member is the award's recipient. The bot hosts
nothing; the announcement is the entire signal.

Chip kinds:

* ``contains_text`` — case-insensitive substring, or a regex when the chip's
  ``regex`` flag is set. Matched against *raw* content, where a role ping is
  ``<@&id>`` — which is why keying on a ping wants the next chip, not this one.
* ``mentions_role`` — the message pings a specific role (``@Hot Seat``).
* ``from_user`` — the author is one specific member.
* ``author_has_role`` — the author holds a role. This is what the old
  "who can award" lever became; it is the anti-farm chip.

An unknown kind never matches, and a rule with **no chips matches nothing** —
both fail closed, so a bad row can park a rule but can never open a faucet.

**Content is read, never stored.** The listener matches chips against
``message.content`` live off the gateway. Nothing here writes content — but it
means text chips cannot be replayed over history (banked rows have no
content). Backfills therefore match on message *shape* instead (see
``scripts/backfill_mention_awards.py``).

Regex chips are admin-authored (the panel is admin-gated) and validated at
save time (``store.validate_conditions``); a pattern that still fails at match
time fails closed, and a pathological one is bounded by Discord's message
length — the admin's own footgun, not a member-reachable one.

Deliberately not inferred:

* **Who may announce**, beyond the chips. A rule with no author chip lets
  anyone in the channel award — the right reading for a baton-pass game
  (the outgoing contestant names their successor), and farmable otherwise;
  ``author_has_role`` / ``from_user`` are the fix.
* **How often.** Dedupe is per *message*, in the caller's payout ledger. A
  member named twice on separate occasions is paid twice, which is the right
  reading of two genuine turns.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

CONDITION_KINDS = frozenset(
    {"contains_text", "mentions_role", "from_user", "author_has_role"}
)


@dataclass(frozen=True)
class Condition:
    """One chip. ``value`` is text for ``contains_text``, an id-string otherwise."""

    kind: str
    value: str
    regex: bool = False


@dataclass(frozen=True)
class Rule:
    """One configured award. Mirrors a ``mention_award_rules`` row."""

    id: int
    channel_id: int
    amount: int
    conditions: tuple[Condition, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Award:
    """A matched rule: who to pay, how much, and who triggered it."""

    rule_id: int
    member_id: int
    amount: int
    announcer_id: int


def phrase_matches(phrase: str, content: str) -> bool:
    """Case-insensitive substring match, whitespace-normalised at the edges.

    Substring rather than word-boundary: the phrase is admin-authored free
    text and Discord renders mentions inline, so a strict match would break
    the moment someone adds punctuation. An empty phrase never matches — it
    would pay on every message in the channel.
    """
    needle = phrase.strip().casefold()
    if not needle:
        return False
    return needle in (content or "").casefold()


def regex_matches(pattern: str, content: str) -> bool:
    """``re.search`` with IGNORECASE; a broken pattern fails closed.

    Patterns are validated at save time, so ``re.error`` here means the row
    was edited outside the panel — refusing to match is the safe reading.
    """
    if not pattern:
        return False
    try:
        return re.search(pattern, content or "", re.IGNORECASE) is not None
    except re.error:
        return False


def _as_id(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def condition_matches(
    cond: Condition,
    *,
    author_id: int,
    author_role_ids: frozenset[int],
    content: str,
    mentioned_role_ids: frozenset[int],
) -> bool:
    """Whether one chip holds for this message. Unknown kinds never match."""
    if cond.kind == "contains_text":
        if cond.regex:
            return regex_matches(cond.value, content)
        return phrase_matches(cond.value, content)
    if cond.kind == "mentions_role":
        rid = _as_id(cond.value)
        return bool(rid) and rid in mentioned_role_ids
    if cond.kind == "from_user":
        uid = _as_id(cond.value)
        return bool(uid) and uid == author_id
    if cond.kind == "author_has_role":
        rid = _as_id(cond.value)
        return bool(rid) and rid in author_role_ids
    return False


def match_rule(
    rule: Rule,
    *,
    channel_id: int,
    author_id: int,
    author_is_bot: bool,
    author_role_ids: Iterable[int],
    content: str,
    mentioned_user_ids: Iterable[int],
    mentioned_role_ids: Iterable[int] = (),
) -> Award | None:
    """The award this message earns under ``rule``, or ``None``.

    Bots never trigger an award: a webhook or another bot echoing the trigger
    would otherwise be a free faucet. A rule with no chips matches nothing.
    """
    if channel_id != rule.channel_id or author_is_bot:
        return None
    if rule.amount < 1:
        return None
    if not rule.conditions:
        return None

    roles = frozenset(int(r) for r in author_role_ids)
    pinged = frozenset(int(r) for r in mentioned_role_ids)
    for cond in rule.conditions:
        if not condition_matches(
            cond,
            author_id=author_id,
            author_role_ids=roles,
            content=content,
            mentioned_role_ids=pinged,
        ):
            return None

    mentioned = {int(u) for u in mentioned_user_ids}
    # Exactly one. A message tagging several people is a group shout, and
    # guessing which of them the chips referred to would pay the wrong member.
    # Role pings don't count here — raw_mentions and raw_role_mentions are
    # separate — so "@Hot Seat your turn @turbodog8" has one user mention.
    if len(mentioned) != 1:
        return None

    member_id = next(iter(mentioned))
    # Self-award is the one farm the design is otherwise wide open to: post
    # the trigger, ping yourself, collect. Blocked regardless of the chips.
    if member_id == author_id:
        return None

    return Award(
        rule_id=rule.id,
        member_id=member_id,
        amount=rule.amount,
        announcer_id=author_id,
    )


def first_match(
    rules: Sequence[Rule],
    *,
    channel_id: int,
    author_id: int,
    author_is_bot: bool,
    author_role_ids: Iterable[int],
    content: str,
    mentioned_user_ids: Iterable[int],
    mentioned_role_ids: Iterable[int] = (),
) -> Award | None:
    """The first rule this message satisfies, or ``None``.

    First rather than all: the payout ledger is keyed on the message id, so
    one message can only ever pay once anyway. Returning the first match makes
    that limit explicit instead of silently dropping later payouts, and rule
    order on the panel becomes the tie-break an admin can see.
    """
    for rule in rules:
        found = match_rule(
            rule,
            channel_id=channel_id,
            author_id=author_id,
            author_is_bot=author_is_bot,
            author_role_ids=author_role_ids,
            content=content,
            mentioned_user_ids=mentioned_user_ids,
            mentioned_role_ids=mentioned_role_ids,
        )
        if found is not None:
            return found
    return None
