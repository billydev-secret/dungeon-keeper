"""Pure mode engine for role menus.

Given what the member holds and what they clicked/selected, decide which roles
to add and remove — no Discord objects, no I/O, fully table-testable. The five
modes (spec §3.3):

- toggle  — click to get, click again to drop
- unique  — only ever one role from the menu at a time
- verify  — gain-only (verification gates)
- drop    — remove-only (opt-out stations)
- binding — first pick is permanent, one ever per member

The max-roles cap follows spec §5: existing holders over a lowered cap keep
their roles; only *growing* past the cap is blocked (swaps at over-cap pass,
because the total doesn't grow).

Guards that need live guild state (required role, cooldown, enabled, bot
hierarchy) live in the interaction layer, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Outcome.error codes — the interaction layer maps these to member-facing text.
ERR_AT_CAP = "at_cap"
ERR_PERMANENT = "permanent"
ERR_NO_CHANGE = "no_change"


@dataclass(frozen=True)
class Outcome:
    adds: tuple[int, ...] = ()
    removes: tuple[int, ...] = ()
    error: str = ""
    bind_role_id: int = 0  # non-zero: record this as the member's permanent pick


def _cap_blocks(current: int, final: int, max_roles: int) -> bool:
    return max_roles > 0 and final > max_roles and final > current


def resolve_click(
    mode: str,
    menu_role_ids: list[int],
    held_role_ids: set[int],
    clicked_role_id: int,
    max_roles: int,
    binding_role_id: int | None,
) -> Outcome:
    """Resolve a button click on one role."""
    held = set(menu_role_ids) & held_role_ids
    has_clicked = clicked_role_id in held

    if mode == "binding":
        if binding_role_id is not None:
            return Outcome(error=ERR_PERMANENT)
        return Outcome(
            adds=() if has_clicked else (clicked_role_id,),
            bind_role_id=clicked_role_id,
        )

    if mode == "verify":
        if has_clicked:
            return Outcome(error=ERR_NO_CHANGE)
        if _cap_blocks(len(held), len(held) + 1, max_roles):
            return Outcome(error=ERR_AT_CAP)
        return Outcome(adds=(clicked_role_id,))

    if mode == "drop":
        if not has_clicked:
            return Outcome(error=ERR_NO_CHANGE)
        return Outcome(removes=(clicked_role_id,))

    if mode == "unique":
        if has_clicked:
            return Outcome(removes=(clicked_role_id,))
        others = _ordered(menu_role_ids, held - {clicked_role_id})
        return Outcome(adds=(clicked_role_id,), removes=others)

    # toggle (default)
    if has_clicked:
        return Outcome(removes=(clicked_role_id,))
    if _cap_blocks(len(held), len(held) + 1, max_roles):
        return Outcome(error=ERR_AT_CAP)
    return Outcome(adds=(clicked_role_id,))


def resolve_selection(
    mode: str,
    menu_role_ids: list[int],
    held_role_ids: set[int],
    selected_role_ids: list[int],
    max_roles: int,
    binding_role_id: int | None,
) -> Outcome:
    """Resolve a dropdown submission (the checked set of role ids)."""
    menu_set = set(menu_role_ids)
    held = menu_set & held_role_ids
    # Ignore anything not (or no longer) in the menu — edits mid-interaction
    # degrade gracefully (spec §5).
    target = set(selected_role_ids) & menu_set

    if mode == "binding":
        if binding_role_id is not None:
            return Outcome(error=ERR_PERMANENT)
        if not target:
            return Outcome(error=ERR_NO_CHANGE)
        pick = _ordered(menu_role_ids, target)[0]
        return Outcome(
            adds=() if pick in held else (pick,), bind_role_id=pick
        )

    if mode == "verify":
        adds = _ordered(menu_role_ids, target - held)
        if not adds:
            return Outcome(error=ERR_NO_CHANGE)
        if _cap_blocks(len(held), len(held) + len(adds), max_roles):
            return Outcome(error=ERR_AT_CAP)
        return Outcome(adds=adds)

    if mode == "drop":
        removes = _ordered(menu_role_ids, target & held)
        if not removes:
            return Outcome(error=ERR_NO_CHANGE)
        return Outcome(removes=removes)

    if mode == "unique" and len(target) > 1:
        # The UI constrains unique to a single pick; if a stale component
        # slips a multi-select through, keep the first in menu order.
        target = {_ordered(menu_role_ids, target)[0]}

    # toggle + unique: the submitted selection becomes the member's set.
    adds = _ordered(menu_role_ids, target - held)
    removes = _ordered(menu_role_ids, held - target)
    if not adds and not removes:
        return Outcome(error=ERR_NO_CHANGE)
    final = len(held) + len(adds) - len(removes)
    if adds and _cap_blocks(len(held), final, max_roles):
        return Outcome(error=ERR_AT_CAP)
    return Outcome(adds=adds, removes=removes)


def _ordered(menu_role_ids: list[int], subset: set[int]) -> tuple[int, ...]:
    """Stable menu-order tuple of ``subset`` (deterministic messages/tests)."""
    return tuple(rid for rid in menu_role_ids if rid in subset)


@dataclass
class CooldownGate:
    """Per-(menu, member) rate limit. In-memory by design — a restart clearing

    cooldowns is harmless, and the map stays tiny (one float per recent
    clicker)."""

    _last: dict[tuple[int, int], float] = field(default_factory=dict)

    def check(
        self, menu_id: int, user_id: int, cooldown_seconds: int, now: float
    ) -> float:
        """Return seconds still to wait (0 = allowed; records the click)."""
        if cooldown_seconds <= 0:
            return 0.0
        key = (menu_id, user_id)
        last = self._last.get(key)
        if last is not None and now - last < cooldown_seconds:
            return cooldown_seconds - (now - last)
        self._last[key] = now
        return 0.0
