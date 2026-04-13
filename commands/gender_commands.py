"""Gender classification commands — mods classify members for NSFW analytics.

All output is ephemeral (admin-only, invisible to regular members).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from services.gender_service import (
    VALID_GENDERS,
    get_gender,
    get_unclassified_member_ids,
    set_gender,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot


# ---------------------------------------------------------------------------
# Interactive classification view — walks through members one at a time
# ---------------------------------------------------------------------------


class _ClassifyView(discord.ui.View):
    """Ephemeral view that shows one unclassified member with gender buttons."""

    def __init__(
        self,
        ctx: AppContext,
        guild: discord.Guild,
        unclassified_ids: list[int],
        index: int,
        invoker_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self._ctx = ctx
        self._guild = guild
        self._unclassified = unclassified_ids
        self._index = index
        self._invoker_id = invoker_id
        self._total = len(unclassified_ids)

        style_map = {
            "male": discord.ButtonStyle.primary,
            "female": discord.ButtonStyle.danger,
            "nonbinary": discord.ButtonStyle.secondary,
        }
        for gender in VALID_GENDERS:
            btn: discord.ui.Button[_ClassifyView] = discord.ui.Button(
                label=gender.capitalize(),
                style=style_map[gender],
                row=0,
            )
            btn.callback = self._make_callback(gender)  # type: ignore[assignment]
            self.add_item(btn)

        skip_btn: discord.ui.Button[_ClassifyView] = discord.ui.Button(
            label="Skip",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        skip_btn.callback = self._on_skip  # type: ignore[assignment]
        self.add_item(skip_btn)

        stop_btn: discord.ui.Button[_ClassifyView] = discord.ui.Button(
            label="Done",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        stop_btn.callback = self._on_stop  # type: ignore[assignment]
        self.add_item(stop_btn)

    def _current_member(self) -> discord.Member | None:
        if self._index >= len(self._unclassified):
            return None
        return self._guild.get_member(self._unclassified[self._index])

    def _build_embed(self) -> discord.Embed:
        member = self._current_member()
        if member is None:
            return discord.Embed(
                description="No more members to classify.", color=discord.Color.green()
            )
        remaining = self._total - self._index
        account_age_days = (discord.utils.utcnow() - member.created_at).days
        joined_days = (
            (discord.utils.utcnow() - member.joined_at).days
            if member.joined_at
            else "?"
        )
        embed = discord.Embed(
            title=f"Classify Member ({remaining} remaining)",
            description=(
                f"**{member.display_name}**\n"
                f"Account age: {account_age_days}d — Joined: {joined_days}d ago"
            ),
            color=discord.Color.blurple(),
        )
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        return embed

    def _make_callback(self, gender: str):
        async def _callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self._invoker_id:
                await interaction.response.defer()
                return
            member = self._current_member()
            if member is None:
                await interaction.response.edit_message(
                    embed=discord.Embed(
                        description="No more members to classify.",
                        color=discord.Color.green(),
                    ),
                    view=None,
                )
                return
            with self._ctx.open_db() as conn:
                set_gender(conn, self._guild.id, member.id, gender, interaction.user.id)
            self._index += 1
            if self._index >= len(self._unclassified):
                await interaction.response.edit_message(
                    embed=discord.Embed(
                        description="All done — no more unclassified members.",
                        color=discord.Color.green(),
                    ),
                    view=None,
                )
            else:
                await interaction.response.edit_message(
                    embed=self._build_embed(), view=self
                )

        return _callback

    async def _on_skip(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        self._index += 1
        if self._index >= len(self._unclassified):
            await interaction.response.edit_message(
                embed=discord.Embed(
                    description="Reached the end of the list.",
                    color=discord.Color.green(),
                ),
                view=None,
            )
        else:
            await interaction.response.edit_message(
                embed=self._build_embed(), view=self
            )

    async def _on_stop(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        classified = self._index
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=f"Stopped — classified {classified} member(s) this session.",
                color=discord.Color.green(),
            ),
            view=None,
        )
        self.stop()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def register_gender_commands(bot: Bot, ctx: AppContext) -> None:
    gender_group = app_commands.Group(
        name="gender",
        description="Tag members by gender for the NSFW analytics breakdown.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @gender_group.command(
        name="set", description="Set or change a member's gender tag."
    )
    @app_commands.describe(member="Member to classify.", gender="Gender to assign.")
    @app_commands.choices(
        gender=[
            app_commands.Choice(name="Male", value="male"),
            app_commands.Choice(name="Female", value="female"),
            app_commands.Choice(name="Non-binary", value="nonbinary"),
        ]
    )
    async def gender_set(
        interaction: discord.Interaction,
        member: discord.Member,
        gender: app_commands.Choice[str],
    ):
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        with ctx.open_db() as conn:
            prev = get_gender(conn, guild_id, member.id)
            set_gender(conn, guild_id, member.id, gender.value, interaction.user.id)

        if prev:
            await interaction.response.send_message(
                f"{member.mention} updated from **{prev.capitalize()}** to **{gender.name}**.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{member.mention} classified as **{gender.name}**.",
                ephemeral=True,
            )

    @gender_group.command(
        name="check", description="See a member's current gender tag."
    )
    @app_commands.describe(member="Member to check.")
    async def gender_check(interaction: discord.Interaction, member: discord.Member):
        guild_id = interaction.guild_id
        if guild_id is None:
            return
        with ctx.open_db() as conn:
            gender = get_gender(conn, guild_id, member.id)

        if gender:
            await interaction.response.send_message(
                f"{member.mention} is classified as **{gender.capitalize()}**.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{member.mention} has not been classified yet.",
                ephemeral=True,
            )

    @gender_group.command(
        name="classify",
        description="Step through unclassified members one by one with buttons.",
    )
    async def gender_classify(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        all_ids = [m.id for m in guild.members if not m.bot]

        with ctx.open_db() as conn:
            unclassified = get_unclassified_member_ids(conn, guild.id, all_ids)

        if not unclassified:
            await interaction.response.send_message(
                "All members have been classified.", ephemeral=True
            )
            return

        view = _ClassifyView(ctx, guild, unclassified, 0, interaction.user.id)
        await interaction.response.send_message(
            embed=view._build_embed(),
            view=view,
            ephemeral=True,
        )

    bot.tree.add_command(gender_group)
