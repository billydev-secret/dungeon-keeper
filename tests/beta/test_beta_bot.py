"""Tests for beta_tools.bot.DkToolsBot — on_ready and on_guild_join hooks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_main_cfg(tmp_path):
    from bot_modules.core.config import Config
    return Config(
        env="dev", token="t", guild_id=9001, db_path=str(tmp_path / "dk_dev.db"),
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )


@pytest.fixture
def mock_beta_cfg():
    from beta_tools.config import BetaConfig
    return BetaConfig(
        tools_token="tt", tools_expected_id=10001,
        puppet_tokens=("p1", "p2", "p3"),
        puppet_expected_ids=(1, 2, 3),
        enabled=True, ambient_rate_multiplier=1.0,
        ambient_autostart=False, llm_blend=False,
    )


def test_dk_tools_bot_construction(mock_main_cfg, mock_beta_cfg):
    from beta_tools.bot import DkToolsBot
    bot = DkToolsBot(main_cfg=mock_main_cfg, beta_cfg=mock_beta_cfg)
    assert bot.main_cfg is mock_main_cfg
    assert bot.beta_cfg is mock_beta_cfg
    assert bot.puppet_manager is None  # set later in setup_hook
    assert bot.webhook_fleet is None


async def test_on_guild_join_leaves_non_test_guild(mock_main_cfg, mock_beta_cfg):
    from beta_tools.bot import DkToolsBot
    bot = DkToolsBot(main_cfg=mock_main_cfg, beta_cfg=mock_beta_cfg)
    wrong_guild = MagicMock()
    wrong_guild.id = 99999  # not 9001
    wrong_guild.name = "WrongGuild"
    wrong_guild.leave = AsyncMock()

    await bot.on_guild_join(wrong_guild)
    wrong_guild.leave.assert_awaited_once()


async def test_on_guild_join_does_not_leave_test_guild(mock_main_cfg, mock_beta_cfg):
    from beta_tools.bot import DkToolsBot
    bot = DkToolsBot(main_cfg=mock_main_cfg, beta_cfg=mock_beta_cfg)
    correct_guild = MagicMock()
    correct_guild.id = 9001
    correct_guild.name = "TestGuild"
    correct_guild.leave = AsyncMock()

    await bot.on_guild_join(correct_guild)
    correct_guild.leave.assert_not_called()


def test_dk_tools_bot_has_ambient_sim_attr(mock_main_cfg, mock_beta_cfg):
    from beta_tools.bot import DkToolsBot
    bot = DkToolsBot(main_cfg=mock_main_cfg, beta_cfg=mock_beta_cfg)
    assert hasattr(bot, "ambient_sim")
    assert bot.ambient_sim is None  # set later in on_ready
    assert bot._chain is None  # set later in setup_hook
