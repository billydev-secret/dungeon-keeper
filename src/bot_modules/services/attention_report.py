"""One-sided (unreciprocated) attention report.

Surfaces candidate member *pairs* where one person (the "initiator") directs
sustained, lopsided attention at another (the "target") who does not reciprocate
— for a moderator to glance at and judge, never for automated action.

Design constraints, taken directly from the research memo (docs/… / artifact):

  * A flag is NOT a verdict. We gate candidates on a volume floor plus a high
    asymmetry cut, then expose the *underlying evidence* (which signals fired,
    over what window) rather than a single black-box score — a bare number
    acquires authority it hasn't earned (the COMPAS anchoring failure mode).
  * Volume alone is not diagnostic: mutual friends are high-volume too. The
    separators are direction (asymmetry), fixation (concentration), and — the
    strongest single cue — escalation *after* the target stops responding.
  * Gender-neutral: the report never uses or infers gender. It surfaces the
    shape of lopsided attention; the human decides what it means.

Directed signals unioned here (each stores actor → target):
  * replies + mentions  → user_interactions_log   (weight WEIGHT_TEXT)
  * reactions           → reaction_log             (weight WEIGHT_REACTION)
  * voice-follows       → voice_follow_log          (weight WEIGHT_VOICE_FOLLOW)

Reactions and voice-follows are live-forward only (no historical backfill), so
early on a report is text-dominated; that is expected and not a bug.
"""

from __future__ import annotations

import math
import sqlite3
import time as _time
from dataclasses import dataclass, field

# ── Tunable weights & thresholds ────────────────────────────────────────────
# Voice-follow (physically showing up where the target already is) is the
# strongest "pursuit" shape, so it counts most; a lone reaction is the weakest.
WEIGHT_TEXT = 1.0
WEIGHT_REACTION = 0.5
WEIGHT_VOICE_FOLLOW = 2.0

WINDOW_DAYS = 30
# Combined weighted events (both directions) a pair must clear before its ratio
# is read at all — so two people who interacted once don't register as "100%
# one-sided" (memo §1.1).
VOLUME_FLOOR = 15.0
# asym = w(A→B) / [w(A→B)+w(B→A)]; 0.5 balanced, →1 one person initiates all.
ASYM_CUT = 0.85
# Below this many distinct targets, "fixation" has a benign reading (a quiet
# user with one friend), so we annotate rather than trumpet it (memo §1.3).
MIN_DISTINCT_TARGETS = 5
# Escalation-after-silence compares the initiator's contact rate in equal
# windows before and after the target's last reciprocal action (memo §1.5).
ESCALATION_HALF_DAYS = 14
# Legible burst descriptor: most initiator→target events in any window this wide.
BURST_WINDOW_SECONDS = 600


@dataclass
class AttentionCandidate:
    """One flagged initiator→target pair, with every component exposed."""

    initiator_id: int
    target_id: int

    # Directed weighted volume, broken out so a mod sees what drove it.
    text_out: int  # replies + mentions initiator→target
    react_out: int  # reactions initiator→target
    voice_follow_out: int  # voice-follows initiator→target
    weight_out: float  # combined weighted initiator→target
    weight_back: float  # combined weighted target→initiator

    asymmetry: float  # w_out / (w_out + w_back), in [0,1]
    concentration: float  # share of initiator's total outbound going to target
    distinct_targets: int  # how many people the initiator engaged at all
    hhi: float  # Herfindahl index of initiator's outbound attention

    # Escalation after the target's last reciprocal action toward initiator.
    escalation: float | None  # rate_after / rate_before; None if not computable
    ever_reciprocated: bool  # did target ever act toward initiator in window?

    burstiness: float | None  # Goh–Barabási B over initiator→target gaps
    max_burst: int  # most initiator→target events within BURST_WINDOW_SECONDS

    reasons: list[str] = field(default_factory=list)  # evidence chips
    cautions: list[str] = field(default_factory=list)  # benign-reading hints


def _fetch_directed_pairs(
    conn: sqlite3.Connection, guild_id: int, since_ts: int
) -> dict[tuple[int, int], dict[str, int]]:
    """Return {(from_id, to_id): {text, react, voice}} counts within the window."""
    edges: dict[tuple[int, int], dict[str, int]] = {}

    def _bump(frm: int, to: int, key: str, n: int) -> None:
        if frm == to:
            return
        edges.setdefault((frm, to), {"text": 0, "react": 0, "voice": 0})[key] += n

    for frm, to, n in conn.execute(
        """
        SELECT from_user_id, to_user_id, COUNT(*)
        FROM user_interactions_log
        WHERE guild_id = ? AND ts >= ?
        GROUP BY from_user_id, to_user_id
        """,
        (guild_id, since_ts),
    ):
        _bump(int(frm), int(to), "text", int(n))

    for frm, to, n in conn.execute(
        """
        SELECT reactor_id, author_id, COUNT(*)
        FROM reaction_log
        WHERE guild_id = ? AND ts >= ?
        GROUP BY reactor_id, author_id
        """,
        (guild_id, since_ts),
    ):
        _bump(int(frm), int(to), "react", int(n))

    for frm, to, n in conn.execute(
        """
        SELECT from_user_id, to_user_id, COUNT(*)
        FROM voice_follow_log
        WHERE guild_id = ? AND ts >= ?
        GROUP BY from_user_id, to_user_id
        """,
        (guild_id, since_ts),
    ):
        _bump(int(frm), int(to), "voice", int(n))

    return edges


def _weighted(counts: dict[str, int]) -> float:
    return (
        counts["text"] * WEIGHT_TEXT
        + counts["react"] * WEIGHT_REACTION
        + counts["voice"] * WEIGHT_VOICE_FOLLOW
    )


def _pair_event_timestamps(
    conn: sqlite3.Connection, guild_id: int, frm: int, to: int, since_ts: int
) -> list[int]:
    """All initiator→target event timestamps (any signal) within the window, sorted."""
    ts: list[int] = []
    for table, a, b in (
        ("user_interactions_log", "from_user_id", "to_user_id"),
        ("reaction_log", "reactor_id", "author_id"),
        ("voice_follow_log", "from_user_id", "to_user_id"),
    ):
        ts.extend(
            int(r[0])
            for r in conn.execute(
                f"SELECT ts FROM {table} WHERE guild_id=? AND {a}=? AND {b}=? AND ts>=?",  # noqa: S608 — table/column names are literals above, not user input
                (guild_id, frm, to, since_ts),
            )
        )
    ts.sort()
    return ts


def _burstiness(timestamps: list[int]) -> float | None:
    """Goh–Barabási B = (σ−⟨τ⟩)/(σ+⟨τ⟩) over inter-event gaps. None if too few."""
    if len(timestamps) < 4:
        return None
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
    if len(gaps) < 3:
        return None
    mean = sum(gaps) / len(gaps)
    if mean <= 0:
        return None
    var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
    sd = math.sqrt(var)
    denom = sd + mean
    return (sd - mean) / denom if denom > 0 else None


def _max_burst(timestamps: list[int], window: int = BURST_WINDOW_SECONDS) -> int:
    """Most events falling within any `window`-second span (sliding)."""
    if not timestamps:
        return 0
    best = 1
    left = 0
    for right in range(len(timestamps)):
        while timestamps[right] - timestamps[left] > window:
            left += 1
        best = max(best, right - left + 1)
    return best


def _escalation(
    out_ts: list[int], back_ts: list[int], half_days: int = ESCALATION_HALF_DAYS
) -> float | None:
    """rate_after / rate_before around the target's last reciprocal action.

    >1 means the initiator contacted the target *more* after they last responded.
    None when the target never reciprocated (caller reports that separately) or
    there's nothing in the "before" window to compare against.
    """
    if not back_ts:
        return None
    pivot = max(back_ts)
    half = half_days * 86400
    before = sum(1 for t in out_ts if pivot - half <= t < pivot)
    after = sum(1 for t in out_ts if pivot <= t < pivot + half)
    if before == 0:
        return None
    return after / before


def compute_one_sided_attention(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    window_days: int = WINDOW_DAYS,
    now_ts: int | None = None,
    volume_floor: float = VOLUME_FLOOR,
    asym_cut: float = ASYM_CUT,
    exclude_ids: set[int] | None = None,
    limit: int = 50,
) -> list[AttentionCandidate]:
    """Return flagged initiator→target pairs, most lopsided first.

    Gating (memo §1.1): a pair surfaces only when combined weighted volume
    clears `volume_floor` AND asymmetry ≥ `asym_cut`. Everything else is
    attached as evidence/cautions for the moderator, not used to hide or rank
    behind a hidden score.
    """
    now_ts = now_ts if now_ts is not None else int(_time.time())
    since_ts = now_ts - window_days * 86400
    exclude_ids = exclude_ids or set()

    edges = _fetch_directed_pairs(conn, guild_id, since_ts)

    # Per-initiator outbound totals for concentration / HHI.
    out_total: dict[int, float] = {}
    out_targets: dict[int, list[float]] = {}
    for (frm, to), counts in edges.items():
        w = _weighted(counts)
        if w <= 0:
            continue
        out_total[frm] = out_total.get(frm, 0.0) + w
        out_targets.setdefault(frm, []).append(w)

    candidates: list[AttentionCandidate] = []
    for (frm, to), counts in edges.items():
        if frm in exclude_ids or to in exclude_ids:
            continue
        w_out = _weighted(counts)
        w_back = _weighted(edges.get((to, frm), {"text": 0, "react": 0, "voice": 0}))
        total = w_out + w_back
        if total < volume_floor:
            continue
        asym = w_out / total if total > 0 else 0.0
        if asym < asym_cut:
            continue

        a_total = out_total.get(frm, w_out) or w_out
        concentration = w_out / a_total if a_total > 0 else 0.0
        targets = out_targets.get(frm, [w_out])
        distinct = len(targets)
        hhi = sum((t / a_total) ** 2 for t in targets) if a_total > 0 else 1.0

        out_ts = _pair_event_timestamps(conn, guild_id, frm, to, since_ts)
        back_ts = _pair_event_timestamps(conn, guild_id, to, frm, since_ts)
        escalation = _escalation(out_ts, back_ts)
        burst = _burstiness(out_ts)
        max_burst = _max_burst(out_ts)

        cand = AttentionCandidate(
            initiator_id=frm,
            target_id=to,
            text_out=counts["text"],
            react_out=counts["react"],
            voice_follow_out=counts["voice"],
            weight_out=w_out,
            weight_back=w_back,
            asymmetry=asym,
            concentration=concentration,
            distinct_targets=distinct,
            hhi=hhi,
            escalation=escalation,
            ever_reciprocated=bool(back_ts),
            burstiness=burst,
            max_burst=max_burst,
        )
        _annotate(cand)
        candidates.append(cand)

    # Transparent ordering (NOT a hidden score): most lopsided & voluminous
    # first, with escalation and never-reciprocated pairs pulled up because the
    # memo names them the strongest cues.
    candidates.sort(
        key=lambda c: (
            not c.ever_reciprocated,
            (c.escalation or 0) > 1.0,
            c.asymmetry,
            c.weight_out,
        ),
        reverse=True,
    )
    return candidates[:limit]


def _annotate(c: AttentionCandidate) -> None:
    """Attach human-readable evidence chips and benign-reading cautions."""
    c.reasons.append(f"{round(c.asymmetry * 100)}% one-directional")
    if not c.ever_reciprocated:
        c.reasons.append("target never responded in-window")
    elif c.escalation is not None and c.escalation > 1.0:
        c.reasons.append(f"contact rose {c.escalation:.1f}× after they went quiet")
    if c.concentration >= 0.4 and c.distinct_targets >= MIN_DISTINCT_TARGETS:
        c.reasons.append(
            f"{round(c.concentration * 100)}% of their attention on this one person"
        )
    if c.voice_follow_out > 0:
        c.reasons.append(f"followed into voice ×{c.voice_follow_out}")
    if c.max_burst >= 6:
        c.reasons.append(f"burst of {c.max_burst} in {BURST_WINDOW_SECONDS // 60} min")

    if c.distinct_targets < MIN_DISTINCT_TARGETS:
        c.cautions.append(
            f"initiator only engages {c.distinct_targets} people — may be a small social circle"
        )
    if c.escalation is not None and c.escalation < 1.0:
        c.cautions.append("contact eased off after last response — trend is cooling")
    if c.voice_follow_out == 0 and c.react_out > c.text_out:
        c.cautions.append("mostly reactions — can read as ordinary support")
