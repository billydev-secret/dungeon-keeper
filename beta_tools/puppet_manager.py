"""PuppetManager — owns the 3 puppet discord.Client instances.

Construction is cheap (no I/O). start_all() actually connects to the gateway.
Persona diff logic (_apply_persona) is a free function for unit testability.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord

from beta_tools.personas import Persona
from beta_tools.safety import check_puppet_guild_membership, check_puppet_identity

log = logging.getLogger("beta_tools.puppet_manager")


@dataclass
class PuppetHandle:
    key: str
    persona: Persona
    token: str
    expected_id: int
    client: Optional[discord.Client] = None
    task: Optional[asyncio.Task] = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class PuppetManager:
    def __init__(
        self,
        *,
        personas: list[Persona],
        tokens: tuple[str, str, str],
        expected_ids: tuple[int, int, int],
        expected_guild_id: int,
    ) -> None:
        if len(personas) != 3 or len(tokens) != 3 or len(expected_ids) != 3:
            raise ValueError(
                f"PuppetManager requires exactly 3 personas/tokens/ids; "
                f"got {len(personas)} personas, {len(tokens)} tokens, {len(expected_ids)} ids"
            )
        self.expected_guild_id = expected_guild_id
        self.handles: list[PuppetHandle] = [
            PuppetHandle(key=p.key, persona=p, token=tokens[i], expected_id=expected_ids[i])
            for i, p in enumerate(personas)
        ]

    def get_handle(self, key: str) -> PuppetHandle:
        for h in self.handles:
            if h.key == key:
                return h
        raise KeyError(f"no puppet with key {key!r}")

    async def start_all(self) -> None:
        """Connect all 3 puppets to the gateway and wait until all are ready.

        Raises if any puppet's start task crashes (bad token, network failure)
        or fails to become ready within a 60-second timeout.
        """
        for h in self.handles:
            client = _new_puppet_client(h, self.expected_guild_id)
            h.client = client
            h.task = asyncio.create_task(client.start(h.token), name=f"puppet-{h.key}")

        # Race: either all 3 ready events fire, or any start task crashes first.
        ready_waits = [asyncio.create_task(h.ready.wait(), name=f"ready-{h.key}") for h in self.handles]
        start_tasks = [h.task for h in self.handles if h.task is not None]
        watched = ready_waits + start_tasks

        _, pending = await asyncio.wait(
            watched,
            timeout=60.0,
            return_when=asyncio.FIRST_EXCEPTION,
        )

        # Cancel anything still pending (we're either done or about to error out).
        for f in pending:
            f.cancel()

        # If any start task crashed, raise its exception.
        for h in self.handles:
            if h.task is not None and h.task.done() and not h.task.cancelled():
                exc = h.task.exception()
                if exc is not None:
                    raise RuntimeError(f"puppet {h.key!r} failed to start") from exc

        # All start tasks are still running but did some of them not become ready?
        unready = [h.key for h in self.handles if not h.ready.is_set()]
        if unready:
            raise RuntimeError(f"puppets never became ready (timeout): {unready}")

    async def apply_personas(self) -> None:
        """Idempotent: apply persona display_name + avatar to each connected puppet."""
        for h in self.handles:
            if h.client is None or h.client.user is None:
                log.warning("puppet %r is not connected; skipping persona apply", h.key)
                continue
            await _apply_persona(h.client, h.persona)

    async def close_all(self) -> None:
        for h in self.handles:
            if h.client is not None:
                try:
                    await h.client.close()
                except Exception:  # noqa: BLE001
                    log.exception("error closing puppet %r", h.key)


def _new_puppet_client(handle: PuppetHandle, expected_guild_id: int) -> discord.Client:
    """Build a fresh discord.Client wired with on_ready safety checks."""
    intents = discord.Intents.default()
    intents.message_content = False  # puppets don't need to read others' messages
    intents.members = True            # for guild membership and member edit
    intents.voice_states = True       # so puppets can join voice in later plans
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        user = client.user
        if user is None:
            log.critical("puppet %r on_ready fired but client.user is None — cannot validate identity", handle.key)
            handle.ready.set()  # unblock start_all so it can timeout/error cleanly
            return
        log.info("puppet %r connected as %s (id=%d)", handle.key, user, user.id)
        check_puppet_identity(user, handle.expected_id, handle.key)
        await check_puppet_guild_membership(client, expected_guild_id, handle.key)
        handle.ready.set()

    @client.event
    async def on_guild_join(guild: discord.Guild) -> None:
        if guild.id != expected_guild_id:
            log.critical(
                "CRITICAL: puppet %r joined unexpected guild %d (%r) — leaving.",
                handle.key, guild.id, guild.name,
            )
            try:
                await guild.leave()
            except Exception:  # noqa: BLE001
                log.exception("puppet %r failed to leave guild %d", handle.key, guild.id)

    return client


async def _apply_persona(client: discord.Client, persona: Persona) -> None:
    """Idempotent: only call user.edit() if name or avatar differs from the persona."""
    user = client.user
    if user is None:
        log.warning("client.user is None; cannot apply persona %r", persona.key)
        return

    desired_name = persona.display_name
    current_name = user.name
    current_avatar_url = user.display_avatar.url

    needs_name_update = current_name != desired_name
    needs_avatar_update = current_avatar_url != persona.avatar_url

    if not needs_name_update and not needs_avatar_update:
        log.info("puppet %r persona already applied; skipping", persona.key)
        return

    edit_kwargs: dict = {}
    if needs_name_update:
        edit_kwargs["username"] = desired_name
    if needs_avatar_update:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(persona.avatar_url) as resp:
                    resp.raise_for_status()
                    edit_kwargs["avatar"] = await resp.read()
        except Exception:  # noqa: BLE001
            log.exception("failed to fetch avatar for persona %r", persona.key)
            # Proceed with name update only
            edit_kwargs.pop("avatar", None)

    if not edit_kwargs:
        log.warning(
            "puppet %r: avatar fetch failed and no other updates needed; skipping edit",
            persona.key,
        )
        return

    log.info("applying persona %r: name_update=%s avatar_update=%s",
             persona.key, needs_name_update, "avatar" in edit_kwargs)
    await user.edit(**edit_kwargs)
