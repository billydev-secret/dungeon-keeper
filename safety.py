"""Startup safety assertions for Dungeon Keeper (spec §8).

Call check_db_path() before the bot connects.
Call check_on_ready() inside on_ready after the bot user is available.
"""

from __future__ import annotations

import logging
import os
import sys

import discord

from config import Config

log = logging.getLogger("dungeonkeeper.safety")

_BANNER_WIDTH = 52


def _banner_line(text: str = "") -> str:
    return f"  {text}"


def print_startup_banner(cfg: Config, bot_user: discord.ClientUser | None = None) -> None:
    bot_tag = str(bot_user) if bot_user else "connecting..."
    lines = [
        "=" * _BANNER_WIDTH,
        _banner_line(f"DUNGEON KEEPER   env={cfg.env.upper()}   bot={bot_tag}"),
        _banner_line(f"guild={cfg.guild_id}"),
        _banner_line(f"db={cfg.db_path}"),
        _banner_line(
            f"reset_db={cfg.reset_dev_db}   seed_fixtures={cfg.seed_dev_fixtures}"
        ),
        "=" * _BANNER_WIDTH,
    ]
    banner = "\n".join(lines)
    # Red background for prod so it's unmissable in terminal
    if cfg.is_prod:
        print(f"\033[41m{banner}\033[0m")
    else:
        print(banner)
    log.info("Startup banner: env=%s guild=%s db=%s", cfg.env, cfg.guild_id, cfg.db_path)


def check_db_path(cfg: Config) -> None:
    """Assert DB path contains 'dev' iff env=='dev' (spec §8.2)."""
    path = cfg.db_path.lower()
    has_dev = "dev" in path
    if cfg.is_dev and not has_dev:
        log.critical(
            "SAFETY: env=dev but db_path=%r does not contain 'dev'. Refusing to start.",
            cfg.db_path,
        )
        sys.exit(1)
    if cfg.is_prod and has_dev:
        log.critical(
            "SAFETY: env=prod but db_path=%r contains 'dev'. Refusing to start.",
            cfg.db_path,
        )
        sys.exit(1)


def check_bot_identity(cfg: Config, bot_user: discord.ClientUser) -> None:
    """Assert bot user ID matches EXPECTED_BOT_ID_{ENV} (spec §8.1).

    Exits if the env var is set and does not match — this catches token/env
    mismatches before any event handlers run.
    """
    suffix = cfg.env.upper()
    expected_raw = os.getenv(f"EXPECTED_BOT_ID_{suffix}")
    if expected_raw is None:
        return  # env var not set; skip check
    try:
        expected = int(expected_raw)
    except ValueError:
        log.warning("EXPECTED_BOT_ID_%s is not a valid integer; skipping identity check", suffix)
        return
    if bot_user.id != expected:
        log.critical(
            "SAFETY: env=%s expected bot ID %d but connected as %d (%s). Refusing to continue.",
            cfg.env,
            expected,
            bot_user.id,
            bot_user,
        )
        sys.exit(1)


async def check_guild_membership(cfg: Config, bot: discord.Client) -> None:
    """Assert bot is in the configured guild (spec §8.3).

    In dev: leaves any unexpected guild and shuts down (prevents the dev bot
    from acting in the wrong server). In prod: only logs unexpected guilds,
    since the prod bot may legitimately serve multiple servers.
    """
    wrong = [g for g in bot.guilds if g.id != cfg.guild_id]
    if wrong and cfg.is_dev:
        for g in wrong:
            log.critical(
                "SAFETY: dev bot is in unexpected guild %d (%r) — leaving and shutting down.",
                g.id,
                g.name,
            )
            try:
                await g.leave()
            except Exception:
                pass
        sys.exit(1)
    for g in wrong:
        log.warning("bot is in additional guild %d (%r)", g.id, g.name)

    if not any(g.id == cfg.guild_id for g in bot.guilds):
        log.critical(
            "SAFETY: bot is not in configured guild %d. Check GUILD_ID_%s.",
            cfg.guild_id,
            cfg.env.upper(),
        )
        sys.exit(1)
