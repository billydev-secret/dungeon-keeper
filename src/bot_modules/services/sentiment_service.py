"""VADER-based sentiment analysis batch pipeline.

Runs periodically to score un-analysed messages.  Results are stored in the
``message_sentiment`` table for consumption by the health dashboard.
"""

from __future__ import annotations

import logging
import sqlite3
import time

log = logging.getLogger("dungeonkeeper.sentiment")

# Lazy-load VADER so the import cost is only paid when actually scoring
_analyzer = None


def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        try:
            from vaderSentiment.vaderSentiment import (  # type: ignore[import-untyped]
                SentimentIntensityAnalyzer,
            )

            _analyzer = SentimentIntensityAnalyzer()
        except ImportError:
            log.warning("vaderSentiment not installed — sentiment analysis disabled")
            return None
    return _analyzer


def _classify_emotion(compound: float, pos: float, neg: float) -> str:
    """Map VADER scores to a simple emotion category."""
    if compound >= 0.5:
        return "joy"
    if compound >= 0.15:
        return "playful"
    if compound <= -0.5:
        return "anger"
    if compound <= -0.15:
        return "frustration"
    return "neutral"


def score_text(text: str | None) -> tuple[float | None, str | None]:
    """Score a single message and return ``(compound, emotion)`` or ``(None, None)``."""
    if not text or len(text.strip()) < 2:
        return None, None
    analyzer = _get_analyzer()
    if analyzer is None:
        return None, None
    scores = analyzer.polarity_scores(text)
    compound = round(scores["compound"], 4)
    emotion = _classify_emotion(compound, scores["pos"], scores["neg"])
    return compound, emotion


def analyze_batch(
    conn: sqlite3.Connection,
    guild_id: int,
    batch_size: int = 500,
) -> int:
    """Score up to *batch_size* messages that have no sentiment entry yet.

    Returns the number of messages scored.
    """
    analyzer = _get_analyzer()
    if analyzer is None:
        return 0

    # Find messages without sentiment scores
    rows = conn.execute(
        """SELECT m.message_id, m.channel_id, m.content
           FROM messages m
           LEFT JOIN message_sentiment ms ON m.message_id = ms.message_id
           WHERE m.guild_id = ? AND ms.message_id IS NULL
             AND m.content IS NOT NULL AND m.content != ''
           ORDER BY m.ts DESC
           LIMIT ?""",
        (guild_id, batch_size),
    ).fetchall()

    if not rows:
        return 0

    now = time.time()
    scored = 0
    insert_data = []

    for r in rows:
        text = r["content"]
        if not text or len(text.strip()) < 2:
            continue
        scores = analyzer.polarity_scores(text)
        compound = scores["compound"]
        emotion = _classify_emotion(compound, scores["pos"], scores["neg"])
        insert_data.append(
            (
                r["message_id"],
                guild_id,
                r["channel_id"],
                round(compound, 4),
                emotion,
                now,
            )
        )
        scored += 1

    if insert_data:
        conn.executemany(
            "INSERT OR IGNORE INTO message_sentiment "
            "(message_id, guild_id, channel_id, sentiment, emotion, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            insert_data,
        )
        # Also backfill the sentiment/emotion columns on the messages table
        conn.executemany(
            "UPDATE messages SET sentiment = ?, emotion = ? "
            "WHERE message_id = ? AND sentiment IS NULL",
            [(row[3], row[4], row[0]) for row in insert_data],
        )
        conn.commit()

    log.debug("Scored %d messages for guild %s", scored, guild_id)
    return scored


def backfill(
    conn: sqlite3.Connection,
    guild_id: int,
    max_messages: int = 10000,
) -> int:
    """One-time backfill of historical messages.  Safe to call repeatedly —
    only processes messages that haven't been scored yet."""
    total = 0
    while total < max_messages:
        batch = analyze_batch(conn, guild_id, batch_size=min(500, max_messages - total))
        if batch == 0:
            break
        total += batch
    return total
