"""QA Tracker service — DB layer for tests, verdicts, and per-guild settings.

One row per posted QA card (``qa_tests``) — sourced from a commit's own
``Testing:`` section or a role-checklist feature block — one verdict per
tester per test (``qa_verdicts``), instant currency pay on a fresh verdict
(daily cap), admin void with clawback. See docs/plans/qa-tracker.md for the
feature design.

Everything rides the caller's connection/transaction, mirroring
``economy_service`` — the caller's commit is the boundary, so a verdict
insert, its payout, and the recomputed test status land (or roll back)
together.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from bot_modules.economy.logic import local_day_bounds
from bot_modules.services.economy_service import apply_credit, apply_debit, get_balance

if TYPE_CHECKING:
    from collections.abc import Iterable

QA_PREFIX = "qa_"

VERDICTS = ("pass", "fail", "blocked")


@dataclass(frozen=True)
class QASettings:
    # On by default: cards start posting at the stage-2 merge, before the
    # stage-3 dashboard exists to flip any switch — and the safe default is
    # admins-only clicking (role_id 0) until a crew role is configured there.
    enabled: bool = True
    # QA-crew role allowed to click verdict buttons; 0 = admins only.
    role_id: int = 0
    # Channel the cards live in (the existing #testing-queue). 0 = unset.
    channel_id: int = 0
    # Coins per fresh verdict (between QOTD 10 and game-win 20).
    reward: int = 15
    # Paid verdicts per tester per guild-local day; further fresh verdicts
    # still record, they just pay nothing. 0 = never pay.
    daily_cap: int = 4


DEFAULT_QA_SETTINGS = QASettings()

_BOOL_KEYS = ["enabled"]
# Everything else on the dataclass is a plain int.
_INT_KEYS = [f.name for f in fields(QASettings) if f.name not in _BOOL_KEYS]

_ALL_KEYS = frozenset(f.name for f in fields(QASettings))


def load_qa_settings(conn: sqlite3.Connection, guild_id: int) -> QASettings:
    """Build a QASettings from stored ``qa_`` config values.

    Guild-scoped only — ``allow_legacy_fallback=False`` so an unconfigured
    guild gets real defaults instead of inheriting the legacy guild_id=0 rows.
    """
    from bot_modules.core.db_utils import get_config_value, parse_bool

    defaults = DEFAULT_QA_SETTINGS
    kwargs: dict[str, object] = {}

    for key in _BOOL_KEYS:
        raw = get_config_value(
            conn, f"{QA_PREFIX}{key}", "", guild_id, allow_legacy_fallback=False
        )
        if raw:
            kwargs[key] = parse_bool(raw, getattr(defaults, key))

    for key in _INT_KEYS:
        raw = get_config_value(
            conn, f"{QA_PREFIX}{key}", "", guild_id, allow_legacy_fallback=False
        )
        if raw:
            try:
                kwargs[key] = int(raw)
            except ValueError:
                pass

    if not kwargs:
        return defaults
    for f in defaults.__dataclass_fields__:
        if f not in kwargs:
            kwargs[f] = getattr(defaults, f)
    return QASettings(**kwargs)  # type: ignore[arg-type]


def save_qa_settings(
    conn: sqlite3.Connection, guild_id: int, values: dict[str, object]
) -> None:
    """Persist a partial dict of settings under the ``qa_`` prefix.

    Every key must name a QASettings field; an unknown key raises KeyError
    so callers can't silently write dead config. Booleans persist as "1"/"0".
    """
    from bot_modules.core.db_utils import set_config_value

    unknown = set(values) - _ALL_KEYS
    if unknown:
        raise KeyError(f"unknown qa setting(s): {sorted(unknown)}")

    for key, value in values.items():
        if isinstance(value, bool):
            stored = "1" if value else "0"
        else:
            stored = str(value)
        set_config_value(conn, f"{QA_PREFIX}{key}", stored, guild_id)


# ── status math (pure) ────────────────────────────────────────────────


def compute_status(verdicts: Iterable[str]) -> str:
    """Fold a test's un-voided verdicts into a status.

    Precedence: any fail ⇒ 'failed' · else any blocked ⇒ 'blocked' · else
    any pass ⇒ 'passed' · else 'pending'. 'archived' is set explicitly by
    an admin and is never computed here.
    """
    seen = set(verdicts)
    if "fail" in seen:
        return "failed"
    if "blocked" in seen:
        return "blocked"
    if "pass" in seen:
        return "passed"
    return "pending"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── tests CRUD ────────────────────────────────────────────────────────


def create_test(
    conn: sqlite3.Connection,
    guild_id: int,
    entry_key: str,
    title: str,
    body_md: str,
    *,
    commit_sha: str | None = None,
    commit_subject: str | None = None,
    channel_id: int | None = None,
    message_id: int | None = None,
) -> int:
    """Insert a test row and return its id.

    Idempotent on (guild_id, entry_key, commit_sha): a re-run of the
    post-commit hook lands on the unique index and returns the existing
    row's id instead of duplicating the card.
    """
    now = _utcnow()
    cur = conn.execute(
        """
        INSERT INTO qa_tests
            (guild_id, entry_key, title, body_md, commit_sha, commit_subject,
             channel_id, message_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, entry_key, commit_sha) DO NOTHING
        """,
        (guild_id, entry_key, title, body_md, commit_sha, commit_subject,
         channel_id, message_id, now, now),
    )
    if (cur.rowcount or 0) > 0:
        return int(cur.lastrowid or 0)
    row = conn.execute(
        """
        SELECT id FROM qa_tests
        WHERE guild_id = ? AND entry_key = ? AND commit_sha IS ?
        """,
        (guild_id, entry_key, commit_sha),
    ).fetchone()
    return int(row["id"])


def get_test(conn: sqlite3.Connection, test_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM qa_tests WHERE id = ?", (test_id,)
    ).fetchone()


def set_test_message(
    conn: sqlite3.Connection, test_id: int, channel_id: int, message_id: int
) -> None:
    """Store the posted card's location back on the test row."""
    conn.execute(
        """
        UPDATE qa_tests SET channel_id = ?, message_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (channel_id, message_id, _utcnow(), test_id),
    )


def set_test_thread(conn: sqlite3.Connection, test_id: int, thread_id: int) -> None:
    """Store the card's lazily-created notes thread on the test row."""
    conn.execute(
        "UPDATE qa_tests SET thread_id = ?, updated_at = ? WHERE id = ?",
        (thread_id, _utcnow(), test_id),
    )


def list_tests(
    conn: sqlite3.Connection, guild_id: int, status: str | None = None
) -> list[sqlite3.Row]:
    """Return a guild's tests, newest first, optionally filtered by status."""
    if status is not None:
        return conn.execute(
            "SELECT * FROM qa_tests WHERE guild_id = ? AND status = ? ORDER BY id DESC",
            (guild_id, status),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM qa_tests WHERE guild_id = ? ORDER BY id DESC",
        (guild_id,),
    ).fetchall()


def list_verdicts(conn: sqlite3.Connection, test_id: int) -> list[sqlite3.Row]:
    """Return every verdict on a test (voided included), oldest first."""
    return conn.execute(
        "SELECT * FROM qa_verdicts WHERE test_id = ? ORDER BY id",
        (test_id,),
    ).fetchall()


def archive_test(conn: sqlite3.Connection, test_id: int) -> bool:
    """Set a test to the terminal 'archived' status; False if no such test."""
    cur = conn.execute(
        "UPDATE qa_tests SET status = 'archived', updated_at = ? WHERE id = ?",
        (_utcnow(), test_id),
    )
    return (cur.rowcount or 0) > 0


# ── verdicts: record (pay on fresh insert) + void (clawback) ─────────


@dataclass(frozen=True)
class VerdictOutcome:
    verdict_id: int
    test_id: int
    fresh: bool  # True = first verdict by this tester; False = re-click update
    paid: int  # coins credited (0 on updates, over-cap, or reward 0)
    status: str  # the test's recomputed status


def _recompute_status(conn: sqlite3.Connection, test_id: int) -> str:
    """Fold un-voided verdicts into the test's status and persist it.

    'archived' is terminal — an archived test is returned untouched.
    ``verified_by``/``verified_at`` always mirror the earliest un-voided
    pass verdict: stamped on the first pass, re-stamped if that pass is
    later voided while another remains, cleared whenever the status is not
    'passed' (a void or an overriding fail can un-verify a card).
    """
    test = conn.execute(
        "SELECT status, verified_by FROM qa_tests WHERE id = ?", (test_id,)
    ).fetchone()
    if test is None:
        raise ValueError(f"unknown test: {test_id}")
    if test["status"] == "archived":
        return "archived"

    rows = conn.execute(
        """
        SELECT user_id, verdict, created_at FROM qa_verdicts
        WHERE test_id = ? AND voided_at IS NULL
        ORDER BY id
        """,
        (test_id,),
    ).fetchall()
    status = compute_status(r["verdict"] for r in rows)

    verified_by: int | None = None
    verified_at: str | None = None
    if status == "passed":
        first_pass = next(r for r in rows if r["verdict"] == "pass")
        verified_by = int(first_pass["user_id"])
        verified_at = first_pass["created_at"]

    conn.execute(
        """
        UPDATE qa_tests
        SET status = ?, verified_by = ?, verified_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, verified_by, verified_at, _utcnow(), test_id),
    )
    return status


def _paid_verdicts_on_day(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    local_day: str,
    tz_offset: float,
) -> int:
    """Count the tester's qa_reward payouts inside the guild-local day.

    Counted from the ledger (one ``qa_reward`` credit per paid verdict) so
    the epoch bounds from ``local_day_bounds`` apply directly; a voided
    verdict's credit stays in the ledger, so voids don't refund cap room.
    """
    start, end = local_day_bounds(local_day, tz_offset)
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM econ_ledger
        WHERE guild_id = ? AND user_id = ? AND kind = 'qa_reward'
          AND created_at >= ? AND created_at < ?
        """,
        (guild_id, user_id, start, end),
    ).fetchone()
    return int(row["n"])


def record_verdict(
    conn: sqlite3.Connection,
    settings: QASettings,
    test_id: int,
    guild_id: int,
    user_id: int,
    verdict: str,
    note: str | None = None,
    *,
    local_day: str,
    tz_offset: float = 0.0,
) -> VerdictOutcome:
    """Upsert a tester's verdict, pay on fresh insert, recompute the status.

    The UNIQUE(test_id, user_id) row is the payment race-anchor, following
    the economy's INSERT-lands ⇒ rowcount dedup pattern: the conflict-target
    INSERT decides freshness, a re-click falls through to an UPDATE of
    verdict/note and never pays again. A fresh verdict pays
    ``settings.reward`` only while the tester is under ``settings.daily_cap``
    paid verdicts for the guild-local day; the credited amount is stamped on
    the verdict row as ``paid_amount``. All writes ride the caller's
    connection — one transaction covers verdict + payout + status.

    Raises ValueError for an unknown verdict, an unknown test, or an
    archived test (archived is terminal; the cog removes the buttons).
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict: {verdict!r}")
    test = conn.execute(
        "SELECT status FROM qa_tests WHERE id = ? AND guild_id = ?",
        (test_id, guild_id),
    ).fetchone()
    if test is None:
        raise ValueError(f"unknown test: {test_id}")
    if test["status"] == "archived":
        raise ValueError(f"test {test_id} is archived")

    now = _utcnow()
    cur = conn.execute(
        """
        INSERT INTO qa_verdicts
            (test_id, guild_id, user_id, verdict, note, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(test_id, user_id) DO NOTHING
        """,
        (test_id, guild_id, user_id, verdict, note, now, now),
    )
    fresh = (cur.rowcount or 0) > 0
    if not fresh:
        conn.execute(
            """
            UPDATE qa_verdicts SET verdict = ?, note = ?, updated_at = ?
            WHERE test_id = ? AND user_id = ?
            """,
            (verdict, note, now, test_id, user_id),
        )
    row = conn.execute(
        "SELECT id FROM qa_verdicts WHERE test_id = ? AND user_id = ?",
        (test_id, user_id),
    ).fetchone()
    verdict_id = int(row["id"])

    paid = 0
    if fresh and settings.reward > 0:
        under_cap = (
            _paid_verdicts_on_day(conn, guild_id, user_id, local_day, tz_offset)
            < settings.daily_cap
        )
        if under_cap:
            paid = apply_credit(
                conn,
                guild_id,
                user_id,
                settings.reward,
                "qa_reward",
                meta={"test_id": test_id, "verdict": verdict},
            )
            conn.execute(
                "UPDATE qa_verdicts SET paid_amount = ? WHERE id = ?",
                (paid, verdict_id),
            )

    status = _recompute_status(conn, test_id)
    return VerdictOutcome(
        verdict_id=verdict_id, test_id=test_id, fresh=fresh, paid=paid, status=status
    )


@dataclass(frozen=True)
class VoidOutcome:
    verdict_id: int
    test_id: int
    clawed: int  # coins actually debited back
    shortfall: int  # paid_amount the wallet no longer covered
    status: str  # the test's recomputed status


def void_verdict(
    conn: sqlite3.Connection, verdict_id: int, actor_id: int
) -> VoidOutcome | None:
    """Void a verdict, claw back its payout, and recompute the test status.

    The economy's first clawback: debits ``min(balance, paid_amount)`` as a
    ``qa_void`` ledger row — ``apply_debit`` refuses to go negative, so a
    spent-down wallet gives back what's there and the uncovered remainder is
    recorded as ``shortfall`` in the debit's meta. The verdict row is kept
    for audit (voided_by/voided_at) and drops out of the status fold.
    Voiding an unknown or already-voided verdict is a no-op returning None.
    """
    row = conn.execute(
        "SELECT * FROM qa_verdicts WHERE id = ?", (verdict_id,)
    ).fetchone()
    if row is None or row["voided_at"] is not None:
        return None

    now = _utcnow()
    conn.execute(
        "UPDATE qa_verdicts SET voided_by = ?, voided_at = ?, updated_at = ? WHERE id = ?",
        (actor_id, now, now, verdict_id),
    )

    clawed = 0
    shortfall = 0
    paid = int(row["paid_amount"])
    if paid > 0:
        balance = get_balance(conn, row["guild_id"], row["user_id"])
        clawed = min(balance, paid)
        shortfall = paid - clawed
        if clawed > 0:
            meta: dict[str, int] = {
                "verdict_id": verdict_id,
                "test_id": int(row["test_id"]),
            }
            if shortfall:
                meta["shortfall"] = shortfall
            apply_debit(
                conn,
                row["guild_id"],
                row["user_id"],
                clawed,
                "qa_void",
                actor_id=actor_id,
                meta=meta,
            )

    status = _recompute_status(conn, int(row["test_id"]))
    return VoidOutcome(
        verdict_id=verdict_id,
        test_id=int(row["test_id"]),
        clawed=clawed,
        shortfall=shortfall,
        status=status,
    )
