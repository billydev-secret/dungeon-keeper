"""`/ask` — member self-service help, answered by the AI advisor.

Thin glue over ``bot_modules.services.advisor_service``; the same brain powers
the dashboard Help panel's ask box. Answers are grounded in the user
manual, so the advisor can't invent commands. Ephemeral + per-user cooldown so one
member can't spend the shared Anthropic budget.

Admin askers additionally get config tools: settings are fetched on demand
(``get_server_settings``) instead of dumped inline, and requested changes come
back as *proposals* rendered here as Apply buttons — the write only happens on
click, re-permission-checked and re-validated (``advisor_actions``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_grant_roles, open_db
from bot_modules.services.branding_service import (
    DEFAULT_ASSISTANT_NAME,
    resolve_assistant_name_conn,
)
from bot_modules.services.advisor_actions import (
    ConfigProposal,
    apply_config_change,
    validate_config_change,
    validate_grant_role_change,
)
from bot_modules.services.advisor_context import (
    FEATURE_KEYS,
    build_asker_context,
    can_see_config,
    fetch_feature_settings,
    is_server_admin,
    is_staff,
)
from bot_modules.services.advisor_gaps import fetch_setup_gaps
from bot_modules.services.advisor_service import (
    MODEL,
    AdvisorTools,
    answer_advisor,
    get_advisor_context_enabled,
    get_advisor_tools_enabled,
    resolve_advisor_model,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger(__name__)

# Discord embed descriptions cap at 4096 chars; leave room for the trailer.
_MAX_DESC = 3900
_MAX_PROPOSALS = 4  # buttons on one reply; also caps blast radius per ask


def _make_tools(
    guild: discord.Guild,
    member: discord.Member,
    db_path: Path,
    proposals: list[ConfigProposal],
) -> AdvisorTools:
    """Config tools for one admin ask; queued proposals land in ``proposals``."""

    def _fetch(feature: str) -> str:
        return fetch_feature_settings(guild, member, db_path, feature)

    def _gaps() -> str:
        return fetch_setup_gaps(db_path, guild.id, member)

    def _queue(prop: ConfigProposal) -> str:
        """Dedupe by what the change targets, then queue it for a button."""
        ident = (prop.target, prop.grant_name, prop.key)
        proposals[:] = [
            p for p in proposals if (p.target, p.grant_name, p.key) != ident
        ]
        if len(proposals) >= _MAX_PROPOSALS:
            return f"Rejected: at most {_MAX_PROPOSALS} changes per ask."
        proposals.append(prop)
        return (
            f"Queued: {prop.display}. NOT applied yet — an Apply button is "
            "attached to your reply; tell the admin to press it to confirm."
        )

    def _propose(key: str, value: str) -> str:
        if not can_see_config(member):  # defense in depth; wiring already gates
            return "Rejected: only server admins can change settings."
        try:
            with open_db(db_path) as conn:
                prop = validate_config_change(
                    conn, guild, key, value, is_admin=is_server_admin(member)
                )
        except ValueError as e:
            return f"Rejected: {e}"
        return _queue(prop)

    def _propose_grant(grant_name: str, field: str, value: str) -> str:
        if not can_see_config(member):  # defense in depth
            return "Rejected: only server admins can change settings."
        try:
            with open_db(db_path) as conn:
                prop = validate_grant_role_change(
                    conn, guild, grant_name, field, value,
                    is_admin=is_server_admin(member),
                )
        except ValueError as e:
            return f"Rejected: {e}"
        return _queue(prop)

    # Only offer the grant tool to a full admin — every field on it decides who
    # ends up with a role, so a Manage Server asker would only be refused.
    admin = is_server_admin(member)
    grant_names: list[str] = []
    if admin:
        try:
            with open_db(db_path) as conn:
                grant_names = sorted(get_grant_roles(conn, guild.id))
        except Exception:
            log.exception("advisor: couldn't list grant roles for guild %s", guild.id)

    return AdvisorTools(
        feature_keys=FEATURE_KEYS,
        fetch_settings=_fetch,
        fetch_gaps=_gaps,
        propose_change=_propose,
        propose_grant=_propose_grant if grant_names else None,
        grant_names=grant_names,
        is_admin=admin,
    )


class _ApplyConfigView(discord.ui.View):
    """One Apply button per queued proposal. The reply is ephemeral, so only
    the asker can click — but each click still re-checks their permissions and
    re-validates the change before writing."""

    def __init__(
        self, db_path: Path, guild: discord.Guild, proposals: list[ConfigProposal]
    ) -> None:
        super().__init__(timeout=600)
        self._db_path = db_path
        self._guild = guild
        for prop in proposals[:_MAX_PROPOSALS]:
            btn: discord.ui.Button = discord.ui.Button(
                style=discord.ButtonStyle.success,
                label=f"Apply: {prop.display}"[:80],
            )
            btn.callback = self._make_callback(btn, prop)
            self.add_item(btn)

    def _make_callback(self, btn: discord.ui.Button, prop: ConfigProposal):
        async def _apply(interaction: discord.Interaction) -> None:
            member = interaction.user
            if not (
                isinstance(member, discord.Member) and can_see_config(member)
            ):
                await interaction.response.send_message(
                    "Only server admins can apply settings changes.", ephemeral=True
                )
                return
            try:
                # admin_only settings are re-checked against the clicker, not
                # the asker — the reply is ephemeral, but the gate shouldn't
                # depend on that being true.
                apply_config_change(
                    self._db_path, self._guild, prop,
                    is_admin=is_server_admin(member),
                )
            except ValueError as e:
                btn.disabled = True
                btn.style = discord.ButtonStyle.secondary
                btn.label = f"Failed: {e}"[:80]
                await interaction.response.edit_message(view=self)
                return
            log.info(
                "%s applied advisor proposal in guild %s: %s",
                member.display_name, self._guild.id, prop.display,
            )
            btn.disabled = True
            btn.label = f"✅ Applied: {prop.display}"[:80]
            await interaction.response.edit_message(view=self)

        return _apply


class AdvisorCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="ask",
        description="Ask the server assistant how to use the server — games, commands, settings.",
    )
    @app_commands.describe(question="What do you want to know how to do?")
    @app_commands.checks.cooldown(1, 12.0, key=lambda i: i.user.id)
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
        log.info("%s used /ask: %.80s", interaction.user.display_name, question)
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        model = MODEL
        assistant_name = DEFAULT_ASSISTANT_NAME
        guild_context: str | None = None
        tools: AdvisorTools | None = None
        proposals: list[ConfigProposal] = []
        if guild is not None:
            db_path = self.ctx.db_path
            member = (
                interaction.user
                if isinstance(interaction.user, discord.Member)
                else None
            )
            with open_db(db_path) as conn:
                # Staff asks get the stronger model whether or not live context
                # is on — the tiering is about answer quality, not context.
                model = resolve_advisor_model(conn, guild.id, staff=is_staff(member))
                assistant_name = resolve_assistant_name_conn(conn, guild.id)
                context_on = get_advisor_context_enabled(conn, guild.id)
                tools_on = get_advisor_tools_enabled(conn, guild.id)
            if context_on:
                if tools_on and member is not None and can_see_config(member):
                    tools = _make_tools(guild, member, db_path, proposals)
                # Tools replace the inline settings dump; the rest of the
                # context (who's asking, channels, pins, docs) stays inline.
                guild_context = build_asker_context(
                    guild, member, db_path, include_config=tools is None
                )

        result = await answer_advisor(
            question, model=model, guild_context=guild_context, tools=tools,
            assistant_name=assistant_name,
        )
        answer = result.answer
        if len(answer) > _MAX_DESC:
            answer = answer[:_MAX_DESC].rstrip() + "…"

        color = (
            await resolve_accent_color(self.ctx.db_path, interaction.guild)
            if interaction.guild
            else None
        )
        embed = discord.Embed(
            title=f"🤖 {assistant_name}",
            description=answer,
            color=color,
        )
        embed.set_footer(
            text=f"{assistant_name} • grounded in the server guide, not always perfect"
        )
        view = (
            _ApplyConfigView(self.ctx.db_path, guild, proposals)
            if proposals and guild is not None
            else discord.utils.MISSING
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"❌ Give me a sec — try again in {error.retry_after:.0f}s."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        log.exception("Unexpected /ask error", exc_info=error)


async def setup(bot: Bot) -> None:
    await bot.add_cog(AdvisorCog(bot, bot.ctx))
