"""Transcript mechanics for the Ask panel's ephemeral chat window.

The Ask panel (``advisor_cog.post_ask_panel``) opens a private, multi-turn chat
with the assistant. Discord gives that surface an awkward constraint: an
ephemeral message has no id anyone can look up later, so there is nowhere to
hang server-side conversation state that a member's *next* click could find
again — not after a restart, and not even reliably before one.

So the conversation is carried **in the message itself**. Every turn is one
embed field; pressing Reply reads the fields back off the message the button
was attached to and hands them to ``advisor_service.answer_advisor`` as its
``history``. Nothing is written to the database, which is why the chat stores
no personal data at all: the transcript exists only inside a message visible to
one member, and vanishes when they dismiss it.

Two properties make that safe rather than clever:

* **The round trip is lossless within the field cap.** What
  :func:`history_from_fields` returns for a rendered transcript is what
  :func:`transcript_fields` was given, save for values clipped to Discord's
  1024-character field limit. The model is therefore shown exactly the
  conversation the member can see — never more, never a stale version.
* **Roles are decided by a marker, not by a name.** The assistant's guild-facing
  name is a branding dial an admin can change at any moment, including midway
  through somebody's chat. Keying the role off the name would silently reclass
  every earlier assistant turn as a user turn the moment it changed, so the
  markers below carry the role and the name is only decoration.

Content fed back in is still untrusted — it round-trips through a message —
but it is untrusted in exactly the way the dashboard's history already is, and
lands in the same ``sanitize_history`` on the way to the model.

Deliberately Discord-free: callers pass ``(name, value)`` pairs rather than
``discord.EmbedField`` objects, so the whole module is importable and testable
without a bot, the way ``panel_registry`` is.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

#: Field-name prefixes that carry the role. See the module docstring — these,
#: not the assistant's branded name, are what :func:`history_from_fields` reads.
USER_MARKER = "💬"
BOT_MARKER = "🤖"

#: Exchanges (question + answer) allowed in one chat. A reply loop invites much
#: longer sessions than one-shot ``/ask`` ever did, and every turn re-sends the
#: whole conversation, so the cost of a chat grows faster than its length. Past
#: this the Reply button is disabled and the member is sent back to the panel.
MAX_EXCHANGES = 5

#: Discord's caps: an embed field value, a field name, and fields per embed.
FIELD_VALUE_LIMIT = 1024
FIELD_NAME_LIMIT = 256
MAX_FIELDS = 25

#: Seconds between Reply presses, per member. Mirrors the 12s cooldown on
#: ``/ask`` — the same shared Anthropic budget is behind both surfaces, and a
#: button is easier to lean on than a slash command.
REPLY_COOLDOWN_SECONDS = 12.0


def _clip(text: str, limit: int) -> str:
    """Trim to ``limit`` characters, marking the cut so it doesn't read as the end."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def turn_field(role: str, content: str, assistant_name: str) -> tuple[str, str]:
    """Render one history turn as an ``(embed field name, value)`` pair."""
    marker = USER_MARKER if role == "user" else BOT_MARKER
    who = "You" if role == "user" else assistant_name
    return (
        _clip(f"{marker} {who}", FIELD_NAME_LIMIT),
        _clip(content, FIELD_VALUE_LIMIT),
    )


def transcript_fields(
    history: Sequence[dict], assistant_name: str
) -> list[tuple[str, str]]:
    """Render a conversation as embed fields, oldest first.

    Keeps the most recent turns when a conversation somehow runs past
    :data:`MAX_FIELDS` — the far end of the chat is the part still being talked
    about, and dropping the opening beats Discord rejecting the whole embed.
    """
    fields = [
        turn_field(str(t.get("role", "")), str(t.get("content", "")), assistant_name)
        for t in history
        if str(t.get("content", "")).strip()
    ]
    return fields[-MAX_FIELDS:]


def history_from_fields(
    fields: Iterable[tuple[str | None, str | None]],
) -> list[dict]:
    """Read a conversation back out of a rendered transcript.

    Anything without a role marker is skipped rather than guessed at, so a field
    added to this embed later — or a click that somehow arrives from a foreign
    message — contributes nothing instead of becoming a spurious turn.
    """
    history: list[dict] = []
    for name, value in fields:
        content = (value or "").strip()
        if not content:
            continue
        label = (name or "").lstrip()
        if label.startswith(USER_MARKER):
            history.append({"role": "user", "content": content})
        elif label.startswith(BOT_MARKER):
            history.append({"role": "assistant", "content": content})
    return history


def exchange_count(history: Sequence[dict]) -> int:
    """How many questions the member has asked in this chat."""
    return sum(1 for t in history if t.get("role") == "user")


def is_full(history: Sequence[dict]) -> bool:
    """Whether this chat has spent its :data:`MAX_EXCHANGES` budget."""
    return exchange_count(history) >= MAX_EXCHANGES


def footer_tail(history: Sequence[dict]) -> str:
    """The budget half of the chat footer, in the member's terms."""
    if is_full(history):
        return "chat limit reached — press Ask on the panel to start a new one"
    return f"{exchange_count(history)} of {MAX_EXCHANGES} questions used"


class ReplyCooldown:
    """Per-member rate limit for the Reply button.

    In memory on purpose: it guards a budget, not a right, so a restart clearing
    it costs one extra question at worst — cheaper than a table and a migration
    to remember a twelve-second grudge. ``discord.app_commands``' own cooldown
    decorator only covers commands, which is why the Reply button needs this.
    """

    def __init__(self, seconds: float = REPLY_COOLDOWN_SECONDS) -> None:
        self._seconds = seconds
        self._last: dict[int, float] = {}

    def remaining(self, user_id: int, now: float) -> float:
        """Seconds still to wait, or ``0.0`` when the member may ask now."""
        last = self._last.get(user_id)
        if last is None:
            return 0.0
        return max(0.0, self._seconds - (now - last))

    def mark(self, user_id: int, now: float) -> None:
        """Record an allowed press, and drop everyone whose wait has expired.

        The prune keeps the dict proportional to who is chatting *now* rather
        than to everyone who ever has — this lives for the process's lifetime.
        """
        self._last = {
            uid: ts
            for uid, ts in self._last.items()
            if now - ts < self._seconds
        }
        self._last[user_id] = now
