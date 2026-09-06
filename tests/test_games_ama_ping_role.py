"""The AMA hot-seat ping role dial, and the one-time seed behind it.

social-prompt-41 replaced a role looked up by the name "AMA" with a dashboard
dial. The dial ships unset, so every guild that already had an ``@AMA`` role
lost its hot-seat announcements silently the day that shipped. The dial is now
seeded from such a role **once** — and the "once" is the part with teeth: an
admin who clears the dial must not have it re-filled on the next launch.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bot_modules.cogs.games_ama_cog import OPT_PING_ROLE
from bot_modules.games_ama.ping_role_service import (
    PING_ROLE_KEY,
    find_seed_role_id,
    parse_role_id,
    ping_role_seed,
    resolve_ping_role_id,
)
from bot_modules.services.games_db import GamesDb

GUILD = 77
AMA_ROLE = 987654321098765432
OTHER_ROLE = 123456789012345678


def _role(name: str, role_id: int) -> SimpleNamespace:
    return SimpleNamespace(name=name, id=role_id)


def _guild(*roles: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(id=GUILD, roles=list(roles))


def test_the_cog_and_the_service_name_the_same_dial():
    """The cog declares the key (the dial-enforcement sweep reads its source);
    the service owns it. They must not drift apart."""
    assert OPT_PING_ROLE == PING_ROLE_KEY


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param("123", 123, id="stored text"),
        pytest.param(123, 123, id="int"),
        pytest.param(None, None, id="unset"),
        pytest.param("", None, id="(none)"),
        pytest.param("0", None, id="zero is (none)"),
        pytest.param("nope", None, id="garbage"),
    ],
)
def test_parse_role_id(raw, expected):
    assert parse_role_id(raw) == expected


@pytest.mark.parametrize(
    "names, expected",
    [
        pytest.param(["AMA"], AMA_ROLE, id="exact"),
        pytest.param(["ama"], AMA_ROLE, id="lowercase"),
        pytest.param([" Ama "], AMA_ROLE, id="padded"),
        pytest.param(["AMA Pings"], None, id="a longer name is not close enough"),
        pytest.param(["ama-ping"], None, id="hyphenated is not close enough"),
        pytest.param([], None, id="no roles at all"),
    ],
)
def test_find_seed_role_id(names, expected):
    roles = [_role(name, AMA_ROLE) for name in names]
    assert find_seed_role_id(roles) == expected


@pytest.mark.parametrize(
    "options, roles, expected",
    [
        pytest.param({}, [_role("AMA", AMA_ROLE)], (AMA_ROLE, True), id="never set: adopt the role"),
        pytest.param({}, [], (None, True), id="never set, no role: record the look"),
        pytest.param(
            {PING_ROLE_KEY: ""}, [_role("AMA", AMA_ROLE)], (None, False),
            id="cleared to (none): stays cleared",
        ),
        pytest.param(
            {PING_ROLE_KEY: str(OTHER_ROLE)}, [_role("AMA", AMA_ROLE)], (OTHER_ROLE, False),
            id="an admin's own pick wins over the @AMA role",
        ),
        pytest.param(
            {"questions_per_turn": 6}, [_role("AMA", AMA_ROLE)], (AMA_ROLE, True),
            id="another dial saved, this one never answered",
        ),
    ],
)
def test_ping_role_seed(options, roles, expected):
    assert ping_role_seed(options, roles) == expected


# ── the write: seeded once, and only once ────────────────────────────


async def _stored_options(db: GamesDb) -> dict | None:
    row = await db.fetchone(
        "SELECT options FROM games_game_config WHERE guild_id = ? AND game_type = 'ama'",
        (GUILD,),
    )
    return None if row is None else json.loads(row[0] or "{}")


async def test_seed_adopts_an_existing_ama_role_and_records_it(sync_db_path):
    db = GamesDb(sync_db_path)
    guild = _guild(_role("Denizen", OTHER_ROLE), _role("AMA", AMA_ROLE))

    assert await resolve_ping_role_id(db, GUILD, {}, guild) == AMA_ROLE
    assert await _stored_options(db) == {PING_ROLE_KEY: str(AMA_ROLE)}

    # Second launch: the stored dial answers, and the guild is not consulted.
    opts = await _stored_options(db)
    assert opts is not None
    assert await resolve_ping_role_id(db, GUILD, opts, _guild()) == AMA_ROLE


async def test_a_guild_with_no_ama_role_records_the_look_and_is_not_asked_again(sync_db_path):
    db = GamesDb(sync_db_path)
    assert await resolve_ping_role_id(db, GUILD, {}, _guild()) is None
    assert await _stored_options(db) == {PING_ROLE_KEY: ""}

    # The admin makes an @AMA role later: the seed does not fire a second time,
    # because "no role" was an answer.
    opts = await _stored_options(db)
    assert opts is not None
    assert await resolve_ping_role_id(db, GUILD, opts, _guild(_role("AMA", AMA_ROLE))) is None
    assert await _stored_options(db) == {PING_ROLE_KEY: ""}


async def test_a_cleared_dial_stays_cleared(sync_db_path):
    """The admin saved the panel with the picker at (none). Nothing may
    helpfully re-fill it on the next launch."""
    db = GamesDb(sync_db_path)
    await db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled, options)"
        " VALUES (?, 'ama', 1, ?)",
        (GUILD, json.dumps({PING_ROLE_KEY: "", "questions_per_turn": 6})),
    )
    opts = await _stored_options(db)
    assert opts is not None

    assert await resolve_ping_role_id(db, GUILD, opts, _guild(_role("AMA", AMA_ROLE))) is None
    assert await _stored_options(db) == {PING_ROLE_KEY: "", "questions_per_turn": 6}


async def test_the_seed_keeps_the_other_dial(sync_db_path):
    db = GamesDb(sync_db_path)
    await db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled, options)"
        " VALUES (?, 'ama', 0, ?)",
        (GUILD, json.dumps({"questions_per_turn": 6})),
    )
    opts = await _stored_options(db)
    assert opts is not None

    assert await resolve_ping_role_id(db, GUILD, opts, _guild(_role("ama", AMA_ROLE))) == AMA_ROLE
    assert await _stored_options(db) == {
        "questions_per_turn": 6, PING_ROLE_KEY: str(AMA_ROLE),
    }
    row = await db.fetchone(
        "SELECT enabled FROM games_game_config WHERE guild_id = ? AND game_type = 'ama'",
        (GUILD,),
    )
    assert row is not None and row[0] == 0, "seeding must not re-enable a game an admin switched off"


async def test_no_guild_means_no_seed_and_no_row(sync_db_path):
    """A launch with no guild in hand (a DM channel, a stubbed scheduler) reads
    the dial and writes nothing."""
    db = GamesDb(sync_db_path)
    assert await resolve_ping_role_id(db, 0, {}, None) is None
    assert await _stored_options(db) is None
