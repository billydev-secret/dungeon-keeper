"""Safety rails for the beta tools sidecar (spec §7).

Layer 1 here: assert_safe_to_start() runs before any Discord connection.
Layers 2-5 are implemented as functions called from later modules:
  - check_tools_bot_identity() / check_tools_guild_membership() — bot.py
  - check_puppet_identity()    / check_puppet_guild_membership() — puppet_manager.py
  - beta_write()                                                — db_gate.py
"""

from __future__ import annotations

import logging
import os
import sys

import discord

from beta_tools.config import BetaConfig, load_beta_config
from config import load_config

log = logging.getLogger("beta_tools.safety")


def assert_safe_to_start() -> BetaConfig:
    """Layer 1: refuse to start outside dev. Returns the loaded BetaConfig."""
    main_cfg = load_config()
    if not main_cfg.is_dev:
        log.critical("CRITICAL: beta_tools refuses to start outside dev env (BOT_ENV=%r)", main_cfg.env)
        sys.exit(1)

    if "dev" not in main_cfg.db_path.lower():
        log.critical("CRITICAL: db_path=%r does not contain 'dev'", main_cfg.db_path)
        sys.exit(1)

    if os.getenv("BETA_TOOLS_ENABLED") != "1":
        log.critical("CRITICAL: BETA_TOOLS_ENABLED must be '1' to launch beta tools")
        sys.exit(1)

    beta_cfg = load_beta_config()

    prod_id_raw = os.getenv("EXPECTED_BOT_ID_PROD")
    if prod_id_raw and prod_id_raw.strip():
        try:
            prod_id = int(prod_id_raw)
        except ValueError:
            prod_id = -1
        if beta_cfg.tools_expected_id == prod_id:
            log.critical(
                "CRITICAL: tools bot id (%d) matches prod bot id — config error",
                beta_cfg.tools_expected_id,
            )
            sys.exit(1)

    log.info(
        "beta_tools safety: env=dev db=%s tools_id=%d puppets=%d — start permitted",
        main_cfg.db_path,
        beta_cfg.tools_expected_id,
        len(beta_cfg.puppet_tokens),
    )
    return beta_cfg


def check_tools_bot_identity(bot_user: discord.ClientUser, expected_id: int) -> None:
    """Layer 2 (a): assert connected bot user matches EXPECTED_BOT_ID_TOOLS."""
    if bot_user.id != expected_id:
        log.critical(
            "CRITICAL: connected as bot id %d (%s) but expected %d. Refusing to continue.",
            bot_user.id, bot_user, expected_id,
        )
        sys.exit(1)


async def check_tools_guild_membership(bot: discord.Client, expected_guild_id: int) -> None:
    """Layer 2 (b): leave any guild that isn't the test guild."""
    wrong = [g for g in bot.guilds if g.id != expected_guild_id]
    for g in wrong:
        log.critical(
            "CRITICAL: tools bot is in unexpected guild %d (%r) — leaving immediately.",
            g.id, g.name,
        )
        try:
            await g.leave()
        except Exception:  # noqa: BLE001 — best-effort; we're shutting down anyway
            log.exception("failed to leave guild %d", g.id)
    if wrong:
        sys.exit(1)
    if not any(g.id == expected_guild_id for g in bot.guilds):
        log.critical("CRITICAL: tools bot is not in the configured test guild %d", expected_guild_id)
        sys.exit(1)


def check_puppet_identity(puppet_user: discord.ClientUser, expected_id: int, key: str) -> None:
    """Layer 3 (a): assert each puppet's bot user matches its expected ID."""
    if puppet_user.id != expected_id:
        log.critical(
            "CRITICAL: puppet %r connected as id %d but expected %d. Refusing to continue.",
            key, puppet_user.id, expected_id,
        )
        sys.exit(1)


async def check_puppet_guild_membership(puppet: discord.Client, expected_guild_id: int, key: str) -> None:
    """Layer 3 (b): puppet leaves any guild that isn't the test guild."""
    wrong = [g for g in puppet.guilds if g.id != expected_guild_id]
    for g in wrong:
        log.critical("CRITICAL: puppet %r in unexpected guild %d (%r) — leaving.", key, g.id, g.name)
        try:
            await g.leave()
        except Exception:  # noqa: BLE001
            log.exception("puppet %r failed to leave guild %d", key, g.id)
    if wrong:
        sys.exit(1)
