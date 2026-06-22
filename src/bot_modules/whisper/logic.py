"""Pure whisper helpers — no Discord API calls, no DB access.

These helpers live separately from ``services/whisper_service.py`` because
that module owns the validation/state-machine rules that get re-exported to
many callers. This module hosts the smaller presentation helpers (status
pills, time formatting, fuzzy member search) that used to live inline in
``cogs/whisper_cog.py`` and were impossible to test without an interaction.

All functions take and return plain Python primitives.
"""

from __future__ import annotations

import time as _time
from collections.abc import Sequence
from typing import Protocol, TypeVar

from bot_modules.services.whisper_models import STATE_SHARED, Whisper
from bot_modules.services.whisper_service import (
    is_locked,
    safe_codefence_content,
)


# ── Time formatting ──────────────────────────────────────────────────────────


def format_time_ago(created_at: float, *, now: float | None = None) -> str:
    """Render a coarse "Xs/Xm/Xh/Xd ago" string for whisper inbox listings.

    Tests pass ``now`` so the output is deterministic; production callers
    omit it and let ``time.time()`` do the work.
    """
    current = now if now is not None else _time.time()
    delta = max(0, int(current - created_at))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    days = delta // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


# ── Whisper status pill / preview ────────────────────────────────────────────


def status_pill(w: Whisper, *, now: float | None = None) -> str:
    """One-word status label used in inbox dropdown rows and embed headers.

    Mirrors the priority order the cog used inline: exposed > solved > locked
    > no-guesses > shared > new. Locked check delegates to the service so the
    30-day cutoff stays defined in one place.
    """
    if w.exposed:
        return "Exposed"
    if w.solved:
        return "Solved"
    if is_locked(w, now=now):
        return "Locked"
    if w.guesses_left == 0:
        return "No guesses"
    if w.state == STATE_SHARED:
        return "Shared"
    return "New"


def preview(text: str, n: int = 60) -> str:
    """Single-line preview, newlines flattened, truncated with ellipsis."""
    out = text.replace("\n", " ").strip()
    if len(out) > n:
        out = out[: n - 1] + "…"
    return out


# ── Fuzzy member ranking (used by 2 filter modals) ───────────────────────────


class _NamedMember(Protocol):
    """Just-enough interface to score a member by ``display_name``.

    Lets tests pass plain objects with a ``display_name`` attribute instead
    of constructing real ``discord.Member`` mocks.
    """

    @property
    def display_name(self) -> str: ...  # pragma: no cover - protocol


# TypeVar-bound generic so callers passing ``list[discord.Member]`` get back
# the same concrete type (not a downcast to the Protocol). This lets the
# View subclasses keep their existing ``list[Member]`` type without an
# invariant-list assignment error.
_M = TypeVar("_M", bound=_NamedMember)


def _score(name_lower: str, q_lower: str) -> int:
    if name_lower == q_lower:
        return 4
    if name_lower.startswith(q_lower):
        return 3
    if q_lower in name_lower:
        return 2
    it = iter(name_lower)
    if all(c in it for c in q_lower):
        return 1
    return 0


def fuzzy_score_members(
    members: Sequence[_M], query: str
) -> list[_M]:
    """Filter ``members`` by ``query`` against ``display_name``.

    Returns the members with a non-zero score, sorted by descending score
    (exact > prefix > substring > subsequence). Used by both the guess
    member picker and the send-target picker — the modal does the parent
    mutation, this returns the new display list.
    """
    q_lower = query.lower()
    scored = sorted(
        ((m, _score(m.display_name.lower(), q_lower)) for m in members),
        key=lambda x: -x[1],
    )
    return [m for m, s in scored if s > 0]


def filter_whispers_by_message(
    whispers: Sequence[Whisper], query: str
) -> list[Whisper]:
    """Case-insensitive substring filter on whisper bodies.

    Replaces the inline list-comp in ``_WhisperInboxFilterModal.on_submit``.
    Returning a fresh list keeps the modal free to assign it without
    worrying about aliasing the parent's source list.
    """
    q_lower = query.lower()
    return [w for w in whispers if q_lower in w.message.lower()]


# ── Share-feed message body ──────────────────────────────────────────────────
#
# The shared-whisper feed post is now a styled embed built by
# ``bot_modules.whisper.embeds.build_share_feed_embed`` — there is no longer a
# plain-text body builder here.


def format_expose_dm_suffix(sender_label: str) -> str:
    """The "💥 Sender: ..." line appended to the DM body on expose."""
    return f"\n\n\U0001f4a5 Sender: {sender_label}"


def format_reply_dm_body(
    *, whisper_id: int, whisper_message: str, reply_content: str
) -> str:
    """Compose the anonymous-reply DM body delivered to the participant.

    Preview of the original whisper is clipped at 200 chars with an
    ellipsis so the DM stays readable even for long whispers.
    """
    preview_text = whisper_message
    if len(preview_text) > 200:
        preview_text = preview_text[:197] + "…"
    return (
        f"\U0001f4ec Anonymous reply on Whisper #{whisper_id} "
        f"*(\"{safe_codefence_content(preview_text)}\")*:\n"
        f"```{safe_codefence_content(reply_content)}```"
    )


# ── Inbox-embed footer (mode-aware) ──────────────────────────────────────────


def check_send_cooldown(
    last_send_at: float | None, *, now: float, cooldown_seconds: int
) -> int | None:
    """Return seconds remaining on the per-sender cooldown, or ``None`` if it
    has expired (the sender may send now).

    The cog used to inline this comparison; teasing it out lets tests cover
    the boundary conditions (no prior send, exactly at boundary, half-way).
    """
    if last_send_at is None or last_send_at == 0:
        return None
    elapsed = now - last_send_at
    if elapsed >= cooldown_seconds:
        return None
    return max(1, int(cooldown_seconds - elapsed))


def prune_recent_target_sends(
    timestamps: Sequence[float], *, now: float, window_seconds: int = 3600
) -> list[float]:
    """Drop timestamps older than ``window_seconds`` ago.

    Used by the per-target hourly cap so the in-memory list doesn't grow
    unbounded — only the entries that still count toward the cap are kept.
    """
    return [t for t in timestamps if now - t < window_seconds]


def format_cooldown_message(remaining_seconds: int) -> str:
    """The user-facing 'slow down' message for the per-sender cooldown."""
    return (
        f"Slow down — wait {remaining_seconds}s before sending another whisper."
    )


def format_hourly_cap_message(cap: int) -> str:
    """The user-facing message when a sender hits the per-target hourly cap."""
    return (
        f"You've sent {cap} whispers to that user in the last hour. "
        "Try again later."
    )


def format_send_dm_body(*, guild_name: str, message: str) -> str:
    """Initial DM body delivered to a whisper's target.

    Stays in one place so the "3 guesses" copy doesn't drift between the
    initial DM and any future reformat.
    """
    body = safe_codefence_content(message.strip())
    return (
        f"\U0001f4ec You got a Whisper from someone in **{guild_name}**.\n"
        f"You have **3 guesses** to figure out who sent it — wrong guesses "
        f"are gone forever.\n"
        f"```{body}```"
    )


LAUNCHER_MESSAGE_BODY = (
    "**Whisper** — anonymous messages with a guessing game."
)


def inbox_select_placeholder(
    *,
    filter_query: str,
    display_count: int,
    page: int,
    page_count: int,
) -> str:
    """Compute the dropdown placeholder for the inbox select.

    Three states:
      - filter active → ``'🔍 "q" — N matches'`` (singular when N == 1).
      - no filter → ``'Pick a whisper… (N total)'``.

    Pagination suffix ``(p/total)`` is appended when ``page_count > 1``.

    Centralizing here makes the "+/-1 plural" rule trivially testable
    without instantiating a Select.
    """
    if filter_query:
        suffix = "es" if display_count != 1 else ""
        placeholder = f'🔍 "{filter_query}" — {display_count} match{suffix}'
    else:
        placeholder = f"Pick a whisper… ({display_count} total)"
    if page_count > 1:
        placeholder += f" ({page + 1}/{page_count})"
    return placeholder


def member_picker_placeholder(
    *,
    filter_query: str,
    display_count: int,
    page: int,
    page_count: int,
    base: str = "Pick the sender…",
) -> str:
    """Compute the placeholder for the guess/send member-picker dropdowns.

    Same plural rules as ``inbox_select_placeholder`` but the default-state
    base text differs ("Pick the sender…" vs "Pick recipient…"), passed in
    by the caller.
    """
    if filter_query:
        suffix = "es" if display_count != 1 else ""
        placeholder = f'🔍 "{filter_query}" — {display_count} match{suffix}'
        if page_count > 1:
            placeholder += f" ({page + 1}/{page_count})"
    elif page_count > 1:
        placeholder = f"{base} ({page + 1}/{page_count})"
    else:
        placeholder = base
    return placeholder


def recompute_inbox_after_delete(
    *,
    all_whispers: list[Whisper],
    display_whispers: list[Whisper],
    deleted_id: int,
    page: int,
    page_size: int,
) -> tuple[list[Whisper], list[Whisper], int, int | None]:
    """Compute new inbox state after the user soft-deletes one whisper.

    Returns ``(new_all, new_display, new_page, new_selected_id)``. The page
    bumps back one step if the deleted row was the only entry on its page;
    selection moves to the first row of the (possibly clamped) page, or
    ``None`` if the inbox is now empty.

    The cog used to do this inline in ``_on_delete``; lifting it out lets
    tests cover the clamp-to-prev-page edge case (delete the only row on
    the last page) without an interaction mock.
    """
    new_all = [w for w in all_whispers if w.id != deleted_id]
    new_display = [w for w in display_whispers if w.id != deleted_id]
    if not new_display:
        return new_all, new_display, page, None
    new_page = page
    if new_page * page_size >= len(new_display):
        new_page = max(0, new_page - 1)
    page_slice_start = new_page * page_size
    page_slice = new_display[page_slice_start : page_slice_start + page_size]
    new_selected_id = page_slice[0].id if page_slice else None
    return new_all, new_display, new_page, new_selected_id


def inbox_action_buttons(
    selected: Whisper, *, mode: str, now: float | None = None,
) -> list[str]:
    """List of action buttons to show on the inbox view for the selected row.

    Returns the keys in the order they should render:
      - ``"guess"`` — recipient, unsolved, has guesses, not locked.
      - ``"share"`` — recipient, still in pending state.
      - ``"reply"`` — always offered to the participant.
      - ``"report"`` — recipient-only.
      - ``"delete"`` — always last; covers soft-delete.

    The cog used to inline these conditions across three branches —
    centralizing here means tests can pin every condition matrix entry.
    """
    from bot_modules.services.whisper_models import STATE_PENDING  # noqa: PLC0415

    actions: list[str] = []
    if mode == "received":
        if (
            not selected.solved
            and selected.guesses_left > 0
            and not is_locked(selected, now=now)
        ):
            actions.append("guess")
        if selected.state == STATE_PENDING:
            actions.append("share")
        actions.append("reply")
        actions.append("report")
    else:
        actions.append("reply")
    actions.append("delete")
    return actions


def inbox_footer(w: Whisper, *, mode: str, now: float | None = None) -> str:
    """Footer text for the selected whisper in either inbox mode.

    Mirrors the priority order used inline: locked > solved > out-of-guesses
    > N-left (received) / N-remain (sent). Centralizing here means tests can
    assert wording without spinning up a Select view.
    """
    if is_locked(w, now=now):
        return "Locked — too old to guess on now."
    if w.solved:
        return "Solved."
    if w.guesses_left == 0:
        return "Out of guesses — the sender stays anonymous."
    if mode == "received":
        return f"{w.guesses_left} guesses left."
    return f"{w.guesses_left} guesses remain for the target."
