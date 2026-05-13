"""Sidecar configuration for the beta tools bot.

Parallel to config.Config (which configures the main Dungeon Keeper bot).
Reads DISCORD_TOKEN_TOOLS, BETA_PUPPET_TOKEN_1..3, and the BETA_* knobs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(override=True)


@dataclass(frozen=True)
class BetaConfig:
    tools_token: str
    tools_expected_id: int
    puppet_tokens: tuple[str, str, str]
    puppet_expected_ids: tuple[int, int, int]
    enabled: bool
    ambient_rate_multiplier: float
    ambient_autostart: bool
    llm_blend: bool


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip() == "1"


def load_beta_config() -> BetaConfig:
    return BetaConfig(
        tools_token=os.environ["DISCORD_TOKEN_TOOLS"],
        tools_expected_id=int(os.environ["EXPECTED_BOT_ID_TOOLS"]),
        puppet_tokens=(
            os.environ["BETA_PUPPET_TOKEN_1"],
            os.environ["BETA_PUPPET_TOKEN_2"],
            os.environ["BETA_PUPPET_TOKEN_3"],
        ),
        puppet_expected_ids=(
            int(os.environ["EXPECTED_BOT_ID_PUPPET_1"]),
            int(os.environ["EXPECTED_BOT_ID_PUPPET_2"]),
            int(os.environ["EXPECTED_BOT_ID_PUPPET_3"]),
        ),
        enabled=_bool_env("BETA_TOOLS_ENABLED", default=False),
        ambient_rate_multiplier=float(os.getenv("BETA_AMBIENT_RATE_MULTIPLIER", "1.0")),
        ambient_autostart=_bool_env("BETA_AMBIENT_AUTOSTART", default=True),
        llm_blend=_bool_env("BETA_LLM_BLEND", default=False),
    )
