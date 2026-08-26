"""A degraded health payload must never be written into the 15-minute cache.

From the 2026-08-06 website deep review. ``/health``'s mod-workload,
mod-engagement, newcomer-funnel and cohort-retention metrics are derived from
the **live** guild object: ``mod_ids`` and ``recent_joins`` come out of
``guild.members``. While the bot is mid-startup, or the gateway's member cache
hasn't been chunked, or the bot isn't in the guild at all, that list is empty
and the metric computes to zeroes. Caching that turns a few seconds of startup
into fifteen minutes of a confidently wrong dashboard.

The rule these tests pin:

* a degraded payload is still **returned** (a blank tile beats an error) but is
  **not written** to the cache;
* an existing good cached value keeps being served through a degraded window;
* a normal, non-degraded payload still caches exactly as before;
* the guard is scoped — metrics that read only the DB cache while degraded.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.web.conftest import StubMember


GUILD = 123


# ── helpers ──────────────────────────────────────────────────────────────


def _cache_keys(fake_ctx) -> set[str]:
    with fake_ctx.open_db() as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT metric_key FROM health_metrics_cache WHERE guild_id = ?",
                (fake_ctx.guild_id,),
            )
        }


def _newcomer(uid: int, *, days_ago: float = 2.0) -> StubMember:
    return StubMember(
        uid,
        joined_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def _seed_newcomer_activity(fake_ctx, uid: int) -> None:
    """A message from *uid* so the funnel has something to count."""
    with fake_ctx.open_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO known_users (guild_id, user_id, username,"
            " display_name, updated_at, is_bot) VALUES (?, ?, ?, ?, 0, 0)",
            (fake_ctx.guild_id, uid, f"u{uid}", f"u{uid}"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO messages (message_id, guild_id, channel_id,"
            " author_id, content, ts) VALUES (?, ?, ?, ?, 'hello', ?)",
            (5000 + uid, fake_ctx.guild_id, 77, uid, time.time() - 3600),
        )
        conn.commit()


# ── _guild_extras: what "degraded" means ─────────────────────────────────


def test_extras_are_degraded_without_a_guild():
    from web_server.routes.health import _guild_extras

    assert _guild_extras(None, None)["degraded"] is True


def test_extras_are_degraded_when_the_member_cache_holds_only_bots():
    """Mid-startup the bot's own member object is often the only one present.

    ``mod_ids``/``recent_joins`` skip bots, so this looks identical to an empty
    guild — and every real guild has at least a human owner, so it can only be
    a cold cache.
    """
    from tests.web.conftest import StubGuild
    from web_server.routes.health import _guild_extras

    guild = StubGuild(GUILD, [StubMember(1, bot=True)])
    assert _guild_extras(None, guild)["degraded"] is True


def test_extras_are_not_degraded_once_a_human_member_is_visible():
    from tests.web.conftest import StubGuild
    from web_server.routes.health import _guild_extras

    guild = StubGuild(GUILD, [StubMember(1, bot=True), _newcomer(2)])
    extras = _guild_extras(None, guild)
    assert extras["degraded"] is False
    assert extras["recent_joins"]


# ── the load-bearing behaviour ───────────────────────────────────────────


def test_degraded_newcomer_funnel_is_returned_but_not_cached(open_client, fake_ctx):
    """The whole point: zeroes computed from a missing guild don't stick."""
    body = open_client.get("/api/health/newcomer-funnel").json()
    assert body["funnel"]["joined"] == 0  # still answered, just empty
    assert not [k for k in _cache_keys(fake_ctx) if "newcomer_funnel" in k]


def test_a_later_live_call_returns_real_data_not_the_degraded_zeroes(
    open_client, fake_ctx, live_guild
):
    """Startup window first, then a warm gateway — the second call must be real."""
    assert open_client.get("/api/health/newcomer-funnel").json()["funnel"]["joined"] == 0

    live_guild([_newcomer(42)])
    _seed_newcomer_activity(fake_ctx, 42)

    body = open_client.get("/api/health/newcomer-funnel").json()
    assert body["funnel"]["joined"] == 1
    assert body["funnel"]["first_message"] == 1
    # And *this* one is worth keeping.
    assert "deep:newcomer_funnel" in _cache_keys(fake_ctx)


def test_a_good_payload_is_still_cached_normally(open_client, fake_ctx, live_guild):
    live_guild([_newcomer(42)])
    _seed_newcomer_activity(fake_ctx, 42)
    first = open_client.get("/api/health/newcomer-funnel").json()
    assert first["funnel"]["joined"] == 1
    assert "deep:newcomer_funnel" in _cache_keys(fake_ctx)

    # Cached: deleting the source rows must not change the answer.
    with fake_ctx.open_db() as conn:
        conn.execute("DELETE FROM messages")
        conn.commit()
    second = open_client.get("/api/health/newcomer-funnel").json()
    assert second["funnel"] == first["funnel"]


def test_a_cached_good_value_survives_a_degraded_window(
    open_client, fake_ctx, live_guild
):
    """The gateway going cold must not evict or overwrite a good answer."""
    live_guild([_newcomer(42)])
    _seed_newcomer_activity(fake_ctx, 42)
    good = open_client.get("/api/health/newcomer-funnel").json()
    assert good["funnel"]["joined"] == 1

    fake_ctx.bot = None  # gateway cache goes cold
    during = open_client.get("/api/health/newcomer-funnel").json()
    assert during["funnel"] == good["funnel"]


@pytest.mark.parametrize(
    "path,fragment",
    [
        ("/api/health/newcomer-funnel", "newcomer_funnel"),
        ("/api/health/cohort-retention", "cohort_retention"),
        ("/api/health/mod-workload", "mod_workload"),
        ("/api/health/mod-engagement", "mod_engagement"),
    ],
)
def test_every_guild_derived_deep_dive_skips_the_cache_while_degraded(
    open_client, fake_ctx, live_guild, path, fragment
):
    assert open_client.get(path).status_code == 200
    assert not [k for k in _cache_keys(fake_ctx) if fragment in k], path

    live_guild([_newcomer(42)])
    assert open_client.get(path).status_code == 200
    assert [k for k in _cache_keys(fake_ctx) if fragment in k], path


# ── the tiles path has the same exposure ─────────────────────────────────


@pytest.mark.parametrize(
    "tile", ["newcomer_funnel", "cohort_retention", "mod_workload"]
)
def test_tiles_do_not_cache_guild_derived_metrics_while_degraded(
    open_client, fake_ctx, live_guild, tile
):
    assert open_client.get(f"/api/health/tiles?tiles={tile}").status_code == 200
    assert tile not in _cache_keys(fake_ctx)

    live_guild([_newcomer(42)])
    assert open_client.get(f"/api/health/tiles?tiles={tile}").status_code == 200
    assert tile in _cache_keys(fake_ctx)


# ── scope: DB-only metrics are unaffected ────────────────────────────────


@pytest.mark.parametrize(
    "path,fragment",
    [
        ("/api/health/gini", "deep:gini"),
        ("/api/health/heatmap", "deep:heatmap"),
        ("/api/health/sentiment", "deep:sentiment"),
        ("/api/health/sentiment-feed", "deep:sentiment_feed"),
    ],
)
def test_db_only_metrics_still_cache_without_a_guild(
    open_client, fake_ctx, path, fragment
):
    """Over-guarding would throw away the cache for metrics that never needed
    the guild at all."""
    assert open_client.get(path).status_code == 200
    assert fragment in _cache_keys(fake_ctx)
