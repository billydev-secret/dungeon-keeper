from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord

from bot_modules.core.utils import disable_all_items

from bot_modules.core.branding import resolve_accent_color
from bot_modules.games.utils.game_manager import (
    create_game, update_game_message, update_game_state,
    modify_payload, get_game_payload, end_game, update_session,
    resolve_names,
)
from ..classic_logic import (
    add_player as cl_add_player,
    add_volunteer as cl_add_volunteer,
    build_initial_payload as cl_build_initial_payload,
    claim_start as cl_claim_start,
    clamp_tier as cl_clamp_tier,
    existing_fill_values as cl_existing_fill_values,
    filter_rescuers as cl_filter_rescuers,
    freeze_rescue as cl_freeze_rescue,
    init_rescue as cl_init_rescue,
    my_blank_ids as cl_my_blank_ids,
    remove_player as cl_remove_player,
    rescuers_done_count as cl_rescuers_done_count,
    set_rescue_fill_state as cl_set_rescue_fill_state,
    store_round1_fills as cl_store_round1_fills,
    store_rescue_fills as cl_store_rescue_fills,
)
from ..data import (
    pick_template, mark_template_used, get_prompts, get_channel_max_tier,
    HEAT_LABELS,
)
from ..distribution import (
    assign_blanks_round_robin, compute_unfilled, assign_rescue,
    players_done_count, unique_contributors,
)
from ..rendering import (
    build_join_embed, build_classic_fill_embed, build_classic_rescue_embed,
    build_classic_rescue_fill_embed, build_classic_reveal_embed,
    render_filled_body_attributed,
)
from ..modals import make_fill_modal
from ..views import (
    JoinView, ClassicFillView, ClassicRescueView, ClassicRescueFillView,
)

log = logging.getLogger(__name__)

FILL_TIMEOUT = 300   # seconds — round 1
CLAIM_TIMEOUT = 45   # seconds — rescue claim window
RESCUE_TIMEOUT = 120 # seconds — round 2 fill
POLL_INTERVAL = 15   # seconds — counter refresh cadence


async def run_classic(cog, *, channel, guild, host_id: int, host_name: str,
                      tier: int, template_id: str | None, tag: str | None) -> str | None:
    """Entry point for a Classic-mode LegitLibs round (interaction-free)."""
    db = cog.db

    # Resolve the guild accent once; every embed below follows it. Guard so a
    # branding/ctx hiccup falls back to each builder's phase color (via
    # ``color=None``), never crashing the game.
    try:
        accent = await resolve_accent_color(cog.bot.ctx.db_path, guild)
    except Exception:
        accent = None

    # Enforce per-channel tier cap
    max_tier = await get_channel_max_tier(db, channel.id)
    tier, clamped = cl_clamp_tier(tier, max_tier)
    if clamped:
        log.info(
            "legitlibs classic: tier clamped to %s (%s) in channel %s",
            max_tier, HEAT_LABELS[max_tier], channel.id,
        )

    # Pick a template
    prompts = await get_prompts(db)
    template = await pick_template(db, guild.id, tier, tag=tag, template_id=template_id)
    if not template:
        log.info(
            "legitlibs classic: no published templates for tier %s tag %r in channel %s",
            tier, tag, channel.id,
        )
        return None

    blanks = template["blanks"]

    # Create game record
    game_id = await create_game(
        db, channel.id, host_id, "legitlibs",
        state="joining",
        payload=cl_build_initial_payload(host_id, tier, template),
    )
    cog._game_canceled.discard(game_id)

    # Fill-phase state shared with submit/poll
    fill_msg: discord.Message | None = None
    fill_deadline: int | None = None

    # Rescue-phase state
    rescue_claim_msg: discord.Message | None = None
    rescue_fill_msg: discord.Message | None = None

    # ── Join phase ──────────────────────────────────────────────────────────
    join_embed = build_join_embed(
        host_name, template["title"], tier, "classic",
        1, template["player_min"], color=accent,
    )

    async def handle_join_action(action_interaction: discord.Interaction, action: str):
        payload = await get_game_payload(db, game_id)
        if payload.get("state") != "joining":
            await action_interaction.response.send_message(
                "The round has already started.", ephemeral=True)
            return

        if action == "join":
            uid = action_interaction.user.id
            if uid in payload["players"]:
                await action_interaction.response.send_message(
                    "You're already in!", ephemeral=True)
                return
            def _add(p):
                cl_add_player(p, uid)
            payload = await modify_payload(db, game_id, _add)
            await action_interaction.response.send_message("✅ You joined!", ephemeral=True)

            new_embed = build_join_embed(
                host_name, template["title"], tier, "classic",
                len(payload["players"]), template["player_min"], color=accent,
            )
            assert action_interaction.message is not None
            try:
                await action_interaction.message.edit(embed=new_embed)
            except discord.HTTPException:
                pass

        elif action == "leave":
            uid = action_interaction.user.id
            if uid == host_id:
                await action_interaction.response.send_message(
                    "You're the host! If you leave, the game will be cancelled. "
                    "Use **Cancel** instead.", ephemeral=True,
                )
                return
            if uid not in payload["players"]:
                await action_interaction.response.send_message(
                    "You're not in this round.", ephemeral=True)
                return
            def _remove(p):
                cl_remove_player(p, uid)
            payload = await modify_payload(db, game_id, _remove)
            await action_interaction.response.send_message("You've left.", ephemeral=True)

            new_embed = build_join_embed(
                host_name, template["title"], tier, "classic",
                len(payload["players"]), template["player_min"], color=accent,
            )
            assert action_interaction.message is not None
            try:
                await action_interaction.message.edit(embed=new_embed)
            except discord.HTTPException:
                pass

        elif action == "start":
            if len(payload["players"]) < template["player_min"]:
                await action_interaction.response.send_message(
                    f"Need at least {template['player_min']} players to start.",
                    ephemeral=True,
                )
                return
            claimed = False
            def _claim_start(p):
                nonlocal claimed
                assignments = assign_blanks_round_robin(blanks, p.get("players", []))
                claimed = cl_claim_start(p, assignments)
            payload = await modify_payload(db, game_id, _claim_start)
            if not claimed:
                await action_interaction.response.send_message(
                    "Round already started.", ephemeral=True)
                return
            await action_interaction.response.defer()
            join_view.stop()
            disable_all_items(join_view)
            assert action_interaction.message is not None
            try:
                await action_interaction.message.edit(view=join_view)
            except discord.HTTPException:
                pass
            await _run_fill_phase(payload)

    async def handle_join_cancel(action_interaction: discord.Interaction) -> None:
        cog._game_canceled.add(game_id)
        await end_game(db, game_id)
        cog._game_canceled.discard(game_id)
        join_view.stop()
        disable_all_items(join_view)
        cog.bot.active_views.pop(game_id, None)
        try:
            await action_interaction.response.edit_message(
                embed=discord.Embed(
                    title="📝 LegitLibs — Cancelled",
                    color=accent if accent is not None else 0x99AAB5,
                ),
                view=join_view,
            )
        except Exception:
            await action_interaction.response.defer()

    join_view = JoinView(game_id, host_id, db, cog.bot,
                         handle_join_action, handle_join_cancel)
    try:
        msg = await channel.send(embed=join_embed, view=join_view)
    except discord.Forbidden:
        await end_game(db, game_id)
        cog.bot.active_views.pop(game_id, None)
        log.warning("legitlibs launch lacked send perms in channel %s", channel.id)
        return None
    await update_game_message(db, game_id, msg.id)
    cog.bot.active_views[game_id] = join_view

    # ── Round 1 fill phase ──────────────────────────────────────────────────
    async def _run_fill_phase(payload: dict):
        nonlocal fill_msg, fill_deadline
        await update_game_state(db, game_id, "filling")

        player_ids = payload["players"]
        assignments = payload["assignments"]
        fill_deadline = int(datetime.now(timezone.utc).timestamp()) + FILL_TIMEOUT
        deadline = fill_deadline

        fill_embed = build_classic_fill_embed(
            host_name, template["title"], tier,
            len(player_ids),
            players_done_count(assignments, payload.get("fills", {}), player_ids),
            deadline, color=accent,
        )
        fill_view = ClassicFillView(
            game_id, host_id, db, cog.bot,
            _handle_round1_submit, _handle_fill_cancel,
        )
        cog.bot.active_views[game_id] = fill_view

        # Local alias: pyright can't narrow the nonlocal `fill_msg` binding.
        fill_msg = sent_msg = await channel.send(embed=fill_embed, view=fill_view)
        await update_game_message(db, game_id, sent_msg.id)

        await update_session(db, channel.id, game_id, player_ids)

        elapsed = 0
        while elapsed < FILL_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            if game_id in cog._game_canceled:
                return

            cur_payload = await get_game_payload(db, game_id)
            if cur_payload.get("state") != "filling":
                return

            cur_fills = cur_payload.get("fills", {})
            done = players_done_count(assignments, cur_fills, player_ids)
            new_embed = build_classic_fill_embed(
                host_name, template["title"], tier,
                len(player_ids), done, deadline, color=accent,
            )
            try:
                await sent_msg.edit(embed=new_embed)
            except discord.HTTPException:
                pass

            if done >= len(player_ids):
                break

        if game_id in cog._game_canceled:
            return

        fill_view.stop()
        disable_all_items(fill_view)
        try:
            await sent_msg.edit(view=fill_view)
        except discord.HTTPException:
            pass

        cur_payload = await get_game_payload(db, game_id)
        unfilled = compute_unfilled(blanks, cur_payload.get("fills", {}))
        if unfilled:
            await _run_rescue_claim_phase()
        else:
            await _run_reveal_phase()

    async def _handle_round1_submit(submit_interaction: discord.Interaction):
        cur_payload = await get_game_payload(db, game_id)
        if cur_payload.get("state") != "filling":
            await submit_interaction.response.send_message(
                "The fill phase is over.", ephemeral=True)
            return

        uid = submit_interaction.user.id
        if uid not in cur_payload["players"]:
            await submit_interaction.response.send_message(
                "You're not in this round.", ephemeral=True)
            return

        assignments = cur_payload.get("assignments", {})
        blank_ids = cl_my_blank_ids(assignments, uid)
        if not blank_ids:
            await submit_interaction.response.send_message(
                "You weren't assigned any blanks this round. "
                "You'll be eligible to volunteer if any go unfilled.",
                ephemeral=True,
            )
            return

        my_blanks = [b for b in blanks if b["id"] in blank_ids]

        cur_fills = cur_payload.get("fills", {})
        prior = cl_existing_fill_values(blanks, cur_fills, blank_ids)

        async def _save_fills(sub_interaction: discord.Interaction,
                              fills: dict, partial: bool):
            if partial:
                # Mid-flow page save — FillModalPage handles this internally
                # by writing to payload["submissions"]. Classic ignores that
                # path; we only write real data on the final (partial=False)
                # submit.
                return

            saved = False
            def _store(p):
                nonlocal saved
                saved = cl_store_round1_fills(p, fills, uid)
            updated_payload = await modify_payload(db, game_id, _store)
            if not saved:
                await sub_interaction.response.send_message(
                    "The fill phase has ended — your fills were not saved.",
                    ephemeral=True,
                )
                return

            had_all = all(bid in cur_fills for bid in blank_ids)
            msg_text = "✅ Fills updated!" if had_all else "✅ Fills saved!"
            await sub_interaction.response.send_message(msg_text, ephemeral=True)

            if fill_msg is not None and fill_deadline is not None \
                    and updated_payload.get("state") == "filling":
                cur_player_ids = updated_payload.get("players", [])
                done = players_done_count(
                    updated_payload.get("assignments", {}),
                    updated_payload.get("fills", {}),
                    cur_player_ids,
                )
                new_embed = build_classic_fill_embed(
                    host_name, template["title"], tier,
                    len(cur_player_ids), done, fill_deadline, color=accent,
                )
                try:
                    await fill_msg.edit(embed=new_embed)
                except discord.HTTPException:
                    pass

        modal = make_fill_modal(
            game_id, db, prompts, my_blanks, tier, _save_fills,
            existing_fills=prior,
        )
        await submit_interaction.response.send_modal(modal)

    async def _handle_fill_cancel(cancel_interaction: discord.Interaction):
        cog._game_canceled.add(game_id)
        cur_payload = await get_game_payload(db, game_id)
        await end_game(db, game_id, player_count=len(cur_payload.get("players", [])))
        cog._game_canceled.discard(game_id)
        view = cog.bot.active_views.pop(game_id, None)
        if view:
            view.stop()
            disable_all_items(view)
        try:
            await cancel_interaction.response.edit_message(
                embed=discord.Embed(
                    title="📝 LegitLibs — Cancelled",
                    color=accent if accent is not None else 0x99AAB5,
                ),
                view=None,
            )
        except Exception:
            await cancel_interaction.response.defer()

    # ── Rescue claim phase ──────────────────────────────────────────────────
    async def _run_rescue_claim_phase():
        nonlocal rescue_claim_msg
        await update_game_state(db, game_id, "rescuing_claim")
        claim_deadline_ts = int(datetime.now(timezone.utc).timestamp()) + CLAIM_TIMEOUT
        def _init_rescue(p):
            cl_init_rescue(p, claim_deadline_ts)
        payload = await modify_payload(db, game_id, _init_rescue)

        unfilled = compute_unfilled(blanks, payload.get("fills", {}))
        deadline = payload["rescue"]["claim_deadline"]

        rescue_embed = build_classic_rescue_embed(
            template["title"], tier, len(unfilled), [], deadline, color=accent,
        )
        rescue_view = ClassicRescueView(
            game_id, host_id, db, cog.bot,
            _handle_volunteer, _handle_rescue_cancel,
        )
        cog.bot.active_views[game_id] = rescue_view
        # Local alias: pyright can't narrow the nonlocal `rescue_claim_msg` binding.
        rescue_claim_msg = claim_msg = await channel.send(embed=rescue_embed, view=rescue_view)
        await update_game_message(db, game_id, claim_msg.id)

        elapsed = 0
        while elapsed < CLAIM_TIMEOUT:
            await asyncio.sleep(min(POLL_INTERVAL, CLAIM_TIMEOUT - elapsed))
            elapsed += POLL_INTERVAL
            if game_id in cog._game_canceled:
                return
            cur_payload = await get_game_payload(db, game_id)
            if cur_payload.get("state") != "rescuing_claim":
                return

            vols = cur_payload.get("rescue", {}).get("volunteers", [])
            new_embed = build_classic_rescue_embed(
                template["title"], tier, len(unfilled),
                resolve_names(guild, vols), deadline, color=accent,
            )
            try:
                await claim_msg.edit(embed=new_embed)
            except discord.HTTPException:
                pass

        if game_id in cog._game_canceled:
            return

        rescue_view.stop()
        disable_all_items(rescue_view)
        try:
            await claim_msg.edit(view=rescue_view)
        except discord.HTTPException:
            pass

        cur_payload = await get_game_payload(db, game_id)
        vols = cur_payload.get("rescue", {}).get("volunteers", [])
        if not vols:
            await _run_reveal_phase()
            return

        unfilled_now = compute_unfilled(blanks, cur_payload.get("fills", {}))
        rescue_assign = assign_rescue(unfilled_now, vols)
        fill_deadline_rescue = int(datetime.now(timezone.utc).timestamp()) + RESCUE_TIMEOUT
        def _freeze(p):
            cl_freeze_rescue(p, rescue_assign, fill_deadline_rescue)
        await modify_payload(db, game_id, _freeze)
        await _run_rescue_fill_phase()

    async def _handle_volunteer(vol_interaction: discord.Interaction):
        uid = vol_interaction.user.id
        outcome = {"value": "closed"}
        def _add_vol(p):
            outcome["value"] = cl_add_volunteer(p, uid)
        await modify_payload(db, game_id, _add_vol)
        if outcome["value"] == "closed":
            await vol_interaction.response.send_message(
                "The claim window is closed.", ephemeral=True)
            return
        if outcome["value"] == "not_player":
            await vol_interaction.response.send_message(
                "Only players in this round can volunteer.", ephemeral=True)
            return
        if outcome["value"] == "already":
            await vol_interaction.response.send_message(
                "You're already signed up. Stay tuned for your blanks.",
                ephemeral=True,
            )
            return
        await vol_interaction.response.send_message(
            "🙋 You're in the rescue squad! You'll get your blanks when the timer runs out.",
            ephemeral=True,
        )

    async def _handle_rescue_cancel(cancel_interaction: discord.Interaction):
        await _handle_fill_cancel(cancel_interaction)

    # ── Rescue fill phase ───────────────────────────────────────────────────
    async def _run_rescue_fill_phase():
        nonlocal rescue_fill_msg
        await update_game_state(db, game_id, "rescuing_fill")
        def _set_rescue_fill(p):
            cl_set_rescue_fill_state(p)
        payload = await modify_payload(db, game_id, _set_rescue_fill)

        rescue_assignments = payload["rescue"]["assignments"]
        vols = payload["rescue"]["volunteers"]
        deadline = payload["rescue"]["fill_deadline"]

        rescuers = cl_filter_rescuers(rescue_assignments, vols)

        fill_embed = build_classic_rescue_fill_embed(
            template["title"], tier,
            cl_rescuers_done_count(rescue_assignments, payload.get("fills", {}), rescuers),
            len(rescuers),
            deadline, color=accent,
        )
        rescue_fill_view = ClassicRescueFillView(
            game_id, host_id, db, cog.bot,
            _handle_rescue_submit, _handle_rescue_cancel,
        )
        cog.bot.active_views[game_id] = rescue_fill_view
        # Local alias: pyright can't narrow the nonlocal `rescue_fill_msg` binding.
        rescue_fill_msg = rfill_msg = await channel.send(embed=fill_embed, view=rescue_fill_view)
        await update_game_message(db, game_id, rfill_msg.id)

        elapsed = 0
        while elapsed < RESCUE_TIMEOUT:
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL
            if game_id in cog._game_canceled:
                return
            cur_payload = await get_game_payload(db, game_id)
            if cur_payload.get("state") != "rescuing_fill":
                return

            done = cl_rescuers_done_count(
                rescue_assignments, cur_payload.get("fills", {}), rescuers,
            )
            new_embed = build_classic_rescue_fill_embed(
                template["title"], tier, done, len(rescuers), deadline, color=accent,
            )
            try:
                await rfill_msg.edit(embed=new_embed)
            except discord.HTTPException:
                pass
            if done >= len(rescuers):
                break

        if game_id in cog._game_canceled:
            return

        rescue_fill_view.stop()
        disable_all_items(rescue_fill_view)
        try:
            await rfill_msg.edit(view=rescue_fill_view)
        except discord.HTTPException:
            pass

        await _run_reveal_phase()

    async def _handle_rescue_submit(submit_interaction: discord.Interaction):
        cur_payload = await get_game_payload(db, game_id)
        if cur_payload.get("state") != "rescuing_fill":
            await submit_interaction.response.send_message(
                "The rescue fill phase is over.", ephemeral=True)
            return

        uid = submit_interaction.user.id
        rescue_assignments = cur_payload.get("rescue", {}).get("assignments", {})
        rescue_blank_ids = cl_my_blank_ids(rescue_assignments, uid)
        if not rescue_blank_ids:
            await submit_interaction.response.send_message(
                "Rescue is for volunteers assigned blanks. Maybe next round!",
                ephemeral=True,
            )
            return

        my_blanks = [b for b in blanks if b["id"] in rescue_blank_ids]
        cur_fills = cur_payload.get("fills", {})
        prior = cl_existing_fill_values(blanks, cur_fills, rescue_blank_ids)

        async def _save_rescue_fills(sub_interaction: discord.Interaction,
                                      fills: dict, partial: bool):
            if partial:
                return
            saved = False
            def _store(p):
                nonlocal saved
                saved = cl_store_rescue_fills(p, fills, uid)
            await modify_payload(db, game_id, _store)
            if saved:
                await sub_interaction.response.send_message(
                    "🙌 Rescue fills saved!", ephemeral=True)
            else:
                await sub_interaction.response.send_message(
                    "The rescue phase has ended — your fills were not saved.",
                    ephemeral=True,
                )

        modal = make_fill_modal(
            game_id, db, prompts, my_blanks, tier, _save_rescue_fills,
            existing_fills=prior,
        )
        await submit_interaction.response.send_modal(modal)

    # ── Reveal phase ────────────────────────────────────────────────────────
    async def _run_reveal_phase():
        cur_payload = await get_game_payload(db, game_id)
        player_ids = cur_payload.get("players", [])
        fills = cur_payload.get("fills", {})
        try:
            await update_game_state(db, game_id, "revealing")

            filled_body = render_filled_body_attributed(
                template["body"], blanks, fills, guild,
            )
            contrib_ids = unique_contributors(fills)
            contrib_names = resolve_names(guild, contrib_ids)

            embed = build_classic_reveal_embed(
                template["title"], tier, filled_body, contrib_names, color=accent,
            )
            await channel.send(embed=embed)

            await mark_template_used(db, guild.id, template["template_id"])
        finally:
            await end_game(db, game_id,
                           player_count=len(player_ids),
                           round_count=1,
                           bot=cog.bot, player_ids=player_ids)
            cog.bot.active_views.pop(game_id, None)
            cog._game_canceled.discard(game_id)

    # Lobby is live; the round advances via button presses (defined above).
    return game_id
