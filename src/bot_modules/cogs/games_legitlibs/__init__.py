import os
import logging

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games.utils.game_manager import check_allowed_channel, get_active_game
from .data import seed_templates_from_file, HEAT_LABELS
from .modes.quiplash import run_quiplash
from .modes.classic import run_classic

log = logging.getLogger(__name__)

_SEED_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "templates_seed.json")

# Kill-switch flag — set to True by /legitlibs-admin killswitch
_MODULE_DISABLED = False


class LegitLibsCog(commands.Cog, name="LegitLibsCog"):
    def __init__(self, bot):
        self.bot = bot
        self._game_canceled: set[str] = set()

    @property
    def db(self):
        return self.bot.games_db

    async def cog_load(self):
        await seed_templates_from_file(self.db, _SEED_PATH, author_id=0)
        log.info("LegitLibsCog loaded.")

    # ── /legitlibs ─────────────────────────────────────────────────────────────
    @app_commands.command(name="legitlibs", description="Start a LegitLibs round!")
    @app_commands.describe(
        mode="Game mode: classic (default), quiplash, or hotseat",
        tier="Heat tier 1–4 (1=Flirty, 2=Spicy, 3=Filthy, 4=Unhinged). Default: 2",
        template_id="Optional: use a specific template by ID",
        tag="Optional: filter templates by tag",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Classic (sequential fill)", value="classic"),
        app_commands.Choice(name="Quiplash (everyone fills, all revealed)", value="quiplash"),
        app_commands.Choice(name="Hot Seat (author picks best fills)", value="hotseat"),
    ])
    @app_commands.choices(tier=[
        app_commands.Choice(name="1 — Flirty 🌶️", value=1),
        app_commands.Choice(name="2 — Spicy 🌶️🌶️", value=2),
        app_commands.Choice(name="3 — Filthy 🌶️🌶️🌶️", value=3),
        app_commands.Choice(name="4 — Unhinged 💀", value=4),
    ])
    async def legitlibs(
        self,
        interaction: discord.Interaction,
        mode: str = "classic",
        tier: int = 2,
        template_id: str = None,
        tag: str = None,
    ):
        global _MODULE_DISABLED
        log.info("%s used /legitlibs in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")

        if _MODULE_DISABLED:
            await interaction.response.send_message(
                "LegitLibs is currently disabled. Ask an admin to re-enable it.", ephemeral=True
            )
            return

        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/config allow-channel`.",
                ephemeral=True,
            )
            return

        existing = await get_active_game(self.db, interaction.channel_id)
        if existing and existing["game_type"] == "legitlibs":
            await interaction.response.send_message(
                "A LegitLibs round is already in progress here. Cancel it first.", ephemeral=True
            )
            return

        await interaction.response.defer()

        if mode == "quiplash":
            await run_quiplash(self, interaction, tier, template_id, tag)
        elif mode == "classic":
            await run_classic(self, interaction, tier, template_id, tag)
        elif mode == "hotseat":
            await interaction.followup.send("Hot Seat mode coming soon!", ephemeral=True)
        else:
            await interaction.followup.send("Unknown mode.", ephemeral=True)

    # ── /legitlibs-admin ───────────────────────────────────────────────────────
    legitlibs_admin = app_commands.Group(
        name="legitlibs-admin",
        description="LegitLibs mod/admin commands",
    )

    @legitlibs_admin.command(name="reload", description="Reload the template library from the database")
    async def reload_templates(self, interaction: discord.Interaction):
        log.info("%s used /legitlibs-admin reload in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if interaction.guild:
            perms = interaction.user.guild_permissions
            if not (perms.administrator or perms.manage_guild):
                await interaction.response.send_message("Mods only.", ephemeral=True)
                return
        await seed_templates_from_file(self.db, _SEED_PATH, author_id=interaction.user.id)
        await interaction.response.send_message("✅ Template library reloaded.", ephemeral=True)

    @legitlibs_admin.command(name="cap-tier", description="Set the maximum heat tier allowed in this channel")
    @app_commands.describe(tier="Maximum tier (1–4)")
    @app_commands.choices(tier=[
        app_commands.Choice(name="1 — Flirty", value=1),
        app_commands.Choice(name="2 — Spicy", value=2),
        app_commands.Choice(name="3 — Filthy", value=3),
        app_commands.Choice(name="4 — Unhinged (no cap)", value=4),
    ])
    async def cap_tier(self, interaction: discord.Interaction, tier: int):
        log.info("%s used /legitlibs-admin cap-tier %d in #%s", interaction.user.display_name, tier, interaction.channel.name if interaction.channel else "unknown")
        if interaction.guild:
            perms = interaction.user.guild_permissions
            if not (perms.administrator or perms.manage_guild):
                await interaction.response.send_message("Mods only.", ephemeral=True)
                return
        await self.db.execute(
            """
            INSERT INTO legitlibs_channel_config (channel_id, max_tier, set_by)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET max_tier = excluded.max_tier, set_by = excluded.set_by, set_at = CURRENT_TIMESTAMP
            """,
            (interaction.channel_id, tier, interaction.user.id),
        )
        await interaction.response.send_message(
            f"✅ Tier cap for this channel set to {tier} ({HEAT_LABELS[tier]}).", ephemeral=True
        )

    @legitlibs_admin.command(name="preview", description="Preview a template without starting a game")
    @app_commands.describe(template_id="Template ID to preview")
    async def preview_template(self, interaction: discord.Interaction, template_id: str):
        log.info("%s used /legitlibs-admin preview in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if interaction.guild:
            perms = interaction.user.guild_permissions
            if not (perms.administrator or perms.manage_guild):
                await interaction.response.send_message("Mods only.", ephemeral=True)
                return
        from .data import get_template_for_preview
        template = await get_template_for_preview(self.db, template_id)
        if not template:
            await interaction.response.send_message(f"No template found with ID `{template_id}`.", ephemeral=True)
            return

        blanks_desc = "\n".join(
            f"  `{{{b['id']}}}` → {b['type']}" for b in template["blanks"]
        )
        body_preview = template["body"][:500] + ("…" if len(template["body"]) > 500 else "")
        await interaction.response.send_message(
            f"**{template['title']}** (tier {template['tier']}, {template['status']})\n"
            f"Tags: {', '.join(template['tags']) or 'none'}\n\n"
            f"```{body_preview}```\n"
            f"**Blanks:**\n{blanks_desc}",
            ephemeral=True,
        )

    @legitlibs_admin.command(name="list", description="List available templates")
    @app_commands.describe(tier="Filter by tier", tag="Filter by tag")
    async def list_templates(self, interaction: discord.Interaction, tier: int = None, tag: str = None):
        log.info("%s used /legitlibs-admin list in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if interaction.guild:
            perms = interaction.user.guild_permissions
            if not (perms.administrator or perms.manage_guild):
                await interaction.response.send_message("Mods only.", ephemeral=True)
                return

        query = "SELECT template_id, title, tier, status, tags FROM legitlibs_templates WHERE status != 'archived'"
        params: list = []
        if tier is not None:
            query += " AND tier = ?"
            params.append(tier)
        query += " ORDER BY tier, title"

        rows = await self.db.fetchall(query, params)
        if not rows:
            await interaction.response.send_message("No templates found.", ephemeral=True)
            return

        import json as _json
        lines = []
        for r in rows[:25]:
            tags = _json.loads(r["tags"])
            if tag and tag.lower() not in [t.lower() for t in tags]:
                continue
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            lines.append(f"`{r['template_id']}` T{r['tier']} **{r['title']}**{tag_str} ({r['status']})")

        if not lines:
            await interaction.response.send_message("No templates match those filters.", ephemeral=True)
            return

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @legitlibs_admin.command(name="killswitch", description="Stop all active LegitLibs rounds and disable the module")
    async def killswitch(self, interaction: discord.Interaction):
        log.info("%s used /legitlibs-admin killswitch in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if interaction.guild:
            if not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("Admins only.", ephemeral=True)
                return
        global _MODULE_DISABLED
        _MODULE_DISABLED = True

        rows = await self.db.fetchall("SELECT game_id FROM games_active_games WHERE game_type = 'legitlibs'")
        from bot_modules.games.utils.game_manager import end_game as _end_game
        for row in rows:
            gid = row["game_id"]
            self._game_canceled.add(gid)
            await _end_game(self.db, gid)
            if gid in self.bot.active_views:
                v = self.bot.active_views.pop(gid)
                v.stop()

        await interaction.response.send_message(
            f"🛑 Kill-switch engaged. {len(rows)} active rounds stopped. LegitLibs is now disabled.",
            ephemeral=True,
        )

    @legitlibs_admin.command(name="enable", description="Re-enable LegitLibs after a killswitch")
    async def enable_module(self, interaction: discord.Interaction):
        log.info("%s used /legitlibs-admin enable in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if interaction.guild:
            if not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("Admins only.", ephemeral=True)
                return
        global _MODULE_DISABLED
        _MODULE_DISABLED = False
        await interaction.response.send_message("✅ LegitLibs re-enabled.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(LegitLibsCog(bot))
