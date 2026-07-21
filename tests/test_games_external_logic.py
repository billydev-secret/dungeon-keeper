"""Tests for games_external.logic — the multi-watch collector config (#70/#65)."""

from __future__ import annotations

import pytest

from bot_modules.games_external import logic
from bot_modules.services.games_db import GamesDb
from migrations import apply_migrations_sync

GUILD = 111
CHAN_A, CHAN_B = 201, 202
GAMEBOT, CATBOT = 620307267241377793, 966695034340663367


@pytest.fixture
def gdb(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return GamesDb(db_path)


@pytest.mark.asyncio
async def test_set_and_get_watch_carries_kind(gdb):
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot_cah", set_by=9)
    row = await logic.get_watch_for_bot(gdb, GUILD, GAMEBOT)
    assert row is not None
    assert row["channel_id"] == CHAN_A
    assert row["kind"] == "gamebot_cah"
    assert row["enabled"] == 1


@pytest.mark.asyncio
async def test_multiple_bots_coexist_per_guild(gdb):
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot_cah", set_by=9)
    await logic.set_watch(gdb, GUILD, CHAN_B, CATBOT, "catbot", set_by=9)

    watches = await logic.list_watches(gdb, GUILD)
    by_bot = {int(w["bot_user_id"]): w for w in watches}
    assert set(by_bot) == {GAMEBOT, CATBOT}
    assert by_bot[CATBOT]["kind"] == "catbot"
    assert by_bot[GAMEBOT]["kind"] == "gamebot_cah"


@pytest.mark.asyncio
async def test_re_watching_same_bot_repoints_not_duplicates(gdb):
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot_cah", set_by=9)
    await logic.set_watch(gdb, GUILD, CHAN_B, GAMEBOT, "gamebot_cah", set_by=9)

    watches = await logic.list_watches(gdb, GUILD)
    assert len(watches) == 1
    assert watches[0]["channel_id"] == CHAN_B  # repointed


@pytest.mark.asyncio
async def test_enable_is_per_bot(gdb):
    await logic.set_watch(gdb, GUILD, CHAN_A, GAMEBOT, "gamebot_cah", set_by=9)
    await logic.set_watch(gdb, GUILD, CHAN_B, CATBOT, "catbot", set_by=9)

    assert await logic.set_watch_enabled(gdb, GUILD, GAMEBOT, False) is True
    # Missing bot toggles nothing.
    assert await logic.set_watch_enabled(gdb, GUILD, 999, False) is False

    enabled = {int(r["bot_user_id"]) for r in await logic.load_all_watches(gdb)}
    assert enabled == {CATBOT}  # only the still-enabled bot warms the cache


@pytest.mark.asyncio
async def test_count_messages_filters_by_bot(gdb):
    for mid, bot in ((1, GAMEBOT), (2, GAMEBOT), (3, CATBOT)):
        await gdb.execute(
            "INSERT INTO games_external_messages "
            "(message_id, guild_id, channel_id, author_id, created_at) "
            "VALUES (?, ?, ?, ?, '2026-07-21T00:00:00')",
            (mid, GUILD, CHAN_A, bot),
        )
    assert await logic.count_messages(gdb, GUILD) == 3
    assert await logic.count_messages(gdb, GUILD, GAMEBOT) == 2
    assert await logic.count_messages(gdb, GUILD, CATBOT) == 1


def test_valid_kinds_expose_labels():
    assert "gamebot_cah" in logic.VALID_WATCH_KINDS
    assert "catbot" in logic.VALID_WATCH_KINDS
    assert logic.WATCH_KIND_LABELS["catbot"] == "Cat Bot"
