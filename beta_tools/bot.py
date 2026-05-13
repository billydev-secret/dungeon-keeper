# beta_tools/bot.py
"""DkToolsBot — the sidecar bot's commands.Bot subclass.

Owns the puppet manager, webhook fleet, ambient sim, and slash command tree.
Slash commands register in setup_hook() once the bot is logged in.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

from beta_tools.config import BetaConfig
from beta_tools.puppet_manager import PuppetManager
from beta_tools.webhook_fleet import WebhookFleet
from core.config import Config

log = logging.getLogger("beta_tools.bot")


class DkToolsBot(commands.Bot):
    def __init__(self, *, main_cfg: Config, beta_cfg: BetaConfig) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.main_cfg = main_cfg
        self.beta_cfg = beta_cfg
        self.puppet_manager: Optional[PuppetManager] = None
        self.webhook_fleet: Optional[WebhookFleet] = None
        self.ambient_sim = None
        self._chain = None

    async def setup_hook(self) -> None:
        # Imported here to avoid circular import: slash modules import DkToolsBot for typing.
        from beta_tools.markov import MarkovChain
        from beta_tools.personas import load_puppet_personas
        from beta_tools.slash import register_all

        # Load Markov chain if available
        chain_path = Path(__file__).resolve().parent.parent / "fixtures" / "markov_chain.json"
        if chain_path.exists():
            self._chain = MarkovChain.load(chain_path)
            log.info(
                "loaded Markov chain: %d bigrams, corpus=%d",
                self._chain.vocab_size,
                self._chain.corpus_size,
            )
        else:
            log.warning("fixtures/markov_chain.json not found — ambient sim will be unavailable")

        fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"
        personas = load_puppet_personas(fixtures_dir / "beta_puppets.yaml")
        pm = PuppetManager(
            personas=personas,
            tokens=self.beta_cfg.puppet_tokens,
            expected_ids=self.beta_cfg.puppet_expected_ids,
            expected_guild_id=self.main_cfg.guild_id,
        )
        self.puppet_manager = pm
        self.webhook_fleet = WebhookFleet()

        register_all(self)
        guild_obj = discord.Object(id=self.main_cfg.guild_id)
        await self.tree.sync(guild=guild_obj)
        log.info("registered /beta commands to guild %d", self.main_cfg.guild_id)

        log.info("starting %d puppets", len(pm.handles))
        await pm.start_all()
        await pm.apply_personas()
        log.info("puppets ready")

    async def on_ready(self) -> None:
        assert self.user is not None
        from beta_tools.ambient_sim import AmbientSim
        from beta_tools.safety import check_tools_bot_identity, check_tools_guild_membership

        check_tools_bot_identity(self.user, self.beta_cfg.tools_expected_id)
        await check_tools_guild_membership(self, self.main_cfg.guild_id)
        log.info(
            "DK Tools ready: %s (id=%d) in guild %d",
            self.user, self.user.id, self.main_cfg.guild_id,
        )

        if self._chain is not None and self.puppet_manager is not None:
            guild = self.get_guild(self.main_cfg.guild_id)
            if guild is not None:
                self.ambient_sim = AmbientSim(
                    chain=self._chain,
                    puppet_manager=self.puppet_manager,
                    guild=guild,
                    beta_cfg=self.beta_cfg,
                )
                if self.beta_cfg.ambient_autostart:
                    self.ambient_sim.start()
                    log.info("ambient sim auto-started")
            else:
                log.warning("guild not found in on_ready — ambient sim disabled")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != self.main_cfg.guild_id:
            log.critical(
                "CRITICAL: DK Tools joined unexpected guild %d (%r) — leaving.",
                guild.id, guild.name,
            )
            try:
                await guild.leave()
            except Exception:  # noqa: BLE001
                log.exception("failed to leave guild %d", guild.id)

    async def close(self) -> None:
        if self.ambient_sim is not None:
            await self.ambient_sim.stop()
        if self.puppet_manager is not None:
            await self.puppet_manager.close_all()
        await super().close()
