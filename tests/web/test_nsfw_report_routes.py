"""Tests for /api/moderation/nsfw-tags and /api/moderation/nsfw-blocks.

What matters here is what the service tests can't reach: the aggregations the
two report panels are built on, and the disagreement counts in particular —
those exist to make the NudeNet blind spot that prompted the Marqo swap
visible, so a query that quietly reports zero would hide exactly the thing the
report is for.
"""

from __future__ import annotations

import time

from bot_modules.core.db_utils import open_db
from bot_modules.services.nsfw_classifier_service import (
    ACTION_LOGGED,
    ACTION_REMOVED,
    SURFACE_SFW,
    SURFACE_SPOILER,
)

BIG_MESSAGE_ID = 1387654321098765432  # > 2**53, must survive as a string
BIG_AUTHOR_ID = 1387654321098765111


def _classify(db_path, guild_id, *, message_id, score, verdict, label=None, ms=120):
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO nsfw_classifications
                (message_id, attachment_id, guild_id, channel_id, verdict,
                 marqo_score, top_label, top_score, model, threshold, label_set,
                 inference_ms, bytes, created_at)
            VALUES (?, 1, ?, 55, ?, ?, ?, ?, 'marqo-384+640m', 0.5, '', ?, 1000, ?)
            """,
            (
                message_id,
                guild_id,
                int(verdict),
                score,
                label,
                0.7 if label else None,
                ms,
                int(time.time()),
            ),
        )


def _block(db_path, guild_id, *, message_id, score, surface, action, author=4242):
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO nsfw_blocks
                (message_id, attachment_id, guild_id, channel_id, author_id,
                 filename, marqo_score, surface, action, created_at)
            VALUES (?, 1, ?, 55, ?, 'holiday.jpg', ?, ?, ?, ?)
            """,
            (
                message_id,
                guild_id,
                author,
                score,
                surface,
                action,
                int(time.time()),
            ),
        )


# ── GET /api/moderation/nsfw-tags ─────────────────────────────────────


def test_tags_empty_on_fresh_db(open_client):
    data = open_client.get("/api/moderation/nsfw-tags").json()
    assert data["classified"] == 0
    assert data["labels"] == []
    assert data["scores"] == []


def test_tags_counts_and_label_distribution(open_client, fake_ctx):
    gid = fake_ctx.guild_id
    _classify(fake_ctx.db_path, gid, message_id=1, score=0.9, verdict=True,
              label="SEX_ACT")
    _classify(fake_ctx.db_path, gid, message_id=2, score=0.95, verdict=True,
              label="SEX_ACT")
    _classify(fake_ctx.db_path, gid, message_id=3, score=0.05, verdict=False)

    data = open_client.get("/api/moderation/nsfw-tags").json()

    assert data["classified"] == 3
    assert data["explicit"] == 2
    assert data["tagged"] == 2
    assert data["labels"][0]["label"] == "SEX_ACT"
    assert data["labels"][0]["count"] == 2
    # Mean verdict score across images carrying the tag.
    assert data["labels"][0]["avg_score"] == 0.925


def test_tags_surfaces_the_blind_spot(open_client, fake_ctx):
    # An image the verdict engine called explicit that the tagger saw nothing
    # in — the exact failure that caused the engine swap. If this ever reads
    # zero while such rows exist, the report is lying about the thing it is for.
    gid = fake_ctx.guild_id
    _classify(fake_ctx.db_path, gid, message_id=1, score=0.91, verdict=True)
    _classify(fake_ctx.db_path, gid, message_id=2, score=0.2, verdict=False,
              label="BUTTOCKS_EXPOSED")

    data = open_client.get("/api/moderation/nsfw-tags").json()

    assert data["explicit_untagged"] == 1
    assert data["tagged_not_explicit"] == 1


def test_tags_ignores_pre_swap_rows(open_client, fake_ctx):
    # Rows written before the swap have no marqo_score and a verdict that came
    # from NudeNet labels instead. Counting them would mix two different
    # meanings of "explicit" into one number.
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            """
            INSERT INTO nsfw_classifications
                (message_id, attachment_id, guild_id, channel_id, verdict,
                 top_label, top_score, model, threshold, label_set,
                 inference_ms, bytes, created_at)
            VALUES (9, 1, ?, 55, 1, 'SEX_ACT', 0.8, '320n', 0.5, 'SEX_ACT',
                    74, 1000, ?)
            """,
            (fake_ctx.guild_id, int(time.time())),
        )

    data = open_client.get("/api/moderation/nsfw-tags").json()

    assert data["classified"] == 0


def test_tags_score_histogram_buckets(open_client, fake_ctx):
    gid = fake_ctx.guild_id
    _classify(fake_ctx.db_path, gid, message_id=1, score=0.04, verdict=False)
    _classify(fake_ctx.db_path, gid, message_id=2, score=0.08, verdict=False)
    _classify(fake_ctx.db_path, gid, message_id=3, score=0.91, verdict=True)
    # Exactly 1.0 has no eleventh bucket to fall into and must fold into the top.
    _classify(fake_ctx.db_path, gid, message_id=4, score=1.0, verdict=True)

    scores = open_client.get("/api/moderation/nsfw-tags").json()["scores"]
    buckets = {s["floor"]: s for s in scores}

    assert buckets[0.0]["count"] == 2
    assert buckets[0.9]["count"] == 2
    assert buckets[0.9]["explicit"] == 2


def test_tags_scoped_to_the_active_guild(open_client, fake_ctx):
    _classify(fake_ctx.db_path, fake_ctx.guild_id, message_id=1, score=0.9,
              verdict=True)
    _classify(fake_ctx.db_path, 999999, message_id=2, score=0.9, verdict=True)

    assert open_client.get("/api/moderation/nsfw-tags").json()["classified"] == 1


# ── GET /api/moderation/nsfw-blocks ───────────────────────────────────


def test_blocks_empty_on_fresh_db(open_client):
    data = open_client.get("/api/moderation/nsfw-blocks").json()
    assert data["entries"] == []
    assert data["total"] == 0


def test_blocks_returns_snowflakes_as_strings(open_client, fake_ctx):
    # A member id above 2**53 must not be rounded into a different, real member
    # by the browser's float maths.
    _block(fake_ctx.db_path, fake_ctx.guild_id, message_id=BIG_MESSAGE_ID,
           score=0.93, surface=SURFACE_SFW, action=ACTION_REMOVED,
           author=BIG_AUTHOR_ID)

    (entry,) = open_client.get("/api/moderation/nsfw-blocks").json()["entries"]

    assert entry["message_id"] == str(BIG_MESSAGE_ID)
    assert entry["author_id"] == str(BIG_AUTHOR_ID)


def test_blocks_splits_removed_from_log_mode(open_client, fake_ctx):
    gid = fake_ctx.guild_id
    _block(fake_ctx.db_path, gid, message_id=1, score=0.9, surface=SURFACE_SFW,
           action=ACTION_REMOVED)
    _block(fake_ctx.db_path, gid, message_id=2, score=0.8, surface=SURFACE_SFW,
           action=ACTION_LOGGED)

    data = open_client.get("/api/moderation/nsfw-blocks").json()

    assert data["total"] == 2
    assert data["removed"] == 1
    assert data["logged"] == 1


def test_blocks_counts_by_surface_and_filters(open_client, fake_ctx):
    gid = fake_ctx.guild_id
    _block(fake_ctx.db_path, gid, message_id=1, score=0.9, surface=SURFACE_SFW,
           action=ACTION_REMOVED)
    _block(fake_ctx.db_path, gid, message_id=2, score=None,
           surface=SURFACE_SPOILER, action=ACTION_REMOVED)

    data = open_client.get("/api/moderation/nsfw-blocks").json()
    assert data["by_surface"] == {SURFACE_SFW: 1, SURFACE_SPOILER: 1}

    filtered = open_client.get(
        "/api/moderation/nsfw-blocks", params={"surface": SURFACE_SPOILER}
    ).json()
    assert filtered["total"] == 1
    # An unreadable image kept distinct from a confident zero — the spoiler gate
    # deletes on UNKNOWN by design, and that is the case worth finding again.
    assert filtered["entries"][0]["score"] is None


def test_blocks_scoped_to_the_active_guild(open_client, fake_ctx):
    _block(fake_ctx.db_path, fake_ctx.guild_id, message_id=1, score=0.9,
           surface=SURFACE_SFW, action=ACTION_REMOVED)
    _block(fake_ctx.db_path, 999999, message_id=2, score=0.9,
           surface=SURFACE_SFW, action=ACTION_REMOVED)

    assert open_client.get("/api/moderation/nsfw-blocks").json()["total"] == 1
