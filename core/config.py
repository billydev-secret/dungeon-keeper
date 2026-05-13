"""Bootstrap-level configuration for Dungeon Keeper.

Handles BOT_ENV selection and per-environment token/guild/db resolution.
Separate from app_context.RuntimeConfig, which manages guild-scoped runtime
settings (mod channels, XP coefficients, etc.) loaded from the database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(override=True)


@dataclass(frozen=True)
class Config:
    env: str
    token: str
    guild_id: int
    db_path: str
    audit_channel_id: int
    reset_dev_db: bool
    seed_dev_fixtures: bool

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


def load_config() -> Config:
    env = os.getenv("BOT_ENV", "dev").lower()
    if env not in ("dev", "prod"):
        raise ValueError(f"BOT_ENV must be 'dev' or 'prod', got {env!r}")
    suffix = env.upper()
    return Config(
        env=env,
        token=os.environ[f"DISCORD_TOKEN_{suffix}"],
        guild_id=int(os.environ[f"GUILD_ID_{suffix}"]),
        db_path=os.environ[f"DB_PATH_{suffix}"],
        audit_channel_id=int(os.environ.get(f"AUDIT_CHANNEL_{suffix}", "0")),
        reset_dev_db=os.getenv("RESET_DEV_DB", "0") == "1" and env == "dev",
        seed_dev_fixtures=os.getenv("SEED_DEV_FIXTURES", "0") == "1" and env == "dev",
    )
