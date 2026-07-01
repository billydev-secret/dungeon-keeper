"""Rules Watch — signal computation and priority scoring.

All DB-bound helpers accept an open sqlite3.Connection.
The monitor calls `identify_target` and `compute_signals`, then `compute_priority`.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Boundary tokens — words/phrases that are explicit stop-signals.
# Only used for public-text detection; not a complete list.
# ---------------------------------------------------------------------------

_BOUNDARY_TOKENS: frozenset[str] = frozenset({
    "stop", "no", "please stop", "not interested", "leave me alone",
    "back off", "stop it", "cut it out", "i said no", "go away",
    "red", "yellow",   # common safewords
})

_BOUNDARY_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(_BOUNDARY_TOKENS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# How far back (seconds) to look for a recent consent-pair revocation.
_REVOCATION_WINDOW_SECS = 72 * 3600


# ---------------------------------------------------------------------------
# Target identification
# ---------------------------------------------------------------------------

@dataclass
class TargetResult:
    target_id: int | None
    confidence: str  # 'high' | 'medium' | 'low' | 'none'


def identify_target(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    author_id: int,
    reply_to_id: int | None,
    mention_ids: list[int],
    *,
    window_messages: list[dict[str, Any]] | None = None,
) -> TargetResult:
    """Determine the most likely target of a message.

    Priority:
      1. Reply-chain target (high confidence)
      2. First @mention (high confidence)
      3. Most-mentioned non-author in recent window (medium confidence)
      4. Unknown (none)
    """
    if reply_to_id is not None:
        row = conn.execute(
            "SELECT author_id FROM messages WHERE message_id = ?",
            (reply_to_id,),
        ).fetchone()
        if row and row["author_id"] != author_id:
            return TargetResult(target_id=int(row["author_id"]), confidence="high")

    non_self = [mid for mid in mention_ids if mid != author_id]
    if non_self:
        return TargetResult(target_id=non_self[0], confidence="high")

    # Count directed mentions in recent window
    if window_messages:
        counts: dict[int, int] = {}
        for msg in window_messages:
            if msg.get("author_id") == author_id:
                for uid in (msg.get("mentions") or []):
                    if uid != author_id:
                        counts[uid] = counts.get(uid, 0) + 1
        if counts:
            best = max(counts, key=lambda k: counts[k])
            return TargetResult(target_id=best, confidence="medium")

    # Last person the author replied to in this channel
    row = conn.execute(
        """
        SELECT m2.author_id
        FROM messages m1
        JOIN messages m2 ON m2.message_id = m1.reply_to_id
        WHERE m1.guild_id = ? AND m1.channel_id = ? AND m1.author_id = ?
          AND m2.author_id != m1.author_id
        ORDER BY m1.ts DESC
        LIMIT 1
        """,
        (guild_id, channel_id, author_id),
    ).fetchone()
    if row:
        return TargetResult(target_id=int(row["author_id"]), confidence="low")

    return TargetResult(target_id=None, confidence="none")


# ---------------------------------------------------------------------------
# Content signals
# ---------------------------------------------------------------------------

def check_boundary_token(content: str) -> bool:
    return bool(_BOUNDARY_RE.search(content))


def detect_slur(content: str) -> bool:
    """Very lightweight slur/identity-attack detector.

    Defers to the guard model for nuance; this catches only overt lexical hits.
    The word list is intentionally minimal — the guard model is the primary
    content engine.  Extend via config in a future tuning pass.
    """
    _SLUR_RE = _get_slur_re()
    return bool(_SLUR_RE.search(content))


_slur_re_cache: re.Pattern[str] | None = None

def _get_slur_re() -> re.Pattern[str]:
    global _slur_re_cache
    if _slur_re_cache is None:
        # Hard slurs — identity attacks that are violations regardless of consent.
        # Deliberately short; the guard model handles the broader taxonomy.
        # NOTE: "slut", "whore", "cunt" are intentionally NOT here — on this
        # kink-positive community they are consensual vocabulary (and appear in
        # GIF/Tenor URLs), so gating the guard model on them produced almost all
        # of the historical false positives. Context is left to the guard model.
        terms = [
            r"f[a4]gg[o0]t", r"\btr[a4]nn[y]?\b", r"\bret[a4]rd\b",
            r"\bn[i1]gg[a4e3]r\b", r"\bch[i1]nk\b", r"\bsp[i1]c\b",
            r"\bk[i1]ke\b", r"\bb[e3]aner\b",
        ]
        _slur_re_cache = re.compile(
            "(" + "|".join(terms) + ")", re.IGNORECASE
        )
    return _slur_re_cache


def compute_vader_trajectory(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    target_id: int,
    n: int = 10,
) -> float | None:
    """Sentiment shift for the target's last N messages in this channel.

    Positive = trending more positive; negative = trending more negative.
    Returns None if fewer than 3 scored messages are available.
    """
    rows = conn.execute(
        """
        SELECT sentiment FROM messages
        WHERE guild_id = ? AND channel_id = ? AND author_id = ?
          AND sentiment IS NOT NULL
        ORDER BY ts DESC
        LIMIT ?
        """,
        (guild_id, channel_id, target_id, n),
    ).fetchall()
    if len(rows) < 3:
        return None
    scores = [float(r["sentiment"]) for r in reversed(rows)]
    half = len(scores) // 2
    early_avg = sum(scores[:half]) / half
    late_avg = sum(scores[half:]) / max(len(scores) - half, 1)
    return round(late_avg - early_avg, 4)


# ---------------------------------------------------------------------------
# Context signals
# ---------------------------------------------------------------------------

def get_mutual_count(
    conn: sqlite3.Connection, guild_id: int, a: int, b: int
) -> int:
    """Sum of directed weights A→B plus B→A from user_interactions."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(weight), 0) as total
        FROM user_interactions
        WHERE guild_id = ?
          AND ((from_user_id = ? AND to_user_id = ?)
               OR (from_user_id = ? AND to_user_id = ?))
        """,
        (guild_id, a, b, b, a),
    ).fetchone()
    return int(row["total"]) if row else 0


def get_reciprocity_ratio(
    conn: sqlite3.Connection, guild_id: int, author: int, target: int
) -> float | None:
    """A→target weight divided by total A↔target weight.

    0.5 = perfectly balanced; 1.0 = entirely one-sided (author→target).
    Returns None if no interactions recorded.
    """
    rows = conn.execute(
        """
        SELECT from_user_id, to_user_id, weight
        FROM user_interactions
        WHERE guild_id = ?
          AND ((from_user_id = ? AND to_user_id = ?)
               OR (from_user_id = ? AND to_user_id = ?))
        """,
        (guild_id, author, target, target, author),
    ).fetchall()
    a_to_t = sum(r["weight"] for r in rows if r["from_user_id"] == author)
    t_to_a = sum(r["weight"] for r in rows if r["from_user_id"] == target)
    total = a_to_t + t_to_a
    if total == 0:
        return None
    return round(a_to_t / total, 4)


def get_consent_state(
    conn: sqlite3.Connection, guild_id: int, a: int, b: int
) -> tuple[bool, bool]:
    """Return (is_paired, recently_revoked) for the pair (a, b).

    recently_revoked is True if a revocation appears in dm_audit_log within
    the last 72 hours and no subsequent pairing exists.
    """
    lo, hi = (a, b) if a < b else (b, a)
    pair_row = conn.execute(
        "SELECT 1 FROM dm_consent_pairs WHERE guild_id = ? AND user_low = ? AND user_high = ?",
        (guild_id, lo, hi),
    ).fetchone()
    is_paired = pair_row is not None

    cutoff = time.time() - _REVOCATION_WINDOW_SECS
    revoke_row = conn.execute(
        """
        SELECT 1 FROM dm_audit_log
        WHERE guild_id = ?
          AND ((user_a_id = ? AND user_b_id = ?) OR (user_a_id = ? AND user_b_id = ?))
          AND action = 'revoke'
          AND timestamp >= ?
        LIMIT 1
        """,
        (guild_id, a, b, b, a, cutoff),
    ).fetchone()
    recently_revoked = (not is_paired) and (revoke_row is not None)

    return is_paired, recently_revoked


def is_dm_tier_mismatch(author_mode: str, target_mode: str) -> bool:
    """True when the author's DM mode is more open than the target's.

    'open' > 'ask' > 'closed' — a mismatch is: author is open, target is closed/ask,
    or author is ask, target is closed.
    """
    rank = {"open": 2, "ask": 1, "closed": 0}
    return rank.get(author_mode, 1) > rank.get(target_mode, 1)


def compute_thread_reciprocity(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    author_id: int,
    target_id: int,
    lookback_messages: int = 30,
) -> float | None:
    """Ratio of target messages to total messages in recent window.

    A low value means the target is contributing much less than the author —
    a one-sided conversation shape.  Returns None if too few messages.
    """
    rows = conn.execute(
        """
        SELECT author_id FROM messages
        WHERE guild_id = ? AND channel_id = ?
          AND author_id IN (?, ?)
        ORDER BY ts DESC
        LIMIT ?
        """,
        (guild_id, channel_id, author_id, target_id, lookback_messages),
    ).fetchall()
    if len(rows) < 4:
        return None
    target_count = sum(1 for r in rows if r["author_id"] == target_id)
    return round(target_count / len(rows), 4)


def count_persistence(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    author_id: int,
    target_id: int,
) -> int:
    """Count author's consecutive directed messages to target with no target reply.

    Looks at the recent channel history; counts from the last target message
    forward.  Only messages from author that mention or reply to target count.
    """
    # Find timestamp of target's most recent message in channel
    last_target_row = conn.execute(
        """
        SELECT ts FROM messages
        WHERE guild_id = ? AND channel_id = ? AND author_id = ?
        ORDER BY ts DESC LIMIT 1
        """,
        (guild_id, channel_id, target_id),
    ).fetchone()
    since_ts = float(last_target_row["ts"]) if last_target_row else 0.0

    # Count author messages directed at target since then
    # "directed" = reply_to a target message, or explicit mention
    mention_count = conn.execute(
        """
        SELECT COUNT(DISTINCT m.message_id) FROM messages m
        JOIN message_mentions mm ON mm.message_id = m.message_id
        WHERE m.guild_id = ? AND m.channel_id = ? AND m.author_id = ?
          AND mm.user_id = ? AND m.ts > ?
        """,
        (guild_id, channel_id, author_id, target_id, since_ts),
    ).fetchone()[0]

    reply_count = conn.execute(
        """
        SELECT COUNT(*) FROM messages m
        JOIN messages m2 ON m2.message_id = m.reply_to_id
        WHERE m.guild_id = ? AND m.channel_id = ? AND m.author_id = ?
          AND m2.author_id = ? AND m.ts > ?
        """,
        (guild_id, channel_id, author_id, target_id, since_ts),
    ).fetchone()[0]

    # Deduplicate (a reply that also mentions counts once)
    return max(mention_count, reply_count)


# ---------------------------------------------------------------------------
# Priority formula
# ---------------------------------------------------------------------------

@dataclass
class Signals:
    # Content
    guard_verdict: str = "ok"
    guard_rule: str | None = None
    guard_confidence: float = 0.0
    slur_signal: bool = False
    vader_compound: float | None = None
    vader_trajectory: float | None = None
    boundary_token_crossed: bool = False
    # Context
    target_confidence: str = "none"
    mutual_interaction_count: int = 0
    reciprocity_ratio: float | None = None
    consent_pair_active: bool = False
    consent_pair_recently_revoked: bool = False
    dm_tier_mismatch: bool = False
    thread_reciprocity_ratio: float | None = None
    persistence_count: int = 0
    target_withdrew: bool = False
    tenure_days: int | None = None


@dataclass
class PriorityResult:
    score: float
    tier: str           # 'immediate' | 'digest' | 'logged'
    reason: str
    factors: list[str] = field(default_factory=list)


def compute_priority(signals: Signals) -> PriorityResult:
    """Apply the priority formula and return tier + human-readable reason."""
    if signals.guard_verdict != "flag":
        return PriorityResult(score=0.0, tier="logged", reason="guard model: ok")

    base = signals.guard_confidence * 10.0
    factors: list[str] = [f"guard conf {signals.guard_confidence:.0%}"]

    # --- Up-weights ---
    if signals.slur_signal:
        base += 3.0
        factors.append("slur/identity attack")

    if signals.boundary_token_crossed:
        base += 4.0
        factors.append("boundary token")

    if signals.consent_pair_recently_revoked:
        base += 3.0
        factors.append("consent recently revoked")

    if signals.persistence_count > 3:
        bump = min(signals.persistence_count - 3, 3)
        base += float(bump)
        factors.append(f"persistence {signals.persistence_count}")

    if signals.target_withdrew:
        base += 2.0
        factors.append("target withdrew")

    if (
        signals.thread_reciprocity_ratio is not None
        and signals.thread_reciprocity_ratio < 0.2
        and signals.persistence_count > 2
    ):
        base += 2.0
        factors.append("one-sided thread")

    if (
        signals.dm_tier_mismatch
        and signals.guard_confidence > 0.5
        and signals.target_confidence != "none"
    ):
        base += 1.0
        factors.append("DM tier mismatch")

    # Weak tenure up-weight: brand-new account targeting an established member
    if signals.tenure_days is not None and signals.tenure_days < 7:
        base += 1.0
        factors.append("new account")

    # --- Down-weights (multiplicative; floored to prevent suppression) ---
    if signals.consent_pair_active:
        base *= 0.6
        factors.append("consent pair (↓)")

    if signals.mutual_interaction_count > 100:
        base *= 0.7
        factors.append(f"mutual history {signals.mutual_interaction_count} (↓)")

    if signals.reciprocity_ratio is not None and signals.reciprocity_ratio > 0.4:
        base *= 0.8
        factors.append("balanced reciprocity (↓)")

    # Context signals are less reliable when target is ambiguous
    if signals.target_confidence in ("low", "none"):
        base *= 0.85
        factors.append(f"target confidence {signals.target_confidence} (↓)")

    # Floor: never suppress entirely
    score = max(1.0, round(base, 2))

    if score >= 7.0:
        tier = "immediate"
    elif score >= 3.0:
        tier = "digest"
    else:
        tier = "logged"

    reason = "; ".join(factors[:5])  # keep it readable
    return PriorityResult(score=score, tier=tier, reason=reason, factors=factors)
