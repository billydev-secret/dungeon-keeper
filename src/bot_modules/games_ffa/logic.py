"""Pure decision logic for Anonymous Truth or Dare (FFA) embed mode.

The cog keeps one ``prompts`` entry per posted prompt message —
``{message_id, prompt, label, reply_count, repliers}`` — and until
2026-09-04 that was the whole game: no ending, no recap and no payout
(anon-tail-70). These helpers give the host's **Close** something to say and
someone to pay, and give the 24h sweep the same roster.

* :func:`note_reply` — bump a prompt entry for one anonymous reply and
  record who sent it (the payout roster; never rendered anywhere).
* :func:`recap_summary` — replies per prompt, the busiest prompt, the
  distinct replier count. Nothing here names a member: the recap is
  as anonymous as the replies were.
* :func:`roster_from_prompts` — the paying roster, de-duplicated.
"""

from __future__ import annotations

from typing import Any


def note_reply(entry: dict[str, Any], user_id: int) -> int:
    """Count one reply against a prompt entry and remember the replier.

    Mutates ``entry`` in place and returns the new reply count. ``repliers``
    is de-duplicated — a member who replies five times is one participant.
    """
    entry["reply_count"] = int(entry.get("reply_count", 0)) + 1
    repliers: list[int] = entry.setdefault("repliers", [])
    uid = int(user_id)
    if uid not in repliers:
        repliers.append(uid)
    return entry["reply_count"]


def roster_from_prompts(prompts: list[dict[str, Any]] | None) -> list[int]:
    """Everyone who replied to any prompt, once each, sorted."""
    roster: set[int] = set()
    for entry in prompts or []:
        if not isinstance(entry, dict):
            continue
        for raw in entry.get("repliers") or []:
            try:
                roster.add(int(raw))
            except (TypeError, ValueError):
                continue
    return sorted(roster)


def recap_summary(prompts: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The close recap's numbers, or ``None`` when no prompt was ever posted.

    ``per_prompt`` keeps posting order — ``(label, text, reply_count)`` per
    prompt; ``busiest`` is the prompt with the most replies (first wins a
    tie), or ``None`` when nothing was answered at all.
    """
    entries = [e for e in (prompts or []) if isinstance(e, dict)]
    if not entries:
        return None
    per_prompt = [
        (
            str(e.get("label") or "TRUTH"),
            str(e.get("prompt") or ""),
            int(e.get("reply_count") or 0),
        )
        for e in entries
    ]
    total = sum(count for _l, _t, count in per_prompt)
    busiest = max(per_prompt, key=lambda row: row[2]) if total > 0 else None
    return {
        "per_prompt": per_prompt,
        "total_replies": total,
        "busiest": busiest,
        "repliers": roster_from_prompts(entries),
        "prompt_count": len(per_prompt),
    }
