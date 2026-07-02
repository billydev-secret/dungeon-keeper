"""Voice Master — persistent panel buttons, modals, helpers."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.services.moderation import write_audit
from bot_modules.services.voice_master_service import (
    MAX_NAME_LEN,
    ActiveChannel,
    EDIT_WINDOW_S,
    access_status_text,
    add_blocked,
    add_trusted,
    can_edit,
    delete_profile,
    get_active_channel,
    get_owned_channel,
    list_blocked,
    list_name_blocklist,
    list_trusted,
    load_voice_master_config,
    lock_status_text,
    record_edit_in_db,
    set_owner,
    set_voice_master_config_value,
    try_dm,
    update_profile_field,
)
from bot_modules.voice_master.embeds import (
    build_claim_done_embed,
    build_claim_prompt_embed,
    build_inline_panel_embed as _build_inline_panel_embed,
    build_knock_request_embed,
    build_panel_embed as _build_panel_embed,
)
from bot_modules.voice_master.logic import (
    MemberInfo,
    PANEL_GROUP_ORDER,
    SPECTATOR_PARTICIPATION_PERMS,
    build_join_url,
    build_transfer_picker_plan,
    classify_access_mode,
    classify_claim_attempt,
    format_edit_rate_limit_error,
    format_hide_result,
    format_invite_dm,
    format_invite_result,
    format_kick_result,
    format_knock_accepted_dm,
    format_limit_result,
    format_lock_result,
    format_rename_result,
    format_reset_result,
    format_spectator_result,
    format_transfer_result,
    panel_group_placeholder,
    panel_metas_for_group,
    parse_limit_input,
    plan_hide_text_grants,
    plan_lock_text_grants,
    plan_spectator_grant_cleanup,
    plan_spectator_speaker_grants,
    plan_unhide_view_cleanup,
    plan_unlock_overwrite_cleanup,
    should_save_profile_field,
    user_picker_labels,
    validate_invite_target,
    validate_kick_target,
    validate_limit_value,
    validate_rename_input,
    validate_transfer_target,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext


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


def _maybe_save_profile_field(
    conn,
    cfg,
    *,
    guild_id: int,
    owner_id: int,
    saveable_key: str,
    profile_field: str,
    value: object,
) -> None:
    """Persist a profile field iff this guild's config permits saving it.

    ``saveable_key`` is the cfg.saveable_fields entry (e.g. "name", "limit",
    "locked", "hidden"); ``profile_field`` is the column on VoiceProfile
    (e.g. "saved_name", "saved_limit", "locked", "hidden").
    """
    if not should_save_profile_field(
        saveable_key=saveable_key,
        disable_saves=cfg.disable_saves,
        saveable_fields=cfg.saveable_fields,
    ):
        return
    update_profile_field(conn, guild_id, owner_id, field=profile_field, value=value)


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
            format_edit_rate_limit_error(
                retry_seconds=retry, window_s=EDIT_WINDOW_S
            ),
        )
        return False
    with ctx.open_db() as conn:
        record_edit_in_db(conn, row.channel_id, now=now)
    return True


# ---------------------------------------------------------------------------
# Lock / Unlock
# ---------------------------------------------------------------------------


async def _sync_lock_member_overwrites(
    ctx: AppContext,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    locked: bool,
) -> None:
    """Keep in-channel members' text-chat access in sync with the lock state.

    On lock, grant ``connect=True`` to everyone currently in the channel so the
    integrated text chat stays usable (Discord ties text-chat access to the
    Connect permission). On unlock, remove those transient grants again, leaving
    the owner's and any trusted/blocked overwrites untouched. Individual
    permission edits are best-effort: a failure on one member must not abort the
    lock toggle, which has already been applied to ``@everyone``.
    """
    if locked:
        bot_member = channel.guild.me
        grant_ids = plan_lock_text_grants(
            present_member_ids=[m.id for m in channel.members],
            owner_id=row.owner_id,
            bot_id=bot_member.id if bot_member is not None else None,
        )
        for uid in grant_ids:
            member = channel.guild.get_member(uid)
            if member is None:
                continue
            overwrite = channel.overwrites_for(member)
            overwrite.connect = True
            try:
                await channel.set_permissions(
                    member,
                    overwrite=overwrite,
                    reason="Voice Master: keep text-chat access while locked",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        return

    with ctx.open_db() as conn:
        trusted_ids = list_trusted(conn, channel.guild.id, row.owner_id)
        blocked_ids = list_blocked(conn, channel.guild.id, row.owner_id)
    # ``channel.overwrites`` rebuilds its dict on each access, so snapshot once.
    # Keep the resolved Member objects: keys are already Members (we drop any
    # unresolved discord.Object), and set_permissions needs a Role/Member
    # target — not a bare snowflake.
    overwrites = channel.overwrites
    overwrite_members = {
        target.id: target
        for target in overwrites
        if isinstance(target, discord.Member)
    }
    cleanup_ids = plan_unlock_overwrite_cleanup(
        member_overwrites=[
            (m.id, overwrites[m].connect, overwrites[m].view_channel)
            for m in overwrite_members.values()
        ],
        owner_id=row.owner_id,
        trusted_ids=trusted_ids,
        blocked_ids=blocked_ids,
    )
    for uid in cleanup_ids:
        try:
            await channel.set_permissions(
                overwrite_members[uid],
                overwrite=None,
                reason="Voice Master: drop transient lock grant on unlock",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


async def _sync_hidden_member_overwrites(
    ctx: AppContext,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    hidden: bool,
) -> None:
    """Keep in-channel members' text-chat access in sync with the hidden state.

    The mirror of :func:`_sync_lock_member_overwrites` for hide. Discord gates a
    voice channel's integrated text chat behind ``View Channel``, so hiding
    (denying it to ``@everyone``) also strips the side chat from the people
    inside. On hide, grant ``view_channel=True`` to everyone currently present
    so the chat stays usable; on unhide, clear that field again, dropping the
    overwrite if nothing else remains — a member also rescued by the lock keeps
    their ``connect`` grant. The two toggles compose: a hidden *and* locked
    channel carries both grants on the same overwrite. Individual edits are
    best-effort: a failure on one member must not abort the hide toggle, which
    has already been applied to ``@everyone``.
    """
    if hidden:
        bot_member = channel.guild.me
        grant_ids = plan_hide_text_grants(
            present_member_ids=[m.id for m in channel.members],
            owner_id=row.owner_id,
            bot_id=bot_member.id if bot_member is not None else None,
        )
        for uid in grant_ids:
            member = channel.guild.get_member(uid)
            if member is None:
                continue
            overwrite = channel.overwrites_for(member)
            overwrite.view_channel = True
            try:
                await channel.set_permissions(
                    member,
                    overwrite=overwrite,
                    reason="Voice Master: keep text-chat access while hidden",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        return

    with ctx.open_db() as conn:
        trusted_ids = list_trusted(conn, channel.guild.id, row.owner_id)
        blocked_ids = list_blocked(conn, channel.guild.id, row.owner_id)
    # Snapshot once — ``channel.overwrites`` rebuilds its dict on each access —
    # and keep the resolved Member objects (set_permissions needs a Member
    # target, not a bare snowflake).
    overwrites = channel.overwrites
    overwrite_members = {
        target.id: target
        for target in overwrites
        if isinstance(target, discord.Member)
    }
    cleanup_ids = plan_unhide_view_cleanup(
        member_overwrites=[
            (m.id, overwrites[m].view_channel)
            for m in overwrite_members.values()
        ],
        owner_id=row.owner_id,
        trusted_ids=trusted_ids,
        blocked_ids=blocked_ids,
    )
    for uid in cleanup_ids:
        member = overwrite_members[uid]
        overwrite = channel.overwrites_for(member)
        # Reset only our own field so a co-existing lock grant survives; drop
        # the whole overwrite only when nothing else is left.
        overwrite.view_channel = None
        try:
            await channel.set_permissions(
                member,
                overwrite=None if overwrite.is_empty() else overwrite,
                reason="Voice Master: drop transient hide grant on unhide",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


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
    # Lock and spectate are mutually exclusive — tear down spectator mode first
    # so its audience deny + speaker grants don't linger under the lock.
    spectator_cleared = False
    if locked:
        with ctx.open_db() as conn:
            gate_role_id = load_voice_master_config(
                conn, channel.guild.id
            ).spectator_gate_role_id
        gate_role = _resolve_gate_role(channel, gate_role_id)
        if _access_mode_for_channel(channel, gate_role=gate_role) == "spectate":
            await _teardown_spectator_overwrites(
                ctx, channel, row, gate_role=gate_role
            )
            spectator_cleared = True
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
    # Discord gates a voice channel's text chat behind Connect, so denying it
    # to @everyone above also cuts text-chat access for the people inside. Keep
    # them online by granting per-member Connect on lock, and tidy those grants
    # away on unlock.
    await _sync_lock_member_overwrites(ctx, channel, row, locked=locked)
    # Advertise the new state on the channel's status line. This rides a
    # separate endpoint from the name edit, so it isn't subject to the
    # 2-per-10-minutes name rate limit and can toggle freely. Cosmetic: a
    # failed status edit must not undo the lock above.
    try:
        await channel.edit(
            status=lock_status_text(locked=locked),
            reason=f"Voice Master: {'lock' if locked else 'unlock'} status marker",
        )
    except (discord.Forbidden, discord.HTTPException):
        pass
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, channel.guild.id)
        _maybe_save_profile_field(
            conn, cfg,
            guild_id=channel.guild.id, owner_id=row.owner_id,
            saveable_key="locked", profile_field="locked", value=locked,
        )
        if spectator_cleared:
            _maybe_save_profile_field(
                conn, cfg,
                guild_id=channel.guild.id, owner_id=row.owner_id,
                saveable_key="spectator", profile_field="spectator", value=False,
            )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_channel_lock" if locked else "vm_channel_unlock",
            actor_id=interaction.user.id,
            extra={"channel_id": channel.id},
        )
    await _ephemeral(interaction, format_lock_result(locked=locked))


# ---------------------------------------------------------------------------
# Spectator mode
# ---------------------------------------------------------------------------
#
# Spectator mode opens the channel to a muted, no-video, read-only audience.
# Ungated, ``@everyone`` is that audience; with a gate role configured,
# ``@everyone`` is denied Connect (visible + readable, can't join) and the gate
# role becomes the audience. The owner, trusted/invited members and anyone
# already inside get explicit participation grants — a member overwrite outranks
# the role/@everyone deny — so they keep full voice/video/chat. Lock and
# spectate are mutually exclusive: enabling one tears the other down first.


def _set_participation(
    overwrite: discord.PermissionOverwrite, value: bool | None
) -> None:
    """Set the spectator participation perms on ``overwrite`` to one value."""
    for perm in SPECTATOR_PARTICIPATION_PERMS:
        setattr(overwrite, perm, value)


def _resolve_gate_role(
    channel: discord.VoiceChannel, gate_role_id: int
) -> discord.Role | None:
    """Resolve the configured gate role, or ``None`` (unset/deleted → ungated)."""
    if not gate_role_id:
        return None
    return channel.guild.get_role(gate_role_id)


def _access_mode_for_channel(
    channel: discord.VoiceChannel, *, gate_role: discord.Role | None
) -> str:
    """Classify the channel's live access mode (``open``/``lock``/``spectate``)."""
    everyone = channel.overwrites_for(channel.guild.default_role)
    gate_ow = (
        channel.overwrites_for(gate_role) if gate_role is not None else None
    )
    return classify_access_mode(
        everyone_connect=everyone.connect,
        everyone_speak=everyone.speak,
        gate_role_set=gate_role is not None,
        gate_role_connect=gate_ow.connect if gate_ow is not None else None,
        gate_role_speak=gate_ow.speak if gate_ow is not None else None,
    )


def _grant_speaker_if_spectating(
    ctx: AppContext,
    channel: discord.VoiceChannel,
    overwrite: discord.PermissionOverwrite,
) -> None:
    """Add participation perms to ``overwrite`` iff the channel is spectating.

    Used by the access-granting sites (invite, knock-accept, transfer): a
    person let in while spectator mode is on should be a full speaker, not land
    muted under the audience deny. No-op in open/lock mode.
    """
    with ctx.open_db() as conn:
        gate_role_id = load_voice_master_config(
            conn, channel.guild.id
        ).spectator_gate_role_id
    gate_role = _resolve_gate_role(channel, gate_role_id)
    if _access_mode_for_channel(channel, gate_role=gate_role) == "spectate":
        _set_participation(overwrite, True)


async def _teardown_spectator_overwrites(
    ctx: AppContext,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    gate_role: discord.Role | None,
) -> None:
    """Remove every spectator-mode overwrite, returning the channel to open.

    Clears the audience deny (``@everyone`` participation + the gated Connect
    deny, and the gate-role overwrite), drops the transient "already-in"
    speaker grants by shape, and resets the persistent speakers' participation
    fields back to inherit. Best-effort per edit. Does not touch the profile or
    status line — callers own those.
    """
    everyone = channel.guild.default_role
    everyone_ow = channel.overwrites_for(everyone)
    _set_participation(everyone_ow, None)
    if everyone_ow.connect is False:
        # Only the gated-spectator path denies @everyone Connect; lock is
        # exclusive with spectate, so clearing it here is safe.
        everyone_ow.connect = None
    try:
        await channel.set_permissions(
            everyone, overwrite=everyone_ow,
            reason="Voice Master: clear spectator audience deny",
        )
    except (discord.Forbidden, discord.HTTPException):
        pass
    if gate_role is not None:
        try:
            await channel.set_permissions(
                gate_role, overwrite=None,
                reason="Voice Master: clear spectator gate-role overwrite",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    with ctx.open_db() as conn:
        trusted_ids = list_trusted(conn, channel.guild.id, row.owner_id)
        blocked_ids = list_blocked(conn, channel.guild.id, row.owner_id)
    overwrites = channel.overwrites
    overwrite_members = {
        target.id: target
        for target in overwrites
        if isinstance(target, discord.Member)
    }
    transient = set(
        plan_spectator_grant_cleanup(
            member_overwrites=[
                (
                    m.id,
                    overwrites[m].speak,
                    overwrites[m].connect,
                    overwrites[m].view_channel,
                )
                for m in overwrite_members.values()
            ],
            owner_id=row.owner_id,
            trusted_ids=trusted_ids,
            blocked_ids=blocked_ids,
        )
    )
    for uid, member in overwrite_members.items():
        ow = overwrites[member]
        if uid in transient:
            # "Already-in" speaker — pure participation grant, remove entirely.
            try:
                await channel.set_permissions(
                    member, overwrite=None,
                    reason="Voice Master: drop transient spectator grant",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        elif ow.speak is True:
            # Persistent speaker (owner/trusted/invited) — keep their access,
            # just reset the participation fields we added back to inherit.
            _set_participation(ow, None)
            try:
                await channel.set_permissions(
                    member, overwrite=ow,
                    reason="Voice Master: reset spectator speaker grant",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass


async def _apply_spectator(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    row: ActiveChannel,
    *,
    spectator: bool,
) -> None:
    ctx = _ctx_from_interaction(interaction)
    if ctx is None:
        return
    await _defer_if_needed(interaction)
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, channel.guild.id)
    gate_role = _resolve_gate_role(channel, cfg.spectator_gate_role_id)
    gated = spectator and gate_role is not None
    everyone = channel.guild.default_role

    if not spectator:
        await _teardown_spectator_overwrites(ctx, channel, row, gate_role=gate_role)
    else:
        # Spectate and lock are exclusive: clear any lock first (the @everyone
        # Connect deny plus the transient text-chat grants it leaves behind).
        everyone_ow = channel.overwrites_for(everyone)
        if everyone_ow.connect is False:
            everyone_ow.connect = None
            try:
                await channel.set_permissions(
                    everyone, overwrite=everyone_ow,
                    reason="Voice Master: clear lock before spectate",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            await _sync_lock_member_overwrites(ctx, channel, row, locked=False)

        # Apply the audience deny.
        everyone_ow = channel.overwrites_for(everyone)
        if gated:
            everyone_ow.connect = False  # block joining; gate role joins below
            _set_participation(everyone_ow, None)
        else:
            _set_participation(everyone_ow, False)
        try:
            await channel.set_permissions(
                everyone, overwrite=everyone_ow,
                reason="Voice Master: enable spectator mode",
            )
        except (discord.Forbidden, discord.HTTPException):
            await _ephemeral(interaction, "Couldn't update channel permissions.")
            return
        if gated:
            gate_ow = channel.overwrites_for(gate_role)
            gate_ow.connect = True
            _set_participation(gate_ow, False)
            try:
                await channel.set_permissions(
                    gate_role, overwrite=gate_ow,
                    reason="Voice Master: spectator gate role",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        # Grant the owner full participation (their persistent overwrite).
        owner = channel.guild.get_member(row.owner_id)
        if owner is not None:
            owner_ow = channel.overwrites_for(owner)
            owner_ow.connect = True
            owner_ow.view_channel = True
            _set_participation(owner_ow, True)
            try:
                await channel.set_permissions(
                    owner, overwrite=owner_ow,
                    reason="Voice Master: owner speaks while spectating",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        # Anyone already inside keeps full participation (transient grant).
        bot_member = channel.guild.me
        for uid in plan_spectator_speaker_grants(
            present_member_ids=[m.id for m in channel.members],
            owner_id=row.owner_id,
            bot_id=bot_member.id if bot_member is not None else None,
        ):
            member = channel.guild.get_member(uid)
            if member is None:
                continue
            mem_ow = channel.overwrites_for(member)
            _set_participation(mem_ow, True)
            try:
                await channel.set_permissions(
                    member, overwrite=mem_ow,
                    reason="Voice Master: speaker present at spectate enable",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

    # Status line (cosmetic — a failure must not undo the overwrites).
    try:
        await channel.edit(
            status=access_status_text(mode="spectate" if spectator else "open"),
            reason="Voice Master: spectator status marker",
        )
    except (discord.Forbidden, discord.HTTPException):
        pass

    with ctx.open_db() as conn:
        _maybe_save_profile_field(
            conn, cfg,
            guild_id=channel.guild.id, owner_id=row.owner_id,
            saveable_key="spectator", profile_field="spectator", value=spectator,
        )
        if spectator:
            # Mutually exclusive with lock — clear any saved lock state too.
            _maybe_save_profile_field(
                conn, cfg,
                guild_id=channel.guild.id, owner_id=row.owner_id,
                saveable_key="locked", profile_field="locked", value=False,
            )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_channel_spectate_on" if spectator else "vm_channel_spectate_off",
            actor_id=interaction.user.id,
            extra={"channel_id": channel.id, "gated": gated},
        )
    await _ephemeral(
        interaction, format_spectator_result(spectator=spectator, gated=gated)
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
    with ctx.open_db() as conn:
        patterns = list_name_blocklist(conn, channel.guild.id)
    result = validate_rename_input(
        new_name, max_len=MAX_NAME_LEN, blocklist_patterns=patterns
    )
    if result.error_message is not None:
        await _ephemeral(interaction, result.error_message)
        return
    new_name = result.cleaned
    # The channel *name* is the only edit subject to Discord's 2-per-10-minutes
    # limit, so a rename is the one action we still rate-gate. Gate here (after
    # validation) so a rejected name never burns a budget slot. Lock state now
    # lives on the status line, so the name is written bare.
    if not await _gate_and_record_edit(interaction, row):
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
        _maybe_save_profile_field(
            conn, cfg,
            guild_id=channel.guild.id, owner_id=row.owner_id,
            saveable_key="name", profile_field="saved_name", value=new_name,
        )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_channel_rename",
            actor_id=interaction.user.id,
            extra={"channel_id": channel.id, "name": new_name},
        )
    await _ephemeral(interaction, format_rename_result(new_name=new_name))


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
        # Reset clears the @everyone Connect denial (i.e. unlocks), so also
        # set the status line back to "open". Status rides a separate endpoint
        # from the overwrites payload, so this is one PATCH + one PUT — neither
        # touches the name rate limit, and the name is left untouched.
        await channel.edit(
            overwrites=new_overwrites,
            status=lock_status_text(locked=False),
            reason="Voice Master: reset by owner",
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
    await _ephemeral(interaction, format_reset_result(also_profile=also_profile))


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
    in_channel = (
        new_owner.voice is not None
        and new_owner.voice.channel is not None
        and new_owner.voice.channel.id == channel.id
    )
    err = validate_transfer_target(
        target_is_bot=new_owner.bot,
        target_is_current_owner=new_owner.id == row.owner_id,
        target_in_channel=in_channel,
    )
    if err is not None:
        await _ephemeral(interaction, err)
        return
    await _defer_if_needed(interaction)
    # Grant the new owner explicit access (so future locks/hides don't bite them).
    overwrite = channel.overwrites_for(new_owner)
    overwrite.connect = True
    overwrite.view_channel = True
    _grant_speaker_if_spectating(ctx, channel, overwrite)
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
        interaction,
        format_transfer_result(new_owner_mention=new_owner.mention),
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
    err = validate_invite_target(
        target_is_bot=target.bot,
        target_is_owner=target.id == row.owner_id,
    )
    if err is not None:
        await _ephemeral(interaction, err)
        return
    await _defer_if_needed(interaction)
    overwrite = channel.overwrites_for(target)
    overwrite.connect = True
    overwrite.view_channel = True
    _grant_speaker_if_spectating(ctx, channel, overwrite)
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
        if remember and should_save_profile_field(
            saveable_key="trusted",
            disable_saves=cfg.disable_saves,
            saveable_fields=cfg.saveable_fields,
        ):
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
    join_url = build_join_url(
        guild_id=channel.guild.id, channel_id=channel.id
    )
    await try_dm(
        target,
        content=format_invite_dm(
            channel_name=channel.name,
            inviter_mention=interaction.user.mention,
            guild_name=channel.guild.name,
            join_url=join_url,
        ),
    )

    await _ephemeral(
        interaction,
        format_invite_result(
            target_mention=target.mention,
            remember=remember,
            cap_evicted_id=cap_evicted,
        ),
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
    err = validate_kick_target(
        target_is_bot=target.bot,
        target_is_self_owner=target.id == row.owner_id,
    )
    if err is not None:
        await _ephemeral(interaction, err)
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
        if remember and should_save_profile_field(
            saveable_key="blocked",
            disable_saves=cfg.disable_saves,
            saveable_fields=cfg.saveable_fields,
        ):
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

    await _ephemeral(
        interaction,
        format_kick_result(
            target_mention=target.mention,
            remember=remember,
            cap_evicted_id=cap_evicted,
        ),
    )


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
    err = validate_limit_value(new_limit)
    if err is not None:
        await _ephemeral(interaction, err)
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
        _maybe_save_profile_field(
            conn, cfg,
            guild_id=channel.guild.id, owner_id=row.owner_id,
            saveable_key="limit", profile_field="saved_limit", value=new_limit,
        )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_channel_limit",
            actor_id=interaction.user.id,
            extra={"channel_id": channel.id, "limit": new_limit},
        )
    await _ephemeral(interaction, format_limit_result(new_limit=new_limit))


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
    # Discord gates a voice channel's text chat behind View Channel, so the
    # deny above also cuts side-chat access for the people inside. Restore it
    # with a per-member view grant on hide, and tidy those grants on unhide.
    await _sync_hidden_member_overwrites(ctx, channel, row, hidden=hidden)
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, channel.guild.id)
        _maybe_save_profile_field(
            conn, cfg,
            guild_id=channel.guild.id, owner_id=row.owner_id,
            saveable_key="hidden", profile_field="hidden", value=hidden,
        )
        write_audit(
            conn,
            guild_id=channel.guild.id,
            action="vm_channel_hide" if hidden else "vm_channel_unhide",
            actor_id=interaction.user.id,
            extra={"channel_id": channel.id},
        )
    await _ephemeral(interaction, format_hide_result(hidden=hidden))


# ---------------------------------------------------------------------------
# Persistent panel dropdowns
# ---------------------------------------------------------------------------


async def _reset_panel_dropdowns(interaction: discord.Interaction) -> None:
    """Re-render the panel so the dropdowns return to their placeholders.

    A select keeps showing the picked option until the message is edited;
    re-attaching a fresh view clears it. Best-effort and independent of how
    the action handler used the interaction response (it edits the message
    via the bot token, not the interaction token).
    """
    msg = interaction.message
    if msg is None:
        return
    try:
        await msg.edit(view=build_panel_view())
    except (discord.Forbidden, discord.HTTPException):
        pass


class _PanelSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"voice_master:select:(?P<group>\w+)",
):
    """One grouped dropdown of channel actions.

    Each option's value is an action key; selecting it dispatches to the
    same handler the buttons used (``_ON_CLICKS``), so behaviour is identical
    — only the presentation changed. The menu is reset to its placeholder
    afterwards so it always reads as a fresh prompt.
    """

    def __init__(self, group: str) -> None:
        options = [
            discord.SelectOption(label=m.label, value=m.action, emoji=m.emoji)
            for m in panel_metas_for_group(group)
        ]
        super().__init__(
            discord.ui.Select(
                custom_id=f"voice_master:select:{group}",
                placeholder=panel_group_placeholder(group),
                min_values=1,
                max_values=1,
                options=options,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["group"])

    async def callback(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        on_click = _ON_CLICKS.get(values[0]) if values else None
        if on_click is not None:
            resolved = await _resolve_owned_channel(interaction)
            if resolved is not None:
                channel, row = resolved
                await on_click(interaction, channel, row)
        await _reset_panel_dropdowns(interaction)


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
        await _apply_rename(interaction, channel, row, new_name=self.new_name.value)


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
        value, parse_err = parse_limit_input(self.new_limit.value)
        if parse_err is not None or value is None:
            await _ephemeral(
                interaction, parse_err or "Limit must be a whole number."
            )
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
        await _apply_limit(interaction, channel, row, new_limit=value)


class _TransferPickerView(discord.ui.View):
    """Ephemeral view for transferring ownership to a member in the channel."""

    def __init__(self, *, channel_id: int, owner_id: int, in_channel: list[discord.Member]) -> None:
        super().__init__(timeout=120)
        self._channel_id = channel_id
        self._owner_id = owner_id
        # Pre-populate the dropdown options from members currently in the channel
        # (UserSelect doesn't support filtering; use a regular Select instead).
        plan = build_transfer_picker_plan(
            [
                MemberInfo(
                    id=m.id,
                    display_name=m.display_name,
                    name=m.name,
                    is_bot=m.bot,
                )
                for m in in_channel
            ],
            owner_id=owner_id,
        )
        if plan.has_options:
            self.member_select.options = [
                discord.SelectOption(
                    label=opt.label, value=opt.value, description=opt.description
                )
                for opt in plan.options
            ]
        else:
            self.member_select.options = [
                discord.SelectOption(
                    label="No eligible members in the channel", value="0"
                )
            ]
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
        labels = user_picker_labels(mode)
        self.user_select.placeholder = labels.placeholder
        self.action_one.label = labels.action_one
        self.action_two.label = labels.action_two

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


# ── Action handlers — one per panel button ───────────────────────────────


async def _on_lock(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    await _apply_lock(interaction, channel, row, locked=True)


async def _on_unlock(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    await _apply_lock(interaction, channel, row, locked=False)


async def _on_spectator(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    await _apply_spectator(interaction, channel, row, spectator=True)


async def _on_unspectator(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    await _apply_spectator(interaction, channel, row, spectator=False)


async def _on_hide(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    await _apply_hide(interaction, channel, row, hidden=True)


async def _on_unhide(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    await _apply_hide(interaction, channel, row, hidden=False)


async def _on_rename(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    await interaction.response.send_modal(_RenameModal(channel.id))


async def _on_limit(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    await interaction.response.send_modal(_LimitModal(channel.id))


async def _on_invite(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    view = _UserPickerView(
        channel_id=channel.id, owner_id=interaction.user.id, mode="invite"
    )
    await interaction.response.send_message(
        "Pick a member to invite, then choose how to grant access:",
        view=view,
        ephemeral=True,
    )


async def _on_kick(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    view = _UserPickerView(
        channel_id=channel.id, owner_id=interaction.user.id, mode="kick"
    )
    await interaction.response.send_message(
        "Pick a member to kick, then choose whether to also block them:",
        view=view,
        ephemeral=True,
    )


async def _on_transfer(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
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


async def _on_reset(
    interaction: discord.Interaction, channel: discord.VoiceChannel, row: ActiveChannel
) -> None:
    view = _ResetConfirmView(
        channel_id=channel.id, owner_id=interaction.user.id
    )
    await interaction.response.send_message(
        "Choose what to reset. Both actions wipe per-member permissions on this channel.",
        view=view,
        ephemeral=True,
    )


_ON_CLICKS: dict[str, Callable[
    [discord.Interaction, discord.VoiceChannel, "ActiveChannel"],
    Awaitable[None],
]] = {
    "lock": _on_lock,
    "unlock": _on_unlock,
    "spectator": _on_spectator,
    "unspectator": _on_unspectator,
    "hide": _on_hide,
    "unhide": _on_unhide,
    "rename": _on_rename,
    "limit": _on_limit,
    "invite": _on_invite,
    "kick": _on_kick,
    "transfer": _on_transfer,
    "reset": _on_reset,
}


async def _handle_claim_button(
    ctx: "AppContext",
    interaction: discord.Interaction,
    channel_id: int,
) -> None:
    """Claim ownership via the in-chat button posted when an owner leaves.

    Mirrors the eligibility + grant logic of ``/voice claim`` but is driven by a
    persistent button so members don't need to know the command. Re-validates
    against live state on every click, so a stale button (owner came back, or
    someone already claimed — both clear ``owner_left_at``) refuses cleanly
    rather than handing off an actively-owned room.
    """
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        await _ephemeral(interaction, "That channel no longer exists.")
        return
    if (
        member.voice is None
        or member.voice.channel is None
        or member.voice.channel.id != channel_id
    ):
        await _ephemeral(interaction, "Join the channel first, then claim it.")
        return
    with ctx.open_db() as conn:
        cfg = load_voice_master_config(conn, guild.id)
        row = get_active_channel(conn, channel_id)
    if row is None:
        await _ephemeral(interaction, "This channel isn't managed by Voice Master.")
        return
    owner = guild.get_member(row.owner_id)
    decision = classify_claim_attempt(
        owner_present=owner is not None,
        owner_left_at=row.owner_left_at,
        now=time.time(),
        owner_grace_s=cfg.owner_grace_s,
        caller_is_owner=row.owner_id == member.id,
    )
    if not decision.eligible:
        await _ephemeral(
            interaction,
            decision.error_message or "You can't claim this channel right now.",
        )
        return
    overwrite = channel.overwrites_for(member)
    overwrite.connect = True
    overwrite.view_channel = True
    _grant_speaker_if_spectating(ctx, channel, overwrite)
    try:
        await channel.set_permissions(
            member, overwrite=overwrite, reason="Voice Master: claim (button)"
        )
    except (discord.Forbidden, discord.HTTPException):
        await _ephemeral(interaction, "Couldn't grant you ownership permissions.")
        return
    with ctx.open_db() as conn:
        set_owner(conn, channel_id, member.id)
        write_audit(
            conn,
            guild_id=guild.id,
            action="vm_claim",
            actor_id=member.id,
            target_id=row.owner_id,
            extra={"channel_id": channel_id, "via": "button"},
        )
    # Retire the prompt in place: swap to a 'claimed' embed and drop the button.
    try:
        await interaction.response.edit_message(
            embed=build_claim_done_embed(
                claimer_mention=member.mention, channel_name=channel.name
            ),
            view=None,
        )
    except (discord.Forbidden, discord.HTTPException, discord.InteractionResponded):
        await _ephemeral(interaction, "You're the new owner of this channel.")


class _ClaimButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"voice_master:claim:(?P<cid>\d+)",
):
    """Persistent 'Claim channel' button posted into a channel's side chat when
    its owner leaves for good. Re-registered via ``add_dynamic_items`` so it
    survives restarts; the callback re-validates eligibility on every click."""

    def __init__(self, channel_id: int) -> None:
        self._channel_id = channel_id
        super().__init__(
            discord.ui.Button(
                label="Claim channel",
                style=discord.ButtonStyle.success,
                emoji="👑",
                custom_id=f"voice_master:claim:{channel_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # noqa: ANN001
        return cls(int(match["cid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = getattr(interaction.client, "_vm_ctx", None)
        if ctx is None:
            await _ephemeral(interaction, "Voice Master is unavailable right now.")
            return
        await _handle_claim_button(ctx, interaction, self._channel_id)


async def post_claim_prompt(
    channel: discord.VoiceChannel,
    *,
    colour: "discord.Colour | None" = None,
) -> discord.Message | None:
    """Post the claim prompt + button into a channel's side chat.

    Called once an owner has been gone past the grace window while members
    remain inside. Best-effort: returns ``None`` if the bot can't post.
    """
    view = discord.ui.View(timeout=None)
    view.add_item(_ClaimButton(channel.id))
    try:
        return await channel.send(
            embed=build_claim_prompt_embed(
                channel_name=channel.name, colour=colour
            ),
            view=view,
        )
    except (discord.Forbidden, discord.HTTPException):
        return None


# Dynamic-item classes the cog re-registers via ``add_dynamic_items`` so the
# panel's selects and the claim button keep working after a restart.
PANEL_DYNAMIC_ITEM_CLASSES = (_PanelSelect, _ClaimButton)


# ---------------------------------------------------------------------------
# Panel posting
# ---------------------------------------------------------------------------


def build_panel_embed(
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Thin wrapper around the pure embed builder; kept for cog import paths."""
    return _build_panel_embed(colour=colour)


def build_panel_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for group in PANEL_GROUP_ORDER:
        view.add_item(_PanelSelect(group))
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
        ctx = _ctx_from_interaction(interaction)
        overwrite = channel.overwrites_for(requester)
        overwrite.connect = True
        overwrite.view_channel = True
        if ctx is not None:
            # If the room is spectating, an accepted knocker comes in as a full
            # speaker (same as an invite), not as a muted spectator.
            _grant_speaker_if_spectating(ctx, channel, overwrite)
        try:
            await channel.set_permissions(
                requester, overwrite=overwrite, reason="Voice Master: knock accepted"
            )
        except (discord.Forbidden, discord.HTTPException):
            await _ephemeral(interaction, "Couldn't update channel permissions.")
            return
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
            content=format_knock_accepted_dm(
                channel_name=channel.name,
                join_url=build_join_url(
                    guild_id=channel.guild.id, channel_id=channel.id
                ),
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
    accent = await resolve_accent_color(ctx.db_path, channel.guild)
    embed = build_knock_request_embed(
        requester_mention=requester.mention,
        owner_mention=owner.mention,
        channel_name=channel.name,
        colour=accent,
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
    accent = await resolve_accent_color(ctx.db_path, channel.guild)
    embed = build_panel_embed(colour=accent)
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


def build_inline_panel_embed(
    owner: discord.Member, colour: "discord.Colour | None" = None
) -> discord.Embed:
    """Owner-greeting embed for the panel posted into the new channel's chat."""
    return _build_inline_panel_embed(owner_mention=owner.mention, colour=colour)


async def post_inline_panel(
    channel: discord.VoiceChannel,
    owner: discord.Member,
    *,
    colour: "discord.Colour | None" = None,
) -> discord.Message | None:
    """Post the control panel into a voice channel's text chat.

    Modern Discord voice channels host their own text chat (the "side chat"),
    so we drop the panel right there — owners don't have to navigate to the
    central control channel. Returns ``None`` if the bot lacks permission to
    post; caller should treat that as non-fatal.
    """
    embed = build_inline_panel_embed(owner, colour=colour)
    view = build_panel_view()
    try:
        return await channel.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        log.exception(
            "voice_master: failed to post inline panel in channel %d", channel.id
        )
        return None
