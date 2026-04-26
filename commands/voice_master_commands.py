"""Voice Master — persistent panel buttons, modals, helpers."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord

from services.moderation import write_audit
from services.voice_master_service import (
    MAX_NAME_LEN,
    ActiveChannel,
    EDIT_WINDOW_S,
    add_blocked,
    add_trusted,
    can_edit,
    delete_profile,
    get_active_channel,
    get_owned_channel,
    list_name_blocklist,
    load_voice_master_config,
    name_is_blocked,
    record_edit_in_db,
    set_owner,
    set_voice_master_config_value,
    try_dm,
    update_profile_field,
)

if TYPE_CHECKING:
    from app_context import AppContext

log = logging.getLogger("dungeonkeeper.voice_master.commands")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx_from_interaction(interaction: discord.Interaction) -> "AppContext | None":
    return getattr(interaction.client, "_vm_ctx", None)


async def _resolve_owned_channel(
    interaction: discord.Interaction,
) -> tuple[discord.VoiceChannel, ActiveChannel] | None:
    """Find the clicker's active voice channel, or send an ephemeral error.

    Returns ``(channel, active_row)`` on success.
    """
    ctx = _ctx_from_interaction(interaction)
    if ctx is None or interaction.guild is None:
        await _ephemeral(
            interaction,
            "Voice Master isn't configured here.",
        )
        return None

    with ctx.open_db() as conn:
        row = get_owned_channel(conn, interaction.guild.id, interaction.user.id)
    if row is None:
        await _ephemeral(
            interaction,
            "You don't own a voice channel right now — join the Hub to create one.",
        )
        return None

    channel = interaction.guild.get_channel(row.channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        # The channel was deleted out from under us; the empty-grace task or
        # the next reconciliation will tidy the DB row.
        await _ephemeral(
            interaction,
            "Your voice channel is no longer available.",
        )
        return None
    return channel, row


async def _ephemeral(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def _defer_if_needed(interaction: discord.Interaction) -> None:
    """Acknowledge the interaction so we have 15 minutes for the slow path.

    Discord enforces a 3-second deadline between interaction receipt and the
    first response. Any helper that's about to call ``channel.edit`` or
    ``channel.set_permissions`` should defer first to stay safely inside that
    window. ``thinking=False`` keeps the UI silent (no "Bot is thinking…").
    """
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except discord.InteractionResponded:
            # Race between concurrent handlers — already deferred, that's fine.
            pass


def _check_edit_budget(
    row: ActiveChannel, *, now: float
) -> tuple[bool, float]:
    """Returns ``(allowed, retry_after_s)`` for a Discord channel edit."""
    return can_edit(now, row.last_edit_at_1, row.last_edit_at_2)


async def _gate_and_record_edit(
    interaction: discord.Interaction,
    row: ActiveChannel,
) -> bool:
    """Check the edit budget, replying ephemerally on rejection.

    On success, persists the new edit timestamp pair and returns True.
    """
    ctx = _ctx_from_interaction(interaction)
    if ctx is None:
        return False
    now = time.time()
    allowed, retry = _check_edit_budget(row, now=now)
    if not allowed:
        await _ephemeral(
            interaction,
            f"Discord limits voice channel edits to 2 per "
            f"{int(EDIT_WINDOW_S/60)} minutes — try again in {int(retry)}s.",
        )
        return False
    with ctx.open_db() as conn:
        record_edit_in_db(conn, row.channel_id, now=now)
    return True


# ---------------------------------------------------------------------------
# Lock / Unlock
# ---------------------------------------------------------------------------


async def _apply_lock(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    locked: bool,
) -> None:
    ctx = _ctx_from_interaction(interaction)
    if ctx is None:
        return
    await _defer_if_needed(interaction)
    everyone = channel.guild.default_role
    overwrite = channel.overwrites_for(everyone)
    overwrite.connect = False if locked else None
    try:
        await channel.set_permissions(
            everyone,
            overwrite=overwrite,
            reason=f"Voice Master: {'lock' if locked else 'unlock'} by owner",
        )
    except (discord.Forbidden, discord.HTTPException):
        await _ephemeral(interaction, "Couldn't update channel permissions.")
        return
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, channel.guild.id)
        if not cfg.disable_saves and "locked" in cfg.saveable_fields:
            update_profile_field(
                conn,
                channel.guild.id,
                row.owner_id,
                field="locked",
                value=locked,
            )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_channel_lock" if locked else "vm_channel_unlock",
            actor_id=interaction.user.id,
            extra={"channel_id": channel.id},
        )
    await _ephemeral(
        interaction, f"Channel **{'locked' if locked else 'unlocked'}**."
    )


async def _apply_rename(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    new_name: str,
) -> None:
    ctx = _ctx_from_interaction(interaction)
    if ctx is None:
        return
    new_name = new_name.strip()
    if not new_name:
        await _ephemeral(interaction, "Channel name can't be empty.")
        return
    if len(new_name) > MAX_NAME_LEN:
        new_name = new_name[:MAX_NAME_LEN]
    with ctx.open_db() as conn:
        patterns = list_name_blocklist(conn, channel.guild.id)
    if name_is_blocked(new_name, patterns):
        await _ephemeral(
            interaction, "That name matches a server-wide content filter — pick another."
        )
        return
    await _defer_if_needed(interaction)
    try:
        await channel.edit(
            name=new_name, reason="Voice Master: rename by owner"
        )
    except (discord.Forbidden, discord.HTTPException):
        await _ephemeral(interaction, "Couldn't rename the channel.")
        return
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, channel.guild.id)
        if not cfg.disable_saves and "name" in cfg.saveable_fields:
            update_profile_field(
                conn,
                channel.guild.id,
                row.owner_id,
                field="saved_name",
                value=new_name,
            )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_channel_rename",
            actor_id=interaction.user.id,
            extra={"channel_id": channel.id, "name": new_name},
        )
    await _ephemeral(interaction, f"Renamed to **{new_name}**.")


async def _reset_channel_overwrites(
    channel: discord.VoiceChannel, *, owner: discord.Member
) -> bool:
    """Wipe every per-member overwrite, leaving @everyone neutral + owner allowed."""
    everyone = channel.guild.default_role
    new_overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        everyone: discord.PermissionOverwrite(),
        owner: discord.PermissionOverwrite(view_channel=True, connect=True),
    }
    try:
        await channel.edit(
            overwrites=new_overwrites, reason="Voice Master: reset by owner"
        )
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def _apply_reset(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    also_profile: bool,
) -> None:
    ctx = _ctx_from_interaction(interaction)
    if ctx is None:
        return
    owner = (
        interaction.guild.get_member(row.owner_id) if interaction.guild else None
    )
    if owner is None:
        await _ephemeral(interaction, "Couldn't resolve channel owner.")
        return
    await _defer_if_needed(interaction)
    ok = await _reset_channel_overwrites(channel, owner=owner)
    if not ok:
        await _ephemeral(interaction, "Couldn't reset channel permissions.")
        return
    with ctx.open_db() as conn:
        if also_profile:
            delete_profile(conn, channel.guild.id, row.owner_id)
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_reset_profile" if also_profile else "vm_reset_channel",
            actor_id=interaction.user.id,
            extra={"channel_id": channel.id},
        )
    msg = (
        "Channel **and** saved profile reset."
        if also_profile
        else "Channel reset to defaults (your saved profile is unchanged)."
    )
    await _ephemeral(interaction, msg)


async def _apply_transfer(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    new_owner: discord.Member,
) -> None:
    ctx = _ctx_from_interaction(interaction)
    if ctx is None:
        return
    if new_owner.bot:
        await _ephemeral(interaction, "Can't transfer ownership to a bot.")
        return
    if new_owner.id == row.owner_id:
        await _ephemeral(interaction, "You're already the owner.")
        return
    if new_owner.voice is None or new_owner.voice.channel is None or new_owner.voice.channel.id != channel.id:
        await _ephemeral(
            interaction,
            "The new owner must currently be in the voice channel.",
        )
        return
    await _defer_if_needed(interaction)
    # Grant the new owner explicit access (so future locks/hides don't bite them).
    overwrite = channel.overwrites_for(new_owner)
    overwrite.connect = True
    overwrite.view_channel = True
    try:
        await channel.set_permissions(
            new_owner, overwrite=overwrite, reason="Voice Master: ownership transfer"
        )
    except (discord.Forbidden, discord.HTTPException):
        await _ephemeral(interaction, "Couldn't update channel permissions.")
        return
    with ctx.open_db() as conn:
        set_owner(conn, channel.id, new_owner.id)
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_transfer",
            actor_id=new_owner.id,
            target_id=row.owner_id,
            extra={"channel_id": channel.id},
        )
    await _ephemeral(
        interaction, f"Ownership transferred to {new_owner.mention}."
    )


async def _apply_invite(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    target: discord.Member,
    remember: bool,
) -> None:
    ctx = _ctx_from_interaction(interaction)
    if ctx is None:
        return
    if target.bot:
        await _ephemeral(interaction, "Can't invite bots.")
        return
    if target.id == row.owner_id:
        await _ephemeral(interaction, "You're already the owner.")
        return
    await _defer_if_needed(interaction)
    overwrite = channel.overwrites_for(target)
    overwrite.connect = True
    overwrite.view_channel = True
    try:
        await channel.set_permissions(
            target, overwrite=overwrite, reason="Voice Master: invite by owner"
        )
    except (discord.Forbidden, discord.HTTPException):
        await _ephemeral(interaction, "Couldn't update channel permissions.")
        return

    cap_evicted: int | None = None
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, channel.guild.id)
        if remember and not cfg.disable_saves and "trusted" in cfg.saveable_fields:
            _, cap_evicted = add_trusted(
                conn,
                channel.guild.id,
                row.owner_id,
                target.id,
                cap=cfg.trust_cap,
            )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_invite",
            actor_id=interaction.user.id,
            target_id=target.id,
            extra={"channel_id": channel.id, "remember": remember},
        )

    # DM the invitee with a clickable jump-into-channel link.
    join_url = (
        f"https://discord.com/channels/{channel.guild.id}/{channel.id}"
    )
    await try_dm(
        target,
        content=(
            f"You've been invited to **{channel.name}** by "
            f"{interaction.user.mention} in **{channel.guild.name}**.\n"
            f"{join_url}"
        ),
    )

    extra = ""
    if cap_evicted is not None:
        extra = f" (Trust list cap reached — removed <@{cap_evicted}>.)"
    word = "remembered" if remember else "invited"
    await _ephemeral(
        interaction, f"{target.mention} {word} for this channel.{extra}"
    )


async def _apply_kick(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    target: discord.Member,
    remember: bool,
) -> None:
    ctx = _ctx_from_interaction(interaction)
    if ctx is None:
        return
    if target.bot:
        await _ephemeral(interaction, "Can't kick bots.")
        return
    if target.id == row.owner_id:
        await _ephemeral(interaction, "You can't kick yourself — transfer ownership first.")
        return
    await _defer_if_needed(interaction)
    overwrite = channel.overwrites_for(target)
    overwrite.connect = False
    try:
        await channel.set_permissions(
            target, overwrite=overwrite, reason="Voice Master: kick by owner"
        )
    except (discord.Forbidden, discord.HTTPException):
        await _ephemeral(interaction, "Couldn't update channel permissions.")
        return

    # Disconnect them if they're currently in the channel.
    if target.voice and target.voice.channel and target.voice.channel.id == channel.id:
        try:
            await target.move_to(None, reason="Voice Master: kicked by owner")
        except (discord.Forbidden, discord.HTTPException):
            pass

    cap_evicted: int | None = None
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, channel.guild.id)
        if remember and not cfg.disable_saves and "blocked" in cfg.saveable_fields:
            _, cap_evicted = add_blocked(
                conn,
                channel.guild.id,
                row.owner_id,
                target.id,
                cap=cfg.block_cap,
            )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_kick",
            actor_id=interaction.user.id,
            target_id=target.id,
            extra={"channel_id": channel.id, "remember": remember},
        )

    extra = ""
    if cap_evicted is not None:
        extra = f" (Block list cap reached — removed <@{cap_evicted}>.)"
    word = "blocked permanently" if remember else "kicked"
    await _ephemeral(interaction, f"{target.mention} {word}.{extra}")


async def _apply_limit(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    new_limit: int,
) -> None:
    ctx = _ctx_from_interaction(interaction)
    if ctx is None:
        return
    if new_limit < 0 or new_limit > 99:
        await _ephemeral(interaction, "User limit must be between 0 and 99 (0 = no cap).")
        return
    await _defer_if_needed(interaction)
    try:
        await channel.edit(
            user_limit=new_limit, reason="Voice Master: limit by owner"
        )
    except (discord.Forbidden, discord.HTTPException):
        await _ephemeral(interaction, "Couldn't update the user limit.")
        return
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, channel.guild.id)
        if not cfg.disable_saves and "limit" in cfg.saveable_fields:
            update_profile_field(
                conn,
                channel.guild.id,
                row.owner_id,
                field="saved_limit",
                value=new_limit,
            )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_channel_limit",
            actor_id=interaction.user.id,
            extra={"channel_id": channel.id, "limit": new_limit},
        )
    await _ephemeral(
        interaction,
        f"User limit set to **{new_limit if new_limit > 0 else 'no cap'}**.",
    )


async def _apply_hide(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    hidden: bool,
) -> None:
    ctx = _ctx_from_interaction(interaction)
    if ctx is None:
        return
    await _defer_if_needed(interaction)
    everyone = channel.guild.default_role
    overwrite = channel.overwrites_for(everyone)
    overwrite.view_channel = False if hidden else None
    try:
        await channel.set_permissions(
            everyone,
            overwrite=overwrite,
            reason=f"Voice Master: {'hide' if hidden else 'unhide'} by owner",
        )
    except (discord.Forbidden, discord.HTTPException):
        await _ephemeral(interaction, "Couldn't update channel permissions.")
        return
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, channel.guild.id)
        if not cfg.disable_saves and "hidden" in cfg.saveable_fields:
            update_profile_field(
                conn,
                channel.guild.id,
                row.owner_id,
                field="hidden",
                value=hidden,
            )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_channel_hide" if hidden else "vm_channel_unhide",
            actor_id=interaction.user.id,
            extra={"channel_id": channel.id},
        )
    await _ephemeral(
        interaction, f"Channel is now **{'hidden' if hidden else 'visible'}**."
    )


# ---------------------------------------------------------------------------
# Persistent button classes
# ---------------------------------------------------------------------------


class LockButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voice_master:lock"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Lock",
                emoji="🔒",
                style=discord.ButtonStyle.secondary,
                custom_id="voice_master:lock",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_lock(interaction, channel, row, locked=True)


class UnlockButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voice_master:unlock"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Unlock",
                emoji="🔓",
                style=discord.ButtonStyle.secondary,
                custom_id="voice_master:unlock",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_lock(interaction, channel, row, locked=False)


class HideButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voice_master:hide"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Hide",
                emoji="👁️",
                style=discord.ButtonStyle.secondary,
                custom_id="voice_master:hide",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_hide(interaction, channel, row, hidden=True)


class RenameButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voice_master:rename"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Rename",
                emoji="✏️",
                style=discord.ButtonStyle.primary,
                custom_id="voice_master:rename",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        await interaction.response.send_modal(_RenameModal(channel.id))


class _RenameModal(discord.ui.Modal, title="Rename voice channel"):
    new_name: discord.ui.TextInput = discord.ui.TextInput(
        label="New channel name",
        placeholder="e.g. Game Night",
        min_length=1,
        max_length=MAX_NAME_LEN,
    )

    def __init__(self, channel_id: int) -> None:
        super().__init__()
        self._channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ctx = _ctx_from_interaction(interaction)
        if ctx is None or interaction.guild is None:
            return
        channel = interaction.guild.get_channel(self._channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            await _ephemeral(interaction, "That channel no longer exists.")
            return
        with ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
        if row is None or row.owner_id != interaction.user.id:
            await _ephemeral(interaction, "You no longer own that channel.")
            return
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_rename(interaction, channel, row, new_name=self.new_name.value)


class LimitButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voice_master:limit"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Limit",
                emoji="🔢",
                style=discord.ButtonStyle.primary,
                custom_id="voice_master:limit",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        await interaction.response.send_modal(_LimitModal(channel.id))


class _LimitModal(discord.ui.Modal, title="Set user limit"):
    new_limit: discord.ui.TextInput = discord.ui.TextInput(
        label="User limit (0–99, 0 = no cap)",
        placeholder="e.g. 5",
        min_length=1,
        max_length=2,
    )

    def __init__(self, channel_id: int) -> None:
        super().__init__()
        self._channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ctx = _ctx_from_interaction(interaction)
        if ctx is None or interaction.guild is None:
            return
        try:
            value = int(self.new_limit.value.strip())
        except ValueError:
            await _ephemeral(interaction, "Limit must be a whole number.")
            return
        channel = interaction.guild.get_channel(self._channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            await _ephemeral(interaction, "That channel no longer exists.")
            return
        with ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
        if row is None or row.owner_id != interaction.user.id:
            await _ephemeral(interaction, "You no longer own that channel.")
            return
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_limit(interaction, channel, row, new_limit=value)


class _TransferPickerView(discord.ui.View):
    """Ephemeral view for transferring ownership to a member in the channel."""

    def __init__(self, *, channel_id: int, owner_id: int, in_channel: list[discord.Member]) -> None:
        super().__init__(timeout=120)
        self._channel_id = channel_id
        self._owner_id = owner_id
        # Pre-populate the dropdown options from members currently in the channel
        # (UserSelect doesn't support filtering; use a regular Select instead).
        opts = [
            discord.SelectOption(
                label=m.display_name,
                value=str(m.id),
                description=f"@{m.name}",
            )
            for m in in_channel
            if not m.bot and m.id != owner_id
        ][:25]
        self.member_select.options = opts or [
            discord.SelectOption(label="No eligible members in the channel", value="0")
        ]
        if not opts:
            self.member_select.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await _ephemeral(interaction, "These buttons aren't for you.")
            return False
        return True

    @discord.ui.select(placeholder="Pick a member in the channel")
    async def member_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        if not interaction.guild or not select.values or select.values[0] == "0":
            await _ephemeral(interaction, "No member chosen.")
            return
        target_id = int(select.values[0])
        target = interaction.guild.get_member(target_id)
        if target is None:
            await _ephemeral(interaction, "That member is no longer in the server.")
            return
        channel = interaction.guild.get_channel(self._channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            await _ephemeral(interaction, "That channel no longer exists.")
            return
        ctx = _ctx_from_interaction(interaction)
        if ctx is None:
            return
        with ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
        if row is None or row.owner_id != interaction.user.id:
            await _ephemeral(interaction, "You no longer own that channel.")
            return
        await _apply_transfer(interaction, channel, row, new_owner=target)


class _UserPickerView(discord.ui.View):
    """Ephemeral follow-up view with a UserSelect + two action buttons.

    ``mode`` is "invite" or "kick" — chooses which apply helper runs and the
    button labels.
    """

    def __init__(self, *, channel_id: int, owner_id: int, mode: str) -> None:
        super().__init__(timeout=120)
        self._channel_id = channel_id
        self._owner_id = owner_id
        self._mode = mode  # "invite" or "kick"
        self._selected: discord.Member | None = None
        self.user_select.placeholder = (
            "Pick a member to invite" if mode == "invite" else "Pick a member to kick"
        )
        self.action_one.label = "Invite" if mode == "invite" else "Kick"
        self.action_two.label = (
            "Trusted invite (remember)" if mode == "invite" else "Permanent block (remember)"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await _ephemeral(interaction, "These buttons aren't for you.")
            return False
        return True

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Pick a member")
    async def user_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.UserSelect,
    ) -> None:
        chosen = select.values[0]
        if isinstance(chosen, discord.Member):
            self._selected = chosen
        else:
            self._selected = (
                interaction.guild.get_member(chosen.id) if interaction.guild else None
            )
        await interaction.response.defer()  # acknowledge — buttons drive the action

    async def _run(self, interaction: discord.Interaction, *, remember: bool) -> None:
        if self._selected is None:
            await _ephemeral(interaction, "Pick a member from the dropdown first.")
            return
        if interaction.guild is None:
            return
        channel = interaction.guild.get_channel(self._channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            await _ephemeral(interaction, "That channel no longer exists.")
            return
        ctx = _ctx_from_interaction(interaction)
        if ctx is None:
            return
        with ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
        if row is None or row.owner_id != interaction.user.id:
            await _ephemeral(interaction, "You no longer own that channel.")
            return
        if not await _gate_and_record_edit(interaction, row):
            return
        if self._mode == "invite":
            await _apply_invite(
                interaction, channel, row, target=self._selected, remember=remember
            )
        else:
            await _apply_kick(
                interaction, channel, row, target=self._selected, remember=remember
            )

    @discord.ui.button(label="Invite", style=discord.ButtonStyle.success, row=1)
    async def action_one(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run(interaction, remember=False)

    @discord.ui.button(label="Trusted invite (remember)", style=discord.ButtonStyle.primary, row=1)
    async def action_two(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run(interaction, remember=True)


class InviteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voice_master:invite"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Invite",
                emoji="👋",
                style=discord.ButtonStyle.success,
                custom_id="voice_master:invite",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, _row = resolved
        view = _UserPickerView(
            channel_id=channel.id, owner_id=interaction.user.id, mode="invite"
        )
        await interaction.response.send_message(
            "Pick a member to invite, then choose how to grant access:",
            view=view,
            ephemeral=True,
        )


class KickButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voice_master:kick"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Kick",
                emoji="🚫",
                style=discord.ButtonStyle.danger,
                custom_id="voice_master:kick",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, _row = resolved
        view = _UserPickerView(
            channel_id=channel.id, owner_id=interaction.user.id, mode="kick"
        )
        await interaction.response.send_message(
            "Pick a member to kick, then choose whether to also block them:",
            view=view,
            ephemeral=True,
        )


class _ResetConfirmView(discord.ui.View):
    """Two-button confirm: 'just this channel' vs 'channel + saved profile'."""

    def __init__(self, *, channel_id: int, owner_id: int) -> None:
        super().__init__(timeout=60)
        self._channel_id = channel_id
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await _ephemeral(interaction, "These buttons aren't for you.")
            return False
        return True

    async def _run(self, interaction: discord.Interaction, *, also_profile: bool) -> None:
        if interaction.guild is None:
            return
        channel = interaction.guild.get_channel(self._channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            await _ephemeral(interaction, "That channel no longer exists.")
            return
        ctx = _ctx_from_interaction(interaction)
        if ctx is None:
            return
        with ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
        if row is None or row.owner_id != interaction.user.id:
            await _ephemeral(interaction, "You no longer own that channel.")
            return
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_reset(interaction, channel, row, also_profile=also_profile)

    @discord.ui.button(label="Reset just this channel", style=discord.ButtonStyle.primary)
    async def reset_channel_only(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run(interaction, also_profile=False)

    @discord.ui.button(label="Reset channel + my saved profile", style=discord.ButtonStyle.danger)
    async def reset_with_profile(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run(interaction, also_profile=True)


class ResetButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voice_master:reset"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Reset",
                emoji="🧹",
                style=discord.ButtonStyle.secondary,
                custom_id="voice_master:reset",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, _row = resolved
        view = _ResetConfirmView(
            channel_id=channel.id, owner_id=interaction.user.id
        )
        await interaction.response.send_message(
            "Choose what to reset. Both actions wipe per-member permissions on this channel.",
            view=view,
            ephemeral=True,
        )


class TransferButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voice_master:transfer"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Transfer",
                emoji="👑",
                style=discord.ButtonStyle.primary,
                custom_id="voice_master:transfer",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, _row = resolved
        in_channel = [m for m in channel.members if not m.bot]
        view = _TransferPickerView(
            channel_id=channel.id,
            owner_id=interaction.user.id,
            in_channel=in_channel,
        )
        await interaction.response.send_message(
            "Transfer ownership to a member currently in your channel:",
            view=view,
            ephemeral=True,
        )


class UnhideButton(discord.ui.DynamicItem[discord.ui.Button], template=r"voice_master:unhide"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Unhide",
                emoji="👀",
                style=discord.ButtonStyle.secondary,
                custom_id="voice_master:unhide",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_hide(interaction, channel, row, hidden=False)


# Order in which buttons appear on the panel. Update this when adding new ones.
PANEL_BUTTON_CLASSES = (
    LockButton,
    UnlockButton,
    HideButton,
    UnhideButton,
    RenameButton,
    LimitButton,
    InviteButton,
    KickButton,
    TransferButton,
    ResetButton,
)


# ---------------------------------------------------------------------------
# Panel posting
# ---------------------------------------------------------------------------


def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Voice Master controls",
        description=(
            "Join the Hub voice channel to spin up your own room.\n"
            "Use these buttons to manage **the channel you currently own**.\n\n"
            "🔒 / 🔓 Lock or unlock — control whether new members can join.\n"
            "👁️ / 👀 Hide or unhide — control whether the channel is visible at all.\n"
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Buttons act on the channel you own. Don't own one? Join the Hub.")
    return embed


def build_panel_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for cls in PANEL_BUTTON_CLASSES:
        view.add_item(cls())
    return view


class _KnockResponseView(discord.ui.View):
    """Owner-only Accept / Deny buttons posted in the control channel."""

    def __init__(self, *, channel_id: int, requester_id: int, owner_id: int) -> None:
        super().__init__(timeout=3600)  # an hour to respond
        self._channel_id = channel_id
        self._requester_id = requester_id
        self._owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await _ephemeral(interaction, "Only the channel owner can answer.")
            return False
        return True

    async def _resolve(
        self, interaction: discord.Interaction
    ) -> tuple[discord.VoiceChannel, discord.Member] | None:
        if interaction.guild is None:
            return None
        channel = interaction.guild.get_channel(self._channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            await _ephemeral(interaction, "That channel no longer exists.")
            return None
        requester = interaction.guild.get_member(self._requester_id)
        if requester is None:
            await _ephemeral(interaction, "The requester is no longer in this server.")
            return None
        return channel, requester

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        resolved = await self._resolve(interaction)
        if resolved is None:
            return
        channel, requester = resolved
        overwrite = channel.overwrites_for(requester)
        overwrite.connect = True
        overwrite.view_channel = True
        try:
            await channel.set_permissions(
                requester, overwrite=overwrite, reason="Voice Master: knock accepted"
            )
        except (discord.Forbidden, discord.HTTPException):
            await _ephemeral(interaction, "Couldn't update channel permissions.")
            return
        ctx = _ctx_from_interaction(interaction)
        if ctx is not None:
            with ctx.open_db() as conn:
                write_audit(
                    conn,
                    guild_id=channel.guild.id,
                    action="vm_invite",
                    actor_id=interaction.user.id,
                    target_id=requester.id,
                    extra={"channel_id": channel.id, "via": "knock"},
                )
        await try_dm(
            requester,
            content=(
                f"Your knock on **{channel.name}** was accepted. "
                f"https://discord.com/channels/{channel.guild.id}/{channel.id}"
            ),
        )
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await interaction.response.edit_message(
                content=f"✅ Accepted by {interaction.user.mention}",
                view=self,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await interaction.response.edit_message(
                content=f"❌ Denied by {interaction.user.mention}",
                view=self,
            )
        except discord.HTTPException:
            pass


async def post_knock_request(
    ctx: "AppContext",
    *,
    channel: discord.VoiceChannel,
    requester: discord.Member,
    owner: discord.Member,
) -> bool:
    """Post a knock notification in the control channel; returns True on success."""
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, channel.guild.id)
    if not cfg.control_channel_id:
        return False
    control = channel.guild.get_channel(cfg.control_channel_id)
    if not isinstance(control, discord.TextChannel):
        return False
    embed = discord.Embed(
        title="🔔 Voice channel knock",
        description=(
            f"{requester.mention} is asking to join **{channel.name}**.\n"
            f"Owner: {owner.mention} — choose below."
        ),
        color=discord.Color.gold(),
    )
    view = _KnockResponseView(
        channel_id=channel.id,
        requester_id=requester.id,
        owner_id=owner.id,
    )
    try:
        await control.send(content=owner.mention, embed=embed, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def post_panel(
    ctx: "AppContext", channel: discord.TextChannel
) -> discord.Message:
    """Post (or repost) the persistent panel into the given text channel."""
    embed = build_panel_embed()
    view = build_panel_view()
    msg = await channel.send(embed=embed, view=view)
    with ctx.open_db() as conn:
        set_voice_master_config_value(
            conn,
            channel.guild.id,
            "voice_master_panel_message_id",
            str(msg.id),
        )
    return msg


def build_inline_panel_embed(owner: discord.Member) -> discord.Embed:
    """Owner-greeting embed for the panel posted into the new channel's chat."""
    return discord.Embed(
        title="Your voice channel is ready",
        description=(
            f"Welcome, {owner.mention}. Use the buttons below to manage **this channel** — "
            "lock/hide it, rename it, set a user limit, invite or kick members, transfer "
            "ownership, or reset it. Changes you make are saved as your default for next time."
        ),
        color=discord.Color.blurple(),
    )


async def post_inline_panel(
    channel: discord.VoiceChannel, owner: discord.Member
) -> discord.Message | None:
    """Post the control panel into a voice channel's text chat.

    Modern Discord voice channels host their own text chat (the "side chat"),
    so we drop the panel right there — owners don't have to navigate to the
    central control channel. Returns ``None`` if the bot lacks permission to
    post; caller should treat that as non-fatal.
    """
    embed = build_inline_panel_embed(owner)
    view = build_panel_view()
    try:
        return await channel.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        log.exception(
            "voice_master: failed to post inline panel in channel %d", channel.id
        )
        return None
