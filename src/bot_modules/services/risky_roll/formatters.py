import logging
from collections.abc import Callable

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.services.embeds import COLOR_GREEN
from . import state as app_state
from .models import PendingQuestionState, PostedQuestionState, PromptKind, RiskyRollState

log = logging.getLogger(__name__)


async def resolve_embed_accent(guild: "discord.Guild | None") -> "discord.Color | None":
    """Resolve the guild's branding accent for a game embed, guarding failures.

    Returns ``None`` when there's no guild, no store/db_path yet, or the
    resolution errors — the caller then falls back to the old state color.
    Never raises: a game embed must render even if branding lookup fails.
    """
    if guild is None:
        return None
    db_path = getattr(app_state.store, "db_path", None)
    if db_path is None:
        return None
    try:
        return await resolve_accent_color(db_path, guild)
    except Exception:
        log.debug("risky_roll: accent resolve failed; using fallback color", exc_info=True)
        return None

# A NameFn turns a user id into embed-ready text: a cached display name (escaped)
# or, for a user we can't resolve, a raw <@id> mention as a last resort.
NameFn = Callable[[int], str]


def _mention(uid: int) -> str:
    return f"<@{uid}>"


def make_name_resolver(guild: "discord.Guild | None") -> NameFn:
    """Return a resolver that prints cached display names as plain text.

    Names are cached at roll time; on a cache miss we backfill from the guild's
    member cache (sync, no network) so a round restored after a restart still
    shows names for members who are still present. Only a genuinely unknown
    user — typically someone who has left — falls back to a <@id> mention.
    """
    def resolve(uid: int) -> str:
        name = app_state.display_names.get(uid)
        if name is None and guild is not None:
            member = guild.get_member(uid)
            if member is not None:
                name = member.display_name
                app_state.display_names[uid] = name
        if name is None:
            return _mention(uid)
        return discord.utils.escape_markdown(name)

    return resolve


def format_user_mentions(user_ids: set[int]) -> str:
    return " ".join(f"<@{uid}>" for uid in sorted(user_ids))


def format_lowest_rolloff_note(
    tied_user_ids: set[int],
    selected_user_id: int | None,
    name_fn: NameFn = _mention,
) -> str:
    if selected_user_id is None or len(tied_user_ids) < 2:
        return ""
    tied = ", ".join(name_fn(uid) for uid in sorted(tied_user_ids))
    return f"{tied} → {name_fn(selected_user_id)}"


def _roll_prefix(user_id: int, roll: int, state: RiskyRollState) -> str:
    if roll == 69:
        return "🔥"
    if not state.is_open:
        if user_id == state.highest_user:
            return "⭐" if roll == 100 else "🥇"
        if user_id == state.lowest_user:
            return "☠️" if roll == 1 else "💀"
    return "🎲"


def _questioner_mentions(state: PendingQuestionState, *, asked: bool) -> str:
    return " and ".join(
        f"<@{uid}>"
        for uid in [state.winner_id, state.extra_questioner_id]
        if uid is not None and (uid in state.questioners_asked) == asked
    )


def build_pending_prompt_content(state: PendingQuestionState) -> str:
    if state.prompt_kind == PromptKind.TWO_QUESTIONERS:
        target_mentions = format_user_mentions(state.participant_user_ids)
        lines = [
            f"☠️ Someone rolled a **1**! {_questioner_mentions(state, asked=False)} "
            f"can each fire a question at {target_mentions}."
        ]
        if state.questioners_asked:
            lines.append(f"{_questioner_mentions(state, asked=True)} already asked.")
        lines.append("Click **Ask Question** to send yours.")
        return "\n".join(lines)

    if state.prompt_kind == PromptKind.DIRECT:
        selected_user_id = next(iter(sorted(state.participant_user_ids)), None)
        lowest_rolloff_note = format_lowest_rolloff_note(state.lowest_tie_user_ids, selected_user_id)
        target_mentions = format_user_mentions(state.participant_user_ids)
        lines = [f"🥇 <@{state.winner_id}> wins the round."]
        if lowest_rolloff_note:
            lines.append(lowest_rolloff_note)
        if len(state.participant_user_ids) > 1:
            lines.append(f"They rolled **100** — click **Ask Question** to send your question to {target_mentions}.")
        else:
            lines.append(f"Click **Ask Question** to send your question to {target_mentions}.")
        return "\n".join(lines)

    return (
        f"🔥 <@{state.winner_id}> rolled **69** — they ask the room.\n"
        "Click **Ask Question** to post your question in a thread."
    )


def build_pending_question_summary(state: PendingQuestionState, question_text: str, asker_id: int | None = None) -> str:
    if state.prompt_kind == PromptKind.TWO_QUESTIONERS:
        uid = asker_id if asker_id is not None else state.winner_id
        target_mentions = format_user_mentions(state.participant_user_ids)
        return f"<@{uid}> asked {target_mentions}:\n> {question_text}"

    if state.prompt_kind == PromptKind.DIRECT:
        target_mentions = format_user_mentions(state.participant_user_ids)
        return f"<@{state.winner_id}> asked {target_mentions}:\n> {question_text}"

    return f"<@{state.winner_id}> rolled 69 and asked:\n> {question_text}"


def _add_reroll_field(
    embed: discord.Embed, state: RiskyRollState, *, show_all_in_message: bool, name_fn: NameFn
) -> None:
    tied = ", ".join(name_fn(uid) for uid in sorted(state.reroll_user_ids))
    reroll_text = f"Tied: {tied}"
    pending = [uid for uid in sorted(state.reroll_user_ids) if uid not in state.rolls]
    if pending:
        reroll_text += "\nWaiting on: " + ", ".join(name_fn(uid) for uid in pending)
    elif show_all_in_message:
        reroll_text += "\nAll rerolls in — close the round."
    embed.add_field(name="⚔️ Reroll", value=reroll_text, inline=False)


def build_embed(
    state: RiskyRollState,
    guild: "discord.Guild | None" = None,
    accent: "discord.Color | None" = None,
) -> discord.Embed:
    name = make_name_resolver(guild)
    # The embed color tracks game STATE, so it follows the guild accent — the
    # one exception is a decided winner with no loser (the old gold state),
    # which marks a win and is therefore always green. When no accent is
    # supplied (no guild / resolution failed) we fall back to the old
    # state-specific colors so the game never renders color-less.
    if not state.is_open and state.highest_user is not None and state.lowest_user is None:
        color = discord.Color(COLOR_GREEN)
    elif accent is not None:
        color = accent
    elif state.is_open:
        color = discord.Color(0xFF9800) if state.reroll_user_ids else discord.Color(0xDC3545)
    else:
        color = discord.Color(0x546E7A)

    embed = discord.Embed(title="🎲 Risky Rolls", color=color)

    if state.is_open:
        if state.reroll_user_ids:
            embed.description = "Tie for highest — the tied players must reroll."
        else:
            embed.description = "Highest roll wins, lowest answers. Press **Roll** to join."
    else:
        embed.description = "Round over."

    if state.is_open and (state.auto_close_players or state.auto_close_minutes):
        parts = []
        if state.auto_close_players:
            parts.append(f"at {state.auto_close_players} players")
        if state.auto_close_minutes:
            parts.append(f"after {state.auto_close_minutes} minute{'s' if state.auto_close_minutes != 1 else ''}")
        embed.set_footer(text=f"Auto-closes {' or '.join(parts)}")

    if not state.rolls:
        embed.add_field(name="Rolls (0)", value="No rolls yet.", inline=False)
        if state.reroll_user_ids:
            _add_reroll_field(embed, state, show_all_in_message=False, name_fn=name)
        return embed

    sorted_rolls = sorted(state.rolls.items(), key=lambda item: item[1], reverse=True)
    lines = [
        f"{_roll_prefix(uid, roll, state)} **{roll}** — {name(uid)}"
        for uid, roll in sorted_rolls
    ]
    embed.add_field(name=f"Rolls ({len(state.rolls)})", value="\n".join(lines), inline=False)

    if state.reroll_user_ids:
        _add_reroll_field(embed, state, show_all_in_message=True, name_fn=name)

    if not state.is_open and state.highest_user:
        high_mention = name(state.highest_user)
        if state.lowest_user is None:
            result = f"**Asks:** {high_mention}\n**Answers:** the room"
            highest_rolloff_note = format_lowest_rolloff_note(state.highest_tie_user_ids, state.highest_user, name)
            if highest_rolloff_note:
                result += f"\n{highest_rolloff_note}"
        else:
            low_mention = name(state.lowest_user)
            winner_rolled_100 = state.rolls.get(state.highest_user) == 100
            loser_rolled_1 = state.rolls.get(state.lowest_user) == 1

            if winner_rolled_100 and state.second_lowest_user is not None:
                result = f"**Asks:** {high_mention} ⭐\n**Answers:** {low_mention} and {name(state.second_lowest_user)}"
            elif loser_rolled_1 and state.second_highest_user is not None:
                result = f"**Asks:** {high_mention} and {name(state.second_highest_user)}\n**Answers:** {low_mention} ☠️"
            else:
                result = f"**Asks:** {high_mention}\n**Answers:** {low_mention}"

            highest_rolloff_note = format_lowest_rolloff_note(state.highest_tie_user_ids, state.highest_user, name)
            if highest_rolloff_note:
                result += f"\n{highest_rolloff_note}"
            lowest_rolloff_note = format_lowest_rolloff_note(state.lowest_tie_user_ids, state.lowest_user, name)
            if lowest_rolloff_note:
                result += f"\n{lowest_rolloff_note}"
            second_lowest_note = format_lowest_rolloff_note(state.second_lowest_tie_user_ids, state.second_lowest_user, name)
            if second_lowest_note:
                result += f"\n{second_lowest_note}"
            second_highest_note = format_lowest_rolloff_note(state.second_highest_tie_user_ids, state.second_highest_user, name)
            if second_highest_note:
                result += f"\n{second_highest_note}"

            if winner_rolled_100 and loser_rolled_1:
                result += "\n*Both the 100 and 1 rules apply.*"

        embed.add_field(name="Result", value=result, inline=False)

    return embed


def build_question_reply_content(
    state: PostedQuestionState,
    replier_id: int,
    reply_text: str,
) -> str:
    target_mentions = format_user_mentions(state.allowed_replier_ids)
    return f"{target_mentions}\n<@{state.asker_id}> asks:\n{state.question_text}\n\n<@{replier_id}>: {reply_text}"


def build_how_to_play_content() -> str:
    return (
        "**🎲 How to Play**\n"
        "**Roll** — Each player presses **Roll** once. You roll a number from **1** to **100**.\n"
        "**Win** — Highest unique roll wins the round; lowest roll is the loser.\n"
        "**Ties for highest** — Tied players auto-reroll until one wins.\n"
        "**Question** — The winner asks the loser a question; the loser must reply.\n"
        "🔥 **Rolled 69** — The winner asks the whole room (in a thread).\n"
        "⭐ **Rolled 100** — The winner asks the bottom 2 players.\n"
        "☠️ **Rolled 1** — The top 2 players each ask the loser.\n"
        "**Close** — Only the round opener (or an admin) can close early."
    )


async def get_text_channel(
    client: discord.Client,
    channel_id: int,
) -> discord.TextChannel | discord.Thread | None:
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.NotFound:
            log.warning("get_text_channel: channel %s not found.", channel_id)
            return None
        except discord.Forbidden:
            log.warning("get_text_channel: forbidden fetching channel %s.", channel_id)
            return None
        except discord.HTTPException:
            log.warning("get_text_channel: HTTP error fetching channel %s.", channel_id, exc_info=True)
            return None

    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        return channel

    log.warning(
        "get_text_channel: channel %s is type %s, not a TextChannel or Thread.",
        channel_id, type(channel).__name__,
    )
    return None


def build_rolloff_embed(
    tied_user_ids: list[int],
    rounds: list[dict[int, int]],
    winner_id: int,
    title: str = "Tie Rolloff",
    pick_lowest: bool = False,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    if color is None:
        color = discord.Color(0xFF9800)
    embed = discord.Embed(title=f"⚔️ {title}", color=color)
    roll_label = "Lowest roll tied" if pick_lowest else "Highest roll tied"
    embed.description = (
        f"{roll_label} — automatic rolloff.\n"
        f"Tied: {', '.join(f'<@{uid}>' for uid in sorted(set(tied_user_ids)))}"
    )

    for index, round_rolls in enumerate(rounds, start=1):
        sorted_rolls = sorted(round_rolls.items(), key=lambda item: item[1], reverse=not pick_lowest)
        lines = [f"🎲 **{roll}** — <@{uid}>" for uid, roll in sorted_rolls]
        embed.add_field(name=f"Round {index}", value="\n".join(lines), inline=False)

    winner_label = "☠️ Selected Lowest" if pick_lowest else "🏆 Rolloff Winner"
    embed.add_field(name=winner_label, value=f"<@{winner_id}>", inline=False)
    return embed


async def post_rolloff_embed(
    channel: discord.abc.Messageable | discord.abc.GuildChannel | None,
    tied_user_ids: list[int],
    rolloff_rounds: list[dict[int, int]],
    winner_id: int,
    channel_id: int,
    title: str = "Tie Rolloff",
    pick_lowest: bool = False,
    accent: "discord.Color | None" = None,
) -> None:
    try:
        if channel is not None and isinstance(channel, (discord.TextChannel, discord.Thread)):
            # A tie rolloff is a game state, so its embed follows the accent;
            # build_rolloff_embed falls back to its old orange when accent is None.
            if accent is None:
                accent = await resolve_embed_accent(getattr(channel, "guild", None))
            await channel.send(
                embed=build_rolloff_embed(
                    tied_user_ids, rolloff_rounds, winner_id, title, pick_lowest, color=accent
                )
            )
    except discord.Forbidden:
        log.exception("Missing access posting rolloff embed in #%s.", getattr(channel, "name", channel_id))
    except (AttributeError, discord.HTTPException):
        log.exception("Failed to post rolloff embed in #%s.", getattr(channel, "name", channel_id))
