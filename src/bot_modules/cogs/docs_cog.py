"""Docs cog — post and sync single-source markdown documents as embeds.

Authoring lives on the dashboard (Config → Docs); this cog is the Discord
surface for *placing* a doc into channels and re-syncing it. Editing a doc's
markdown anywhere re-renders every channel it's posted in.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.docs import db as docs_db
from bot_modules.docs import sync as docs_sync

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot


def _summarize(results: list[docs_sync.SyncResult]) -> str:
    if not results:
        return "This doc isn't posted anywhere yet — use `/docs post` first."
    lines = []
    for r in results:
        if r.status == "missing_channel":
            lines.append(f"• <#{r.channel_id}> — ⚠️ channel unavailable")
        elif r.status == "forbidden":
            lines.append(f"• <#{r.channel_id}> — 🚫 {r.detail}")
        elif r.status == "error":
            lines.append(f"• <#{r.channel_id}> — ❌ {r.detail}")
        else:
            parts = []
            if r.created:
                parts.append(f"{r.created} posted")
            if r.edited:
                parts.append(f"{r.edited} updated")
            if r.deleted:
                parts.append(f"{r.deleted} removed")
            lines.append(f"• <#{r.channel_id}> — {', '.join(parts) or 'up to date'}")
    return "\n".join(lines)


class DocsCog(commands.Cog):
    docs = app_commands.Group(
        name="docs",
        description="Post and sync single-source markdown documents.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    def __init__(self, bot: "Bot", ctx: "AppContext") -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    # ── shared helpers ───────────────────────────────────────────────

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return False
        return True

    async def _load_doc(self, guild_id: int, doc_key: str) -> dict | None:
        def _q() -> dict | None:
            with self.ctx.open_db() as conn:
                return docs_db.get_doc(conn, guild_id, doc_key)

        return await asyncio.to_thread(_q)

    async def _doc_key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        guild_id = interaction.guild.id if interaction.guild else self.ctx.guild_id

        def _q() -> list[dict]:
            with self.ctx.open_db() as conn:
                return docs_db.list_docs(conn, guild_id)

        docs = await asyncio.to_thread(_q)
        cur = current.lower()
        out: list[app_commands.Choice[str]] = []
        for d in docs:
            if cur in d["doc_key"].lower() or cur in (d["title"] or "").lower():
                label = d["doc_key"]
                if d["title"]:
                    label = f"{d['doc_key']} — {d['title']}"
                out.append(app_commands.Choice(name=label[:100], value=d["doc_key"]))
            if len(out) >= 25:
                break
        return out

    # ── /docs post ───────────────────────────────────────────────────

    @docs.command(name="post", description="Post a doc into a channel (and keep it synced).")
    @app_commands.describe(
        doc_key="Which doc to post.",
        channel="Channel to post it in (defaults to here).",
    )
    @app_commands.autocomplete(doc_key=_doc_key_autocomplete)
    async def post(
        self,
        interaction: discord.Interaction,
        doc_key: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self._guard(interaction):
            return
        guild = interaction.guild
        target = channel or interaction.channel
        if guild is None or not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Pick a text channel to post into.", ephemeral=True
            )
            return

        doc = await self._load_doc(guild.id, doc_key)
        if doc is None:
            await interaction.response.send_message(
                f"No doc named `{doc_key}`. Create it on the dashboard first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await docs_sync.post_doc(self.ctx, guild, doc, target.id)
        await interaction.followup.send(
            f"**{doc['title'] or doc_key}** → {_summarize([result])}", ephemeral=True
        )

    # ── /docs sync ───────────────────────────────────────────────────

    @docs.command(name="sync", description="Re-render a doc everywhere it's posted.")
    @app_commands.describe(doc_key="Doc to sync (leave blank to sync all docs).")
    @app_commands.autocomplete(doc_key=_doc_key_autocomplete)
    async def sync(
        self, interaction: discord.Interaction, doc_key: str | None = None
    ) -> None:
        if not await self._guard(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if doc_key:
            doc = await self._load_doc(guild.id, doc_key)
            if doc is None:
                await interaction.followup.send(
                    f"No doc named `{doc_key}`.", ephemeral=True
                )
                return
            results = await docs_sync.sync_doc(self.ctx, guild, doc)
            await interaction.followup.send(
                f"**{doc['title'] or doc_key}** synced:\n{_summarize(results)}",
                ephemeral=True,
            )
            return

        def _all() -> list[dict]:
            with self.ctx.open_db() as conn:
                return docs_db.list_docs(conn, guild.id)

        all_docs = await asyncio.to_thread(_all)
        if not all_docs:
            await interaction.followup.send("No docs to sync.", ephemeral=True)
            return
        lines = []
        for doc in all_docs:
            results = await docs_sync.sync_doc(self.ctx, guild, doc)
            if results:
                touched = sum(r.created + r.edited + r.deleted for r in results)
                bad = [r for r in results if r.status != "ok"]
                flag = " ⚠️" if bad else ""
                lines.append(
                    f"• **{doc['title'] or doc['doc_key']}** — {len(results)} place(s),"
                    f" {touched} message(s){flag}"
                )
        await interaction.followup.send(
            "Synced all docs:\n" + ("\n".join(lines) or "Nothing posted anywhere yet."),
            ephemeral=True,
        )

    # ── /docs unpost ─────────────────────────────────────────────────

    @docs.command(name="unpost", description="Remove a doc from a channel.")
    @app_commands.describe(doc_key="Doc to remove.", channel="Channel to remove it from.")
    @app_commands.autocomplete(doc_key=_doc_key_autocomplete)
    async def unpost(
        self,
        interaction: discord.Interaction,
        doc_key: str,
        channel: discord.TextChannel,
    ) -> None:
        if not await self._guard(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        doc = await self._load_doc(guild.id, doc_key)
        if doc is None:
            await interaction.response.send_message(
                f"No doc named `{doc_key}`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        removed = await docs_sync.unpost_doc(self.ctx, doc, channel.id)
        if removed:
            await interaction.followup.send(
                f"Removed **{doc['title'] or doc_key}** from {channel.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"**{doc['title'] or doc_key}** wasn't posted in {channel.mention}.",
                ephemeral=True,
            )

    # ── /docs list ───────────────────────────────────────────────────

    @docs.command(name="list", description="List docs and where they're posted.")
    async def list_docs(self, interaction: discord.Interaction) -> None:
        if not await self._guard(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        def _q() -> list[tuple[dict, list[dict]]]:
            with self.ctx.open_db() as conn:
                docs = docs_db.list_docs(conn, guild.id)
                return [(d, docs_db.list_placements(conn, d["id"])) for d in docs]

        rows = await asyncio.to_thread(_q)
        if not rows:
            await interaction.response.send_message(
                "No docs yet — create one on the dashboard (Config → Docs).",
                ephemeral=True,
            )
            return
        lines = []
        for doc, placements in rows:
            where = (
                ", ".join(f"<#{p['channel_id']}>" for p in placements)
                if placements
                else "_not posted_"
            )
            lines.append(f"**{doc['title'] or doc['doc_key']}** (`{doc['doc_key']}`) → {where}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: "Bot") -> None:
    await bot.add_cog(DocsCog(bot, bot.ctx))
