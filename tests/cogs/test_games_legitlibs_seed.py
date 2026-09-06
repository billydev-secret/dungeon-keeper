"""LegitLibs starter pack: the seed file resolves and loads (trivia-tail-82).

Three stacked bugs meant the pack had never loaded in prod: the path pointed
at ``<repo>/templates_seed.json`` (the file lives in ``bot_modules/games/``),
the rows carried string ids against an ``INTEGER PRIMARY KEY`` (every insert
died with a datatype mismatch), and a "skip if any published template exists"
guard meant a guild that had authored one template of its own never got the
pack at all. The seed is now keyed on (global, title, body) so it backfills
existing guilds exactly once and re-runs are no-ops.

Also the tag tolerance in ``_row_to_template``: the dashboard stores tags as
a comma-separated string (prod has ``'poem'``), the seed used to store a JSON
list, and the gameplay reader assumed JSON — so a tier-4 pick crashed on the
one prod template that had a tag.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from bot_modules.cogs.games_legitlibs.data import (
    SEED_PATH,
    _row_to_template,
    pick_template,
    seed_templates_from_file,
)
from bot_modules.services.games_db import GamesDb

GUILD = 111


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


async def _count(db, where: str = "1=1") -> int:
    row = await db.fetchone(f"SELECT COUNT(*) AS n FROM legitlibs_templates WHERE {where}")
    assert row is not None
    return row["n"]


def test_seed_path_resolves_to_the_shipped_pack():
    assert Path(SEED_PATH).is_file(), SEED_PATH
    pack = json.loads(Path(SEED_PATH).read_text(encoding="utf-8"))
    assert pack, "the starter pack is empty"
    for t in pack:
        assert t["title"] and t["body"] and t["blanks"]
        assert 1 <= t["tier"] <= 4


async def test_seed_loads_the_pack_into_the_global_pool(db):
    added = await seed_templates_from_file(db, SEED_PATH, author_id=0)
    pack = json.loads(Path(SEED_PATH).read_text(encoding="utf-8"))
    assert added == len(pack)
    assert await _count(db, "guild_id = 0 AND status = 'published'") == len(pack)
    # Every seeded row is drawable by any guild.
    assert await pick_template(db, GUILD, tier=1) is not None


async def test_seed_backfills_a_guild_that_already_has_templates_once(db):
    # A guild authored its own template first — the old guard skipped the
    # pack for good because "a published template exists".
    await db.execute(
        "INSERT INTO legitlibs_templates (title, body, tier, tags, status, blanks, guild_id) "
        "VALUES ('Mine', 'a {b1} b', 1, '', 'published', "
        "'[{\"id\":\"b1\",\"pos\":\"noun\"}]', ?)",
        (GUILD,),
    )
    pack_size = len(json.loads(Path(SEED_PATH).read_text(encoding="utf-8")))

    assert await seed_templates_from_file(db, SEED_PATH, author_id=0) == pack_size
    assert await _count(db) == pack_size + 1

    # Second boot: nothing new.
    assert await seed_templates_from_file(db, SEED_PATH, author_id=0) == 0
    assert await _count(db) == pack_size + 1


async def test_seed_missing_file_is_a_noop(db, tmp_path):
    assert await seed_templates_from_file(db, str(tmp_path / "nope.json"), author_id=0) == 0
    assert await _count(db) == 0


@pytest.mark.parametrize(
    "stored,expected",
    [
        pytest.param("", [], id="empty"),
        pytest.param(None, [], id="null"),
        pytest.param("poem", ["poem"], id="bare-word-from-dashboard"),
        pytest.param("dating, romantic", ["dating", "romantic"], id="comma-list"),
        pytest.param('["dating", "romantic"]', ["dating", "romantic"], id="json-list"),
    ],
)
def test_row_to_template_reads_dashboard_and_json_tags(stored, expected):
    row = {
        "template_id": 1, "title": "T", "body": "a {b1} b", "tier": 1,
        "tags": stored, "status": "published", "player_min": 2, "player_max": 4,
        "blanks": '[{"id":"b1","pos":"noun"}]', "author_id": 0, "notes": "",
        "use_count": 0,
    }
    assert _row_to_template(row)["tags"] == expected
