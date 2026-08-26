"""Sticky todo board — pure rendering and change detection.

No Discord objects and no DB here: the cog hands in plain row mappings and gets
back embed-ready text plus a signature. That keeps the board's layout and its
"has anything actually changed?" rule unit-testable without Discord mocks.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from bot_modules.services.embeds import rel_ts

#: How many pending tasks the board lists before it defers to the dashboard.
#: Discord caps a description at 4096 chars; this keeps us far under it and
#: keeps the board glanceable rather than a wall.
MAX_BOARD_ROWS = 15

#: Where a task is cut short on the board. Slightly wider than the 42 the old
#: padded cell allowed, since nothing is spent on padding any more — and a hard
#: cap is what stops one essay-length task from owning a whole phone screen.
TASK_CLIP = 44

RECURRING_MARKER = "🔁"

#: Marks a row a shop purchase spawned, so a mod can tell paid work — which
#: has a member's coins sitting in escrow behind it — from an ordinary task.
ORDER_MARKER = "🎁"

EMPTY_BODY = "Nothing pending — the list is clear. ✨"


def _clip(text: str, width: int) -> str:
    """Cut to ``width`` with a trailing ellipsis, per the embed style guide."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _flatten(text: str) -> str:
    """Collapse a task onto one line so a pasted multi-line task can't break the grid."""
    return " ".join(str(text or "").split())


def render_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    total: int | None = None,
    limit: int = MAX_BOARD_ROWS,
) -> str:
    """The board body: one padded monospace cell per task, age outside the span.

    ``rows`` are pending todos oldest-first (see ``todo_service.pending_todos``),
    so the longest-waiting task sits at the top where it nags. ``total`` is how
    many are pending overall — the caller fetches only a screenful, so it can't
    be inferred from ``len(rows)``.

    **Only the id is monospace, and there is no age.** The task used to sit in
    a cell padded to a fixed 48 characters, so "more qotd prompts" took the
    full width exactly like a sentence-long one and every row wrapped on a
    phone. Dropping the padding alone was not enough: measured against the 13
    real tasks on the production board, a relative age costs about nine extra
    wrapped lines, because "2 months ago" pushes almost every short row over a
    phone's width on its own. No layout that keeps it beats the padded one it
    replaced.

    So the age goes. The list is oldest-first, so *position* already says what
    has waited longest — which was the age's job here — and the exact date
    stays one tap away on the dashboard's Todo List page. Net on that real
    board: 22 wrapped lines against the old 27, while showing more of each task
    than the old cell did (``TASK_CLIP`` > its 42).
    """
    if not rows:
        return EMPTY_BODY

    total = len(rows) if total is None else total
    shown = rows[:limit]
    if not shown:  # limit <= 0; nothing to render, so nothing to measure
        return EMPTY_BODY

    lines: list[str] = []
    for row in shown:
        marker = f" {RECURRING_MARKER}" if row.get("recurring_id") else ""
        # A shop order says who it is for. The name is resolved by the cog and
        # attached as ``buyer_name`` — it is deliberately NOT in the task text
        # (see economy/shop_items.todo_task_text), so it appears only here, and
        # an erased buyer simply has none to show.
        if row.get("purchase_id"):
            marker += f" {ORDER_MARKER}"
        buyer = _flatten(row.get("buyer_name") or "")
        for_whom = f" · for {buyer}" if buyer else ""
        task = _clip(_flatten(row["task"]), TASK_CLIP)
        lines.append(f"`#{row['id']}`{marker} {task}{for_whom}")

    hidden = total - len(shown)
    if hidden > 0:
        lines.append(f"\n…and **{hidden}** more on the dashboard.")
    return "\n".join(lines)


def render_footer(total: int) -> str:
    noun = "task" if total == 1 else "tasks"
    return f"{total} pending {noun} · updates automatically"


def board_signature(
    rows: Iterable[Mapping[str, Any]],
    total: int | None = None,
    *,
    limit: int = MAX_BOARD_ROWS,
) -> tuple:
    """A hashable fingerprint of what the board *shows*.

    The refresh loop compares this against the last edit and skips the API call
    when nothing changed. Deliberately excludes ``created_at`` age text —
    ``<t:…:R>`` ticks client-side, so a board whose only change is "2h" becoming
    "3h" needs no edit at all. ``total`` is part of the fingerprint because
    completing a task *below* the visible window still changes the footer.

    Only the first ``limit`` rows count: callers fetch a sentinel row past the
    visible window to detect overflow, and that row is never rendered.
    """
    shown = tuple(
        (
            row["id"],
            _flatten(row["task"]),
            bool(row.get("recurring_id")),
            bool(row.get("purchase_id")),
            # In the fingerprint because a buyer who changes their nickname
            # changes what the board says, and the row is otherwise identical.
            _flatten(row.get("buyer_name") or ""),
        )
        for row in list(rows)[:limit]
    )
    return (shown, len(shown) if total is None else total)


def complete_option_label(row: Mapping[str, Any]) -> tuple[str, str]:
    """``(label, description)`` for one entry of the Complete select menu.

    Discord caps a select option label at 100 chars and a description at 100.
    """
    label = f"#{row['id']} {_flatten(row['task'])}"
    if len(label) > 100:
        label = label[:99] + "…"
    desc = _flatten(row.get("description") or "")
    if len(desc) > 100:
        desc = desc[:99] + "…"
    return label, desc


# ── The chore board ─────────────────────────────────────────────────────────
#
# A second board, scoped to rows a recurring definition spawned. It is a
# *scoreboard*, not a pending list: a ticked chore stays visible until the next
# reset replaces it, because "did we do the QOTD today?" cannot be answered by
# a board that deletes the answer the moment it is yes.

CHORE_DONE = "✅"
CHORE_OPEN = "⬜"
CHORE_MISSED = "❌"

#: Shown only from two consecutive days up — a "🔥 1" on every chore that
#: happened once is noise, and makes a real run harder to spot.
STREAK_MARKER = "🔥"
STREAK_MIN = 2

EMPTY_CHORES = "No recurring chores set up yet — add them on the dashboard. ✨"


def chore_state(row: Mapping[str, Any]) -> str:
    """``'done' | 'missed' | 'open'`` for one chore's **latest** instance.

    A definition with no instance yet reads as ``open``: it has simply not come
    round, which is indistinguishable to a mod from due-and-not-done-yet, and
    inventing a fourth state for it would earn nothing.

    ``'missed'`` is defensive rather than routine. The daily reset closes the
    old instance and opens its replacement in a single call, so the latest
    instance is normally open or done and never missed — a *previous* miss
    reaches the board through ``missed_previous`` instead (see
    ``render_chore_rows``). This branch covers a row written off by
    ``mark_missed`` with nothing spawned behind it, which the service permits.
    """
    if row.get("completed_at"):
        return "done"
    if row.get("missed_at"):
        return "missed"
    return "open"


_CHORE_BOXES = {"done": CHORE_DONE, "missed": CHORE_MISSED, "open": CHORE_OPEN}


def render_chore_rows(
    rows: Sequence[Mapping[str, Any]], *, limit: int = MAX_BOARD_ROWS
) -> str:
    """The chore board body: a state box, the chore, then who and when.

    ``rows`` come from ``todo_recurring_service.chore_board_rows`` in
    time-of-day order, each already carrying its ``streak`` and (resolved by
    the cog, which is the only layer that may touch Discord) a
    ``completed_by_name``.

    The state box leads the line and the name is bold rather than padded
    monospace: a fixed-width cell wrapped on a phone and left a bare backtick
    on its own line, and the boxes align just as well by simply starting the
    line.
    """
    if not rows:
        return EMPTY_CHORES

    shown = rows[:limit]
    if not shown:  # limit <= 0
        return EMPTY_CHORES

    lines: list[str] = []
    for row in shown:
        state = chore_state(row)
        # The state box starts the line, so the boxes still form a column
        # without a padded cell behind them — and the chore name is free to
        # wrap rather than stranding a lone backtick on a phone.
        cell = f"{_CHORE_BOXES[state]} **{_flatten(row['task'])}**"

        trailing: list[str] = []
        if state == "done":
            who = _flatten(row.get("completed_by_name") or "")
            when = rel_ts(row["completed_at"])
            trailing.append(f"{who} · {when}" if who else when)
        elif state == "missed":
            # Say it in words as well as in the box: a lone ❌ in a channel
            # reads as an error, not as a day that was skipped.
            trailing.append("missed")
        elif row.get("missed_previous"):
            # The reachable miss. The reset spawns today's row the moment it
            # writes yesterday's off, so the board's row is always the fresh
            # one — without this the three days nobody did the chore would show
            # as a plain ⬜ and the record the reset exists to keep would be
            # invisible on the surface built to display it.
            trailing.append(f"{CHORE_MISSED} missed last run")

        streak = int(row.get("streak") or 0)
        if streak >= STREAK_MIN:
            trailing.append(f"{STREAK_MARKER} {streak}")

        suffix = (" — " + " · ".join(trailing)) if trailing else ""
        lines.append(f"{cell}{suffix}")

    hidden = len(rows) - len(shown)
    if hidden > 0:
        lines.append(f"\n…and **{hidden}** more on the dashboard.")
    return "\n".join(lines)


def tickable_chores(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The rows the Mark Done button can actually offer.

    A chore needs a todo row behind it to tick, and it has to still be open: a
    done one has nothing to tick and a missed one is closed business that
    ``complete_todo`` refuses anyway. Lives here, next to ``chore_state``, so
    the button and the board are reading the same rows through the same rule —
    they disagreed for as long as this filter lived alone in the cog.
    """
    return [
        row
        for row in rows
        if row.get("todo_id") is not None and chore_state(row) == "open"
    ]


def nothing_to_tick_message(rows: Sequence[Mapping[str, Any]]) -> str:
    """What Mark Done says when it has nothing to offer.

    Three different truths, and saying the wrong one is how a mod concludes the
    button is broken:

    * no chores configured at all;
    * chores exist but none has come round yet — a weekly chore set up on a day
      it doesn't run has no instance and no miss, it simply isn't due;
    * everything due really is ticked off.

    Only the third was ever said. The middle case used to appear over a board
    drawing those chores ⬜ open, which reads as the button refusing work that
    is plainly visible above it.
    """
    if not rows:
        return EMPTY_CHORES
    waiting = [
        row
        for row in rows
        if row.get("todo_id") is None and row.get("next_run_at")
    ]
    if waiting:
        soonest = min(waiting, key=lambda r: float(r["next_run_at"]))
        task = _flatten(soonest["task"])
        return (
            f"Nothing due yet — **{task}** first lands "
            f"{rel_ts(float(soonest['next_run_at']))}. ⏳"
        )
    return "Every chore is already ticked off. ✨"


def render_chore_footer(rows: Sequence[Mapping[str, Any]]) -> str:
    """``2 of 3 done · updates automatically`` — the scoreboard's score.

    Counts every active chore, including any past the visible limit, so the
    footer never disagrees with the dashboard about how the day went.
    """
    total = len(rows)
    if not total:
        return "no chores yet · configure on the dashboard"
    done = sum(1 for row in rows if chore_state(row) == "done")
    # Counts the same thing the rows show: a chore still open whose previous
    # run was written off. Counting ``chore_state == "missed"`` instead would
    # be a number that is always zero, because the reset never leaves the
    # latest instance in that state.
    missed = sum(
        1
        for row in rows
        if chore_state(row) == "missed"
        or (chore_state(row) == "open" and row.get("missed_previous"))
    )
    text = f"{done} of {total} done"
    if missed:
        text += f" · {missed} missed last run"
    return f"{text} · updates automatically"


def chore_signature(
    rows: Iterable[Mapping[str, Any]], *, limit: int = MAX_BOARD_ROWS
) -> tuple:
    """A fingerprint of what the chore board *shows*.

    Same rule as ``board_signature``: excludes the completion timestamp itself,
    because ``<t:…:R>`` re-renders client-side and an age ticking over is not a
    reason to spend an API call. The *state* is in, so a tick repaints; so is
    ``completed_by_name``, since a row can change hands without changing state
    if a completion is undone and redone by someone else.
    """
    rows = list(rows)
    shown = tuple(
        (
            row["recurring_id"],
            _flatten(row["task"]),
            chore_state(row),
            bool(row.get("missed_previous")),
            _flatten(row.get("completed_by_name") or ""),
            int(row.get("streak") or 0),
        )
        for row in rows[:limit]
    )
    return (shown, len(rows))


# ── The combined board ──────────────────────────────────────────────────────
#
# Migration 180 merged the two boards back into one. The renderers above stay
# exactly as they were and become *section* builders: the chore scoreboard
# answers "did we do it today?", the task list answers "what's outstanding?",
# and a mod reads both without the server having to spend two channels on the
# question. See the migration for why the split was undone.

CHORE_HEADING = "🔁 **Today's chores**"
TASK_HEADING = "📋 **Tasks**"

#: Chores are a short daily list, so they take a bounded slice off the top and
#: leave the rest of the budget to tasks.
MAX_CHORE_ROWS = 8

#: ...but never all of it. A day with many chores must not push the task list
#: off the board entirely — that is precisely the failure the merge was meant
#: to end, just pointing the other way.
MIN_TASK_ROWS = 3

EMPTY_BOARD = "Nothing pending and no chores yet — all clear. ✨"


def task_row_budget(chores_shown: int, *, limit: int = MAX_BOARD_ROWS) -> int:
    """How many task rows fit under the chores, never fewer than MIN_TASK_ROWS."""
    return max(MIN_TASK_ROWS, limit - chores_shown)


def render_board(
    chore_rows: Sequence[Mapping[str, Any]],
    task_rows: Sequence[Mapping[str, Any]],
    *,
    task_total: int | None = None,
    limit: int = MAX_BOARD_ROWS,
) -> str:
    """The whole board body: the chore scoreboard, then the task list.

    Each section is omitted when it has nothing in it, rather than rendering a
    heading over an empty-state sentence — two "nothing here" lines stacked on
    one board reads as broken. When *both* are empty the board says so once.

    ``task_rows`` must already exclude chore-spawned rows (see
    ``todo_service.pending_todos(exclude_chores=True)``): the chore section
    shows those, with more state than a task line can carry.
    """
    sections: list[str] = []
    chores_shown = min(len(chore_rows), MAX_CHORE_ROWS)
    if chore_rows:
        sections.append(
            CHORE_HEADING + "\n" + render_chore_rows(chore_rows, limit=MAX_CHORE_ROWS)
        )
    if task_rows:
        sections.append(
            TASK_HEADING
            + "\n"
            + render_rows(
                task_rows,
                total=task_total,
                limit=task_row_budget(chores_shown, limit=limit),
            )
        )
    if not sections:
        return EMPTY_BOARD
    return "\n\n".join(sections)


def render_board_footer(
    chore_rows: Sequence[Mapping[str, Any]], task_total: int
) -> str:
    """``2 of 3 chores done · 25 tasks · updates automatically``.

    Both halves, because the board now answers both questions and a footer that
    reported only one would silently contradict the section it left out. The
    chore half is dropped entirely in a guild with no chores configured, rather
    than reading "0 of 0 chores done".
    """
    parts: list[str] = []
    if chore_rows:
        done = sum(1 for row in chore_rows if chore_state(row) == "done")
        parts.append(f"{done} of {len(chore_rows)} chores done")
    noun = "task" if task_total == 1 else "tasks"
    parts.append(f"{task_total} {noun}")
    parts.append("updates automatically")
    return " · ".join(parts)


def board_content_signature(
    chore_rows: Sequence[Mapping[str, Any]],
    task_rows: Sequence[Mapping[str, Any]],
    task_total: int | None = None,
    *,
    limit: int = MAX_BOARD_ROWS,
) -> tuple:
    """A fingerprint of the whole board, for the refresh loop's skip check.

    Just the two section fingerprints side by side — each already excludes the
    relative timestamps that re-render client-side, so a board whose only
    change is "2h" becoming "3h" still costs no API call.
    """
    chores_shown = min(len(chore_rows), MAX_CHORE_ROWS)
    return (
        chore_signature(chore_rows, limit=MAX_CHORE_ROWS),
        board_signature(
            task_rows, task_total, limit=task_row_budget(chores_shown, limit=limit)
        ),
    )


def completable_options(
    chore_rows: Sequence[Mapping[str, Any]],
    task_rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Everything the one Complete button can offer, chores first.

    Two boards meant two buttons — Complete for tasks, Mark Done for chores —
    and a mod had to know which list a row was on before they could tick it.
    One board offers one button over both, so a chore row carries its
    ``todo_id`` forward as ``id``: the thing being completed is a todo either
    way, and the select only ever needed the id.

    Chores take the same bounded slice here that they take on the board.
    Without the cap, a guild with 25+ open chore instances would fill Discord's
    25-option select with chores alone and no ordinary task could be ticked off
    from Discord at all — the exact capability the merge existed to restore.
    """
    options: list[Mapping[str, Any]] = [
        {"id": row["todo_id"], "task": row["task"], "description": ""}
        for row in tickable_chores(chore_rows)[:MAX_CHORE_ROWS]
    ]
    options.extend(task_rows)
    return options
