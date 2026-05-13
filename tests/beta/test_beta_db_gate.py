"""Tests for beta_tools.db_gate."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bot_modules.core.config import Config


@pytest.fixture
def dev_cfg(tmp_path):
    return Config(
        env="dev", token="t", guild_id=9001, db_path=str(tmp_path / "dk_dev.db"),
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )


@pytest.fixture
def prod_cfg():
    return Config(
        env="prod", token="t", guild_id=1, db_path="dungeonkeeper.db",
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )


async def test_beta_write_executes_in_dev(dev_cfg):
    from beta_tools.db_gate import beta_write
    db = AsyncMock()
    await beta_write(db, "INSERT INTO foo VALUES (?)", (1,), cfg=dev_cfg)
    db.execute.assert_called_once_with("INSERT INTO foo VALUES (?)", (1,))


async def test_beta_write_refuses_in_prod(prod_cfg):
    from beta_tools.db_gate import beta_write
    db = AsyncMock()
    with pytest.raises(RuntimeError, match="non-dev environment"):
        await beta_write(db, "INSERT INTO foo VALUES (?)", (1,), cfg=prod_cfg)
    db.execute.assert_not_called()
