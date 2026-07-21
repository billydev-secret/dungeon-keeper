from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord

from bot_modules.core.utils import disable_all_items

from bot_modules.games.utils.game_manager import (
    create_game, update_game_message, update_game_state,
    modify_payload, get_game_payload, end_game, update_session,
    resolve_name,
)
from bot_modules.games.constants import GAME_ICONS, PHASE_RESULTS
from bot_modules.core.branding import resolve_accent_color
from ..data import (
    pick_template, mark_template_used, get_prompts, get_channel_max_tier,
    HEAT_LABELS,
)
from ..quiplash_logic import (
    add_player as ql_add_player,
    build_initial_payload as ql_build_initial_payload,
    claim_start as ql_claim_start,
    clamp_tier as ql_clamp_tier,
    collect_complete_submissions as ql_collect_complete_submissions,
    get_prior_submission as ql_get_prior_submission,
    shuffle_reveal_order as ql_shuffle_reveal_order,
    store_submission as ql_store_submission,
    submitted_count as ql_submitted_count,
)
from ..rendering import (
    build_join_embed, build_fill_embed, build_reveal_embed,
    build_no_submissions_embed, render_filled_body,
    render_redacted_body,
)
from ..modals import make_fill_modal
from ..views import JoinView, QuiplashFillView

log = logging.getLogger(__name__)

FILL_TIMEOUT = 300  # seconds players have to submit

_GAME_ICONS_LL = GAME_ICONS["legitlibs"]


async def run_quiplash(cog, *, channel, guild, host_id: int, host_name: str,
                       tier: int, template_id: str | None, tag: str | None) -> str | None:
    """Entry point for a Quiplash-mode LegitLibs round (interaction-free)."""
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
    tier, clamped = ql_clamp_tier(tier, max_tier)
    if clamped:
        log.info(
            "legitlibs quiplash: tier clamped to %s (%s) in channel %s",
            max_tier, HEAT_LABELS[max_tier], channel.id,
        )

    # Pick a template
    prompts = await get_prompts(db)
    template = await pick_template(db, guild.id, tier, tag=tag, template_id=template_id)
    if not template:
        log.info(
            "legitlibs quiplash: no published templates for tier %s tag %r in channel %s",
            tier, tag, channel.id,
        )
        return None

    blanks = template["blanks"]

    # Create game record
    game_id = await create_game(
        db, channel.id, host_id, "legitlibs",
        state="joining",
        payload=ql_build_initial_payload(host_id, tier, template),
    )
    cog._game_canceled.discard(game_id)

    # ── Join phase ──────────────────────────────────────────────────────────
    join_embed = build_join_embed(
        host_name, template["title"], tier, "quiplash", 1, template["player_min"], color=accent,
    )

    async def handle_join_action(action_interaction: discord.Interaction, action: str):
        payload = await get_game_payload(db, game_id)
        if payload.get("state") != "joining":
            await action_interaction.response.send_message("The round has already started.", ephemeral=True)
            return

        if action == "join":
            uid = action_interaction.user.id
            if uid in payload["players"]:
                await action_interaction.response.send_message("You're already in!", ephemeral=True)
                return
            def _add(p):
                ql_add_player(p, uid)
            payload = await modify_payload(db, game_id, _add)
            await action_interaction.response.send_message("✅ You joined!", ephemeral=True)

            new_embed = build_join_embed(
                host_name, template["title"], tier, "quiplash",
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
                    f"Need at least {template['player_min']} players to start.", ephemeral=True
                )
                return
            claimed = False
            def _claim_start(p):
                nonlocal claimed
                claimed = ql_claim_start(p)
            payload = await modify_payload(db, game_id, _claim_start)
            if not claimed:
                await action_interaction.response.send_message("Round already started.", ephemeral=True)
                return
            await action_interaction.response.defer()
            join_view.stop()
            disable_all_items(join_view)
            assert action_interaction.message is not None
            try:
                await action_interaction.message.edit(view=join_view)
            except discord.HTTPException:
                pass
            await _run_fill_phase(action_interaction, payload)

    async def handle_cancel(action_interaction: discord.Interaction) -> None:
        cog._game_canceled.add(game_id)
        await end_game(db, game_id)
        cog._game_canceled.discard(game_id)
        join_view.stop()
        disable_all_items(join_view)
        cog.bot.active_views.pop(game_id, None)
        try:
            await action_interaction.response.edit_message(
                embed=discord.Embed(
                    title=f"{_GAME_ICONS_LL} LegitLibs — Cancelled",
                    color=accent if accent is not None else 0x99AAB5,
                ),
                view=join_view,
            )
        except Exception:
            await action_interaction.response.defer()

    join_view = JoinView(game_id, host_id, db, cog.bot, handle_join_action, handle_cancel)
    try:
        msg = await channel.send(embed=join_embed, view=join_view)
    except discord.Forbidden:
        await end_game(db, game_id)
        cog.bot.active_views.pop(game_id, None)
        log.warning("legitlibs launch lacked send perms in channel %s", channel.id)
        return None
    await update_game_message(db, game_id, msg.id)
    cog.bot.active_views[game_id] = join_view

    # ── Fill phase ──────────────────────────────────────────────────────────
    async def _run_fill_phase(start_interaction: discord.Interaction, payload: dict):
        await update_game_state(db, game_id, "filling")
        # NOTE: claim_start already set state="filling" in payload; this is
        # belt-and-braces in case some other code path lands here without it.
        def _set_filling(p):
            p["state"] = "filling"
        await modify_payload(db, game_id, _set_filling)

        player_ids = payload["players"]
        deadline = int((datetime.now(timezone.utc).timestamp()) + FILL_TIMEOUT)
        redacted = render_redacted_body(template["body"], blanks)

        fill_embed = build_fill_embed(
            host_name, template["title"], tier,
            len(player_ids), 0, deadline,
            redacted_body=redacted, color=accent,
        )
        fill_view = QuiplashFillView(game_id, host_id, db, cog.bot, _handle_submit_press, _handle_fill_cancel)
        cog.bot.active_views[game_id] = fill_view

        fill_msg = await channel.send(embed=fill_embed, view=fill_view)
        await update_game_message(db, game_id, fill_msg.id)

        await update_session(db, channel.id, game_id, player_ids)

        # Wait for timer, updating the counter periodically
        elapsed = 0
        while elapsed < FILL_TIMEOUT:
            await asyncio.sleep(15)
            elapsed += 15
            if game_id in cog._game_canceled:
                return

            cur_payload = await get_game_payload(db, game_id)
            if cur_payload.get("state") != "filling":
                return

            submitted = ql_submitted_count(cur_payload, player_ids)
            new_embed = build_fill_embed(
                host_name, template["title"], tier,
                len(player_ids), submitted, deadline,
                redacted_body=redacted, color=accent,
            )
            try:
                await fill_msg.edit(embed=new_embed)
            except discord.HTTPException:
                pass

            # Early exit if everyone submitted
            if submitted >= len(player_ids):
                break

        if game_id in cog._game_canceled:
            return

        # Disable fill view
        fill_view.stop()
        disable_all_items(fill_view)
        try:
            await fill_msg.edit(view=fill_view)
        except discord.HTTPException:
            pass

        await _run_reveal_phase(fill_msg)

    async def _handle_submit_press(submit_interaction: discord.Interaction):
        cur_payload = await get_game_payload(db, game_id)
        if cur_payload.get("state") != "filling":
            await submit_interaction.response.send_message("The fill phase is over.", ephemeral=True)
            return

        uid = submit_interaction.user.id
        if uid not in cur_payload["players"]:
            await submit_interaction.response.send_message("You're not in this round.", ephemeral=True)
            return

        prior_fills, had_complete = ql_get_prior_submission(cur_payload, uid)

        async def _save_fills(sub_interaction: discord.Interaction, fills: dict, partial: bool):
            saved = False
            def _store(p):
                nonlocal saved
                saved = ql_store_submission(p, uid, fills, partial)
            await modify_payload(db, game_id, _store)
            if saved:
                msg = "✅ Fills updated!" if had_complete else "✅ Fills saved! You can resubmit to tweak them."
                await sub_interaction.response.send_message(msg, ephemeral=True)
            else:
                await sub_interaction.response.send_message("The fill phase has ended — your fills were not saved.", ephemeral=True)

        modal = make_fill_modal(game_id, db, prompts, blanks, tier, _save_fills, existing_fills=prior_fills)
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

    # ── Reveal phase ────────────────────────────────────────────────────────
    async def _run_reveal_phase(fill_msg: discord.Message):
        cur_payload = await get_game_payload(db, game_id)
        player_ids = cur_payload.get("players")
        if not player_ids:
            return
        submissions = cur_payload.get("submissions", {})

        # Collect complete (non-partial) submissions
        complete = ql_collect_complete_submissions(submissions)

        try:
            if not complete:
                await channel.send(embed=build_no_submissions_embed(template["title"], tier, color=accent))
                return

            await update_game_state(db, game_id, "revealing")

            # Shuffle order for anonymous reveal
            uid_list = ql_shuffle_reveal_order(list(complete.keys()))

            total = len(uid_list)

            if total == 1:
                # Single submission — reveal attributed immediately
                uid = uid_list[0]
                fills = complete[uid]["fills"]
                name = resolve_name(guild, int(uid))
                filled = render_filled_body(template["body"], blanks, fills)
                embed = build_reveal_embed(template["title"], tier, filled, 1, 1, color=accent)
                embed.set_footer(text=f"📝 LegitLibs  •  by {name}")
                await channel.send(embed=embed)
            else:
                await channel.send(f"**Revealing {total} submissions…**")
                for i, uid in enumerate(uid_list, 1):
                    fills = complete[uid]["fills"]
                    filled = render_filled_body(template["body"], blanks, fills)
                    embed = build_reveal_embed(template["title"], tier, filled, i, total, color=accent)
                    await channel.send(embed=embed)
                    await asyncio.sleep(3)

                # Cast reveal — show who wrote which submission
                cast_lines = [
                    f"**#{i}** — {resolve_name(guild, int(uid))}"
                    for i, uid in enumerate(uid_list, 1)
                ]
                cast_embed = discord.Embed(
                    title=f"{_GAME_ICONS_LL} WHO WROTE WHAT",
                    description="\n".join(cast_lines),
                    color=accent if accent is not None else PHASE_RESULTS,
                )
                await channel.send(embed=cast_embed)

            await mark_template_used(db, guild.id, template["template_id"])
        finally:
            await end_game(db, game_id, player_count=len(player_ids), round_count=1,
                           bot=cog.bot, player_ids=player_ids)
            cog.bot.active_views.pop(game_id, None)
            cog._game_canceled.discard(game_id)

    # Lobby is live; the round advances via button presses (defined above).
    return game_id
