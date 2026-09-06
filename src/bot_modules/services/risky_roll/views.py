import asyncio
import logging
import math
import time

import discord

from bot_modules.duels.filters import contains_disallowed_content
from bot_modules.games.utils.question_source import channel_allows_nsfw, get_ffa_prompt
from . import state as app_state
from .formatters import (
    build_embed,
    build_fallback_question_content,
    build_how_to_play_content,
    build_pending_chase_content,
    build_pending_prompt_content,
    build_pending_question_summary,
    build_posted_chase_content,
    build_question_reply_content,
    format_user_mentions,
    get_text_channel,
    resolve_embed_accent,
)
from .logic import (
    PAYOFF_TICK_SECONDS,
    PayoffAction,
    PayoffDials,
    build_main_prompt_state,
    build_one_rule_prompt_state,
    choose_roll,
    effective_min_game_seconds,
    fallback_abandoned,
    fallback_blocked,
    has_blocked_edge,
    posted_chase_blocked,
    pending_payoff_action,
    record_fallback_failure,
    posted_chase_due,
    unasked_questioners,
)
from .models import (
    PendingQuestionState,
    PostedQuestionState,
    PromptKind,
    RiskyRollState,
    RoundResult,
)
log = logging.getLogger(__name__)

# Shared by the ordinary path and the no-contact path, deliberately. Both
# refusals are reached two ways — a round genuinely too small to resolve, and a
# round the gate will not let resolve — and the whole point of the second is
# that it is indistinguishable from the first. Two copies of the literal would
# let a copy edit to one of them turn the refusal into a tell, with no test
# failing (docs/no_contact_spec.md, "The disclosure rules").
NOT_ENOUGH_TEXT = "At least 2 players must roll."
AUTO_CLOSE_NOT_ENOUGH_TEXT = "Round auto-closed: not enough players rolled."


async def _blocked_pairs_in(state: RiskyRollState, *extra_user_ids: int) -> set[tuple[int, int]]:
    """No-contact pairs sitting in this round, read fresh from the list.

    Returns an empty set when the game has no db path (tests that drive the
    views without a cog load) — the gate is additive, so an unconfigured
    lookup must not break the ordinary round.
    """
    if app_state.db_path is None:
        return set()
    from bot_modules.services import no_contact_service

    user_ids = set(state.rolls) | set(extra_user_ids)
    return await asyncio.to_thread(
        no_contact_service.no_contact_pairs_among,
        app_state.db_path,
        state.guild_id,
        user_ids,
    )


async def _record_history(state: RiskyRollState) -> None:
    """Put the resolved round on the games record before its rows go.

    Best-effort: a history failure is logged and never holds up the close —
    the winner's prompt is what the room is waiting on.
    """
    if app_state.store is None:
        return
    try:
        await app_state.store.record_round_history(state)
    except Exception:
        log.exception("Risky Rolls: failed to record round %s to history.", state.game_id)


async def schedule_auto_close(client: discord.Client, game_id: str, delay: float) -> None:
    if delay > 0:
        await asyncio.sleep(delay)
    await auto_close_round(client, game_id)


async def auto_close_round(client: discord.Client, game_id: str) -> None:
    async with app_state.get_game_lock(game_id):
        app_state.auto_close_tasks.pop(game_id, None)

        state = app_state.active_games.get(game_id)
        if not state or not state.is_open:
            return

        channel_id = state.channel_id

        # Before resolving, not after: `resolve` flips `is_open` and runs the
        # hidden tie roll-offs as a side effect, so a refusal that came later
        # would have to unpick them.
        blocked_pairs = await _blocked_pairs_in(state)
        gate_blocked = has_blocked_edge(state.rolls, blocked_pairs)

        resolution = state.resolve()
        channel = await get_text_channel(client, channel_id)

        if gate_blocked or resolution.result_type == RoundResult.NOT_ENOUGH:
            state.is_open = False
            app_state.active_games.pop(game_id, None)
            if app_state.store is not None:
                await app_state.store.delete_round(game_id)
            if channel is not None:
                await disable_round_message(state, channel)
                await channel.send(AUTO_CLOSE_NOT_ENOUGH_TEXT)
            return

        closed_view = RiskyRollView(game_id)
        closed_view.disable_all_items()

        channel_forbidden = False
        if state.message_id is not None and channel is not None:
            guild = getattr(channel, "guild", None)
            accent = await resolve_embed_accent(guild)
            try:
                await channel.get_partial_message(state.message_id).edit(
                    embed=build_embed(state, guild, accent), view=closed_view
                )
            except discord.Forbidden:
                channel_forbidden = True
                log.error(
                    "Auto-close: bot is missing access to #%s (game %s).",
                    getattr(channel, "name", channel_id), game_id,
                )
            except (discord.NotFound, discord.HTTPException):
                log.exception("Auto-close: failed to edit round message in #%s.", getattr(channel, "name", channel_id))

        app_state.active_games.pop(game_id, None)
        await _record_history(state)
        if app_state.store is not None:
            await app_state.store.delete_round(game_id)

        if channel is None:
            log.error("Auto-close: could not access channel %s; round closed with no prompt sent.", channel_id)
            return

        if channel_forbidden:
            log.error(
                "Auto-close: skipping winner prompt for game %s — bot has no access to #%s.",
                game_id, getattr(channel, "name", channel_id),
            )
            return

        await _send_question_prompts_channel(client, channel, game_id, state, resolution)


async def _register_prompt(
    game_id: str,
    prompt_state: PendingQuestionState,
    message: discord.Message | discord.WebhookMessage,
) -> None:
    prompt_state.prompt_message_id = message.id
    app_state.pending_questions[game_id] = prompt_state
    if app_state.store is not None:
        await app_state.store.save_pending_question(prompt_state)


async def _register_posted_question(posted: PostedQuestionState) -> None:
    app_state.posted_questions[posted.message_id] = posted
    if app_state.store is not None:
        try:
            await app_state.store.save_posted_question(posted)
        except Exception:
            app_state.posted_questions.pop(posted.message_id, None)
            log.exception("Failed to persist posted question state for message %s.", posted.message_id)


async def _clear_posted_question(message_id: int) -> None:
    app_state.posted_questions.pop(message_id, None)
    if app_state.store is not None:
        await app_state.store.delete_posted_question(message_id)


async def _send_question_message(
    *,
    interaction: discord.Interaction,
    pending: PendingQuestionState,
    asker_id: int,
    question_text: str,
    asker_rolled_100: bool,
    target_rolled_1: bool,
) -> bool:
    target_mentions = format_user_mentions(pending.participant_user_ids)
    try:
        question_msg = await interaction.followup.send(
            content=f"{target_mentions}\n<@{asker_id}> asks:\n{question_text}",
            allowed_mentions=discord.AllowedMentions(users=True),
            ephemeral=False,
            wait=True,
            view=QuestionReplyView(),
        )
    except discord.HTTPException:
        log.exception("Failed to deliver question for game %s.", pending.game_id)
        await interaction.followup.send("I could not send the question. Please try again.", ephemeral=True)
        return False

    posted = PostedQuestionState(
        message_id=question_msg.id,
        channel_id=pending.channel_id,
        guild_id=pending.guild_id,
        asker_id=asker_id,
        allowed_replier_ids=set(pending.participant_user_ids),
        question_text=question_text,
        asker_rolled_100=asker_rolled_100,
        target_rolled_1=target_rolled_1,
    )
    await _register_posted_question(posted)
    return True


async def _send_and_register_prompt(send_fn, game_id: str, prompt_state: PendingQuestionState):
    message = await send_fn(
        content=build_pending_prompt_content(prompt_state),
        allowed_mentions=discord.AllowedMentions(users=True),
        view=SixtyNineQuestionView(game_id),
    )
    try:
        await _register_prompt(game_id, prompt_state, message)
    except Exception:
        app_state.pending_questions.pop(game_id, None)
        if app_state.store is not None:
            await app_state.store.delete_pending_question(game_id)
        raise
    return message


async def _try_send_one_rule_prompt(send_fn, game_id: str, state: RiskyRollState) -> None:
    one_rule_prompt = build_one_rule_prompt_state(game_id, state)
    if one_rule_prompt is None:
        return
    one_game_id = f"{game_id}:1"
    try:
        await _send_and_register_prompt(send_fn, one_game_id, one_rule_prompt)
    except Exception:
        log.exception("Failed to send 1-rule prompt for game %s.", game_id)
        app_state.pending_questions.pop(one_game_id, None)
        if app_state.store is not None:
            await app_state.store.delete_pending_question(one_game_id)


async def _send_question_prompts_channel(
    client: discord.Client,
    channel: discord.TextChannel | discord.Thread,
    game_id: str,
    state: RiskyRollState,
    resolution,
) -> None:
    ensure_payoff_chaser(client)
    main_prompt = build_main_prompt_state(game_id, state, resolution.result_type)
    if main_prompt is None:
        log.warning("Auto-close: no prompt state built for game %s.", game_id)
        return

    try:
        await _send_and_register_prompt(channel.send, game_id, main_prompt)
    except discord.Forbidden:
        log.error("Auto-close: missing access to #%s (game %s).", getattr(channel, "name", state.channel_id), game_id)
        return
    except Exception:
        log.exception("Auto-close: failed to send winner prompt for game %s.", game_id)
        await disable_pending_question_message(client, main_prompt, "Risky Rolls could not prepare the question prompt.")
        try:
            await channel.send("The round ended but the winner prompt could not be sent. Please start a new round.")
        except Exception:
            log.exception("Auto-close: also failed to send fallback message for game %s.", game_id)
        return

    if resolution.result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
        return

    await _try_send_one_rule_prompt(channel.send, game_id, state)


async def _send_question_prompts_followup(
    interaction: discord.Interaction,
    game_id: str,
    state: RiskyRollState,
    resolution,
) -> None:
    ensure_payoff_chaser(interaction.client)
    main_prompt = build_main_prompt_state(game_id, state, resolution.result_type)
    if main_prompt is None:
        log.warning("Close: no prompt state built for game %s.", game_id)
        return

    async def send_via_followup(**kwargs):
        return await interaction.followup.send(wait=True, **kwargs)

    try:
        await _send_and_register_prompt(send_via_followup, game_id, main_prompt)
    except Exception:
        await disable_pending_question_message(interaction.client, main_prompt, "Risky Rolls could not prepare the question prompt.")
        raise

    if resolution.result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
        return

    await _try_send_one_rule_prompt(send_via_followup, game_id, state)


async def _create_room_thread(
    channel, state: PendingQuestionState, question_text: str
) -> discord.Thread | None:
    """The public thread a 69 room question is asked in.

    Hung off the prompt message when there is one, else a fresh thread in the
    channel; ``None`` (and a log line) when the channel is not a text channel
    or Discord refuses, in which case the caller posts in the channel itself.
    """
    thread_name = question_text[:97] + "…" if len(question_text) > 97 else question_text
    try:
        if isinstance(channel, discord.TextChannel) and state.prompt_message_id is not None:
            partial_msg = channel.get_partial_message(state.prompt_message_id)
            return await partial_msg.create_thread(name=thread_name, auto_archive_duration=1440)
        if isinstance(channel, discord.TextChannel):
            return await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,
            )
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Failed to create thread for 69 question in game %s.", state.game_id)
    return None


async def _room_pings(state: PendingQuestionState, asker_id: int) -> set[int]:
    """Who a room question @-pings: every participant but the asker's
    no-contact partners (docs/no_contact_spec.md, Risky Rolls)."""
    pinged = set(state.participant_user_ids)
    if app_state.db_path is not None:
        from bot_modules.services import no_contact_service

        partners = await asyncio.to_thread(
            no_contact_service.no_contact_partners,
            app_state.db_path,
            state.guild_id,
            asker_id,
        )
        pinged -= partners
    return pinged


# ── Chasing the payoff ───────────────────────────────────────────────
#
# One background loop, started lazily from the game's own traffic (a roll, a
# round closing) rather than from cog load, which this module does not own.
# Each tick it reads the two dashboard dials fresh and looks at every pending
# prompt and posted question in memory. The decisions are in logic.py
# (`pending_payoff_action`, `posted_chase_due`); this is only the sending.

_chaser_task: asyncio.Task | None = None


def ensure_payoff_chaser(client: discord.Client) -> None:
    """Start the chaser loop if it is not already running.

    A no-op without a store (tests that drive the views bare), so nothing
    here spawns a task under a test that never asked for one.
    """
    global _chaser_task
    if app_state.store is None:
        return
    if _chaser_task is not None and not _chaser_task.done():
        return
    _chaser_task = asyncio.create_task(_payoff_chaser_loop(client), name="risky-payoff-chaser")


def stop_payoff_chaser() -> None:
    global _chaser_task
    if _chaser_task is not None:
        _chaser_task.cancel()
        _chaser_task = None


async def _payoff_chaser_loop(client: discord.Client) -> None:
    while True:
        await asyncio.sleep(PAYOFF_TICK_SECONDS)
        try:
            await run_payoff_pass(client)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Risky Rolls: payoff pass failed; will retry next tick.")


async def _payoff_dials_for(guild_id: int | None) -> PayoffDials:
    if guild_id is None or app_state.store is None:
        return PayoffDials()
    try:
        return (await app_state.store.load_payoff_dials()).get(guild_id, PayoffDials())
    except Exception:
        log.exception("Risky Rolls: could not read the payoff dials for guild %s.", guild_id)
        return PayoffDials()


async def run_payoff_pass(client: discord.Client, now: float | None = None) -> int:
    """One sweep of the pending prompts and posted questions. Returns how
    many chases or fallbacks went out.

    At most one action per channel per pass: with the dials just switched
    on over a backlog of stale prompts, this drains them a message every
    tick instead of dumping a week of questions into the room at once. In
    steady state two prompts falling due inside one tick is rare, and a
    five-minute delay on the second is invisible.
    """
    if app_state.store is None:
        return 0
    now = time.time() if now is None else now
    dials_by_guild = await app_state.store.load_payoff_dials()
    if not any(d.enabled for d in dials_by_guild.values()):
        return 0

    acted = 0
    touched_channels: set[int] = set()

    for pending in list(app_state.pending_questions.values()):
        dials = dials_by_guild.get(pending.guild_id)
        if dials is None or pending.channel_id in touched_channels:
            continue
        action = pending_payoff_action(pending, dials, now)
        if action is None:
            continue
        async with app_state.get_game_lock(pending.game_id):
            # Re-check under the lock: the winner may have asked meanwhile.
            if app_state.pending_questions.get(pending.game_id) is not pending:
                continue
            if pending_payoff_action(pending, dials, now) != action:
                continue
            try:
                if action is PayoffAction.FALLBACK:
                    sent = await _post_fallback_question(client, pending, now)
                    if not sent:
                        await _record_fallback_failure(pending, now)
                else:
                    sent = await _chase_pending(client, pending, dials, now)
            except Exception:
                log.exception("Risky Rolls: %s failed for prompt %s.", action.value, pending.game_id)
                if action is PayoffAction.FALLBACK:
                    await _record_fallback_failure(pending, now)
                continue
        if sent:
            acted += 1
            touched_channels.add(pending.channel_id)

    for posted in list(app_state.posted_questions.values()):
        dials = dials_by_guild.get(posted.guild_id)
        if dials is None or posted.channel_id in touched_channels:
            continue
        if not posted_chase_due(posted, dials, now):
            continue
        async with app_state.get_message_lock(posted.message_id):
            if app_state.posted_questions.get(posted.message_id) is not posted:
                continue
            try:
                sent = await _chase_posted(client, posted, now)
            except Exception:
                log.exception("Risky Rolls: chase failed for question %s.", posted.message_id)
                continue
        if sent:
            acted += 1
            touched_channels.add(posted.channel_id)

    return acted


async def _record_fallback_failure(pending: PendingQuestionState, now: float) -> None:
    """Count one failed fallback and persist it, giving up after the last.

    Every reason a fallback can fail is one the next tick cannot fix — an
    empty bank, a channel the bot can no longer reach, a pairing the
    no-contact list now forbids — so the attempt is stamped and the prompt
    backs off (``logic.fallback_retry_delay``) instead of being picked up
    again five minutes later for the week the row lives.
    """
    record_fallback_failure(pending, now)
    if app_state.store is not None:
        try:
            await app_state.store.save_pending_question(pending)
        except Exception:
            # In-memory the count still stands, so the backoff holds until a
            # restart; losing the row must not take the whole pass down.
            log.exception("Risky Rolls: could not record a failed fallback for %s.", pending.game_id)
    if fallback_abandoned(pending):
        log.warning(
            "Risky Rolls: giving up on a fallback question for prompt %s after %d attempts.",
            pending.game_id, pending.fallback_attempts,
        )


async def _chase_pending(
    client: discord.Client, pending: PendingQuestionState, dials: PayoffDials, now: float
) -> bool:
    channel = await get_text_channel(client, pending.channel_id)
    if channel is None:
        return False
    await channel.send(
        content=build_pending_chase_content(pending, dials),
        allowed_mentions=discord.AllowedMentions(users=True),
    )
    pending.chased_at = now
    if app_state.store is not None:
        await app_state.store.save_pending_question(pending)
    return True


async def _chase_posted(client: discord.Client, posted: PostedQuestionState, now: float) -> bool:
    """The one re-ping of the answerer(s) of a posted question.

    Only the answerers ring: the asker's ``<@id>`` is in the content so it
    renders as a name, not so they get pinged about their own question. A
    pairing the no-contact list now forbids is skipped the same silent way
    as the fallback — the row is still stamped as chased so the next tick
    does not pick it again, which is indistinguishable from the dial being
    off.
    """
    channel = await get_text_channel(client, posted.channel_id)
    if channel is None:
        return False
    blocked_pairs = await _blocked_pairs_in_ids(
        posted.guild_id, posted.allowed_replier_ids | {posted.asker_id}
    )
    sent = False
    if not posted_chase_blocked(posted, blocked_pairs):
        await channel.send(
            content=build_posted_chase_content(posted),
            allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(id=uid) for uid in sorted(posted.allowed_replier_ids)]
            ),
        )
        sent = True
    posted.chased_at = now
    if app_state.store is not None:
        await app_state.store.save_posted_question(posted)
    return sent


async def draw_fallback_question(games_db, allow_nsfw: bool) -> str | None:
    """A Truth from the question bank, for a winner who never asked.

    The Truth or Dare bank is the same source the rotation rooms' prompts
    come from; a Truth is a question one person puts to another, which is
    exactly the seat the winner left empty. ``allow_nsfw`` is the channel's
    own age gate (`channel_allows_nsfw`), never a bot-side toggle.
    """
    picked = await get_ffa_prompt(games_db, kind="truth", allow_nsfw=allow_nsfw)
    if picked is None:
        return None
    _label, text = picked
    return text


async def _post_fallback_question(
    client: discord.Client, pending: PendingQuestionState, now: float
) -> bool:
    """Post a bank question as the winner's, so the loser still answers.

    Returns False (and leaves the prompt alone for the next tick) when the
    channel or the bank cannot be reached. A pairing the no-contact list now
    forbids is skipped the same silent way — nothing posts, nothing says why.
    """
    games_db = getattr(client, "games_db", None)
    if games_db is None:
        log.warning("Risky Rolls: no games_db on the client; cannot draw a fallback question.")
        return False
    channel = await get_text_channel(client, pending.channel_id)
    if channel is None:
        return False

    owed = unasked_questioners(pending)
    if not owed:
        return False
    asker_id = owed[0]
    targets = set(pending.participant_user_ids)

    if pending.prompt_kind == PromptKind.ROOM:
        pinged = await _room_pings(pending, asker_id)
    else:
        blocked_pairs = await _blocked_pairs_in_ids(pending.guild_id, targets | {asker_id})
        if fallback_blocked(asker_id, targets, blocked_pairs):
            return False
        pinged = targets

    question_text = await draw_fallback_question(games_db, channel_allows_nsfw(channel))
    if question_text is None:
        log.warning("Risky Rolls: the question bank had nothing to ask for prompt %s.", pending.game_id)
        return False

    content = build_fallback_question_content(pending, asker_id, question_text, pinged)
    mentions = discord.AllowedMentions(users=True)

    if pending.prompt_kind == PromptKind.ROOM:
        thread = await _create_room_thread(channel, pending, question_text)
        target: discord.abc.Messageable = thread if thread is not None else channel
        await target.send(content=content, allowed_mentions=mentions)
    else:
        message = await channel.send(content=content, allowed_mentions=mentions, view=QuestionReplyView())
        posted = PostedQuestionState(
            message_id=message.id,
            channel_id=pending.channel_id,
            guild_id=pending.guild_id,
            asker_id=asker_id,
            allowed_replier_ids=targets,
            question_text=question_text,
            asker_rolled_100=pending.prompt_kind == PromptKind.DIRECT and len(targets) > 1,
            target_rolled_1=pending.prompt_kind == PromptKind.TWO_QUESTIONERS,
            from_bank=True,
            created_at=now,
        )
        await _register_posted_question(posted)

    pending.questioners_asked.add(asker_id)
    if pending.questions_remaining > 0:
        # A 1-rule prompt where neither questioner asked: the deck has spoken
        # for the winner and the second questioner still owes theirs. Keep
        # the prompt exactly as the modal does when the first of two asks by
        # hand — re-saved (``created_at`` untouched) with its message updated
        # — so the next tick speaks for them too, one message apart.
        if app_state.store is not None:
            await app_state.store.save_pending_question(pending)
        if pending.prompt_message_id is not None:
            try:
                await channel.get_partial_message(pending.prompt_message_id).edit(
                    content=build_pending_prompt_content(pending),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        return True

    app_state.pending_questions.pop(pending.game_id, None)
    if app_state.store is not None:
        await app_state.store.delete_pending_question(pending.game_id)
    await disable_pending_question_message(
        client,
        pending,
        build_pending_question_summary(pending, question_text, asker_id, from_bank=True),
    )
    return True


async def _blocked_pairs_in_ids(guild_id: int, user_ids: set[int]) -> set[tuple[int, int]]:
    if app_state.db_path is None:
        return set()
    from bot_modules.services import no_contact_service

    return await asyncio.to_thread(
        no_contact_service.no_contact_pairs_among, app_state.db_path, guild_id, user_ids
    )


class BaseRiskyRollView(discord.ui.View):
    def __init__(self, game_id: str = ""):
        super().__init__(timeout=None)
        self.game_id = game_id

    def disable_all_items(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        # Interaction token expired (user clicked too slowly) — nothing we can do
        if isinstance(error, discord.NotFound) and error.code == 10062:
            log.debug(
                "Interaction expired in %s (game %s) — user clicked after token timeout",
                type(self).__name__, self.game_id or "?",
            )
            return
        if self.game_id:
            log.exception("Unhandled error in %s (game %s)", type(self).__name__, self.game_id, exc_info=error)
        else:
            log.exception("Unhandled error in %s", type(self).__name__, exc_info=error)
        msg = "Something went wrong. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class RiskyRollView(BaseRiskyRollView):
    @discord.ui.button(
        label="Roll",
        style=discord.ButtonStyle.primary,
        custom_id="rr:roll",
        emoji="🎲",
    )
    async def roll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        # The chaser is started lazily from the game's own traffic rather than
        # at cog load, so prompts restored across a restart are picked up by
        # the first roll after it, not the next round close.
        ensure_payoff_chaser(interaction.client)
        async with app_state.get_game_lock(self.game_id):
            state = app_state.active_games.get(self.game_id)
            if not state or not state.is_open:
                await interaction.followup.send("No open round to roll in.", ephemeral=True)
                return

            if not state.can_roll(interaction.user.id):
                await interaction.followup.send("You already rolled this round.", ephemeral=True)
                return

            # The draw itself is the gate. A round with no no-contact pair in
            # it — nearly every round — takes the same honest randint it always
            # did; only a value that would pair a blocked couple is redrawn.
            # Nothing is refused, so there is no refusal to disguise.
            blocked_pairs = await _blocked_pairs_in(state, interaction.user.id)
            roll = choose_roll(state.rolls, interaction.user.id, blocked_pairs)
            state.add_roll(interaction.user.id, roll)
            # Cache the roller's name so the roster embed can show it as text
            # instead of a <@id> mention that some viewers can't resolve.
            app_state.display_names[interaction.user.id] = interaction.user.display_name
            if app_state.store is not None:
                await app_state.store.save_single_roll(state.game_id, interaction.user.id, roll)

            log.info(
                "Channel #%s: %s rolled %s",
                getattr(interaction.channel, "name", state.channel_id),
                interaction.user.display_name,
                roll,
            )

            accent = await resolve_embed_accent(interaction.guild)
            await interaction.edit_original_response(embed=build_embed(state, interaction.guild, accent), view=self)

            if state.auto_close_players and len(state.rolls) == state.auto_close_players:
                task = app_state.auto_close_tasks.pop(self.game_id, None)
                if task:
                    task.cancel()
                elapsed = time.time() - state.created_at
                min_secs = effective_min_game_seconds(
                    app_state.min_game_seconds, state.guild_id, state.skip_min_game_time
                )
                delay = max(0.0, min_secs - elapsed)
                app_state.auto_close_tasks[self.game_id] = asyncio.create_task(
                    schedule_auto_close(interaction.client, self.game_id, delay)
                )
            guild_id = state.guild_id

        # Quest trigger, outside the game lock — the roll itself is the
        # qualifying act, so it fires here rather than at round close.
        from typing import cast

        from bot_modules.core.app_context import Bot
        from bot_modules.economy.game_rewards import fire_member_trigger

        await fire_member_trigger(
            cast(Bot, interaction.client), guild_id, interaction.user.id,
            "risky_roll", occurrence=str(self.game_id),
        )

    @discord.ui.button(
        label="Help",
        style=discord.ButtonStyle.secondary,
        custom_id="rr:help",
        emoji="❓",
    )
    async def how_to_play_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        dials = await _payoff_dials_for(interaction.guild_id)
        await interaction.response.send_message(
            content=build_how_to_play_content(dials),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Close Round",
        style=discord.ButtonStyle.danger,
        custom_id="rr:close",
        emoji="🔒",
    )
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with app_state.get_game_lock(self.game_id):
            state = app_state.active_games.get(self.game_id)
            if not state or not state.is_open:
                await interaction.response.send_message("No active game.", ephemeral=True)
                return

            is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
            if interaction.user.id != state.opener_id and not is_admin:
                await interaction.response.send_message(
                    "❌ Only the round opener can close this round.",
                    ephemeral=True,
                )
                return

            min_secs = effective_min_game_seconds(
                app_state.min_game_seconds, state.guild_id, state.skip_min_game_time
            )
            if min_secs:
                elapsed = time.time() - state.created_at
                remaining = math.ceil(min_secs - elapsed)
                if remaining > 0:
                    await interaction.response.send_message(
                        f"This round cannot be closed yet. Please wait {remaining} more second(s).",
                        ephemeral=True,
                    )
                    return

            # Authoritative. The roll-time nudge is what keeps this from
            # firing; this is what makes it a guarantee. Runs before `resolve`,
            # which would otherwise close the round out from under a refusal.
            blocked_pairs = await _blocked_pairs_in(state)
            if has_blocked_edge(state.rolls, blocked_pairs):
                await interaction.response.send_message(NOT_ENOUGH_TEXT, ephemeral=True)
                return

            resolution = state.resolve()

            if resolution.result_type == RoundResult.NOT_ENOUGH:
                await interaction.response.send_message(NOT_ENOUGH_TEXT, ephemeral=True)
                return

            task = app_state.auto_close_tasks.pop(self.game_id, None)
            if task:
                task.cancel()

            app_state.active_games.pop(self.game_id, None)
            await _record_history(state)
            if app_state.store is not None:
                await app_state.store.delete_round(self.game_id)

            closed_view = RiskyRollView(self.game_id)
            closed_view.disable_all_items()

            try:
                accent = await resolve_embed_accent(interaction.guild)
                await interaction.response.edit_message(embed=build_embed(state, interaction.guild, accent), view=closed_view)
            except discord.HTTPException:
                log.exception("Failed to close round in #%s.", getattr(interaction.channel, "name", state.channel_id))
                await interaction.response.send_message(
                    "Round closed, but the message could not be updated. Start a new round.",
                    ephemeral=True,
                )
                return

            await _send_question_prompts_followup(interaction, self.game_id, state, resolution)


class SixtyNineQuestionModal(discord.ui.Modal, title="Ask A Question"):
    question = discord.ui.TextInput(
        label="Your question",
        placeholder="What do you want to ask them?",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, game_id: str):
        super().__init__()
        self.game_id = game_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async with app_state.get_game_lock(self.game_id):
            state = app_state.pending_questions.get(self.game_id)
            if state is None:
                await interaction.response.send_message(
                    "There is no pending winner question for this round.",
                    ephemeral=True,
                )
                return

            asker_id = interaction.user.id

            if asker_id not in state.allowed_questioners():
                await interaction.response.send_message(
                    "❌ Only the eligible players can send a question.",
                    ephemeral=True,
                )
                return

            if asker_id in state.questioners_asked:
                await interaction.response.send_message(
                    "You already asked your question.",
                    ephemeral=True,
                )
                return

            question_text = self.question.value.strip()
            if not question_text:
                await interaction.response.send_message(
                    "Enter a question before sending it.",
                    ephemeral=True,
                )
                return

            if contains_disallowed_content(question_text):
                await interaction.response.send_message(
                    "That question contains disallowed content. Please rephrase.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            if state.prompt_kind == PromptKind.ROOM:
                thread = await _create_room_thread(interaction.channel, state, question_text)

                # A room question is not directed contact, so it posts intact
                # and the thread stays public — she can read it if she wants,
                # exactly as she can read anything else he says in the channel.
                # What she does not get is the bot @-pinging her with his words
                # attached (docs/no_contact_spec.md, Risky Rolls).
                pinged = await _room_pings(state, asker_id)
                all_mentions = format_user_mentions(pinged)
                content = f"{all_mentions}\n<@{asker_id}> asks:\n{question_text}"

                try:
                    if thread is not None:
                        await thread.send(content=content, allowed_mentions=discord.AllowedMentions(users=True))
                    else:
                        await interaction.followup.send(
                            content=content,
                            allowed_mentions=discord.AllowedMentions(users=True),
                            ephemeral=False,
                        )
                except discord.HTTPException:
                    log.exception("Failed to post 69 question for game %s.", self.game_id)
                    await interaction.followup.send("I could not send the question. Please try again.", ephemeral=True)
                    return

                app_state.pending_questions.pop(self.game_id, None)
                if app_state.store is not None:
                    await app_state.store.delete_pending_question(self.game_id)
                await disable_pending_question_message(
                    interaction.client,
                    state,
                    build_pending_question_summary(state, question_text, asker_id),
                )
                await interaction.followup.send("Question posted in a thread.", ephemeral=True)
                return

            if state.prompt_kind == PromptKind.TWO_QUESTIONERS:
                if not await _send_question_message(
                    interaction=interaction,
                    pending=state,
                    asker_id=asker_id,
                    question_text=question_text,
                    asker_rolled_100=False,
                    target_rolled_1=True,
                ):
                    return

                state.questioners_asked.add(asker_id)

                if state.questions_remaining > 0:
                    if app_state.store is not None:
                        await app_state.store.save_pending_question(state)
                    remaining_id = next(
                        uid for uid in [state.winner_id, state.extra_questioner_id]
                        if uid is not None and uid not in state.questioners_asked
                    )
                    channel = await get_text_channel(interaction.client, state.channel_id)
                    if channel is not None and state.prompt_message_id is not None:
                        try:
                            await channel.get_partial_message(state.prompt_message_id).edit(
                                content=build_pending_prompt_content(state),
                                allowed_mentions=discord.AllowedMentions(users=True),
                            )
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                    await interaction.followup.send(
                        f"Question sent! Waiting for <@{remaining_id}> to ask their question.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions(users=False),
                    )
                    return

                app_state.pending_questions.pop(self.game_id, None)
                if app_state.store is not None:
                    await app_state.store.delete_pending_question(self.game_id)
                await disable_pending_question_message(
                    interaction.client,
                    state,
                    build_pending_question_summary(state, question_text, asker_id),
                )
                await interaction.followup.send("Question sent.", ephemeral=True)
                return

            if not await _send_question_message(
                interaction=interaction,
                pending=state,
                asker_id=asker_id,
                question_text=question_text,
                asker_rolled_100=len(state.participant_user_ids) > 1,
                target_rolled_1=False,
            ):
                return

            app_state.pending_questions.pop(self.game_id, None)
            if app_state.store is not None:
                await app_state.store.delete_pending_question(self.game_id)
            await disable_pending_question_message(
                interaction.client,
                state,
                build_pending_question_summary(state, question_text, asker_id),
            )
            target_count = len(state.participant_user_ids)
            await interaction.followup.send(
                "Question sent to the selected player." if target_count == 1 else "Question sent to both players.",
                ephemeral=True,
            )


class SixtyNineQuestionView(BaseRiskyRollView):
    @discord.ui.button(
        label="Ask Question",
        style=discord.ButtonStyle.success,
        custom_id="rr:ask",
        emoji="💬",
    )
    async def ask_question_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        async with app_state.get_game_lock(self.game_id):
            state = app_state.pending_questions.get(self.game_id)
            if state is None:
                await interaction.response.send_message(
                    "There is no pending winner question for this round.",
                    ephemeral=True,
                )
                return

            if interaction.user.id not in state.allowed_questioners():
                await interaction.response.send_message(
                    "❌ Only the eligible players can send a question.",
                    ephemeral=True,
                )
                return

            if interaction.user.id in state.questioners_asked:
                await interaction.response.send_message(
                    "You already asked your question.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(SixtyNineQuestionModal(self.game_id))


class QuestionReplyModal(discord.ui.Modal, title="Reply"):
    reply = discord.ui.TextInput(
        label="Your reply",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, message_id: int):
        super().__init__()
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async with app_state.get_message_lock(self.message_id):
            state = app_state.posted_questions.get(self.message_id)
            if state is None:
                await interaction.response.send_message(
                    "Someone already replied to this question.", ephemeral=True
                )
                return
            if interaction.user.id not in state.allowed_replier_ids:
                await interaction.response.send_message(
                    "❌ Only the question's recipient can reply.", ephemeral=True
                )
                return

            reply_text = self.reply.value.strip()
            if not reply_text:
                await interaction.response.send_message("Enter a reply before sending it.", ephemeral=True)
                return

            # The reply is posted publicly, so it needs the same slur/abuse
            # guard the question already gets — this was the unfiltered half.
            if contains_disallowed_content(reply_text):
                await interaction.response.send_message(
                    "That reply contains disallowed content. Please rephrase.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            reply_content = build_question_reply_content(state, interaction.user.id, reply_text)
            channel = await get_text_channel(interaction.client, state.channel_id)
            if channel is None:
                await interaction.followup.send(
                    "Could not update the question message; your reply wasn't recorded — please try again.",
                    ephemeral=True,
                )
                return

            try:
                await channel.get_partial_message(self.message_id).edit(
                    content=reply_content,
                    embed=None,
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound:
                await _clear_posted_question(self.message_id)
                await interaction.followup.send("The question message no longer exists.", ephemeral=True)
                return
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Failed to edit question message %s.", self.message_id)
                await interaction.followup.send(
                    "Could not update the question message; your reply wasn't recorded — please try again.",
                    ephemeral=True,
                )
                return

            await _clear_posted_question(self.message_id)
            await interaction.followup.send("Reply sent.", ephemeral=True)


class QuestionReplyView(BaseRiskyRollView):
    @discord.ui.button(
        label="Reply",
        style=discord.ButtonStyle.primary,
        custom_id="rr:reply",
        emoji="✏️",
    )
    async def reply_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.message is None:
            return
        state = app_state.posted_questions.get(interaction.message.id)
        if state is None:
            await interaction.response.send_message("This reply window has closed.", ephemeral=True)
            return
        if interaction.user.id not in state.allowed_replier_ids:
            await interaction.response.send_message(
                "❌ Only the question's recipient can reply.", ephemeral=True
            )
            return
        await interaction.response.send_modal(QuestionReplyModal(message_id=interaction.message.id))


async def disable_round_message(
    state: RiskyRollState,
    channel: discord.abc.Messageable | discord.abc.GuildChannel | None,
) -> None:
    if state.message_id is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    view = RiskyRollView(state.game_id)
    view.disable_all_items()

    accent = await resolve_embed_accent(channel.guild)
    try:
        await channel.get_partial_message(state.message_id).edit(embed=build_embed(state, channel.guild, accent), view=view)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return


async def disable_pending_question_message(
    client: discord.Client,
    state: PendingQuestionState,
    content: str,
) -> None:
    if state.prompt_message_id is None:
        return

    channel = await get_text_channel(client, state.channel_id)
    if channel is None:
        return

    view = SixtyNineQuestionView(state.game_id)
    view.disable_all_items()

    try:
        await channel.get_partial_message(state.prompt_message_id).edit(
            content=content, view=view, allowed_mentions=discord.AllowedMentions.none()
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return
