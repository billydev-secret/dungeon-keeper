"""Scheduler loop (_process_due) branch behavior, over a real schema + GamesDb."""

from bot_modules.games.utils.game_manager import create_game
from bot_modules.services.games_db import GamesDb
from bot_modules.services import scheduled_games_service as svc

NOW = 1_000_000.0
GUILD = 9001
CHAN = 4242


class _Msg:
    id = 777
    jump_url = "http://x"


class _Chan:
    def __init__(self, cid):
        self.id = cid
        self.name = "games"
        self.guild = None
        self.sends = []

    async def send(self, *a, **k):
        self.sends.append(a[0] if a else k.get("content"))
        return _Msg()


class _Bot:
    def __init__(self, games_db, channels, launchers):
        self.games_db = games_db
        self._channels = channels
        self.game_launchers = launchers

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        if cid in self._channels:
            return self._channels[cid]
        raise RuntimeError("not found")

    def get_guild(self, gid):
        return None


def _make_bot(games_db, launched, *, has_channel=True):
    async def fake_launch(*, channel, host_id, host_name, guild_id, options):
        launched.append({"channel": channel.id, "options": options})
        return "fake-gid"

    channels = {CHAN: _Chan(CHAN)} if has_channel else {}
    return _Bot(games_db, channels, {"wyr": fake_launch})


async def _insert(games_db, **over):
    cols = dict(
        guild_id=GUILD, channel_id=CHAN, game_type="wyr", options="{}",
        created_by=2001, created_at=NOW, time_of_day=1200, recurrence="daily",
        recur_days=None, start_date=None, next_run_at=NOW, giveup_at=None,
        announce=0, announce_role_id=None, status="active",
        last_run_at=None, last_status=None,
    )
    cols.update(over)
    keys = ", ".join(cols)
    ph = ", ".join("?" for _ in cols)
    await games_db.execute(
        f"INSERT INTO games_scheduled ({keys}) VALUES ({ph})", tuple(cols.values())
    )
    return await games_db.fetchone(
        "SELECT * FROM games_scheduled ORDER BY id DESC LIMIT 1"
    )


async def _row(games_db, sched_id):
    return await games_db.fetchone("SELECT * FROM games_scheduled WHERE id = ?", (sched_id,))


# ── tests ─────────────────────────────────────────────────────────────────────

async def test_recurring_free_channel_launches_and_advances(sync_db_path):
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched)
    row = await _insert(db, recurrence="daily")

    await svc._process_due(bot, db, row, NOW)

    assert len(launched) == 1 and launched[0]["channel"] == CHAN
    after = await _row(db, row["id"])
    assert after["last_status"] == "launched"
    assert after["status"] == "active"
    assert after["next_run_at"] > NOW  # advanced to a future slot


async def test_photo_still_fires_after_leaving_schedulable(sync_db_path):
    # 'photo' was pulled from SCHEDULABLE_GAME_TYPES (it's the standalone Photo
    # Challenge feature now), but its rows — created via /api/photo-challenge —
    # must still run on this shared, game-type-agnostic loop. Guards this exact
    # reuse contract against a future "clean up the constant" regression.
    db = GamesDb(sync_db_path)
    launched = []

    async def fake_launch(*, channel, host_id, host_name, guild_id, options):
        launched.append({"channel": channel.id, "game": "photo"})
        return "photo-gid"

    bot = _Bot(db, {CHAN: _Chan(CHAN)}, {"photo": fake_launch})
    row = await _insert(db, game_type="photo", recurrence="daily")

    await svc._process_due(bot, db, row, NOW)

    assert len(launched) == 1 and launched[0]["channel"] == CHAN
    after = await _row(db, row["id"])
    assert after["last_status"] == "launched"
    assert after["next_run_at"] > NOW


async def test_recurring_busy_channel_skips_and_advances(sync_db_path):
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched)
    await create_game(db, CHAN, 1, "wyr")  # channel now busy
    row = await _insert(db, recurrence="daily")

    await svc._process_due(bot, db, row, NOW)

    assert launched == []  # not launched
    after = await _row(db, row["id"])
    assert after["last_status"] == "skipped_active"
    assert after["next_run_at"] > NOW


async def test_busy_check_skips_without_announcing(sync_db_path):
    # Games that track rounds in-memory (e.g. risky_roll) expose a busy-check so the
    # scheduler skips a busy channel instead of pinging "starting now!" then failing
    # to launch a duplicate. The existing in-progress game is left to ride.
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched)

    async def busy_check(channel_id):
        return channel_id == CHAN

    bot.game_busy_checks = {"wyr": busy_check}
    row = await _insert(db, recurrence="daily", announce=1, announce_role_id=555)

    await svc._process_due(bot, db, row, NOW)

    assert launched == []                       # game not started
    assert bot._channels[CHAN].sends == []      # no "starting now!" ping
    after = await _row(db, row["id"])
    assert after["last_status"] == "skipped_active"
    assert after["next_run_at"] > NOW           # advanced to next slot; round rides


async def test_once_busy_before_giveup_stays_due(sync_db_path):
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched)
    await create_game(db, CHAN, 1, "wyr")
    row = await _insert(db, recurrence="once", start_date="2030-01-01",
                        next_run_at=NOW, giveup_at=NOW + 3600)

    await svc._process_due(bot, db, row, NOW)

    assert launched == []
    after = await _row(db, row["id"])
    assert after["status"] == "active"            # still active → retried next poll
    assert after["next_run_at"] == NOW            # unchanged (still due)
    assert after["last_status"] == "skipped_active"


async def test_once_busy_after_giveup_marks_done(sync_db_path):
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched)
    await create_game(db, CHAN, 1, "wyr")
    row = await _insert(db, recurrence="once", start_date="2030-01-01",
                        next_run_at=NOW, giveup_at=NOW - 10)

    await svc._process_due(bot, db, row, NOW)

    assert launched == []
    after = await _row(db, row["id"])
    assert after["status"] == "done"
    assert after["last_status"] == "skipped_giveup"


async def test_disabled_game_skips(sync_db_path):
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched)
    await db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled) VALUES (?, ?, 0)",
        (GUILD, "wyr"),
    )
    row = await _insert(db, recurrence="daily")

    await svc._process_due(bot, db, row, NOW)

    assert launched == []
    after = await _row(db, row["id"])
    assert after["last_status"] == "skipped_disabled"
    assert after["next_run_at"] > NOW


async def test_unreachable_channel_errors(sync_db_path):
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched, has_channel=False)
    row = await _insert(db, recurrence="daily")

    await svc._process_due(bot, db, row, NOW)

    assert launched == []
    after = await _row(db, row["id"])
    assert after["last_status"] == "error"


async def test_launcher_returning_none_records_error(sync_db_path):
    # Launchers return None on failure (e.g. missing perms) without raising —
    # the loop must record 'error', not 'launched'.
    db = GamesDb(sync_db_path)

    async def none_launch(*, channel, host_id, host_name, guild_id, options):
        return None

    bot = _Bot(db, {CHAN: _Chan(CHAN)}, {"wyr": none_launch})
    row = await _insert(db, recurrence="daily")

    await svc._process_due(bot, db, row, NOW)

    after = await _row(db, row["id"])
    assert after["last_status"] == "error"
    assert after["next_run_at"] > NOW


async def test_once_past_giveup_free_channel_marks_missed(sync_db_path):
    # Bot was offline through the slot: even with a free channel the loop must
    # NOT ping "starting now!" for a one-time game whose giveup_at deadline has
    # passed — it marks the row missed instead of firing hours late.
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched)
    row = await _insert(db, recurrence="once", start_date="2030-01-01",
                        next_run_at=NOW - svc.GIVEUP_GRACE_SECONDS - 100,
                        giveup_at=NOW - 100)

    await svc._process_due(bot, db, row, NOW)

    assert launched == []                       # not fired late
    assert bot._channels[CHAN].sends == []      # no "starting now!" ping
    after = await _row(db, row["id"])
    assert after["status"] == "done"
    assert after["last_status"] == "skipped_giveup"


async def test_recurring_stale_slot_skips_and_advances(sync_db_path):
    # A daily slot more than the grace window old (bot offline overnight) is
    # stale: skip it and roll to the next slot rather than launching late.
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched)
    row = await _insert(db, recurrence="daily",
                        next_run_at=NOW - svc.GIVEUP_GRACE_SECONDS - 100)

    await svc._process_due(bot, db, row, NOW)

    assert launched == []                       # not fired late
    assert bot._channels[CHAN].sends == []
    after = await _row(db, row["id"])
    assert after["status"] == "active"          # still recurring
    assert after["last_status"] == "skipped_late"
    assert after["next_run_at"] > NOW           # advanced to a future slot


async def test_recurring_slightly_late_still_launches(sync_db_path):
    # Within the grace window a recurring slot fires normally (a brief restart
    # right after the slot shouldn't drop the game).
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched)
    row = await _insert(db, recurrence="daily", next_run_at=NOW - 60)

    await svc._process_due(bot, db, row, NOW)

    assert len(launched) == 1
    after = await _row(db, row["id"])
    assert after["last_status"] == "launched"


async def test_once_free_channel_launches_and_done(sync_db_path):
    db = GamesDb(sync_db_path)
    launched = []
    bot = _make_bot(db, launched)
    row = await _insert(db, recurrence="once", start_date="2030-01-01", next_run_at=NOW,
                        giveup_at=NOW + 3600)

    await svc._process_due(bot, db, row, NOW)

    assert len(launched) == 1
    after = await _row(db, row["id"])
    assert after["status"] == "done"
    assert after["last_status"] == "launched"
