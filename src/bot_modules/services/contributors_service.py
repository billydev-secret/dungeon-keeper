"""Contributors report — who carries the community, along five separate axes.

Replaces the member quality score, which blended four signals into one weighted
number.  A nine-week backtest (``docs/plans/quality-score-revisit.md``) showed
that composite scoring AUC 0.845 against 30-day forward silence — its own stated
purpose — while ``days since last activity`` alone scores 0.910, and that the
largest weight in it (Engagement Given, 40%) scored 0.513, a coin flip.  The
deeper problem was not the weights: across the measured leaderboards not one
member appeared on all three of the original families, so there is no set of
weights that describes these roles.  They are held by different people.

Design constraints, carried from that investigation:

  * **Five views, no composite, no overall rank.**  Each view surfaces the
    underlying counts beside its score, per the house standard for analytics
    features set by ``attention_report.py`` — a bare number acquires authority
    it hasn't earned.
  * **Channel-adjust everything.**  On raw hit rate the top conversation
    catalyst restarted 72.9% of the quiet moments it entered; adjusting for how
    often *anyone* restarts those particular channels dropped them out of the
    top twelve.  They post into rooms that restart easily.  A metric that isn't
    relative to its channel mostly measures which room someone likes.
  * **Normalise by opportunity, not volume.**  Raw counts of "newcomers
    answered" reproduce the breadth ranking almost exactly, because the most
    active people do the most of everything.  Only a share-of-their-own-replies
    lift separates *disproportionate* welcoming from simply being busy.
  * **Mod-only.**  Members never see this, and there is no Discord surface.  A
    number nobody can see cannot be optimised for, which is the whole defence
    against Goodhart in a server this size.

Deliberately not built: an "Icebreakers" view (how often a member's reply is the
first answer someone got).  85.5% of all replied-to messages receive exactly one
distinct replier, so the base rate is ~85% and every member clusters between
1.1x and 1.25x.  The metric cannot discriminate; don't rebuild it.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_DAYS = 90  # also the floor: reaction_log only starts 2026-04-05

# A newcomer is someone inside this many days of their *first message*.  Join
# events are not used: Discord resets ``joined_at`` when a member rejoins, which
# is what let a returning member score 331% on the old scorer's 25% component.
NEWCOMER_DAYS = 14

# Conversation catalyst: a message breaking this much channel silence, judged by
# what happens in the window after it.
LULL_SECONDS = 3 * 3600
RESPONSE_SECONDS = 1800
RESPONSE_MIN_MESSAGES = 3
RESPONSE_MIN_PEOPLE = 2

# Sample floors before a channel earns a baseline of its own.
MIN_CHANNEL_POSTS = 30
MIN_CHANNEL_ATTEMPTS = 5

# Sample floors before a member appears in a view.  Open question in the plan
# doc: lower floors admit noisy small-sample rows, higher ones hide quieter
# contributors.  Revisit once the panel is visible.
MIN_POSTS = 15
MIN_REVIVAL_ATTEMPTS = 8
MIN_REPLIES_SENT = 150
MIN_ACTS_GIVEN = 50

# Under-attended weighting: a target receiving the median gets weight 1.0, and
# the weight is capped at this multiple so one near-silent target can't dominate
# a member's whole score.
ATTENTION_WEIGHT_CAP = 8.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ContributorEntry:
    """One member's standing in one view.

    ``score`` is the sort key — a lift against a baseline for every view except
    Connectors, where it is the raw partner count (there is nothing to be
    relative to).  The remaining fields are the evidence the panel renders
    beside it; a view leaves the ones it doesn't use at zero.
    """

    user_id: int
    score: float
    volume: int  # sample size: posts, revival attempts, or replies sent
    own_rate: float = 0.0
    baseline: float = 0.0
    partners: int = 0
    given: int = 0
    received: int = 0
    concentration: float = 0.0


@dataclass
class ContributorsReport:
    popular: list[ContributorEntry] = field(default_factory=list)
    catalyst: list[ContributorEntry] = field(default_factory=list)
    connectors: list[ContributorEntry] = field(default_factory=list)
    welcomers: list[ContributorEntry] = field(default_factory=list)
    under_attended: list[ContributorEntry] = field(default_factory=list)
    window_days: int = WINDOW_DAYS
    members_considered: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lift(actual: float, expected: float) -> float:
    """Ratio of observed to expected, or 0.0 where there was no opportunity."""
    return actual / expected if expected > 0 else 0.0


def _leave_one_out(
    channel_total: float,
    channel_count: float,
    own_total: float,
    own_count: float,
    floor: int,
) -> float | None:
    """A channel's rate with this member's own rows removed.

    A member who dominates a small room would otherwise be measured largely
    against themselves, pulling their lift toward 1.0 and hiding exactly the
    outperformance the view exists to surface.  Returns ``None`` when what's
    left is too small to be a baseline.
    """
    others_count = channel_count - own_count
    if others_count < floor:
        return None
    return (channel_total - own_total) / others_count


def _rank(entries: list[ContributorEntry]) -> list[ContributorEntry]:
    return sorted(entries, key=lambda e: (-e.score, -e.volume, e.user_id))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_contributors_report(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    now: datetime | None = None,
    window_days: int | None = None,
    member_ids: set[int] | None = None,
    include_bots: bool = False,
) -> ContributorsReport:
    """Compute all five views over one rolling window.

    ``member_ids`` restricts the output to people still in the guild.  The route
    passes live guild members where it has them and falls back to
    ``known_users.current_member`` otherwise; ``None`` means "everyone who
    appears in the data", which is what the tests use.
    """
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    win = window_days if window_days is not None else WINDOW_DAYS
    start = now_ts - win * 86400

    bots: set[int] = set()
    if not include_bots:
        bots = {
            int(r[0])
            for r in conn.execute(
                "SELECT user_id FROM known_users WHERE guild_id = ? AND is_bot = 1",
                (guild_id,),
            )
        }

    if member_ids is None:
        member_ids = {
            int(r[0])
            for r in conn.execute(
                "SELECT user_id FROM known_users"
                " WHERE guild_id = ? AND current_member = 1",
                (guild_id,),
            )
        } or None  # empty table → don't filter everyone out

    def eligible(uid: int) -> bool:
        return uid not in bots and (member_ids is None or uid in member_ids)

    # -- one bulk fetch of the dominant volume, ordered for the channel walk --
    rows = conn.execute(
        "SELECT message_id, author_id, ts, channel_id, reply_to_id FROM messages"
        " WHERE guild_id = ? AND ts >= ? ORDER BY channel_id, ts",
        (guild_id, int(start)),
    ).fetchall()
    msgs = [(int(r[0]), int(r[1]), float(r[2]), int(r[3]), r[4]) for r in rows]
    author_of = {m[0]: m[1] for m in msgs}

    attachments = {
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT ma.message_id FROM message_attachments ma"
            " JOIN messages m ON ma.message_id = m.message_id"
            " WHERE m.guild_id = ? AND m.ts >= ?",
            (guild_id, int(start)),
        )
    }

    # first message ever, for newcomer status — not joined_at, see NEWCOMER_DAYS
    first_seen = {
        int(r[0]): float(r[1])
        for r in conn.execute(
            "SELECT author_id, MIN(ts) FROM messages WHERE guild_id = ?"
            " GROUP BY author_id",
            (guild_id,),
        )
    }

    reactions = [
        (int(r[0]), int(r[1]), int(r[2]))
        for r in conn.execute(
            "SELECT reactor_id, author_id, message_id FROM reaction_log"
            " WHERE guild_id = ? AND ts >= ?",
            (guild_id, int(start)),
        )
    ]

    # ------------------------------------------------------------------
    # Shared aggregates
    # ------------------------------------------------------------------
    unique_reactors: dict[int, set[int]] = defaultdict(set)
    unique_repliers: dict[int, set[int]] = defaultdict(set)
    # distinct people who responded to a member at all, over the window — kept
    # per author so the popular-content view doesn't rescan every message
    audience: dict[int, set[int]] = defaultdict(set)
    outbound: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    received: dict[int, int] = defaultdict(int)
    replies_sent: dict[int, int] = defaultdict(int)
    replies_to_newcomer: dict[int, int] = defaultdict(int)
    newcomers_reached: dict[int, set[int]] = defaultdict(set)

    for reactor, target, mid in reactions:
        if reactor in bots or target in bots or reactor == target:
            continue
        unique_reactors[mid].add(reactor)
        audience[target].add(reactor)
        outbound[reactor][target] += 1
        received[target] += 1

    for mid, uid, ts, _ch, reply_to in msgs:
        if uid in bots or reply_to is None:
            continue
        target = author_of.get(int(reply_to))
        if target is None or target == uid or target in bots:
            continue
        unique_repliers[int(reply_to)].add(uid)
        audience[target].add(uid)
        outbound[uid][target] += 1
        received[target] += 1
        replies_sent[uid] += 1
        seen = first_seen.get(target)
        if seen is not None and seen >= start and ts - seen <= NEWCOMER_DAYS * 86400:
            replies_to_newcomer[uid] += 1
            newcomers_reached[uid].add(target)

    report = ContributorsReport(
        window_days=win,
        members_considered=len({m[1] for m in msgs if eligible(m[1])}),
    )

    # ------------------------------------------------------------------
    # 1. Popular content — responders per post vs the channel's own average
    # ------------------------------------------------------------------
    ch_value: dict[int, float] = defaultdict(float)
    ch_posts: dict[int, int] = defaultdict(int)
    mine: dict[int, dict[int, list[float]]] = defaultdict(
        lambda: defaultdict(lambda: [0.0, 0.0])
    )
    for mid, uid, _ts, ch, reply_to in msgs:
        if uid in bots:
            continue
        if reply_to is not None and mid not in attachments:
            continue  # a "post" is a conversation starter or an attachment
        value = len(unique_reactors.get(mid, ())) + len(unique_repliers.get(mid, ()))
        ch_value[ch] += value
        ch_posts[ch] += 1
        mine[uid][ch][0] += value
        mine[uid][ch][1] += 1

    for uid, per_channel in mine.items():
        if not eligible(uid):
            continue
        n = int(sum(v[1] for v in per_channel.values()))
        got = sum(v[0] for v in per_channel.values())
        expected = 0.0
        for ch, (own_value, own_posts) in per_channel.items():
            rate = _leave_one_out(
                ch_value[ch], ch_posts[ch], own_value, own_posts, MIN_CHANNEL_POSTS
            )
            if rate is not None:
                expected += own_posts * rate
        if n < MIN_POSTS or expected <= 0:
            continue
        report.popular.append(
            ContributorEntry(
                user_id=uid,
                score=_lift(got, expected),
                volume=n,
                own_rate=got / n,
                baseline=expected / n,
                partners=len(audience.get(uid, ())),
            )
        )

    # ------------------------------------------------------------------
    # 2. Conversation catalyst — restarted a quiet room, vs the room's base rate
    # ------------------------------------------------------------------
    by_channel: dict[int, list[tuple[int, int, float, int, object]]] = defaultdict(list)
    for m in msgs:
        by_channel[m[3]].append(m)

    ch_attempts: dict[int, int] = defaultdict(int)
    ch_wins: dict[int, int] = defaultdict(int)
    per_member: dict[int, dict[int, list[int]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0])
    )
    for ch, series in by_channel.items():
        for i, (_mid, uid, ts, _c, _rt) in enumerate(series):
            if i == 0 or uid in bots:
                continue
            if ts - series[i - 1][2] < LULL_SECONDS:
                continue
            others: set[int] = set()
            replies = 0
            for j in range(i + 1, len(series)):
                if series[j][2] - ts > RESPONSE_SECONDS:
                    break
                other = series[j][1]
                if other != uid and other not in bots:
                    others.add(other)
                    replies += 1
            won = (
                replies >= RESPONSE_MIN_MESSAGES
                and len(others) >= RESPONSE_MIN_PEOPLE
            )
            ch_attempts[ch] += 1
            ch_wins[ch] += won
            per_member[uid][ch][0] += 1
            per_member[uid][ch][1] += won

    for uid, per_channel in per_member.items():
        if not eligible(uid):
            continue
        attempts = sum(v[0] for v in per_channel.values())
        wins = sum(v[1] for v in per_channel.values())
        expected = 0.0
        for ch, (own_attempts, own_wins) in per_channel.items():
            rate = _leave_one_out(
                ch_wins[ch],
                ch_attempts[ch],
                own_wins,
                own_attempts,
                MIN_CHANNEL_ATTEMPTS,
            )
            if rate is not None:
                expected += own_attempts * rate
        if attempts < MIN_REVIVAL_ATTEMPTS or expected <= 0:
            continue
        report.catalyst.append(
            ContributorEntry(
                user_id=uid,
                score=_lift(wins, expected),
                volume=attempts,
                own_rate=wins / attempts,
                baseline=expected / attempts,
                given=wins,
            )
        )

    # ------------------------------------------------------------------
    # 3. Connectors — breadth, reciprocity, and how spread the attention is
    # ------------------------------------------------------------------
    partners: dict[int, set[int]] = defaultdict(set)
    for src, targets in outbound.items():
        for tgt in targets:
            partners[src].add(tgt)
            partners[tgt].add(src)

    for uid, targets in outbound.items():
        if not eligible(uid):
            continue
        total = sum(targets.values())
        if total < MIN_ACTS_GIVEN:
            continue
        got = received.get(uid, 0)
        report.connectors.append(
            ContributorEntry(
                user_id=uid,
                score=float(len(partners[uid])),
                volume=total,
                partners=len(partners[uid]),
                given=total,
                received=got,
                own_rate=total / got if got else 0.0,
                concentration=max(targets.values()) / total,
            )
        )

    # ------------------------------------------------------------------
    # 4. Welcomers — share of their replies aimed at newcomers, vs server share
    # ------------------------------------------------------------------
    total_replies = sum(replies_sent.values())
    total_to_new = sum(replies_to_newcomer.values())
    server_share = total_to_new / total_replies if total_replies else 0.0
    for uid, sent in replies_sent.items():
        if not eligible(uid) or sent < MIN_REPLIES_SENT or server_share <= 0:
            continue
        share = replies_to_newcomer.get(uid, 0) / sent
        report.welcomers.append(
            ContributorEntry(
                user_id=uid,
                score=_lift(share, server_share),
                volume=sent,
                own_rate=share,
                baseline=server_share,
                partners=len(newcomers_reached.get(uid, ())),
                given=replies_to_newcomer.get(uid, 0),
            )
        )

    # ------------------------------------------------------------------
    # 5. Lifts the under-attended — replies weighted by the target's usual share
    # ------------------------------------------------------------------
    # A continuous inverse-attention weight rather than a "bottom quartile" cut:
    # a group defined by receiving little will always receive a small share of
    # everything, so a threshold version is partly tautological.
    if received:
        median_received = statistics.median(received.values()) or 1.0
        floor = median_received / ATTENTION_WEIGHT_CAP
        weighted: dict[int, float] = defaultdict(float)
        acts: dict[int, int] = defaultdict(int)
        for uid, targets in outbound.items():
            for tgt, count in targets.items():
                weighted[uid] += count * (
                    median_received / max(float(received.get(tgt, 0)), floor)
                )
                acts[uid] += count
        total_weight = sum(weighted.values())
        total_acts = sum(acts.values())
        server_weight = total_weight / total_acts if total_acts else 0.0
        for uid, n in acts.items():
            if not eligible(uid) or n < MIN_ACTS_GIVEN or server_weight <= 0:
                continue
            report.under_attended.append(
                ContributorEntry(
                    user_id=uid,
                    score=_lift(weighted[uid] / n, server_weight),
                    volume=n,
                    own_rate=weighted[uid] / n,
                    baseline=server_weight,
                    partners=len(outbound[uid]),
                )
            )

    report.popular = _rank(report.popular)
    report.catalyst = _rank(report.catalyst)
    report.connectors = _rank(report.connectors)
    report.welcomers = _rank(report.welcomers)
    report.under_attended = _rank(report.under_attended)
    return report
