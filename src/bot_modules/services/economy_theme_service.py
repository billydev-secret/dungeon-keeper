"""Flash Themes — the paid, mod-approved themed day (migration 188).

A member pays to name the day's theme; a mod approves it; approved themes
queue up and the hourly loop runs the oldest one whenever the theme channel is
free. Going live posts one card in ``theme_channel_id`` and pins it — the
announcement *is* the pinned message, not a second one — and the sweep unpins
it when the window is up. An empty queue posts nothing: a day without a theme
is simply a normal day, not an announcement that there is no theme.

The fourth consumer of :mod:`economy_submission_store`, so the ledger
mechanics — charge at submit, exactly-once refunds, the one-in-flight-per-
member rule, the stale-pending sweep — are not reimplemented here. What is
here is what only this product knows:

* **The queue is FIFO and promotion waits for a free channel.** Unlike Pin of
  the Day there is no supersede: a paid theme is never cut short by a newer
  one, so :func:`next_approved` is only ever consulted when nothing is live.
* **A theme that ran is not refunded** — the member got their day. Pin of the
  Day makes the same call, and for the same reason.
* **Price 0 does not mean off.** :func:`theme_enabled` reads the real
  ``flash_theme_enabled`` toggle, so a guild can run free themed days without
  the dial secretly meaning "disabled" (the overload the per-perk switches
  were added to remove).

Discord I/O stays with the caller, matching the pin and sponsor services: this
layer takes the message ids it is handed and never touches the gateway.

State machine::

    pending ──approve──> approved ──(slot free)──> live ──(window)──> expired
       │                     │
       ├──deny────> denied   └──withdraw──> denied   (both refund)
       └──expire──> expired                          (refunds; pending only)

Ledger kinds: ``flash_theme`` (debit at submit), ``flash_theme_refund``.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot_modules.services import economy_submission_store as store
from bot_modules.services.economy_service import get_balance

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

# A theme name is a headline — long enough to be evocative, short enough to
# read at a glance in a channel's pinned list. The blurb is the part that
# tells members what to actually post; both caps match the modal's own.
MAX_TITLE_LEN = 60
MIN_TITLE_LEN = 3
MAX_BLURB_LEN = 300
MIN_BLURB_LEN = 1

#: Clamp on the configured window. Below an hour nobody sees it; beyond a week
#: a single purchase has quietly bought the channel for a fortnight.
MIN_THEME_HOURS = 1
MAX_THEME_HOURS = 24 * 7

_OPEN_STATES = ("pending", "approved", "live")
SPEND_KIND = "flash_theme"
REFUND_KIND = "flash_theme_refund"

PRODUCT = store.SubmissionProduct(
    table="econ_theme_submissions",
    spend_kind=SPEND_KIND,
    refund_kind=REFUND_KIND,
    open_states=_OPEN_STATES,
)


@dataclass(frozen=True)
class ThemeOutcome:
    """Result of a submit: the row id and what it cost."""

    submission_id: int
    price: int


def theme_price(settings: EconSettings) -> int:
    """Configured price. 0 is a free themed day, not an off switch."""
    return max(0, int(settings.price_flash_theme))


def theme_enabled(settings: EconSettings) -> bool:
    """On only when the toggle is set AND a theme channel exists.

    The toggle is deliberately separate from the price: a zero price used to
    be how a consumable was disabled, which meant "free" and "off" were the
    same value and neither could be expressed alone. A missing channel still
    disables it, because there is nowhere to announce.
    """
    return bool(settings.flash_theme_enabled) and int(settings.theme_channel_id) > 0


def theme_window_seconds(settings: EconSettings) -> float:
    """How long a live theme holds the channel, clamped to something sane."""
    hours = max(MIN_THEME_HOURS, min(MAX_THEME_HOURS, int(settings.theme_hours)))
    return hours * 3600.0


def open_submission(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    """The member's in-flight theme (pending, approved or live), if any."""
    return store.open_submission(conn, PRODUCT, guild_id, user_id)


def submit_theme(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    title: str,
    blurb: str,
) -> ThemeOutcome:
    """Charge for and queue one themed day. ValueError carries member-facing text.

    Validation runs before the debit and the debit before the insert, so a
    rejected submission never costs anything and a failed insert never strands
    a payment (both live in the caller's transaction).
    """
    if not theme_enabled(settings):
        raise ValueError("Buying a themed day isn't enabled here.")
    name = " ".join(title.split())
    text = "\n".join(line.rstrip() for line in blurb.splitlines()).strip()
    if len(name) < MIN_TITLE_LEN:
        raise ValueError("That's a bit short for a theme name.")
    if len(name) > MAX_TITLE_LEN:
        raise ValueError(f"Theme names are limited to {MAX_TITLE_LEN} characters.")
    if len(text) < MIN_BLURB_LEN:
        raise ValueError(
            "Add a line or two saying what people should post — a bare name "
            "doesn't tell anyone what to do with the day."
        )
    if len(text) > MAX_BLURB_LEN:
        raise ValueError(f"The description is limited to {MAX_BLURB_LEN} characters.")
    if open_submission(conn, guild_id, user_id) is not None:
        raise ValueError(
            "You already have a theme waiting or running — once it's had its "
            "day (or gets turned down) you can buy another."
        )

    price = theme_price(settings)
    unit = settings.currency_plural or "coins"
    submission_id = store.charge_and_insert(
        conn, PRODUCT, guild_id, user_id, price, {"title": name, "blurb": text}
    )
    if submission_id is None:
        have = get_balance(conn, guild_id, user_id)
        raise ValueError(f"A themed day costs {price} {unit} — you have {have}.")
    return ThemeOutcome(submission_id=submission_id, price=price)


def approve(
    conn: sqlite3.Connection, submission_id: int, *, resolver_id: int
) -> sqlite3.Row:
    """Accept a pending theme into the queue. No refund — nothing is lost yet.

    Approval does not post anything: the theme waits its turn and the hourly
    loop runs it when the channel is free.
    """
    row = store.get(conn, PRODUCT, submission_id)
    if row is None:
        raise ValueError("That theme no longer exists.")
    if str(row["state"]) != "pending":
        raise ValueError(f"That theme is already {row['state']}.")
    fresh = store.move_state(
        conn, PRODUCT, submission_id,
        from_state="pending", to_state="approved", resolver_id=resolver_id,
    )
    if fresh is None:
        raise ValueError("That theme was just resolved by someone else.")
    return fresh


def deny(
    conn: sqlite3.Connection,
    submission_id: int,
    *,
    resolver_id: int,
    deny_reason: str = "",
) -> sqlite3.Row:
    """Decline a pending theme and refund it. A queued one is pulled with
    :func:`withdraw_approved`; a running one with :func:`take_down`."""
    row = store.get(conn, PRODUCT, submission_id)
    if row is None:
        raise ValueError("That theme no longer exists.")
    if str(row["state"]) != "pending":
        raise ValueError(f"That theme is already {row['state']}.")
    fresh = store.move_state(
        conn, PRODUCT, submission_id,
        from_state="pending", to_state="denied",
        resolver_id=resolver_id, deny_reason=deny_reason, refund_reason="denied",
    )
    if fresh is None:
        raise ValueError("That theme was just resolved by someone else.")
    return fresh


def withdraw_approved(
    conn: sqlite3.Connection, submission_id: int, *, resolver_id: int, reason: str = ""
) -> sqlite3.Row:
    """Pull a queued theme back out before it ever ran, refunding it.

    Distinct from :func:`take_down` on purpose: this one never had its day, so
    the money goes back.
    """
    if store.get(conn, PRODUCT, submission_id) is None:
        raise ValueError("That theme no longer exists.")
    fresh = store.move_state(
        conn, PRODUCT, submission_id,
        from_state="approved", to_state="denied",
        resolver_id=resolver_id, deny_reason=reason, refund_reason="withdrawn",
    )
    if fresh is None:
        raise ValueError("That theme isn't waiting in the queue.")
    return fresh


def live_theme(conn: sqlite3.Connection, guild_id: int) -> sqlite3.Row | None:
    """The theme currently holding the channel, if any."""
    return conn.execute(
        "SELECT * FROM econ_theme_submissions WHERE guild_id = ? AND state = 'live'",
        (guild_id,),
    ).fetchone()


def next_approved(conn: sqlite3.Connection, guild_id: int) -> sqlite3.Row | None:
    """The oldest queued theme waiting for a free channel (FIFO).

    Only meaningful when nothing is live — a paid theme is never cut short to
    make room for the next, so the caller checks :func:`live_theme` first.
    """
    return conn.execute(
        "SELECT * FROM econ_theme_submissions WHERE guild_id = ? AND state = 'approved' "
        "ORDER BY created_at ASC, id ASC LIMIT 1",
        (guild_id,),
    ).fetchone()


def go_live(
    conn: sqlite3.Connection,
    submission_id: int,
    *,
    theme_channel_id: int,
    theme_message_id: int,
    window_seconds: float,
    now: float | None = None,
) -> sqlite3.Row:
    """Promote a queued theme to live, recording its announcement and clock.

    Called *after* the card is posted and pinned, so the message ids land in
    the same UPDATE as the state — matching Pin of the Day. If this raises,
    the caller deletes the message it just posted: the row was resolved out
    from under it (denied, withdrawn) and its money is already back.
    """
    now = time.time() if now is None else now
    fresh = store.move_state(
        conn, PRODUCT, submission_id,
        from_state="approved", to_state="live", now=now,
        extra={
            "went_live_at": now,
            "expires_at": now + window_seconds,
            "theme_channel_id": theme_channel_id,
            "theme_message_id": theme_message_id,
        },
    )
    if fresh is None:
        raise ValueError("That theme isn't waiting in the queue.")
    return fresh


def take_down(
    conn: sqlite3.Connection, submission_id: int, *, resolver_id: int
) -> sqlite3.Row:
    """End a running theme early (a mod yank). No refund — its day was up.

    Returns the row carrying the announcement's channel/message ids so the
    caller can unpin it.
    """
    fresh = store.move_state(
        conn, PRODUCT, submission_id,
        from_state="live", to_state="expired", resolver_id=resolver_id,
    )
    if fresh is None:
        raise ValueError("That theme isn't running any more.")
    return fresh


def expire_live_themes(
    conn: sqlite3.Connection, guild_id: int, *, now: float
) -> list[sqlite3.Row]:
    """Retire themes past their window. Returns rows to unpin.

    No refund: a theme that ran its day is a completed purchase. The caller
    unpins each returned row's announcement after the transaction commits.
    """
    due = conn.execute(
        "SELECT * FROM econ_theme_submissions "
        "WHERE guild_id = ? AND state = 'live' AND expires_at IS NOT NULL "
        "AND expires_at <= ?",
        (guild_id, now),
    ).fetchall()
    out: list[sqlite3.Row] = []
    for row in due:
        if store.move_state(
            conn, PRODUCT, int(row["id"]),
            from_state="live", to_state="expired", now=now,
        ) is not None:
            out.append(row)
    return out


def expire_stale_pending(
    conn: sqlite3.Connection, settings: EconSettings, guild_id: int, *, now: float
) -> list[sqlite3.Row]:
    """Expire and refund pending themes no mod reviewed. Returns the rows.

    Pending only. An *approved* theme is waiting for the channel to free up,
    which is the queue working as designed, not staleness — expiring one would
    refund a member whose theme was about to run.
    """
    return store.expire_stale_pending(
        conn, PRODUCT, guild_id,
        days=max(0, int(settings.theme_expire_days)), now=now,
    )


def queue_depth(conn: sqlite3.Connection, guild_id: int) -> int:
    """How many approved themes are waiting their turn."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM econ_theme_submissions "
        "WHERE guild_id = ? AND state = 'approved'",
        (guild_id,),
    ).fetchone()
    return int(row["c"])


def list_submissions(
    conn: sqlite3.Connection, guild_id: int, state: str | None = None, limit: int = 100
) -> list[sqlite3.Row]:
    """Submissions for the dashboard queue, oldest first (newest when unfiltered)."""
    return store.list_for(conn, PRODUCT, guild_id, state, limit)


def get_submission(
    conn: sqlite3.Connection, submission_id: int
) -> sqlite3.Row | None:
    return store.get(conn, PRODUCT, submission_id)


def set_submission_card(
    conn: sqlite3.Connection, submission_id: int, channel_id: int, message_id: int
) -> None:
    """Record where the approval card lives so it can be edited on resolution."""
    store.set_card(conn, PRODUCT, submission_id, channel_id, message_id)


def anonymise_live_theme(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> int:
    """Detach a RUNNING theme from an erased member, leaving it running.

    Erasure has to reach the buyer's name, but a live theme holds something
    outside its own table: a pinned announcement in the theme channel that
    only the expiry sweep knows how to take down, and the sweep finds its work
    by reading ``state = 'live'`` rows. Deleting the row would strip the
    member's name and simultaneously strand that pin at the top of the channel
    forever, with no row left to say it should ever come down.

    So a live theme is anonymised rather than deleted — the ``todos``
    precedent in docs/data_register.md. ``user_id`` goes to 0, which is the
    same unknown-member every other surface renders and which also takes the
    row out of the purge's ``WHERE user_id = ?`` sweep; the window, the pin
    ids and the state are untouched, so the announcement comes down on
    schedule. Every non-live row of theirs is deleted normally.

    Returns the number of rows detached (0 or 1 — the partial unique index
    allows only one live theme per guild). Called at the top of
    ``econ_purge_user``, before the deletes.
    """
    cur = conn.execute(
        "UPDATE econ_theme_submissions SET user_id = 0 "
        "WHERE guild_id = ? AND user_id = ? AND state = 'live'",
        (guild_id, user_id),
    )
    return cur.rowcount or 0
