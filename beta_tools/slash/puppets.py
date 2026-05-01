"""/beta-puppets-* and /beta-ghosts-impersonate slash commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from beta_tools.slash._base import reject_if_not_mod

log = logging.getLogger("beta_tools.slash.puppets")


# ── Handlers (testable as plain async functions) ─────────────────────


async def _puppets_list_handler(bot, interaction: discord.Interaction) -> None:
    if not await reject_if_not_mod(interaction):
        return
    pm = bot.puppet_manager
    if pm is None:
        await interaction.response.send_message("Puppet manager not initialized yet.", ephemeral=True)
        return
    lines = []
    for h in pm.handles:
        if h.client is not None and h.client.user is not None:
            user = h.client.user
            ready = "✅" if (h.ready and h.ready.is_set()) else "⏳"
            lines.append(f"{ready} `{h.key}` → {user} (id={user.id})")
        else:
            lines.append(f"❌ `{h.key}` → not connected")
    msg = "\n".join(["**Puppet roster:**"] + lines) if lines else "No puppets configured."
    await interaction.response.send_message(msg, ephemeral=True)


async def _puppets_reload_handler(bot, interaction: discord.Interaction) -> None:
    if not await reject_if_not_mod(interaction):
        return
    if bot.puppet_manager is None:
        await interaction.response.send_message("Puppet manager not initialized yet.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    from beta_tools.personas import load_puppet_personas
    new_personas = load_puppet_personas("fixtures/beta_puppets.yaml")
    if len(new_personas) != len(bot.puppet_manager.handles):
        await interaction.followup.send(
            f"Reload failed: fixture has {len(new_personas)} personas but {len(bot.puppet_manager.handles)} puppets are connected.",
            ephemeral=True,
        )
        return
    # Update each handle's persona in-place, then re-apply.
    for h, new in zip(bot.puppet_manager.handles, new_personas):
        h.persona = new
    await bot.puppet_manager.apply_personas()
    await interaction.followup.send(f"Reloaded {len(new_personas)} personas.", ephemeral=True)


async def _puppets_reconnect_handler(bot, interaction: discord.Interaction, *, key: str) -> None:
    if not await reject_if_not_mod(interaction):
        return
    if bot.puppet_manager is None:
        await interaction.response.send_message("Puppet manager not initialized yet.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        handle = bot.puppet_manager.get_handle(key)
    except KeyError:
        await interaction.followup.send(f"Unknown puppet key {key!r}.", ephemeral=True)
        return
    log.info("reconnecting puppet %r", key)
    if handle.client is not None:
        try:
            await handle.client.close()
        except Exception:  # noqa: BLE001
            log.exception("error closing puppet %r before reconnect", key)
    # Build a fresh client and start it.
    import asyncio
    from beta_tools.puppet_manager import _new_puppet_client
    handle.ready.clear()
    handle.client = _new_puppet_client(handle, bot.main_cfg.guild_id)
    handle.task = asyncio.create_task(handle.client.start(handle.token), name=f"puppet-{handle.key}")
    await handle.ready.wait()
    await interaction.followup.send(f"Puppet `{key}` reconnected.", ephemeral=True)


async def _puppets_impersonate_handler(
    bot,
    interaction: discord.Interaction,
    *,
    key: str,
    channel: discord.TextChannel,
    text: str,
) -> None:
    if not await reject_if_not_mod(interaction):
        return
    if bot.puppet_manager is None:
        await interaction.response.send_message("Puppet manager not initialized yet.", ephemeral=True)
        return
    try:
        handle = bot.puppet_manager.get_handle(key)
    except KeyError:
        await interaction.response.send_message(f"Unknown puppet key {key!r}.", ephemeral=True)
        return
    if handle.client is None:
        await interaction.response.send_message(f"Puppet `{key}` is not connected.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    puppet_channel = handle.client.get_channel(channel.id)
    if puppet_channel is None:
        await interaction.followup.send(
            f"Puppet `{key}` cannot see channel {channel.mention}.", ephemeral=True,
        )
        return
    await puppet_channel.send(text)
    await interaction.followup.send(
        f"Posted to {channel.mention} as `{key}`.", ephemeral=True,
    )


async def _ghosts_impersonate_handler(
    bot,
    interaction: discord.Interaction,
    *,
    display_name: str,
    avatar_url: str,
    channel: discord.TextChannel,
    text: str,
) -> None:
    if not await reject_if_not_mod(interaction):
        return
    if bot.webhook_fleet is None:
        await interaction.response.send_message("Webhook fleet not initialized yet.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await bot.webhook_fleet.send(
        channel, content=text, username=display_name, avatar_url=avatar_url,
    )
    await interaction.followup.send(
        f"Posted to {channel.mention} as ghost `{display_name}`.", ephemeral=True,
    )


# ── Registration on the slash command tree ───────────────────────────


def register(bot) -> None:
    guild_obj = discord.Object(id=bot.main_cfg.guild_id)

    @bot.tree.command(name="beta-puppets-list", description="Show puppet roster + connection state", guild=guild_obj)
    async def list_cmd(interaction: discord.Interaction) -> None:
        await _puppets_list_handler(bot, interaction)

    @bot.tree.command(name="beta-puppets-reload", description="Re-read fixtures/beta_puppets.yaml and reapply personas", guild=guild_obj)
    async def reload_cmd(interaction: discord.Interaction) -> None:
        await _puppets_reload_handler(bot, interaction)

    @bot.tree.command(name="beta-puppets-reconnect", description="Reconnect a single puppet", guild=guild_obj)
    @app_commands.describe(key="Puppet key (alice, bob, clara)")
    async def reconnect_cmd(interaction: discord.Interaction, key: str) -> None:
        await _puppets_reconnect_handler(bot, interaction, key=key)

    @bot.tree.command(name="beta-puppets-impersonate", description="Post a message as a specific puppet", guild=guild_obj)
    @app_commands.describe(
        key="Puppet key (alice, bob, clara)",
        channel="Target channel",
        text="Message text",
    )
    async def impersonate_cmd(
        interaction: discord.Interaction,
        key: str,
        channel: discord.TextChannel,
        text: str,
    ) -> None:
        await _puppets_impersonate_handler(bot, interaction, key=key, channel=channel, text=text)

    @bot.tree.command(name="beta-ghosts-impersonate", description="Post a message via webhook with a custom name+avatar", guild=guild_obj)
    @app_commands.describe(
        display_name="Display name shown on the message",
        avatar_url="Avatar image URL",
        channel="Target channel",
        text="Message text",
    )
    async def ghost_impersonate_cmd(
        interaction: discord.Interaction,
        display_name: str,
        avatar_url: str,
        channel: discord.TextChannel,
        text: str,
    ) -> None:
        await _ghosts_impersonate_handler(
            bot, interaction,
            display_name=display_name, avatar_url=avatar_url,
            channel=channel, text=text,
        )
