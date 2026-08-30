from __future__ import annotations

import logging
from types import SimpleNamespace

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.auto_delete_service import (
    MAX_DELETE_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
    auto_delete_rule_exists,
    delete_tracked_messages_older_than,
    format_duration_seconds,
    init_auto_delete_tables,
    list_auto_delete_rules_for_guild,
    pop_due_auto_delete_message_ids,
    remove_auto_delete_rule,
    remove_tracked_auto_delete_message,
    remove_tracked_auto_delete_messages,
    should_track_auto_delete_message,
    touch_auto_delete_rule_run,
    track_auto_delete_message,
    upsert_auto_delete_rule,
)


# ── format_duration_seconds ───────────────────────────────────────────

def test_format_zero():
    assert format_duration_seconds(0) == "0s"


@pytest.mark.parametrize("secs,expected", [
    (60, "1 minute"), (120, "2 minutes"),
    (3600, "1 hour"), (7200, "2 hours"),
    (86400, "1 day"), (172800, "2 days"),
])
def test_singular_and_plural(secs, expected):
    assert format_duration_seconds(secs) == expected


@pytest.mark.parametrize("secs", (90, 3661))
def test_non_round_falls_back_to_seconds(secs):
    assert "seconds" in format_duration_seconds(secs)


# ── auto_delete DB tests ──────────────────────────────────────────────

@pytest.fixture
def ad_db(tmp_path):
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)
    return db_path


def test_upsert_creates_and_lists_rules(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    upsert_auto_delete_rule(ad_db, 1, 200, 43200, 7200)
    rules = list_auto_delete_rules_for_guild(ad_db, 1)
    assert len(rules) == 2
    assert int(rules[0]["channel_id"]) == 100
    assert int(rules[1]["channel_id"]) == 200


def test_upsert_updates_existing_rule(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    upsert_auto_delete_rule(ad_db, 1, 100, 43200, 7200)
    rules = list_auto_delete_rules_for_guild(ad_db, 1)
    assert len(rules) == 1
    assert int(rules[0]["max_age_seconds"]) == 43200
    assert int(rules[0]["interval_seconds"]) == 7200


def test_rules_are_scoped_by_guild(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    upsert_auto_delete_rule(ad_db, 2, 100, 86400, 3600)
    assert len(list_auto_delete_rules_for_guild(ad_db, 1)) == 1
    assert len(list_auto_delete_rules_for_guild(ad_db, 2)) == 1
    assert len(list_auto_delete_rules_for_guild(ad_db, 3)) == 0


def test_remove_rule_returns_true_when_found(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    assert remove_auto_delete_rule(ad_db, 1, 100) is True
    assert list_auto_delete_rules_for_guild(ad_db, 1) == []


def test_remove_rule_returns_false_when_missing(ad_db):
    assert remove_auto_delete_rule(ad_db, 1, 999) is False


def test_auto_delete_rule_exists(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    with open_db(ad_db) as conn:
        assert auto_delete_rule_exists(conn, 1, 100) is True
        assert auto_delete_rule_exists(conn, 1, 999) is False
        assert auto_delete_rule_exists(conn, 2, 100) is False


def test_touch_rule_updates_last_run_ts(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600, last_run_ts=0.0)
    touch_auto_delete_rule_run(ad_db, 1, 100, 9999.0)
    rules = list_auto_delete_rules_for_guild(ad_db, 1)
    assert abs(float(rules[0]["last_run_ts"]) - 9999.0) < 0.001


def test_track_and_pop_due_messages_respects_cutoff(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        track_auto_delete_message(conn, 1, 100, 1002, 200.0)
        track_auto_delete_message(conn, 1, 100, 1003, 300.0)
        due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=250.0)
    assert [mid for mid, _ in due] == [1001, 1002]


def test_track_message_is_idempotent(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0)
    assert [mid for mid, _ in due] == [1001]


def test_remove_tracked_message(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    remove_tracked_auto_delete_message(ad_db, 1, 100, 1001)
    with open_db(ad_db) as conn:
        assert pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0) == []


def test_remove_tracked_messages_bulk(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        track_auto_delete_message(conn, 1, 100, 1002, 200.0)
        track_auto_delete_message(conn, 1, 100, 1003, 300.0)
    remove_tracked_auto_delete_messages(ad_db, 1, 100, {1001, 1002})
    with open_db(ad_db) as conn:
        due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0)
    assert [mid for mid, _ in due] == [1003]


def test_remove_tracked_messages_bulk_empty_set_is_noop(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    remove_tracked_auto_delete_messages(ad_db, 1, 100, set())
    with open_db(ad_db) as conn:
        due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0)
    assert [mid for mid, _ in due] == [1001]


def test_remove_rule_also_clears_tracked_messages(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    remove_auto_delete_rule(ad_db, 1, 100)
    with open_db(ad_db) as conn:
        assert pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0) == []


# ── media_only mode ───────────────────────────────────────────────────

def test_new_rule_defaults_to_not_media_only(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    rules = list_auto_delete_rules_for_guild(ad_db, 1)
    assert bool(rules[0]["media_only"]) is False


def test_upsert_persists_media_only(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600, media_only=True)
    rules = list_auto_delete_rules_for_guild(ad_db, 1)
    assert bool(rules[0]["media_only"]) is True


def test_should_track_false_when_no_rule(ad_db):
    with open_db(ad_db) as conn:
        assert should_track_auto_delete_message(conn, 1, 100, has_media=True) is False
        assert should_track_auto_delete_message(conn, 1, 100, has_media=False) is False


def test_should_track_regular_rule_queues_everything(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600, media_only=False)
    with open_db(ad_db) as conn:
        assert should_track_auto_delete_message(conn, 1, 100, has_media=True) is True
        assert should_track_auto_delete_message(conn, 1, 100, has_media=False) is True


def test_should_track_media_only_rule_queues_only_media(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600, media_only=True)
    with open_db(ad_db) as conn:
        assert should_track_auto_delete_message(conn, 1, 100, has_media=True) is True
        assert should_track_auto_delete_message(conn, 1, 100, has_media=False) is False


def test_toggling_media_only_clears_tracked_queue(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600, media_only=False)
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    # Flip the mode: stale queue (built under "all") must be dropped so the
    # sweep can't delete a text message the media_only rule promises to keep.
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600, media_only=True)
    with open_db(ad_db) as conn:
        assert pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0) == []


def test_editing_age_without_mode_change_keeps_queue(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600, media_only=True)
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    # Same mode, different age/interval — the queue must survive (a Save posts
    # every field, so unconditional clearing would wipe the queue each edit).
    upsert_auto_delete_rule(ad_db, 1, 100, 43200, 7200, media_only=True)
    with open_db(ad_db) as conn:
        due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0)
    assert [mid for mid, _ in due] == [1001]


def test_media_only_column_migrated_onto_legacy_table(tmp_path):
    db_path = tmp_path / "legacy.db"
    with open_db(db_path) as conn:
        # Simulate a pre-media_only schema (no media_only column).
        conn.execute(
            """
            CREATE TABLE auto_delete_rules (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                max_age_seconds INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL,
                last_run_ts REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, channel_id)
            )
            """
        )
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)
    upsert_auto_delete_rule(db_path, 1, 100, 86400, 3600, media_only=True)
    rules = list_auto_delete_rules_for_guild(db_path, 1)
    assert bool(rules[0]["media_only"]) is True


# ── deletion attribution ──────────────────────────────────────────────
#
# The sweep claims its message ids *before* calling the Discord API. That order
# is the whole mechanism: the gateway delete event that follows names no actor,
# and marking is first-writer-wins, so a claim made afterwards would race and
# lose an arbitrary subset of the sweep to the generic ``discord`` source.


@pytest.fixture
def archive_db(tmp_path):
    """A db with both the auto-delete tables and a small message archive."""
    from bot_modules.services.message_store import init_message_tables, store_message

    db_path = tmp_path / "archive.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)
        init_message_tables(conn)
        for mid in (1, 2):
            store_message(
                conn,
                message_id=mid,
                guild_id=1,
                channel_id=100,
                author_id=50,
                content="swept away",
                reply_to_id=None,
                ts=1_000,
                attachment_urls=[],
                mention_ids=[],
            )
    return db_path


def _flags(db_path):
    with open_db(db_path) as conn:
        return {
            r["message_id"]: (r["deleted_at"], r["deleted_source"])
            for r in conn.execute(
                "SELECT message_id, deleted_at, deleted_source FROM messages"
            )
        }


def test_claim_attributes_the_sweep_and_survives_the_gateway_event(archive_db):
    from bot_modules.services.auto_delete_service import _claim_deleted
    from bot_modules.services.message_store import (
        DELETE_SOURCE_AUTO_DELETE,
        DELETE_SOURCE_DISCORD,
        mark_messages_deleted,
    )

    _claim_deleted(archive_db, 1, {1, 2})
    # The gateway event lands moments later carrying no actor.
    with open_db(archive_db) as conn:
        mark_messages_deleted(conn, 1, {1, 2}, DELETE_SOURCE_DISCORD, 99_999)

    flags = _flags(archive_db)
    assert [s for _ts, s in flags.values()] == [DELETE_SOURCE_AUTO_DELETE] * 2


def test_release_rolls_back_a_delete_discord_refused(archive_db):
    from bot_modules.services.auto_delete_service import _claim_deleted, _release_deleted

    _claim_deleted(archive_db, 1, {1, 2})
    _release_deleted(archive_db, 1, {1})

    flags = _flags(archive_db)
    assert flags[1] == (None, None), "a message still on Discord must not read as deleted"
    assert flags[2][1] == "auto_delete"


def test_claim_never_aborts_a_sweep(tmp_path):
    """Bookkeeping failure must not stop messages from being deleted."""
    from bot_modules.services.auto_delete_service import _claim_deleted, _release_deleted

    missing = tmp_path / "nonexistent" / "no.db"
    _claim_deleted(missing, 1, {1})  # must not raise
    _release_deleted(missing, 1, {1})


# ── bounded retry on a failed delete ──────────────────────────────────
#
# A transient HTTP error used to untrack the messages it failed on ("avoid
# infinite retry"), which made them permanent orphans: the sweep is
# queue-driven, and the bounded startup scan can't see a message older than
# ``last_run_ts - max_age``. Three messages were lost this way in
# #flash-channel on 2026-08-13. Failures now cost an attempt and a backoff
# instead of the queue row.


OPERATOR_ID = 424242


@pytest.fixture
def operator_dm(monkeypatch):
    """The give-up DM goes to SUPPORT_USER_ID — the same operator the watchdog pages."""
    monkeypatch.setenv("SUPPORT_USER_ID", str(OPERATOR_ID))


class _StubUser:
    def __init__(self, user_id: int, sent: list[tuple[int, str]]):
        self.id = user_id
        self._sent = sent

    async def send(self, content: str):
        self._sent.append((self.id, content))


class _StubBot:
    def __init__(self):
        self.dms: list[tuple[int, str]] = []

    def get_user(self, user_id: int):
        return _StubUser(user_id, self.dms)


class _StubResponse:
    def __init__(self, status: int):
        self.status = status
        self.reason = "stub"


def _http_error(status: int = 500, code: int = 0) -> discord.HTTPException:
    return discord.HTTPException(_StubResponse(status), {"code": code, "message": "boom"})


class _StubPartial:
    def __init__(self, message_id: int, channel: _SweepChannel):
        self.id = message_id
        self._channel = channel

    async def delete(self, *, reason: str | None = None):
        del reason
        self._channel.individual_attempts.append(self.id)
        exc = self._channel.raise_on_individual
        if exc is not None:
            raise exc
        self._channel.deleted.append(self.id)


class _PinsIterator:
    """What discord.py 2.6+ hands back from ``channel.pins()``."""

    def __init__(self, channel: "_SweepChannel"):
        self._channel = channel
        self._ids = iter(sorted(channel.pinned))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._channel.raise_on_pins is not None:
            raise self._channel.raise_on_pins
        try:
            return SimpleNamespace(id=next(self._ids))
        except StopIteration:
            raise StopAsyncIteration from None


class _SweepChannel:
    """Minimal channel for driving delete_tracked_messages_older_than."""

    def __init__(self, *, raise_on_bulk=None, raise_on_individual=None,
                 pinned=(), raise_on_pins=None):
        self.id = 100
        self.name = "flash-channel"
        self.raise_on_bulk = raise_on_bulk
        self.raise_on_individual = raise_on_individual
        self.pinned: set[int] = set(pinned)
        self.raise_on_pins = raise_on_pins
        self.pins_calls = 0
        self.bulk_attempts: list[list[int]] = []
        self.individual_attempts: list[int] = []
        self.deleted: list[int] = []

    def pins(self, **kwargs):
        del kwargs
        self.pins_calls += 1
        return _PinsIterator(self)

    def get_partial_message(self, message_id: int):
        return _StubPartial(message_id, self)

    async def delete_messages(self, partials, *, reason: str):
        del reason
        ids = [p.id for p in partials]
        self.bulk_attempts.append(ids)
        if self.raise_on_bulk is not None:
            raise self.raise_on_bulk
        self.deleted.extend(ids)


def _queue_state(db_path, message_id: int = 1001):
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT attempts, next_attempt_ts FROM auto_delete_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()


async def _sweep(db_path, channel, *, now_ts: float, bot=None):
    return await delete_tracked_messages_older_than(
        db_path,
        1,
        channel,  # type: ignore[arg-type]
        cutoff_ts=now_ts,
        reason="test",
        now_ts=now_ts,
        bot=bot,
    )


@pytest.mark.asyncio
async def test_http_failure_keeps_message_tracked_for_retry(ad_db):
    """The regression: a transient bulk-delete error must not orphan the message."""
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    channel = _SweepChannel(raise_on_bulk=_http_error(500))

    queued, deleted, failed = await _sweep(ad_db, channel, now_ts=1_000.0)

    assert (queued, deleted, failed) == (1, 0, 1)
    row = _queue_state(ad_db)
    assert row is not None, "a failed delete must leave the message in the queue"
    assert row["attempts"] == 1
    assert row["next_attempt_ts"] == pytest.approx(1_000.0 + 60)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "still_tracked", "counted_failed"),
    [
        pytest.param(_http_error(500), True, 1, id="server-error-retries"),
        pytest.param(_http_error(429), True, 1, id="rate-limited-retries"),
        pytest.param(_http_error(400, code=50034), True, 1, id="bad-request-retries"),
        pytest.param(
            discord.NotFound(_StubResponse(404), {"code": 10008}), False, 0,
            id="lone-not-found-drops-clean",
        ),
        pytest.param(
            discord.Forbidden(_StubResponse(403), {"code": 50013}), True, 1,
            id="forbidden-keeps-and-aborts",
        ),
    ],
)
async def test_bulk_error_variants(ad_db, error, still_tracked, counted_failed):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    channel = _SweepChannel(raise_on_bulk=error)

    _queued, _deleted, failed = await _sweep(ad_db, channel, now_ts=1_000.0)

    assert failed == counted_failed
    assert (_queue_state(ad_db) is not None) is still_tracked


@pytest.mark.asyncio
async def test_bulk_not_found_retries_the_chunk_one_at_a_time(ad_db):
    """A 404 on a chunk doesn't say which id is stale, so the survivors get
    tried individually rather than being dropped alongside the dead one."""
    with open_db(ad_db) as conn:
        for mid in (1001, 1002):
            track_auto_delete_message(conn, 1, 100, mid, 100.0)
    channel = _SweepChannel(
        raise_on_bulk=discord.NotFound(_StubResponse(404), {"code": 10008})
    )

    await _sweep(ad_db, channel, now_ts=1_000.0)

    assert sorted(channel.individual_attempts) == [1001, 1002]
    assert _queue_state(ad_db, 1001) is None and _queue_state(ad_db, 1002) is None


@pytest.mark.asyncio
async def test_forbidden_does_not_consume_the_retry_budget(ad_db):
    """A permission gap is channel-wide, not a verdict on this message."""
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    channel = _SweepChannel(raise_on_bulk=discord.Forbidden(_StubResponse(403), {}))

    await _sweep(ad_db, channel, now_ts=1_000.0)

    row = _queue_state(ad_db)
    assert row["attempts"] == 0
    assert row["next_attempt_ts"] == 0


@pytest.mark.asyncio
async def test_backoff_defers_the_retry_and_then_succeeds(ad_db):
    """Backed-off messages are invisible until due, then delete normally."""
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    failing = _SweepChannel(raise_on_bulk=_http_error(500))
    await _sweep(ad_db, failing, now_ts=1_000.0)

    # 30s later the message is still parked — not even counted as due.
    early = _SweepChannel()
    assert await _sweep(ad_db, early, now_ts=1_030.0) == (0, 0, 0)
    assert early.bulk_attempts == []

    # Past the 60s backoff it sweeps normally and leaves the queue.
    late = _SweepChannel()
    assert await _sweep(ad_db, late, now_ts=1_061.0) == (1, 1, 0)
    assert late.deleted == [1001]
    assert _queue_state(ad_db) is None


@pytest.mark.asyncio
async def test_retry_budget_exhausts_loudly_and_stops_retrying(ad_db, caplog, operator_dm):
    """Five failures, then the message is abandoned: logged with a traceback,
    DM'd once, and never retried again — but the row stays as evidence."""
    caplog.set_level(logging.ERROR, logger="dungeonkeeper.auto_delete")
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)

    bot = _StubBot()
    now = 1_000.0
    for expected_attempt, backoff in enumerate(RETRY_BACKOFF_SECONDS, start=1):
        channel = _SweepChannel(raise_on_bulk=_http_error(500))
        await _sweep(ad_db, channel, now_ts=now, bot=bot)
        assert channel.bulk_attempts == [[1001]], "the retry has to actually happen"
        assert _queue_state(ad_db)["attempts"] == expected_attempt
        now += backoff + 1

    # One try per backoff step, plus the first: the last failure spends the budget.
    final = _SweepChannel(raise_on_bulk=_http_error(500))
    await _sweep(ad_db, final, now_ts=now, bot=bot)
    assert _queue_state(ad_db)["attempts"] == MAX_DELETE_ATTEMPTS

    # Sixth pass: the message is out of budget, so it isn't even due.
    quiet = _SweepChannel(raise_on_bulk=_http_error(500))
    assert await _sweep(ad_db, quiet, now_ts=now, bot=bot) == (0, 0, 0)
    assert quiet.bulk_attempts == []

    # Loud exactly once, with the Discord status/code and a traceback.
    give_ups = [r for r in caplog.records if "gave up" in r.getMessage()]
    assert len(give_ups) == 1
    assert give_ups[0].levelname == "ERROR"
    assert give_ups[0].exc_info is not None
    assert "500" in give_ups[0].getMessage()

    assert len(bot.dms) == 1, "one DM per abandoned message, not per attempt"
    recipient, text = bot.dms[0]
    assert recipient == OPERATOR_ID
    assert "1001" in text and "flash-channel" in text


@pytest.mark.asyncio
async def test_failed_chunk_does_not_block_the_rest_of_the_queue(ad_db):
    """The old untrack existed to stop a failing chunk stalling the drain loop;
    the backoff has to buy the same protection without dropping anything."""
    with open_db(ad_db) as conn:
        for mid in (1001, 1002):
            track_auto_delete_message(conn, 1, 100, mid, 100.0)

    # First sweep fails on everything and parks both.
    await _sweep(ad_db, _SweepChannel(raise_on_bulk=_http_error(500)), now_ts=1_000.0)
    # A later sweep drains them; the loop must terminate, not spin.
    ok = _SweepChannel()
    queued, deleted, failed = await _sweep(ad_db, ok, now_ts=1_100.0)

    assert (queued, deleted, failed) == (2, 2, 0)
    assert sorted(ok.deleted) == [1001, 1002]


@pytest.mark.asyncio
async def test_individual_delete_failure_also_retries(ad_db):
    """Messages past the bulk window take the one-at-a-time path; same rule."""
    old_ts = 1_000.0 - (14 * 86400)
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, old_ts)
    channel = _SweepChannel(raise_on_individual=_http_error(500))

    await _sweep(ad_db, channel, now_ts=1_000.0)

    assert channel.individual_attempts == [1001]
    assert channel.bulk_attempts == []
    row = _queue_state(ad_db)
    assert row is not None and row["attempts"] == 1


@pytest.mark.asyncio
async def test_individual_not_found_still_drops_clean(ad_db):
    old_ts = 1_000.0 - (14 * 86400)
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, old_ts)
    channel = _SweepChannel(
        raise_on_individual=discord.NotFound(_StubResponse(404), {"code": 10008})
    )

    await _sweep(ad_db, channel, now_ts=1_000.0)

    assert _queue_state(ad_db) is None


def test_retry_columns_migrated_onto_legacy_table(tmp_path):
    """Existing queues predate the retry columns and must default to due-now."""
    db_path = tmp_path / "legacy.db"
    with open_db(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE auto_delete_messages (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (guild_id, channel_id, message_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO auto_delete_messages VALUES (1, 100, 1001, 100.0)"
        )
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)
        due = pop_due_auto_delete_message_ids(
            conn, 1, 100, cutoff_ts=9999.0, now_ts=9999.0
        )
    assert [mid for mid, _ in due] == [1001]


# ── pinned messages are exempt ───────────────────────────────────────────
#
# The queue path used to delete by message id without ever asking whether the
# message was pinned, while the history scan skipped pinned messages. That
# split is what deleted a paid Flash Theme card out of #🔥│flash-channel an
# hour into its 24-hour window (2026-08-29): the channel carries a one-hour
# auto-delete rule, and the card was tracked at post time like any other bot
# message.


@pytest.mark.asyncio
async def test_pinned_message_survives_the_sweep_and_stays_tracked(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        track_auto_delete_message(conn, 1, 100, 1002, 100.0)
    channel = _SweepChannel(pinned={1001})

    await _sweep(ad_db, channel, now_ts=1_000.0)

    assert channel.deleted == [1002]
    # Kept, not dropped: unpinning it must hand it back to the sweep.
    with open_db(ad_db) as conn:
        left = [
            r["message_id"]
            for r in conn.execute("SELECT message_id FROM auto_delete_messages")
        ]
    assert left == [1001]


@pytest.mark.asyncio
async def test_unpinning_hands_the_message_back_to_the_sweep(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    channel = _SweepChannel(pinned={1001})

    await _sweep(ad_db, channel, now_ts=1_000.0)
    assert channel.deleted == []

    channel.pinned.clear()
    await _sweep(ad_db, channel, now_ts=1_000.0)
    assert channel.deleted == [1001]


@pytest.mark.asyncio
async def test_unreadable_pins_skip_the_pass_rather_than_delete_blind(ad_db):
    """Failing closed is the point — the alternative deletes something paid for."""
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    channel = _SweepChannel(raise_on_pins=_http_error(500))

    queued, deleted, failed = await _sweep(ad_db, channel, now_ts=1_000.0)

    assert (queued, deleted, failed) == (0, 0, 0)
    assert channel.deleted == []
    # No attempt charged: the next tick retries at full budget.
    row = _queue_state(ad_db)
    assert row is not None and row["attempts"] == 0


@pytest.mark.asyncio
async def test_pins_are_read_once_per_sweep_not_once_per_message(ad_db):
    with open_db(ad_db) as conn:
        for mid in range(1001, 1006):
            track_auto_delete_message(conn, 1, 100, mid, 100.0)
    channel = _SweepChannel()

    await _sweep(ad_db, channel, now_ts=1_000.0)

    assert channel.pins_calls == 1
    assert len(channel.deleted) == 5
