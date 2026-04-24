"""Shared pytest fixtures for Dungeon Keeper tests (spec §9.5)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Ensure project root is on sys.path so all project modules are importable
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import aiosqlite

from config import Config
from migrations import apply_migrations
from tests.fakes import FakeGuild, FakeRole, FakeUser, fake_interaction as _fake_interaction


@pytest_asyncio.fixture
async def temp_db(tmp_path):
    """Open an aiosqlite connection with the full schema applied."""
    path = tmp_path / "test.db"
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await apply_migrations(db)
    yield db
    await db.close()


@pytest.fixture
def test_config(tmp_path) -> Config:
    """A dev Config pointing at tmp_path — no real env vars required."""
    return Config(
        env="dev",
        token="fake-token",
        guild_id=9001,
        db_path=str(tmp_path / "test.db"),
        audit_channel_id=9999,
        reset_dev_db=False,
        seed_dev_fixtures=False,
    )


@pytest.fixture
def fake_interaction():
    """A MagicMock discord.Interaction with standard AsyncMock response methods."""
    return _fake_interaction()


@pytest.fixture
def guild_with_mods() -> FakeGuild:
    """A FakeGuild pre-populated with Mod and Jailed roles."""
    g = FakeGuild()
    g.roles[5001] = FakeRole(id=5001, name="Mod")
    g.roles[5002] = FakeRole(id=5002, name="Jailed")
    g.roles[5003] = FakeRole(id=5003, name="Admin")
    return g


@pytest.fixture
def mod_user(guild_with_mods) -> FakeUser:
    """A FakeUser with the Mod role."""
    mod_role = guild_with_mods.roles[5001]
    return FakeUser(id=2001, name="mod_user", roles=[mod_role])


@pytest.fixture
def regular_user() -> FakeUser:
    return FakeUser(id=3001, name="regular_user", roles=[])
