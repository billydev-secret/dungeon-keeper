"""Entry point for DK Tools sidecar.

Usage:
    BOT_ENV=dev BETA_TOOLS_ENABLED=1 python -m beta_tools

Refuses to run outside dev. See beta_tools.safety for the full set of guards.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

from beta_tools.bot import DkToolsBot
from beta_tools.safety import assert_safe_to_start
from core.config import load_config


def _setup_logging() -> None:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    log_path = Path(__file__).parent.parent / "log_beta_tools.txt"
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, encoding="utf-8", maxBytes=2_000_000, backupCount=1,
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(stream)
    root.addHandler(file_handler)


async def _main() -> None:
    _setup_logging()
    log = logging.getLogger("beta_tools.main")
    beta_cfg = assert_safe_to_start()  # exits on any safety violation
    main_cfg = load_config()
    log.info("DK Tools starting in dev (guild=%d, db=%s)", main_cfg.guild_id, main_cfg.db_path)
    bot = DkToolsBot(main_cfg=main_cfg, beta_cfg=beta_cfg)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler(*_args) -> None:
        log.info("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows: signal handlers via add_signal_handler not supported. Fall back to default.
            signal.signal(sig, lambda *_a: stop_event.set())

    bot_task = asyncio.create_task(bot.start(beta_cfg.tools_token), name="dk-tools-bot")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")
    _, pending = await asyncio.wait(
        {bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
    )
    log.info("shutting down")
    if not bot.is_closed():
        await bot.close()
    for t in pending:
        t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
