"""The shared SQL under the six per-game mini-game stores.

Each game's ``db.py`` used to hand-roll these; they now pass their own table
name to one implementation. What's worth testing is the SQL these build —
the callers are thin enough that a wrong statement is the only way to break
them — plus the identifier guard, which is new safety rather than a lift.
"""

from __future__ import annotations

import pytest

from bot_modules.games.utils import game_store


class FakeDb:
    """Records statements instead of running them."""

    def __init__(self, row=None):
        self.calls: list[tuple[str, tuple]] = []
        self._row = row

    async def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), tuple(params)))

    async def fetchone(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), tuple(params)))
        return self._row

    @property
    def sql(self) -> str:
        return self.calls[-1][0]

    @property
    def params(self) -> tuple:
        return self.calls[-1][1]


# ── upsert_config ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_writes_an_upsert_against_the_named_table():
    db = FakeDb()
    await game_store.upsert_config(db, "chicken_config", 7, min_players=3, timer=45)
    assert db.sql == (
        "INSERT INTO chicken_config (guild_id, min_players, timer) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (guild_id) DO UPDATE SET "
        "min_players = excluded.min_players, timer = excluded.timer"
    )
    assert db.params == (7, 3, 45)


@pytest.mark.asyncio
async def test_upsert_with_no_fields_touches_the_db_at_all():
    """"Nothing changed" is a normal answer from a settings panel."""
    db = FakeDb()
    await game_store.upsert_config(db, "chicken_config", 7)
    assert db.calls == []


# ── set_game_state ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_and_extras_go_in_one_update():
    """One statement so a game is never observed half-moved."""
    db = FakeDb()
    await game_store.set_game_state(db, "mc_games", 12, "RESOLVED", winner_id=5)
    assert db.sql == "UPDATE mc_games SET state = ?, winner_id = ? WHERE id = ?"
    assert db.params == ("RESOLVED", 5, 12)


@pytest.mark.asyncio
async def test_state_alone_still_updates():
    db = FakeDb()
    await game_store.set_game_state(db, "mc_games", 12, "ACTIVE")
    assert db.sql == "UPDATE mc_games SET state = ? WHERE id = ?"
    assert db.params == ("ACTIVE", 12)


# ── the two lookups ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pair_lookup_matches_either_direction():
    """Whoever challenged whom, it's the same live duel."""
    db = FakeDb(row={"id": 1})
    got = await game_store.fetch_live_game_for_pair(
        db, "quickdraw_games", 7, 100, 200, ("PENDING", "ACTIVE")
    )
    assert got == {"id": 1}
    assert "state IN (?,?)" in db.sql
    assert db.params == (7, "PENDING", "ACTIVE", 100, 200, 200, 100)


@pytest.mark.asyncio
async def test_pair_lookup_takes_its_states_from_the_caller():
    """Pressure cooker has an ACCEPTED step the others don't; reading that
    list wrong would let a second duel start on top of a live one."""
    db = FakeDb()
    await game_store.fetch_live_game_for_pair(
        db, "pressure_games", 7, 1, 2, ("PENDING", "ACCEPTED", "ACTIVE", "RESOLVED")
    )
    assert "state IN (?,?,?,?)" in db.sql
    assert db.params[1:5] == ("PENDING", "ACCEPTED", "ACTIVE", "RESOLVED")


@pytest.mark.asyncio
async def test_challenger_lookup_takes_the_newest_pending_one():
    db = FakeDb(row=None)
    got = await game_store.fetch_pending_game_for_challenger(
        db, "hot_potato_games", 7, 88, 100
    )
    assert got is None
    assert "state = 'PENDING'" in db.sql
    assert "ORDER BY created_at DESC LIMIT 1" in db.sql
    assert db.params == (7, 88, 100)


# ── the identifier guard ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("games; DROP TABLE users", id="statement-break"),
        pytest.param("games WHERE 1=1", id="clause-append"),
        pytest.param("", id="empty"),
        pytest.param("9lives", id="leading-digit"),
        pytest.param("a-b", id="hyphen"),
        pytest.param(None, id="not-a-string"),
    ],
)
def test_only_bare_identifiers_are_accepted(bad):
    """Table and column names can't be bound as parameters, so they're
    formatted in. Every caller passes a literal today; a shared builder is
    where that stops being self-evident."""
    with pytest.raises(ValueError):
        game_store._ident(bad)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda db: game_store.upsert_config(db, "x; DROP TABLE y", 1, a=1),
            id="upsert-table",
        ),
        pytest.param(
            lambda db: game_store.upsert_config(db, "cfg", 1, **{"a = 1; --": 2}),
            id="upsert-column",
        ),
        pytest.param(
            lambda db: game_store.set_game_state(db, "x; DROP TABLE y", 1, "S"),
            id="state-table",
        ),
        pytest.param(
            lambda db: game_store.set_game_state(db, "g", 1, "S", **{"bad col": 2}),
            id="state-column",
        ),
    ],
)
async def test_a_bad_identifier_never_reaches_the_database(call):
    db = FakeDb()
    with pytest.raises(ValueError):
        await call(db)
    assert db.calls == []
