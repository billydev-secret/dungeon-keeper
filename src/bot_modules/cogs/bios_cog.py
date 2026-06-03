"""Bios cog — wizard-driven member profiles.

Thin orchestrator: holds the in-memory session registry, dispatches the
``/bio`` slash command and the persistent trigger button into
``WizardSession``, listens for ``on_member_remove`` to clean up posted
bios, and sweeps orphan wizard channels on cog load.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.bios import db as bios_db
from bot_modules.bios.config import BiosConfig
from bot_modules.bios.views import PersistentTriggerView, ResumeRestartView
from bot_modules.bios.wizard import WizardSession, build_session

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.bios")

_WIZARD_CHANNEL_RE = re.compile(r"^bio-(\d+)$")


class BiosCog(commands.Cog):
    def __init__(self, bot: "Bot", ctx: "AppContext") -> None:
        self.bot = bot
        self.ctx = ctx
        self._sessions: dict[tuple[int, int], WizardSession] = {}
        super().__init__()

    async def cog_load(self) -> None:
        # Persistent button on the bios channel survives restart.
        self.bot.add_view(PersistentTriggerView())
        # Sweep orphan wizard channels left by a crashed/restarted session.
        asyncio.create_task(self._sweep_orphans())

    async def _sweep_orphans(self) -> None:
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            try:
                def _load_cfg(gid: int = guild.id) -> BiosConfig:
                    with self.ctx.open_db() as conn:
                        return BiosConfig.load(conn, gid)

                cfg = await asyncio.to_thread(_load_cfg)
                if cfg.wizard_category_id == 0:
                    continue
                category = guild.get_channel(cfg.wizard_category_id)
                if not isinstance(category, discord.CategoryChannel):
                    continue
                for ch in list(category.text_channels):
                    if not _WIZARD_CHANNEL_RE.match(ch.name):
                        continue
                    if any(
                        s.channel is not None and s.channel.id == ch.id
                        for s in self._sessions.values()
                    ):
                        continue
                    try:
                        await ch.delete(reason="Bios wizard orphan cleanup")
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        log.warning(
                            "Forbidden deleting orphan wizard channel %d in guild %d",
                            ch.id,
                            guild.id,
                        )
                    except discord.HTTPException:
                        log.exception("HTTP error deleting orphan %d", ch.id)
            except Exception:
                log.exception("Orphan sweep failed for guild %d", guild.id)

    # ── /bio command ─────────────────────────────────────────────────

    @app_commands.command(name="bio", description="Create or update your bio.")
    @app_commands.guild_only()
    async def bio_command(self, interaction: discord.Interaction) -> None:
        await self._start_or_resume(interaction)

    # ── Dispatcher (also called by PersistentTriggerView) ────────────

    async def _start_or_resume(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Bios are only available inside the server.", ephemeral=True
            )
            return

        member: discord.Member = interaction.user
        guild = interaction.guild

        def _read_state() -> tuple[BiosConfig, bool]:
            with self.ctx.open_db() as conn:
                cfg = BiosConfig.load(conn, guild.id)
                tmpl = bios_db.get_template(conn, guild.id)
                has_active = (
                    tmpl is not None and bios_db.has_any_active_field(conn, tmpl.id)
                )
            return cfg, has_active

        cfg, has_active_field = await asyncio.to_thread(_read_state)

        if not cfg.configured or not has_active_field:
            await interaction.response.send_message(
                "Bios aren't set up yet — ask an admin to finish the dashboard config.",
                ephemeral=True,
            )
            return

        key = (guild.id, member.id)
        existing_session = self._sessions.get(key)
        if existing_session is not None and existing_session.channel is not None:
            await self._prompt_resume_restart(interaction, existing_session, cfg)
            return

        try:
            await self._spawn_session(interaction, member, cfg)
        except Exception:
            log.exception("Failed to spawn bio wizard for %d", member.id)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "Couldn't start your bio wizard — please try again.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "Couldn't start your bio wizard — please try again.",
                        ephemeral=True,
                    )
            except discord.HTTPException:
                pass

    async def _spawn_session(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        cfg: BiosConfig,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        session = await build_session(self, member, cfg)
        if not session.state.fields:
            await interaction.followup.send(
                "Bios aren't set up yet — ask an admin to add at least one field.",
                ephemeral=True,
            )
            return
        self._sessions[(member.guild.id, member.id)] = session
        try:
            channel = await session.create_channel()
        except discord.Forbidden:
            self._sessions.pop((member.guild.id, member.id), None)
            await interaction.followup.send(
                "I don't have permission to create the wizard channel. "
                "Ask an admin to give me Manage Channels in the configured category.",
                ephemeral=True,
            )
            return
        except RuntimeError:
            self._sessions.pop((member.guild.id, member.id), None)
            await interaction.followup.send(
                "Wizard category not found — ask an admin to fix the bios config.",
                ephemeral=True,
            )
            return
        session.start_loop()
        await interaction.followup.send(
            f"Started your bio wizard in {channel.mention}.",
            ephemeral=True,
        )

    async def _prompt_resume_restart(
        self,
        interaction: discord.Interaction,
        session: WizardSession,
        cfg: BiosConfig,
    ) -> None:
        member = session.member
        ch = session.channel

        async def on_resume(inner: discord.Interaction) -> None:
            try:
                await inner.response.edit_message(
                    content=(
                        f"Resumed — head back to {ch.mention if ch else 'your wizard channel'}."
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass

        async def on_restart(inner: discord.Interaction) -> None:
            await session.cancel("restart")
            try:
                await inner.response.edit_message(
                    content="Restarting…",
                    view=None,
                )
            except discord.HTTPException:
                pass
            try:
                await self._spawn_session_from_followup(inner, member, cfg)
            except Exception:
                log.exception("Restart failed for %d", member.id)

        view = ResumeRestartView(
            on_resume=on_resume, on_restart=on_restart, owner_id=member.id
        )
        await interaction.response.send_message(
            f"You already have a bio session running in {ch.mention if ch else 'a wizard channel'}.\n"
            "Resume where you left off, or restart from scratch?",
            view=view,
            ephemeral=True,
        )

    async def _spawn_session_from_followup(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        cfg: BiosConfig,
    ) -> None:
        # Same as _spawn_session but uses followup (the prior interaction
        # was already responded to with the resume/restart prompt).
        session = await build_session(self, member, cfg)
        if not session.state.fields:
            await interaction.followup.send(
                "Bios aren't set up yet — ask an admin to add at least one field.",
                ephemeral=True,
            )
            return
        self._sessions[(member.guild.id, member.id)] = session
        try:
            channel = await session.create_channel()
        except (discord.Forbidden, RuntimeError):
            self._sessions.pop((member.guild.id, member.id), None)
            await interaction.followup.send(
                "Couldn't recreate the wizard channel. Ask an admin to verify the config.",
                ephemeral=True,
            )
            return
        session.start_loop()
        await interaction.followup.send(
            f"Restarted — head to {channel.mention}.", ephemeral=True
        )

    # ── on_member_remove → delete posted bio ─────────────────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            def _load() -> bios_db.StoredBio | None:
                with self.ctx.open_db() as conn:
                    return bios_db.get_user_bio(conn, member.guild.id, member.id)

            bio = await asyncio.to_thread(_load)
            if bio is None:
                return
            channel = member.guild.get_channel(bio.channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(bio.message_id)
                    await msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException:
                    log.exception("Failed to delete bio msg for %d", member.id)

            def _archive() -> None:
                with self.ctx.open_db() as conn:
                    bios_db.archive_user_bio(conn, member.guild.id, member.id)

            await asyncio.to_thread(_archive)
        except Exception:
            log.exception("on_member_remove bio cleanup failed for %d", member.id)


async def setup(bot: "Bot") -> None:
    await bot.add_cog(BiosCog(bot, bot.ctx))
