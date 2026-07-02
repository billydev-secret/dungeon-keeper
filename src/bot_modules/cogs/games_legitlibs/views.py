import json
import logging
import uuid

import discord
from bot_modules.games.constants import HOW_TO_PLAY, GAME_ICONS

log = logging.getLogger(__name__)
_ICON = GAME_ICONS["legitlibs"]


def _is_host_or_mod(interaction: discord.Interaction, host_id: int) -> bool:
    if interaction.user.id == host_id:
        return True
    if interaction.guild:
        perms = interaction.user.guild_permissions
        return perms.administrator or perms.manage_guild
    return False


class _CancelConfirmView(discord.ui.View):
    """Two-step confirmation to avoid accidental round destruction."""

    def __init__(self, on_confirm):
        super().__init__(timeout=60)
        self._on_confirm = on_confirm

    @discord.ui.button(label="Yes, cancel round", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(content="🛑 Cancelling…", view=self)
        except discord.HTTPException:
            pass
        await self._on_confirm(interaction)

    @discord.ui.button(label="Keep playing", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancel aborted.", view=None)


class JoinView(discord.ui.View):
    """Lobby view: join, start (host only), cancel (host/mod), how-to-play."""

    def __init__(self, game_id: str, host_id: int, db, bot, on_start_callback, on_cancel_callback):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self._on_start = on_start_callback
        self._on_cancel = on_cancel_callback

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="ml_join", row=0)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await self._on_start(interaction, action="join")

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="ml_leave", row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await self._on_start(interaction, action="leave")

    @discord.ui.button(label="▶ Start", style=discord.ButtonStyle.primary, custom_id="ml_start", row=0)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not _is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("Only the host or a mod can start the round.", ephemeral=True)
            return
        await self._on_start(interaction, action="start")

    @discord.ui.button(label="✕ Cancel", style=discord.ButtonStyle.secondary, custom_id="ml_cancel_lobby", row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not _is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("Only the host or a mod can cancel.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Cancel this round? The lobby will close.",
            view=_CancelConfirmView(self._on_cancel),
            ephemeral=True,
        )

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="ml_htp_lobby", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY.get("legitlibs", ""), ephemeral=True)


class QuiplashFillView(discord.ui.View):
    """Fill phase view: submit button + cancel. Updated with submission counter."""

    def __init__(self, game_id: str, host_id: int, db, bot, on_submit_callback, on_cancel_callback):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self._on_submit = on_submit_callback
        self._on_cancel = on_cancel_callback

    @discord.ui.button(label="📝 Submit Fills", style=discord.ButtonStyle.primary, custom_id="ml_submit_fills", row=0)
    async def submit_fills(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await self._on_submit(interaction)

    @discord.ui.button(label="✕ Cancel", style=discord.ButtonStyle.secondary, custom_id="ml_cancel_fill", row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not _is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("Only the host or a mod can cancel.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Cancel this round? Players' in-progress fills will be lost.",
            view=_CancelConfirmView(self._on_cancel),
            ephemeral=True,
        )

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="ml_htp_fill", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY.get("legitlibs", ""), ephemeral=True)


class ClassicFillView(discord.ui.View):
    """Classic round 1 fill phase: Submit button + host-only Cancel + How-to-Play."""

    def __init__(self, game_id: str, host_id: int, db, bot,
                 on_submit_callback, on_cancel_callback):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self._on_submit = on_submit_callback
        self._on_cancel = on_cancel_callback

    @discord.ui.button(label="📝 Submit Fills", style=discord.ButtonStyle.primary,
                       custom_id="ml_cl_submit", row=0)
    async def submit_fills(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label,
                 interaction.channel.name if interaction.channel else "unknown")
        await self._on_submit(interaction)

    @discord.ui.button(label="✕ Cancel", style=discord.ButtonStyle.secondary,
                       custom_id="ml_cl_cancel_fill", row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label,
                 interaction.channel.name if interaction.channel else "unknown")
        if not _is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message(
                "Only the host or a mod can cancel.", ephemeral=True)
            return
        await self._on_cancel(interaction)

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary,
                       custom_id="ml_cl_htp_fill", row=1)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label,
                 interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(
            HOW_TO_PLAY.get("legitlibs", ""), ephemeral=True)


class ClassicRescueView(discord.ui.View):
    """Rescue claim window: Volunteer button + host-only Cancel."""

    def __init__(self, game_id: str, host_id: int, db, bot,
                 on_volunteer_callback, on_cancel_callback):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self._on_volunteer = on_volunteer_callback
        self._on_cancel = on_cancel_callback

    @discord.ui.button(label="🙋 Volunteer", style=discord.ButtonStyle.success,
                       custom_id="ml_cl_volunteer", row=0)
    async def volunteer(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label,
                 interaction.channel.name if interaction.channel else "unknown")
        await self._on_volunteer(interaction)

    @discord.ui.button(label="✕ Cancel", style=discord.ButtonStyle.secondary,
                       custom_id="ml_cl_cancel_rescue", row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label,
                 interaction.channel.name if interaction.channel else "unknown")
        if not _is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message(
                "Only the host or a mod can cancel.", ephemeral=True)
            return
        await self._on_cancel(interaction)


class ClassicRescueFillView(discord.ui.View):
    """Rescue fill round: Submit button (rescue assignees only) + host-only Cancel."""

    def __init__(self, game_id: str, host_id: int, db, bot,
                 on_submit_callback, on_cancel_callback):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self._on_submit = on_submit_callback
        self._on_cancel = on_cancel_callback

    @discord.ui.button(label="📝 Submit Fills", style=discord.ButtonStyle.primary,
                       custom_id="ml_cl_rescue_submit", row=0)
    async def submit_fills(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label,
                 interaction.channel.name if interaction.channel else "unknown")
        await self._on_submit(interaction)

    @discord.ui.button(label="✕ Cancel", style=discord.ButtonStyle.secondary,
                       custom_id="ml_cl_cancel_rescue_fill", row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label,
                 interaction.channel.name if interaction.channel else "unknown")
        if not _is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message(
                "Only the host or a mod can cancel.", ephemeral=True)
            return
        await self._on_cancel(interaction)


class ReportView(discord.ui.View):
    """Persistent view attached to reveal output so players can flag bad content."""

    def __init__(self, db, game_id: str, snapshot: dict):
        super().__init__(timeout=None)
        self.db = db
        self.game_id = game_id
        # snapshot = {"title": str, "body": str, "reveals": [(name, fills_dict, filled_body), ...]}
        self.snapshot = snapshot

    @discord.ui.button(label="⚠️ Report", style=discord.ButtonStyle.secondary)
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_modal(_ReportModal(self.db, self.game_id, self.snapshot))


class _ReportModal(discord.ui.Modal):
    reason = discord.ui.TextInput(
        label="What's wrong? (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
        placeholder="e.g. targeted harassment, slur, real-world PII…",
    )

    def __init__(self, db, game_id: str, snapshot: dict):
        super().__init__(title="Report this round")
        self.db = db
        self.game_id = game_id
        self.snapshot = snapshot

    async def on_submit(self, interaction: discord.Interaction):
        report_id = str(uuid.uuid4())
        content = json.dumps({
            "title": self.snapshot.get("title"),
            "body": self.snapshot.get("body"),
            "reveals": [
                {"name": name, "fills": fills, "filled": filled}
                for name, fills, filled in self.snapshot.get("reveals", [])
            ],
            "reason": str(self.reason.value or "").strip(),
        })
        try:
            await self.db.execute(
                "INSERT INTO legitlibs_reports (report_id, game_id, submission_content, reporter_id) VALUES (?, ?, ?, ?)",
                (report_id, self.game_id, content, interaction.user.id),
            )
            log.info("%s filed LegitLibs report %s for game %s", interaction.user.display_name, report_id, self.game_id)
            await interaction.response.send_message(
                "🛡️ Thanks — mods will review this round.", ephemeral=True
            )
        except Exception as e:
            log.exception("Failed to store LegitLibs report: %s", e)
            await interaction.response.send_message(
                "Couldn't file that report — please ping a mod directly.", ephemeral=True
            )
