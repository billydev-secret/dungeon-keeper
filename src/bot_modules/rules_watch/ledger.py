"""Rules Watch — the ledger: concrete acts, recorded, never accused.

Two narrow detectors from the 2026-07-20 tuning spec (§7.3, §7.4). Neither is a
behavioural classifier — that approach failed three independent times (§12.2b,
§12.2c). Each of these fires on a *specific thing somebody said*, records it with
a date, and stops. The value is that when a human is already reviewing someone,
the prior acts are on the record instead of being reconstructed from memory.

Both were measured by replaying the full guild corpus through this module
(395,095 messages with surviving content, ~163 days / 5.4 months) before being
finalised. The volumes below are the reason the patterns are shaped the way they
are; if you widen a pattern, re-measure.

    dm_consent      2 hits  — both Ciccio, 2.5 months before his ban
    cross_platform  3 hits  — Burner ×2 (benign, an old commenter) and, the one
                              that matters, Whoami23 naming lily's Reddit post on
                              2026-07-11, the actioned case

    combined ~0.9 rows/month. All three actioned people in the corpus surface;
    no mod, greeter, or ordinary member does.

All DB-bound helpers accept an open sqlite3.Connection.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# §7.3 — DM-consent tripwire
# ---------------------------------------------------------------------------
#
# §8.1 resolved a conflict that matters enormously here. Dona's etiquette guide
# *endorses* asking publicly before DMing ("Hey, is it cool if I DM you about
# your latest post?"), while Rule 5 says use the consent bot. So public DM-asking
# must NOT fire — it is the behaviour the server teaches.
#
# What fires is the **bot-disclaimer**: claiming the consent bot is unusable, as
# cover for going around it. That is the evasion, and it is what Ciccio and
# bigoryx both said. Measured alone it hits 14× (0.03/day) but picks up cat-bot
# and jail-bot outage chatter; requiring DM intent alongside it cuts to 2, both
# true positives.

_DISCLAIMER_RE = re.compile(
    r"(don'?t|do not|dont|didn'?t|didnt|can'?t|cant|couldn'?t|couldnt|never|idk|not sure)"
    r"\s+(\w+\s+){0,3}"
    r"(know how to use|how to use|how to work|figure out|figured out|work out"
    r"|get.{0,12}to work|have access to|understand)"
    r"|(wasn'?t|isn'?t|not|never)\s+working"
    r"|(is|was)\s+(broken|down|acting up)"
    r"|\bi hate the\b",
    re.IGNORECASE,
)

_BOT_RE = re.compile(r"\bbots?\b", re.IGNORECASE)

# Intent to open a DM channel with the addressee. Deliberately requires a
# second person — "the bot is broken" about a game bot has no DM intent and
# must not fire.
_DM_INTENT_RE = re.compile(
    r"\b(dm|message|msg|pm)\s+(you|u)\b"
    r"|\bcan i (dm|message|msg|pm)\b"
    r"|\bok(ay)? to (dm|message)\b"
    r"|\bup for dms?\b"
    r"|\bask (it|the bot) if\b"
    r"|\byour dms?\b"
    r"|\bdms? (you|u)\b",
    re.IGNORECASE,
)

# Mods discussing a broken bot in their own channels is not a tripwire. Matched
# against the channel name.
_MOD_CHANNEL_RE = re.compile(r"mod-chat|ticket|admin|staff", re.IGNORECASE)

# bigoryx's script split the disclaimer and the DM ask across separate messages.
# Allowing a short same-author, same-channel window costs nothing measurable
# (still 2 hits on the corpus) and covers that shape.
_DM_INTENT_WINDOW_SECS = 300


# ---------------------------------------------------------------------------
# §7.4 — Cross-platform reference
# ---------------------------------------------------------------------------
#
# ⚠️ This is where §7.4 as written is wrong, and the corpus says so plainly.
#
# §7.4 proposes firing when A names B's off-platform presence, listing `reddit`,
# `insta`, `OF`, `snap`, `telegram`, `your post`, `your bio`. Measured:
#
#   bare platform mention                    850 hits
#   + directed at the addressee               67 hits
#   of which the intake ritual                70 hits (overlapping)
#   demonstrated observation, not the ritual   4 hits
#
# The dominant class by far is **the server's own welcome ritual** — greeters and
# mods asking new arrivals "What's your Reddit name? I like connecting the
# faces". Shipping §7.4 as specified would have built another most-beloved-member
# detector (§7.7), firing mostly on mods doing their job. §6.5 warned that the
# intake ritual would trip a classifier; it does.
#
# What separates the actioned case is not *naming* a platform but **demonstrating
# you already went and looked** — whoami23 naming the content of lily's Reddit
# post. Asking for a handle is social; arriving with her content already in hand
# is the pursuit signature. So the detector requires observation, and explicitly
# subtracts the ritual.
#
# Also dropped from §7.4's list:
#   `your bio`  — bios are an IN-server feature here, not off-platform.
#   `your post` — in the photo channels this means an in-server post.
#   bare `OF`   — unusable; matches the word "of". Requires "onlyfans".

_PLATFORM_ALT = (
    r"reddit|instagram|insta|onlyfans|only ?fans|snapchat|telegram|tiktok"
)

_PLATFORM_RE = re.compile(rf"\b({_PLATFORM_ALT})\b", re.IGNORECASE)

# The intake ritual and friendly recognition. Endorsed behaviour — exempts.
_RITUAL_RE = re.compile(
    rf"(what'?s|whats|drop|share|have)\s+(your|ur|a)\s+(\w+\s+){{0,2}}({_PLATFORM_ALT})"
    rf"|({_PLATFORM_ALT})\s+(name|handle|profile|user)"
    rf"|(know|recogni[sz]e|seen|from)\s+you\s+(from|on)\s+({_PLATFORM_ALT})"
    rf"|are you from ({_PLATFORM_ALT})"
    rf"|you'?re from ({_PLATFORM_ALT})",
    re.IGNORECASE,
)

# Author demonstrates he has already viewed the addressee's off-platform content.
_OBSERVED_RE = re.compile(
    r"\b(saw|seen|watched|looked at|checked out|found|came across"
    r"|commented on|liked|upvoted|stumbled)\b.{0,50}\b(your|ur)\b"
    rf"|\b(your|ur)\s+({_PLATFORM_ALT})\s+(post|pic|photo|vid|video|content|profile)"
    rf"|\b(your|ur)\s+(post|pic|photo|vid|video)\b.{{0,30}}({_PLATFORM_ALT})",
    re.IGNORECASE,
)

# If the target raised the platform herself *in this conversation*, referencing
# it is responsive, not pursuit.
#
# ⚠️ This window is deliberately tight, and an earlier draft got it badly wrong.
# A 30-day guild-wide version of this exemption suppressed the Whoami23 case —
# because lily is a Reddit poster who talks about Reddit, so she always had a
# recent mention somewhere. Worse, one of the mentions granting him immunity was
# lily reporting velocibaker for finding her Reddit profile. A broad exemption
# gives blanket immunity to exactly the population this is meant to protect:
# the women whose off-platform presence is known. Keep it same-channel and
# short, so it means "she just brought it up" and nothing more.
_TARGET_RAISED_LOOKBACK_SECS = 6 * 3600

_EXCERPT_CAP = 240


@dataclass
class LedgerHit:
    kind: str                      # 'dm_consent' | 'cross_platform'
    matched_phrase: str
    excerpt: str
    platform: str | None = None


def _excerpt(content: str) -> str:
    text = " ".join((content or "").split())
    return text[:_EXCERPT_CAP]


# ---------------------------------------------------------------------------
# Pure-text predicates (no DB) — the unit tests live mostly here
# ---------------------------------------------------------------------------

def has_bot_disclaimer(content: str) -> bool:
    """True if the message claims the consent bot is unusable/broken.

    Says nothing about DM intent — `detect_dm_consent` requires that too.
    """
    text = content or ""
    return bool(_BOT_RE.search(text) and _DISCLAIMER_RE.search(text))


def has_dm_intent(content: str) -> bool:
    """True if the message expresses intent to open a DM with the addressee."""
    return bool(_DM_INTENT_RE.search(content or ""))


def names_platform(content: str) -> str | None:
    """Return the first off-platform service named, or None."""
    m = _PLATFORM_RE.search(content or "")
    return m.group(1).lower() if m else None


def is_intake_ritual(content: str) -> bool:
    """True if this is the welcome-ritual handle exchange or friendly recognition.

    This is endorsed behaviour performed constantly by greeters and mods (70 hits
    in the corpus). It exempts.
    """
    return bool(_RITUAL_RE.search(content or ""))


def demonstrates_observation(content: str) -> bool:
    """True if the author shows he has already viewed the addressee's content."""
    return bool(_OBSERVED_RE.search(content or ""))


# ---------------------------------------------------------------------------
# DB-bound detection
# ---------------------------------------------------------------------------

def _channel_is_mod_space(conn: sqlite3.Connection, guild_id: int, channel_id: int) -> bool:
    row = conn.execute(
        "SELECT channel_name FROM known_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchone()
    if not row or not row["channel_name"]:
        return False
    return bool(_MOD_CHANNEL_RE.search(row["channel_name"]))


def _author_showed_dm_intent_nearby(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    author_id: int,
    at_ts: float,
    window_secs: int = _DM_INTENT_WINDOW_SECS,
) -> bool:
    """True if the author expressed DM intent within `window_secs` in this channel."""
    rows = conn.execute(
        """
        SELECT content FROM messages
        WHERE guild_id = ? AND channel_id = ? AND author_id = ?
          AND ts >= ? AND ts <= ?
          AND content IS NOT NULL AND content != ''
        """,
        (guild_id, channel_id, author_id, at_ts - window_secs, at_ts + window_secs),
    ).fetchall()
    return any(has_dm_intent(r["content"]) for r in rows)


def detect_dm_consent(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    author_id: int,
    content: str,
    at_ts: float,
) -> LedgerHit | None:
    """Record a bot-disclaimer used alongside intent to DM someone.

    Requires all of:
      - a claim that the bot is unusable/broken,
      - DM intent, in this message or within 5 minutes from the same author,
      - a non-mod channel.

    Public DM-asking on its own is endorsed by the etiquette guide and never
    fires (§8.1).
    """
    if not has_bot_disclaimer(content):
        return None
    if _channel_is_mod_space(conn, guild_id, channel_id):
        return None
    if not (
        has_dm_intent(content)
        or _author_showed_dm_intent_nearby(
            conn, guild_id, channel_id, author_id, at_ts
        )
    ):
        return None

    m = _DISCLAIMER_RE.search(content or "")
    return LedgerHit(
        kind="dm_consent",
        matched_phrase=m.group(0) if m else "bot disclaimer",
        excerpt=_excerpt(content),
    )


def _target_raised_platform(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    target_id: int,
    platform: str,
    before_ts: float,
    lookback_secs: int = _TARGET_RAISED_LOOKBACK_SECS,
) -> bool:
    """True if the target raised this platform herself, here, just now.

    Scoped to the channel and a short window on purpose — see the comment on
    `_TARGET_RAISED_LOOKBACK_SECS`. This must mean "she just brought it up",
    not "she has an internet presence".
    """
    rows = conn.execute(
        """
        SELECT content FROM messages
        WHERE guild_id = ? AND channel_id = ? AND author_id = ?
          AND ts >= ? AND ts < ?
          AND content IS NOT NULL AND content != ''
        """,
        (guild_id, channel_id, target_id, before_ts - lookback_secs, before_ts),
    ).fetchall()
    pat = re.compile(rf"\b{re.escape(platform)}\b", re.IGNORECASE)
    return any(pat.search(r["content"]) for r in rows)


def detect_cross_platform(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    author_id: int,
    target_id: int | None,
    content: str,
    at_ts: float,
) -> LedgerHit | None:
    """Record the author demonstrating he viewed the target's off-platform content.

    Requires all of:
      - an off-platform service named,
      - demonstrated observation, not a handle request (the intake ritual is
        explicitly exempt — it is 70 of the corpus's ~70 directed hits),
      - a resolved target (the directedness filter §11 calls load-bearing),
      - the target not having raised that platform here in the last few hours.
    """
    platform = names_platform(content)
    if platform is None:
        return None
    if is_intake_ritual(content):
        return None
    if not demonstrates_observation(content):
        return None
    if target_id is None or target_id == author_id:
        return None
    if _target_raised_platform(
        conn, guild_id, channel_id, target_id, platform, at_ts
    ):
        return None

    m = _OBSERVED_RE.search(content or "")
    return LedgerHit(
        kind="cross_platform",
        matched_phrase=m.group(0) if m else platform,
        excerpt=_excerpt(content),
        platform=platform,
    )


# ---------------------------------------------------------------------------
# Persistence + review queries
# ---------------------------------------------------------------------------

def record_hit(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    hit: LedgerHit,
    message_id: int,
    channel_id: int,
    author_id: int,
    target_id: int | None,
    target_confidence: str | None = None,
    detected_at: float | None = None,
) -> int | None:
    """Insert a ledger row. Returns its id, or None if already recorded."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO rules_ledger (
            guild_id, kind, message_id, channel_id, author_id,
            target_id, target_confidence,
            matched_phrase, excerpt, platform, detected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id, hit.kind, message_id, channel_id, author_id,
            target_id, target_confidence,
            hit.matched_phrase, hit.excerpt, hit.platform,
            detected_at if detected_at is not None else time.time(),
        ),
    )
    return cur.lastrowid if cur.rowcount else None


def get_ledger(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Ledger rows for review, newest first."""
    if kind:
        return conn.execute(
            """
            SELECT * FROM rules_ledger
            WHERE guild_id = ? AND kind = ?
            ORDER BY detected_at DESC LIMIT ? OFFSET ?
            """,
            (guild_id, kind, limit, offset),
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM rules_ledger
        WHERE guild_id = ?
        ORDER BY detected_at DESC LIMIT ? OFFSET ?
        """,
        (guild_id, limit, offset),
    ).fetchall()


def get_repeat_authors(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    kind: str = "cross_platform",
    min_targets: int = 2,
) -> list[sqlite3.Row]:
    """Authors who hit the same ledger against ≥N distinct targets.

    §7.4: "Escalate hard on ≥2 distinct targets — that is what separated
    whoami23 from a one-off warning." This is still not an alert; it is the
    query a human runs when deciding how seriously to take a pattern.
    """
    return conn.execute(
        """
        SELECT author_id,
               COUNT(DISTINCT target_id) AS distinct_targets,
               COUNT(*)                  AS hits,
               MIN(detected_at)          AS first_at,
               MAX(detected_at)          AS last_at
        FROM rules_ledger
        WHERE guild_id = ? AND kind = ? AND target_id IS NOT NULL
        GROUP BY author_id
        HAVING COUNT(DISTINCT target_id) >= ?
        ORDER BY distinct_targets DESC, hits DESC
        """,
        (guild_id, kind, min_targets),
    ).fetchall()
