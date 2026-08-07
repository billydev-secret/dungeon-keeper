"""Pure matching for Mention Awards: trigger phrase → pay the member tagged.

A rule is four levers — **channel, phrase, amount, announcer role** — and the
event it recognises is one message:

    @Hot Seat your turn @turbodog8 ! Let's all find out more about him!

When a message in the rule's channel contains the phrase and @-mentions
exactly one member, that member is the award's recipient. The bot hosts
nothing; the announcement is the entire signal.

**Content is read, never stored.** The listener matches ``phrase`` against
``message.content`` live off the gateway. Nothing here writes content, and
message-content storage stays off — but it does mean a phrase rule cannot be
replayed over history, since banked rows have no content. Backfills therefore
match on message *shape* instead (see ``scripts/backfill_mention_awards.py``).

Deliberately not inferred:

* **Who may announce** beyond the role lever. Where the game is a baton pass —
  the outgoing contestant names their successor — there is no fixed host, so
  ``announcer_role_id`` of 0 means "anyone in the channel". That is the
  permissive setting and it is farmable; the role is the fix.
* **How often.** Dedupe is per *message*, in the caller's payout ledger. A
  member named twice on separate occasions is paid twice, which is the right
  reading of two genuine turns.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    """One configured award. Mirrors a ``mention_award_rules`` row."""

    id: int
    channel_id: int
    phrase: str
    amount: int
    announcer_role_id: int = 0


@dataclass(frozen=True)
class Award:
    """A matched rule: who to pay, how much, and who triggered it."""

    rule_id: int
    member_id: int
    amount: int
    announcer_id: int


def phrase_matches(phrase: str, content: str) -> bool:
    """Case-insensitive substring match, whitespace-normalised at the edges.

    Substring rather than word-boundary or regex: the phrase is admin-authored
    free text (``"your turn"``, ``"takes the hot seat"``) and Discord renders
    mentions inline, so a strict match would break the moment someone adds
    punctuation. An empty phrase never matches — it would pay on every message
    in the channel.
    """
    needle = phrase.strip().casefold()
    if not needle:
        return False
    return needle in (content or "").casefold()


def match_rule(
    rule: Rule,
    *,
    channel_id: int,
    author_id: int,
    author_is_bot: bool,
    author_role_ids: Iterable[int],
    content: str,
    mentioned_user_ids: Iterable[int],
) -> Award | None:
    """The award this message earns under ``rule``, or ``None``.

    ``author_role_ids`` gates on ``rule.announcer_role_id`` when it is set.
    Bots never trigger an award: a webhook or another bot echoing the phrase
    would otherwise be a free faucet.
    """
    if channel_id != rule.channel_id or author_is_bot:
        return None
    if rule.amount < 1:
        return None
    if not phrase_matches(rule.phrase, content):
        return None

    if rule.announcer_role_id:
        if rule.announcer_role_id not in {int(r) for r in author_role_ids}:
            return None

    mentioned = {int(u) for u in mentioned_user_ids}
    # Exactly one. A message tagging several people is a group shout, and
    # guessing which of them the phrase referred to would pay the wrong member.
    if len(mentioned) != 1:
        return None

    member_id = next(iter(mentioned))
    # Self-award is the one farm the rule is otherwise wide open to: type the
    # phrase, ping yourself, collect. Blocked regardless of the role lever.
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
        )
        if found is not None:
            return found
    return None
