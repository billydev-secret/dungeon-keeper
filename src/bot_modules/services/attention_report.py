"""One-sided (unreciprocated) attention report.

Surfaces candidate member *pairs* where one person (the "initiator") directs
sustained, lopsided attention at another (the "target") who does not reciprocate
— for a moderator to glance at and judge, never for automated action.

Design constraints, taken directly from the research memo (docs/… / artifact):

  * A flag is NOT a verdict. We gate candidates, then expose the *underlying
    evidence* (which signals fired, over what window) rather than a single
    black-box score — a bare number acquires authority it hasn't earned (the
    COMPAS anchoring failure mode).
  * Volume alone is not diagnostic: mutual friends are high-volume too. The
    separators are direction, fixation (concentration), and — the strongest
    single cue — escalation *after* the target stops responding.
  * Gender-neutral: the report never uses or infers gender. It surfaces the
    shape of lopsided attention; the human decides what it means.

The gate (rebuilt 2026-08-26). Three things changed, each measured against 30
days of live data on both servers this bot runs on:

  1. **The gate reads approach, not combined volume.** It used to demand 15
     combined weighted events from *both* directions before reading the ratio,
     and then a pair-local asymmetry of 0.85. Those two demands pull opposite
     ways: sustained one-sided pursuit is low-volume and unanswered, so the
     floor excluded exactly the region the report is for. Among pairs clearing
     that floor the 99th percentile of asymmetry was 0.75 — the cut sat above
     the entire empirical distribution, and the one pair server-wide that
     passed both was a false positive. The floor is now on ``approach_out``:
     replies, mentions and voice-follows, the acts that ask for a response.
  2. **Reciprocation is judged against the target's own habit.** ``asymmetry``
     is pair-local, so a member who posts a lot and rarely answers reads as
     "non-reciprocating" toward everyone who engages them — a fact about the
     target, not about any initiator. ``reciprocation_shortfall`` compares what
     this initiator got back against what the same target gives their *other*
     partners. Asymmetry survives as evidence, not as a gate.
  3. **Concentration is a gate, not a footnote.** A pair holding 2% of an
     initiator's outbound attention across 87 distinct targets is evidence
     *against* fixation. It used to produce a caution and surface anyway.

Reactions stay in the evidence and out of the gate: 30 unanswered reactions
cleared the old floor by themselves, and an unanswered reaction is far weaker
evidence than an unanswered reply.

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

# ── The gate ────────────────────────────────────────────────────────────────
# Weighted initiator→target events that *ask for a response* — replies,
# mentions, voice-follows. Reactions are deliberately absent (see module
# docstring). 4.0 is four unanswered replies, or two voice-follows. On prod
# that yields 4 candidates on the main guild and 6 on the second; at 5.0 the
# main guild drops to 1, which reads as "nothing concerning here" — the false
# assurance this report must not give.
APPROACH_FLOOR = 4.0
# How far below the target's own reciprocation habit this initiator falls.
# 1.0 = the target gives this person nothing while giving others their usual.
RECIPROCATION_SHORTFALL_CUT = 0.8
# Share of everything the initiator directs at anyone that goes to this one
# person. Below this the pair is one of many and fixation has no support.
CONCENTRATION_FLOOR = 0.05
# With no other partner to compare a target against, assume reciprocation
# should roughly match the approach. Neutral: it neither excuses nor accuses.
NEUTRAL_RECIPROCATION_RATE = 1.0
# A target who gives back far more than they receive shouldn't make every
# partner look neglected; cap the expectation.
MAX_RECIPROCATION_RATE = 3.0

# ── Evidence (annotation only, never gating) ────────────────────────────────
# Below this many distinct targets, "fixation" has a benign reading (a quiet
# user with one friend), so we annotate rather than trumpet it (memo §1.3).
MIN_DISTINCT_TARGETS = 5
# Escalation-after-silence compares the initiator's contact rate in windows of
# EQUAL length before and after the target's last reciprocal action (memo
# §1.5). Capped at this many days, and shortened to whatever has actually
# elapsed since — an "after" of 3 days against a "before" of 14 is not a trend.
ESCALATION_HALF_DAYS = 14
# Under this much elapsed time the after-window is noise; report nothing.
MIN_ESCALATION_SPAN_DAYS = 3
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
    approach_out: float  # weighted text + voice-follows only — what the gate reads

    asymmetry: float  # w_out / (w_out + w_back), in [0,1]. Evidence, not a gate.
    # What the target gives back per unit received, across their OTHER partners
    # — the base rate the pair is judged against. NEUTRAL_RECIPROCATION_RATE
    # when this initiator is the only partner they have.
    target_reciprocation_rate: float
    expected_back: float  # weight_out × target_reciprocation_rate
    # 1 − weight_back/expected_back, clamped to [0,1]. 0 = this target treats
    # the initiator exactly as they treat everyone else.
    reciprocation_shortfall: float
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


def _approach(counts: dict[str, int]) -> float:
    """Weighted events that ask for a response — reactions excluded.

    A reply, a mention or turning up in someone's voice channel puts a claim on
    the other person. A reaction doesn't, which is why it is evidence here and
    never a reason a pair surfaces.
    """
    return counts["text"] * WEIGHT_TEXT + counts["voice"] * WEIGHT_VOICE_FOLLOW


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
    out_ts: list[int],
    back_ts: list[int],
    now_ts: int,
    half_days: int = ESCALATION_HALF_DAYS,
) -> float | None:
    """rate_after / rate_before around the target's last reciprocal action.

    >1 means the initiator contacted the target *more* after they last responded.
    None when the target never reciprocated (caller reports that separately),
    when too little time has passed since to mean anything, or when there's
    nothing in the "before" window to compare against.

    **Both windows are the same length.** This used to compare a fixed 14 days
    of "before" against however much of 14 days had actually elapsed, so a
    target who last replied three days ago had three days of contact counted
    against fourteen — a ratio structurally pushed below 1, which then fired
    "contact eased off, trend is cooling" as an artefact of recency. On prod,
    three quarters of pairs with a computable escalation were in that state.
    """
    if not back_ts:
        return None
    pivot = max(back_ts)
    span = min(half_days * 86400, now_ts - pivot)
    if span < MIN_ESCALATION_SPAN_DAYS * 86400:
        return None
    before = sum(1 for t in out_ts if pivot - span <= t < pivot)
    after = sum(1 for t in out_ts if pivot <= t < pivot + span)
    if before == 0:
        return None
    return after / before


def compute_one_sided_attention(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    window_days: int = WINDOW_DAYS,
    now_ts: int | None = None,
    approach_floor: float = APPROACH_FLOOR,
    shortfall_cut: float = RECIPROCATION_SHORTFALL_CUT,
    concentration_floor: float = CONCENTRATION_FLOOR,
    exclude_ids: set[int] | None = None,
    limit: int = 50,
) -> list[AttentionCandidate]:
    """Return flagged initiator→target pairs, most lopsided first.

    A pair surfaces only when all three hold:

      * ``approach_out >= approach_floor`` — enough acts that ask for a
        response. Reactions don't count toward this.
      * ``concentration >= concentration_floor`` — this person is a real share
        of where the initiator's attention goes, not one of ninety.
      * the target reciprocated nothing at all, **or**
        ``reciprocation_shortfall >= shortfall_cut`` — they give this
        initiator far less than they give their other partners.

    Everything else is attached as evidence/cautions for the moderator, not
    used to hide or rank behind a hidden score.
    """
    now_ts = now_ts if now_ts is not None else int(_time.time())
    since_ts = now_ts - window_days * 86400
    exclude_ids = exclude_ids or set()

    edges = _fetch_directed_pairs(conn, guild_id, since_ts)

    # Per-initiator outbound totals for concentration / HHI. Excluded ids (bots)
    # are dropped from BOTH endpoints here, not just from the candidate gate
    # below, so a human's concentration/distinct-target evidence reflects only
    # their attention toward other people.
    out_total: dict[int, float] = {}
    in_total: dict[int, float] = {}
    out_targets: dict[int, list[float]] = {}
    for (frm, to), counts in edges.items():
        if frm in exclude_ids or to in exclude_ids:
            continue
        w = _weighted(counts)
        if w <= 0:
            continue
        out_total[frm] = out_total.get(frm, 0.0) + w
        in_total[to] = in_total.get(to, 0.0) + w
        out_targets.setdefault(frm, []).append(w)

    candidates: list[AttentionCandidate] = []
    for (frm, to), counts in edges.items():
        if frm in exclude_ids or to in exclude_ids:
            continue
        w_out = _weighted(counts)
        w_back = _weighted(edges.get((to, frm), {"text": 0, "react": 0, "voice": 0}))
        approach_out = _approach(counts)
        if approach_out < approach_floor:
            continue

        a_total = out_total.get(frm, w_out) or w_out
        concentration = w_out / a_total if a_total > 0 else 0.0
        if concentration < concentration_floor:
            continue

        # Base rate, leave-one-out: how does this target treat their *other*
        # partners? Including this pair would make it self-referential — a
        # target whose only partner is this initiator would always look like
        # they reciprocate exactly as expected, however little they give.
        recv_other = in_total.get(to, 0.0) - w_out
        give_other = out_total.get(to, 0.0) - w_back
        rate = (
            give_other / recv_other if recv_other > 0 else NEUTRAL_RECIPROCATION_RATE
        )
        rate = max(0.0, min(MAX_RECIPROCATION_RATE, rate))
        expected_back = w_out * rate
        shortfall = (
            max(0.0, min(1.0, 1.0 - w_back / expected_back))
            if expected_back > 0
            else (0.0 if w_back > 0 else 1.0)
        )
        # Two routes in: nothing came back at all, or far less came back than
        # this target gives everyone else.
        if w_back > 0 and shortfall < shortfall_cut:
            continue

        total = w_out + w_back
        asym = w_out / total if total > 0 else 0.0
        targets = out_targets.get(frm, [w_out])
        distinct = len(targets)
        hhi = sum((t / a_total) ** 2 for t in targets) if a_total > 0 else 1.0

        out_ts = _pair_event_timestamps(conn, guild_id, frm, to, since_ts)
        back_ts = _pair_event_timestamps(conn, guild_id, to, frm, since_ts)
        escalation = _escalation(out_ts, back_ts, now_ts)
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
            approach_out=approach_out,
            asymmetry=asym,
            target_reciprocation_rate=rate,
            expected_back=expected_back,
            reciprocation_shortfall=shortfall,
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

    # Transparent ordering (NOT a hidden score): unanswered first, then
    # escalating, then by how far below the target's own habit the
    # reciprocation falls, then by how much was directed at them. The memo
    # names silence and escalation the strongest cues.
    candidates.sort(
        key=lambda c: (
            not c.ever_reciprocated,
            (c.escalation or 0) > 1.0,
            c.reciprocation_shortfall,
            c.approach_out,
        ),
        reverse=True,
    )
    return candidates[:limit]


def _annotate(c: AttentionCandidate) -> None:
    """Attach human-readable evidence chips and benign-reading cautions."""
    c.reasons.append(f"{round(c.asymmetry * 100)}% one-directional")
    if not c.ever_reciprocated:
        c.reasons.append("target never responded in-window")
    else:
        if c.escalation is not None and c.escalation > 1.0:
            c.reasons.append(f"contact rose {c.escalation:.1f}× after they went quiet")
        # The base-rate comparison, said in words: "they do answer people, just
        # not this one". Only worth showing when there IS another partner to
        # have compared against — otherwise the rate is the neutral prior and
        # this would dress up an assumption as a measurement.
        if (
            c.target_reciprocation_rate != NEUTRAL_RECIPROCATION_RATE
            and c.reciprocation_shortfall >= RECIPROCATION_SHORTFALL_CUT
        ):
            c.reasons.append(
                f"{round(c.reciprocation_shortfall * 100)}% less back than this "
                "target gives their other partners"
            )
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
    # The floor is deliberately low so quiet, unanswered pursuit can surface at
    # all. Say when a candidate is sitting on it rather than letting four
    # events read like forty.
    if c.approach_out <= APPROACH_FLOOR:
        c.cautions.append(
            f"only {_approach_events(c)} approaches — thin evidence either way"
        )
    if c.target_reciprocation_rate == NEUTRAL_RECIPROCATION_RATE and c.ever_reciprocated:
        c.cautions.append(
            "no other partners to compare the target against — reciprocation is "
            "judged against a neutral assumption, not their habits"
        )


def _approach_events(c: AttentionCandidate) -> int:
    """Raw count behind ``approach_out`` — for copy that says "4 approaches"."""
    return c.text_out + c.voice_follow_out
