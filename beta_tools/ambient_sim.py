"""AmbientSim — central dispatcher loop for puppet ambient traffic."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import discord

from beta_tools.config import BetaConfig
from beta_tools.markov import MarkovChain
from beta_tools.puppet_manager import PuppetHandle, PuppetManager

log = logging.getLogger("beta_tools.ambient_sim")

_BASE_INTERVAL: float = 15.0
_BURST_INTERVAL: float = 5.0
_BURST_DURATION: float = 30.0
_JITTER: float = 0.2
_HTTP_ERROR_SLEEP: float = 10.0
_LOOP_ERROR_SLEEP: float = 30.0


class AmbientSim:
    def __init__(
        self,
        chain: MarkovChain,
        puppet_manager: PuppetManager,
        guild: discord.Guild,
        beta_cfg: BetaConfig,
    ) -> None:
        self._chain = chain
        self._puppet_manager = puppet_manager
        self._guild = guild
        self._beta_cfg = beta_cfg
        self._task: Optional[asyncio.Task] = None
        self._posts: int = 0
        self._last_post_at: float = 0.0
        self._last_post: Optional[tuple[str, str, float]] = None
        self._warned_channels: set[str] = set()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def posts_since_start(self) -> int:
        return self._posts

    @property
    def last_post(self) -> Optional[tuple[str, str, float]]:
        return self._last_post

    def start(self) -> None:
        if self.is_running:
            return
        self._posts = 0
        self._last_post_at = 0.0
        self._last_post = None
        self._warned_channels = set()
        self._task = asyncio.create_task(self._loop(), name="ambient-sim")
        log.info("ambient sim started (rate_multiplier=%.2f)", self._beta_cfg.ambient_rate_multiplier)

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        log.info("ambient sim stopped after %d posts", self._posts)

    def _base_interval(self) -> float:
        return _BASE_INTERVAL / max(self._beta_cfg.ambient_rate_multiplier, 0.1)

    def _sleep_duration(self) -> float:
        if time.monotonic() - self._last_post_at < _BURST_DURATION:
            return _BURST_INTERVAL
        base = self._base_interval()
        return max(1.0, base + base * random.uniform(-_JITTER, _JITTER))

    def _pick_puppet(self) -> PuppetHandle:
        handles = self._puppet_manager.handles
        weights = [h.persona.activity_weight for h in handles]
        return random.choices(handles, weights=weights, k=1)[0]

    def _resolve_channel(self, name: str) -> Optional[discord.TextChannel]:
        for ch in self._guild.text_channels:
            if ch.name == name:
                return ch
        if name not in self._warned_channels:
            log.warning("ambient sim: channel %r not found in guild (will not warn again)", name)
            self._warned_channels.add(name)
        return None

    def _pick_channel(self, handle: PuppetHandle) -> Optional[tuple[discord.TextChannel, str]]:
        affinities = dict(handle.persona.channel_affinities)
        while affinities:
            names = list(affinities.keys())
            weights = [affinities[n] for n in names]
            idx = random.choices(range(len(names)), weights=weights, k=1)[0]
            name = names[idx]
            del affinities[name]
            ch = self._resolve_channel(name)
            if ch is not None:
                return ch, name
        return None

    async def _tick(self) -> None:
        handle = self._pick_puppet()

        if handle.client is None or not handle.ready.is_set():
            log.debug("puppet %r not ready, skipping tick", handle.key)
            return

        result = self._pick_channel(handle)
        if result is None:
            log.debug("no resolvable channels for puppet %r, skipping tick", handle.key)
            return
        channel, channel_name = result

        raw_channel = handle.client.get_channel(channel.id)
        if raw_channel is None:
            log.debug("puppet %r cannot see channel %r, skipping", handle.key, channel_name)
            return

        text = self._chain.generate(handle.persona.message_length_bias)
        if not text:
            return

        await raw_channel.send(text)  # type: ignore[union-attr]  # _resolve_channel guarantees TextChannel
        self._last_post_at = time.monotonic()
        self._last_post = (handle.key, channel_name, time.time())
        self._posts += 1
        log.debug("ambient sim: %r posted in #%s", handle.key, channel_name)

    async def _loop(self) -> None:
        log.info("ambient sim loop running")
        while True:
            try:
                await asyncio.sleep(self._sleep_duration())
                await self._tick()
            except asyncio.CancelledError:
                log.info("ambient sim loop cancelled")
                return
            except discord.Forbidden:
                log.warning("ambient sim: Forbidden error on tick — skipping")
            except discord.HTTPException as exc:
                log.warning("ambient sim: HTTP error %s — sleeping %.0fs", exc, _HTTP_ERROR_SLEEP)
                await asyncio.sleep(_HTTP_ERROR_SLEEP)
            except Exception:
                log.exception("ambient sim: unexpected error — sleeping %.0fs", _LOOP_ERROR_SLEEP)
                await asyncio.sleep(_LOOP_ERROR_SLEEP)
