"""LegitLibs template guild-scoping in the gameplay selector (migration 124).

Templates are per-guild (guild_id > 0) with an opt-in global pool (guild_id 0).
``pick_template`` must draw a guild's own templates plus the global ones, and
NEVER another guild's — the safety gate for the per-guild model.
"""

from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from bot_modules.cogs.games_legitlibs.data import pick_template
from bot_modules.services.games_db import GamesDb

GUILD_A = 111
GUILD_B = 222


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


async def _add(db, template_id, guild_id, *, tier=1, status="published", title="T"):
    await db.execute(
        "INSERT INTO legitlibs_templates "
        "(template_id, title, body, tier, tags, status, blanks, guild_id) "
        "VALUES (?, ?, ?, ?, '[]', ?, '[]', ?)",
        (template_id, title, "a {1} b", tier, status, guild_id),
    )


async def test_pick_draws_own_and_global_never_another_guild(db):
    await _add(db, 1, GUILD_A, title="A-owned")
    await _add(db, 2, 0, title="global")
    await _add(db, 3, GUILD_B, title="B-owned")

    seen = set()
    for _ in range(40):
        t = await pick_template(db, GUILD_A, tier=5)
        assert t is not None
        seen.add(t["template_id"])

    assert seen <= {1, 2}, "guild A drew a template it shouldn't see"
    assert 3 not in seen, "guild A drew guild B's private template"


async def test_pick_returns_none_when_only_another_guilds_templates_exist(db):
    await _add(db, 1, GUILD_B, title="B-only")
    assert await pick_template(db, GUILD_A, tier=5) is None


async def test_global_template_is_drawn_by_every_guild(db):
    await _add(db, 1, 0, title="shared")
    assert (await pick_template(db, GUILD_A, tier=5))["template_id"] == 1
    assert (await pick_template(db, GUILD_B, tier=5))["template_id"] == 1


async def test_tier_and_status_still_filter_within_scope(db):
    await _add(db, 1, GUILD_A, tier=4, status="published")   # too high a tier
    await _add(db, 2, GUILD_A, tier=1, status="draft")       # not published
    assert await pick_template(db, GUILD_A, tier=2) is None
    await _add(db, 3, GUILD_A, tier=1, status="published")
    assert (await pick_template(db, GUILD_A, tier=2))["template_id"] == 3
