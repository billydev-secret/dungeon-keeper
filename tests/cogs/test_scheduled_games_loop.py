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

    async def send(self, *a, **k):
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
