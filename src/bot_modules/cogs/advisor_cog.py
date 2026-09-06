"""`/ask` and the Ask panel — member self-service help, answered by the AI advisor.

Thin glue over ``bot_modules.services.advisor_service``; the same brain powers
the dashboard Help panel's ask box. Answers are grounded in the user
manual, so the advisor can't invent commands. Ephemeral + per-user cooldown so one
member can't spend the shared Anthropic budget.

Two Discord surfaces sit on that brain. ``/ask`` is the one-shot, full-power
one: it carries the config tools and their Apply buttons. The **Ask panel** is a
button in a channel that opens a private, multi-turn chat window — the same
grounding, a shorter register, no config tools. Its transcript lives in the
ephemeral message rather than in any table (see ``advisor_chat_logic``), so the
chat stores nothing about anybody.

``public: True`` (mods only) turns the ask into a short tutorial written for the
whole room. It is answered ephemerally first and only reaches the channel when
the mod presses Post, and it is always generated on the plain path — @everyone
context, no config tools, no Apply buttons — so nothing the asker can see but
the room can't ends up in a public message.

Admin askers additionally get config tools: settings are fetched on demand
(``get_server_settings``) instead of dumped inline, and requested changes come
back as *proposals* rendered here as Apply buttons — the write only happens on
click, re-permission-checked and re-validated (``advisor_actions``).
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import get_grant_roles, open_db
from bot_modules.services.branding_service import (
    DEFAULT_ASSISTANT_NAME,
    resolve_assistant_name_conn,
)
from bot_modules.services.advisor_actions import (
    ConfigProposal,
    apply_config_change,
    validate_config_change,
    validate_grant_role_change,
)
from bot_modules.services.advisor_context import (
    FEATURE_KEYS,
    build_asker_context,
    can_post_public,
    can_see_config,
    fetch_feature_settings,
    is_server_admin,
    is_staff,
)
from bot_modules.services.advisor_chat_logic import (
    MAX_EXCHANGES,
    ReplyCooldown,
    exchange_count,
    footer_tail,
    history_from_fields,
    is_full,
    transcript_fields,
)
from bot_modules.services.advisor_gaps import fetch_setup_gaps
from bot_modules.services.advisor_service import (
    MAX_QUESTION_CHARS,
    MODEL,
    AdvisorTools,
    answer_advisor,
    get_advisor_context_enabled,
    get_advisor_tools_enabled,
    resolve_advisor_model,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

# Discord embed descriptions cap at 4096 chars; leave room for the trailer.
_MAX_DESC = 3900

# Where a public answer may be posted: messageable, and able to answer "may
# this member speak here?". Deliberately concrete rather than
# ``discord.abc.Messageable`` — a DM channel is messageable but has no
# ``permissions_for``, and a category or forum parent has no ``send``.
_POSTABLE = (
    discord.TextChannel,
    discord.Thread,
    discord.VoiceChannel,
    discord.StageChannel,
)
_Postable = (
    discord.TextChannel | discord.Thread | discord.VoiceChannel | discord.StageChannel
)
_MAX_PROPOSALS = 4  # buttons on one reply; also caps blast radius per ask


def _proposal_fields(embed: discord.Embed, proposals: list[ConfigProposal]) -> None:
    """Spell out every pending write, in text the model did not author.

    The embed description is the assistant's own prose, so it can describe a
    change one way and propose another; the button label truncates at 80 chars,
    which for a text setting shows only the first ~50 characters of the value.
    Since the human Apply click is *the* prompt-injection defence
    (``advisor_actions`` docstring), the admin has to be able to read the whole
    of what they're confirming. One field per proposal — Discord caps a field
    value at 1024, comfortably above the 200-char value limit.
    """
    for i, prop in enumerate(proposals[:_MAX_PROPOSALS], start=1):
        scope = (
            f"{prop.grant_name} role grant"
            if prop.target == "grant_role"
            else "server setting"
        )
        embed.add_field(
            name=f"Pending change {i} — press Apply to confirm",
            value=f"{prop.display}\n-# {scope} · `{prop.key}`"[:1024],
            inline=False,
        )


def _make_tools(
    guild: discord.Guild,
    member: discord.Member,
    db_path: Path,
    proposals: list[ConfigProposal],
) -> AdvisorTools:
    """Config tools for one admin ask; queued proposals land in ``proposals``."""

    def _fetch(feature: str) -> str:
        return fetch_feature_settings(guild, member, db_path, feature)

    def _gaps() -> str:
        return fetch_setup_gaps(db_path, guild.id, member)

    def _queue(prop: ConfigProposal) -> str:
        """Dedupe by what the change targets, then queue it for a button."""
        ident = (prop.target, prop.grant_name, prop.key)
        proposals[:] = [
            p for p in proposals if (p.target, p.grant_name, p.key) != ident
        ]
        if len(proposals) >= _MAX_PROPOSALS:
            return f"Rejected: at most {_MAX_PROPOSALS} changes per ask."
        proposals.append(prop)
        return (
            f"Queued: {prop.display}. NOT applied yet — an Apply button is "
            "attached to your reply; tell the admin to press it to confirm."
        )

    def _propose(key: str, value: str) -> str:
        if not can_see_config(member):  # defense in depth; wiring already gates
            return "Rejected: only server admins can change settings."
        try:
            with open_db(db_path) as conn:
                prop = validate_config_change(
                    conn, guild, key, value, is_admin=is_server_admin(member)
                )
        except ValueError as e:
            return f"Rejected: {e}"
        return _queue(prop)

    def _propose_grant(grant_name: str, field: str, value: str) -> str:
        if not can_see_config(member):  # defense in depth
            return "Rejected: only server admins can change settings."
        try:
            with open_db(db_path) as conn:
                prop = validate_grant_role_change(
                    conn, guild, grant_name, field, value,
                    is_admin=is_server_admin(member),
                )
        except ValueError as e:
            return f"Rejected: {e}"
        return _queue(prop)

    # Only offer the grant tool to a full admin — every field on it decides who
    # ends up with a role, so a Manage Server asker would only be refused.
    admin = is_server_admin(member)
    grant_names: list[str] = []
    if admin:
        try:
            with open_db(db_path) as conn:
                grant_names = sorted(get_grant_roles(conn, guild.id))
        except Exception:
            log.exception("advisor: couldn't list grant roles for guild %s", guild.id)

    return AdvisorTools(
        feature_keys=FEATURE_KEYS,
        fetch_settings=_fetch,
        fetch_gaps=_gaps,
        propose_change=_propose,
        propose_grant=_propose_grant if grant_names else None,
        grant_names=grant_names,
        is_admin=admin,
    )


class _ApplyConfigView(discord.ui.View):
    """One Apply button per queued proposal. The reply is ephemeral, so only
    the asker can click — but each click still re-checks their permissions and
    re-validates the change before writing."""

    def __init__(
        self,
        bot: Bot,
        db_path: Path,
        guild: discord.Guild,
        proposals: list[ConfigProposal],
    ) -> None:
        super().__init__(timeout=600)
        self._bot = bot
        self._db_path = db_path
        self._guild = guild
        for prop in proposals[:_MAX_PROPOSALS]:
            btn: discord.ui.Button = discord.ui.Button(
                style=discord.ButtonStyle.success,
                label=f"Apply: {prop.display}"[:80],
            )
            btn.callback = self._make_callback(btn, prop)
            self.add_item(btn)

    def _make_callback(self, btn: discord.ui.Button, prop: ConfigProposal):
        async def _apply(interaction: discord.Interaction) -> None:
            member = interaction.user
            if not (
                isinstance(member, discord.Member) and can_see_config(member)
            ):
                await interaction.response.send_message(
                    "❌ Only server admins can apply settings changes.", ephemeral=True
                )
                return
            try:
                # admin_only settings are re-checked against the clicker, not
                # the asker — the reply is ephemeral, but the gate shouldn't
                # depend on that being true.
                feature_slug = apply_config_change(
                    self._db_path, self._guild, prop,
                    is_admin=is_server_admin(member),
                    actor_id=member.id,
                )
            except ValueError as e:
                btn.disabled = True
                btn.style = discord.ButtonStyle.secondary
                btn.label = f"Failed: {e}"[:80]
                await interaction.response.edit_message(view=self)
                return
            if feature_slug:
                # The same signal a dashboard save sends, so a cog that caches
                # its config at boot (the whisper launcher's known-guild set,
                # the casino hub) picks the change up without a restart.
                self._bot.dispatch(f"{feature_slug}_config_change", self._guild.id)
            log.info(
                "%s applied advisor proposal in guild %s: %s",
                member.display_name, self._guild.id, prop.display,
            )
            btn.disabled = True
            btn.label = f"✅ Applied: {prop.display}"[:80]
            await interaction.response.edit_message(view=self)

        return _apply


def _answer_embed(
    *,
    question: str,
    answer: str,
    assistant_name: str,
    color: int | discord.Colour | None,
    asker: discord.abc.User | None = None,
) -> discord.Embed:
    """One answer card.

    The question is the title in both modes — a public reader needs it to make
    sense of the answer, and privately it beats a title that only repeats the
    assistant's name. ``asker`` is set for the public post only: the room can't
    otherwise tell who asked for it, and a mod putting the bot's words in a
    channel should be named on them.
    """
    if asker is not None:
        answer = f"{answer}\n\n-# Ask your own question with `/ask`."
    title = f"🤖 {question}"
    if len(title) > 256:  # Discord's embed title cap
        title = title[:255].rstrip() + "…"
    embed = discord.Embed(title=title, description=answer, color=color)
    who = f"Asked by {asker.display_name} • " if asker is not None else ""
    embed.set_footer(
        text=f"{who}{assistant_name} • grounded in the server guide, not always perfect"
    )
    return embed


class _PublicPostView(discord.ui.View):
    """Preview → publish, for a public ``/ask``.

    The answer is *written* for the room (``public_tutorial``) but lands
    ephemerally first, so the mod reads what everyone else is about to read
    while it can still be thrown away. Nothing reaches the channel without this
    click, and what posts is byte-identical to what was previewed.
    """

    def __init__(
        self,
        *,
        channel: _Postable,
        embed: discord.Embed,
        asker_id: int,
    ) -> None:
        # Same window as the Apply buttons — long enough to read the preview
        # and think about it, short enough not to leave a stale Post button.
        super().__init__(timeout=600)
        self._channel = channel
        self._embed = embed
        self._asker_id = asker_id
        self._posted = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # The preview is ephemeral, so only the asker can see these buttons —
        # the check is here so the gate doesn't depend on that staying true.
        if interaction.user.id != self._asker_id:
            await interaction.response.send_message(
                "That isn't your preview.", ephemeral=True
            )
            return False
        return True

    def _freeze(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()

    @discord.ui.button(label="Post to channel", style=discord.ButtonStyle.success)
    async def post(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        # Claim the click before doing anything slow. The buttons stay live in
        # the mod's client until the edit below lands, so an impatient second
        # press arrives while the first send is still in flight and would post
        # the tutorial twice.
        if self._posted:
            return
        self._posted = True
        # Re-check the power at click time, like the Apply buttons do: a mod
        # stripped of their role mid-preview holds a live Post button for the
        # rest of the ten minutes otherwise. Identity is checked in
        # ``interaction_check``; Discard stays open to them either way, since
        # tidying the preview away is never the harmful direction.
        clicker = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else None
        )
        if not can_post_public(clicker):
            self._freeze()
            await interaction.response.edit_message(
                content="❌ You can no longer post answers to the channel.",
                view=self,
            )
            return
        # Acknowledge first: a component interaction has to be answered within
        # 3s, and ``send`` can sit out a channel rate-limit for longer than
        # that. Without this the post lands but the reply reads "interaction
        # failed", which invites exactly the second press guarded above.
        await interaction.response.defer()
        try:
            # Embeds never ping, but the allow-list is explicit so a future
            # edit that moves text into the message body can't start pinging.
            await self._channel.send(
                embed=self._embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            self._freeze()
            await interaction.edit_original_response(
                content="❌ I can't post in this channel — check my permissions here.",
                view=self,
            )
            return
        except discord.HTTPException:
            log.exception("advisor: public post failed")
            self._freeze()
            await interaction.edit_original_response(
                content="❌ Discord wouldn't take that post. Try again in a moment.",
                view=self,
            )
            return
        log.info(
            "%s posted an /ask answer publicly in #%s",
            interaction.user.display_name,
            getattr(self._channel, "name", "?"),
        )
        self._freeze()
        await interaction.edit_original_response(
            content="✅ Posted to the channel.", view=self
        )

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary)
    async def discard(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self._freeze()
        await interaction.response.edit_message(
            content="Discarded — nothing was posted.", embed=None, view=self
        )


# ═══════════════════════════════════════════════════════════════════════════
# ASK PANEL — a private, multi-turn chat window opened from a channel panel
# ═══════════════════════════════════════════════════════════════════════════
#
# The panel is a public message with one button; the chat it opens is
# ephemeral, so only the member who pressed it ever sees their conversation.
# The transcript lives in that message's embed fields and nowhere else — see
# ``advisor_chat_logic`` for why, and for the round-trip that reads it back.
#
# Unlike ``/ask``, this surface deliberately gets **no config tools and no
# Apply buttons**. A proposal only exists in the memory of the view that
# rendered it, which would either strand the chat's own buttons on a restart or
# leave Apply buttons that silently do nothing; and a public help panel is the
# wrong doorway for changing server settings anyway. Admins keep ``/ask``,
# where that machinery lives and is tested.


# Per-member Reply rate limit, shared by every live chat in the process. Module
# level rather than on the cog because a DynamicItem is rebuilt from its
# custom_id on click and holds no reference to the cog that registered it.
_REPLY_COOLDOWN = ReplyCooldown()

_CHAT_NOT_YOURS = "That isn't your chat — press **Ask a question** on the panel to start your own."


async def _cooldown_blocked(interaction: discord.Interaction, user_id: int) -> bool:
    """Refuse and report if this member asked too recently; otherwise record it.

    Shared by *both* entry points on purpose. Guarding only Reply would leave
    the guard unarmed on the path members actually use: opening a fresh chat is
    one click away, so ``MAX_EXCHANGES`` caps a window, not a member's spend.
    Every ask here is a billed call carrying the whole manual, so what needs
    rate-limiting is the model call, not the button that happens to make it.
    """
    now = time.monotonic()
    wait = _REPLY_COOLDOWN.remaining(user_id, now)
    if wait > 0:
        await interaction.response.send_message(
            f"❌ Give me a sec — try again in {wait:.0f}s.", ephemeral=True
        )
        return True
    _REPLY_COOLDOWN.mark(user_id, now)
    return False


def _ask_panel_embed(assistant_name: str, color: int | discord.Colour | None):
    """The public panel members press to open a chat."""
    return discord.Embed(
        title=f"🤖 Ask {assistant_name}",
        description=(
            f"Stuck on something? Ask {assistant_name} — how a game works, what "
            "a command does, where to find a setting, or how something here is "
            "set up.\n\n"
            "**Only you see the answer**, and you can keep replying to dig "
            f"further. {assistant_name} answers from the server guide, so it "
            "won't invent commands — if it doesn't know, it'll say so."
        ),
        color=color,
    )


def _chat_embed(
    history: list[dict], assistant_name: str, color: int | discord.Colour | None
) -> discord.Embed:
    """Render the whole conversation as one card.

    Every turn is a field, which is what makes the transcript readable *and*
    machine-readable: :func:`history_from_fields` reads these same fields back
    on the next Reply, so what the member sees is exactly what the model is
    given.
    """
    embed = discord.Embed(title=f"🤖 Ask {assistant_name}", color=color)
    for name, value in transcript_fields(history, assistant_name):
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(
        text=(
            f"{assistant_name} • grounded in the server guide, not always "
            f"perfect • {footer_tail(history)}"
        )
    )
    return embed


async def _run_chat(
    bot: Bot,
    guild: discord.Guild | None,
    member: discord.Member | None,
    question: str,
    history: list[dict],
) -> tuple[str, bool, str]:
    """Answer one chat turn. Returns ``(answer, ok, assistant_name)``.

    Mirrors the private ``/ask`` path — same per-guild model tiering, same
    per-asker server context — minus the config tools, and with
    ``include_config=False`` to match: with no tools to act on them, a settings
    dump would only be raw config text sitting in a member-facing help chat.
    """
    model = MODEL
    assistant_name = DEFAULT_ASSISTANT_NAME
    guild_context: str | None = None
    if guild is not None:
        db_path = bot.ctx.db_path
        with open_db(db_path) as conn:
            model = resolve_advisor_model(conn, guild.id, staff=is_staff(member))
            assistant_name = resolve_assistant_name_conn(conn, guild.id)
            context_on = get_advisor_context_enabled(conn, guild.id)
        if context_on:
            guild_context = build_asker_context(
                guild, member, db_path, include_config=False
            )
    result = await answer_advisor(
        question,
        history,
        model=model,
        guild_context=guild_context,
        assistant_name=assistant_name,
        chat=True,
    )
    return result.answer, result.ok, assistant_name


#: Chat generation per asker. Bumped when a turn completes and when the chat is
#: ended, so an answer that was still being written can tell it has been
#: overtaken before it edits the window. Two races made this necessary: pressing
#: End chat while a reply was in flight brought the "closed" chat back, transcript
#: and live buttons and all; and two replies in flight meant the slower one wrote
#: its pre-edit snapshot over the faster one's turn, dropping it silently.
#:
#: In memory on purpose. It is only meaningful for the seconds an answer takes,
#: and a restart in that window loses nothing that matters — an absent entry
#: reads as "not superseded", which is the behaviour this had before.
_chat_gen: dict[int, int] = {}


def _bump_chat_gen(user_id: int) -> int:
    _chat_gen[user_id] = _chat_gen.get(user_id, 0) + 1
    return _chat_gen[user_id]


async def _show_chat(
    interaction: discord.Interaction,
    *,
    history: list[dict],
    assistant_name: str,
    color: int | discord.Colour | None,
    user_id: int,
    notice: str | None = None,
    edit: bool,
) -> None:
    """Draw the chat window, updating it in place where there is one to update.

    ``edit`` is False only for the first turn, which has no window yet.

    The fallback covers a window that can no longer be edited — a dead token, a
    message the member dismissed mid-answer — where a resend is the only way
    they get the answer they waited for. It is *not* a retry of a payload
    Discord rejected on its merits: an over-length embed is prevented upstream
    by ``transcript_fields``' whole-embed budget, precisely so this path never
    resends something guaranteed to fail again. When it does fire the member
    can briefly hold two windows, so the replacement says which one is live.
    """
    embed = _chat_embed(history, assistant_name, color)
    view = _ChatView(user_id, full=is_full(history))
    if edit:
        try:
            await interaction.edit_original_response(
                content=notice, embed=embed, view=view
            )
            return
        except discord.HTTPException:
            log.exception("advisor chat: in-place edit failed, opening a new window")
            notice = (
                f"{notice}\n" if notice else ""
            ) + "-# Your earlier chat window is out of date — keep using this one."
    await interaction.followup.send(
        content=notice or discord.utils.MISSING,
        embed=embed,
        view=view,
        ephemeral=True,
    )


class AskPanelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"advisor_panel:ask",
):
    """Persistent '💬 Ask a question' button on the public Ask panel."""

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Ask a question",
                emoji="💬",
                style=discord.ButtonStyle.primary,
                custom_id="advisor_panel:ask",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        if await _cooldown_blocked(interaction, interaction.user.id):
            return
        await interaction.response.send_modal(_AskModal(history=None))


class AskChatReplyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"advisor_chat:reply:(?P<uid>\d+)",
):
    """'💬 Reply' on a live chat window.

    The asker's id rides in the custom_id for two reasons: the button is
    rebuilt from that id after a restart, so a conversation survives one; and
    the ownership check below then doesn't have to rest on the message being
    ephemeral, the same belt-and-braces ``_PublicPostView`` uses.
    """

    def __init__(self, user_id: int, *, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label="Reply",
                emoji="💬",
                style=discord.ButtonStyle.primary,
                custom_id=f"advisor_chat:reply:{user_id}",
                disabled=disabled,
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(_CHAT_NOT_YOURS, ephemeral=True)
            return
        # interaction.message is the chat window itself here; a modal's own
        # submit has message = None, so the transcript is read now and handed
        # to the modal — the same pattern the ticket close flow uses.
        message = interaction.message
        history = history_from_fields(
            [(f.name, f.value) for f in message.embeds[0].fields]
            if message and message.embeds
            else []
        )
        if is_full(history):
            await interaction.response.send_message(
                f"That chat has used all {MAX_EXCHANGES} of its questions — press "
                "**Ask a question** on the panel to start a fresh one.",
                ephemeral=True,
            )
            return
        if await _cooldown_blocked(interaction, self.user_id):
            return
        await interaction.response.send_modal(
            _AskModal(history=history, generation=_chat_gen.get(self.user_id, 0))
        )


class AskChatEndButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"advisor_chat:end:(?P<uid>\d+)",
):
    """'✖ End chat' — clears the window so the transcript stops being on screen."""

    def __init__(self, user_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="End chat",
                emoji="✖️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"advisor_chat:end:{user_id}",
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(_CHAT_NOT_YOURS, ephemeral=True)
            return
        # Bump first: an answer already being written must not edit this window
        # back into existence after it is cleared.
        _bump_chat_gen(self.user_id)
        # Wiping the embed is the whole point: the transcript only ever existed
        # in this message, so clearing it is what "end the chat" means here.
        await interaction.response.edit_message(
            content="Chat closed. Press **Ask a question** on the panel any time.",
            embed=None,
            view=None,
        )


class _ChatView(discord.ui.View):
    """The two buttons under a chat window.

    ``timeout=None`` with dynamic items: the buttons route by custom_id alone,
    with no message id to register, so a chat keeps working across a restart —
    the transcript is in the message, and the asker's id is in the button.
    """

    def __init__(self, user_id: int, *, full: bool) -> None:
        super().__init__(timeout=None)
        self.add_item(AskChatReplyButton(user_id, disabled=full))
        self.add_item(AskChatEndButton(user_id))


class _AskModal(discord.ui.Modal):
    """The question box, for both the first ask and every reply after it."""

    def __init__(self, *, history: list[dict] | None, generation: int = 0) -> None:
        first = history is None
        super().__init__(title="Ask a question" if first else "Reply")
        #: None on the first turn — there is no window to update yet.
        self._history = history
        #: What ``_chat_gen`` read when the button was pressed. If it has moved
        #: by the time this answer is ready, the window belongs to someone
        #: else's turn now — or to no chat at all.
        self._generation = generation
        self.question: discord.ui.TextInput = discord.ui.TextInput(
            label="What would you like to know?" if first else "Your reply",
            style=discord.TextStyle.paragraph,
            max_length=MAX_QUESTION_CHARS,
            required=True,
            placeholder=(
                "e.g. how do I earn coins?" if first else "Ask a follow-up…"
            ),
        )
        self.add_item(self.question)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        question = str(self.question.value or "").strip()
        history = self._history
        first = history is None
        if first:
            # No window yet: this creates one.
            await interaction.response.defer(thinking=True, ephemeral=True)
        else:
            # Launched from a component, so a plain defer is a deferred
            # *message update* — edit_original_response then rewrites the chat
            # window in place instead of stacking a second one beneath it.
            await interaction.response.defer()
            # That defer is deliberately invisible, which on its own leaves the
            # window unchanged and the Reply button live for the seconds the
            # model takes. The member's natural second press then comes back as
            # "give me a sec", which reads as being told off for spamming
            # rather than "still thinking". Cosmetic, so a failure here is
            # swallowed: the answer below still lands.
            with contextlib.suppress(discord.HTTPException):
                await interaction.edit_original_response(
                    content="💭 Thinking…",
                    view=_ChatView(interaction.user.id, full=True),
                )

        bot = cast("Bot", interaction.client)
        guild = interaction.guild
        member = (
            interaction.user if isinstance(interaction.user, discord.Member) else None
        )
        answer, ok, assistant_name = await _run_chat(
            bot, guild, member, question, history or []
        )
        color = (
            await safe_resolve_accent(bot.ctx, guild, log_label="advisor")
            if guild is not None
            else None
        )

        if not ok:
            if first:
                await interaction.followup.send(answer, ephemeral=True)
                return
            # Leave the conversation exactly as it was. A model that couldn't
            # be reached shouldn't cost them one of their questions, and the
            # transcript they already have shouldn't vanish with it.
            await _show_chat(
                interaction,
                history=history or [],
                assistant_name=assistant_name,
                color=color,
                user_id=interaction.user.id,
                notice=f"⚠️ {answer}",
                edit=True,
            )
            return

        new_history = [
            *(history or []),
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]
        log.info(
            "%s asked the Ask panel (turn %d): %.80s",
            interaction.user.display_name,
            exchange_count(new_history),
            question,
        )
        if not first and _chat_gen.get(interaction.user.id, 0) != self._generation:
            # Overtaken while the model was writing: the chat was ended, or a
            # second reply already landed. Editing the window now would either
            # resurrect a closed chat or paste this turn over a newer one. Hand
            # the answer over as its own message instead — it was still asked
            # and answered, and losing it silently is the worse failure.
            await interaction.followup.send(
                content=(
                    "-# That chat moved on while this was being written, so here "
                    "is the answer on its own."
                ),
                embed=_chat_embed(new_history, assistant_name, color),
                ephemeral=True,
            )
            return
        if not first:
            _bump_chat_gen(interaction.user.id)
        await _show_chat(
            interaction,
            history=new_history,
            assistant_name=assistant_name,
            color=color,
            user_id=interaction.user.id,
            edit=not first,
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        log.exception("Ask panel chat failed", exc_info=error)
        msg = "❌ Something went wrong there — press Ask on the panel to try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class AdvisorCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    async def cog_load(self) -> None:
        # Re-register the persistent buttons so clicks on an already-posted
        # panel — and on a chat window someone left open — still route after a
        # restart. All the state they need is in the custom_id and, for a
        # chat, in the message itself.
        self.bot.add_dynamic_items(
            AskPanelButton, AskChatReplyButton, AskChatEndButton
        )

    async def post_ask_panel(self, guild, channel):
        """Post the Ask panel into ``channel``.

        Entry point for the dashboard's panel poster (``panel_registry``).
        Like the ticket panel this posts a fresh message each time rather than
        editing a sticky one: the button carries no state, so an older panel
        keeps working, and silently killing one members may have bookmarked
        would be worse than leaving two live.

        Returns the new message, or None if Discord refused the post.
        """
        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="advisor")
        with open_db(self.bot.ctx.db_path) as conn:
            assistant_name = resolve_assistant_name_conn(conn, guild.id)
        view = discord.ui.View(timeout=None)
        view.add_item(AskPanelButton())
        try:
            return await channel.send(
                embed=_ask_panel_embed(assistant_name, accent), view=view
            )
        except discord.HTTPException:
            log.exception("advisor: couldn't post the Ask panel in guild %s", guild.id)
            return None

    @app_commands.command(
        name="ask",
        description="Ask the server assistant how to use the server — games, commands, settings.",
    )
    @app_commands.describe(
        question="What do you want to know how to do?",
        public="Post the answer in this channel as a short tutorial (mods only)",
    )
    @app_commands.checks.cooldown(1, 12.0, key=lambda i: i.user.id)
    async def ask(
        self,
        interaction: discord.Interaction,
        question: str,
        public: bool = False,
    ) -> None:
        log.info(
            "%s used /ask%s: %.80s",
            interaction.user.display_name,
            " (public)" if public else "",
            question,
        )
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        member = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else None
        )
        channel = interaction.channel
        # Set only once a public ask has cleared every gate below, so it doubles
        # as "this answer is allowed to be published, and here".
        post_to: _Postable | None = None
        if public:
            # Every refusal happens before the answer round-trip: no point
            # spending a model call on something that can't be posted.
            if guild is None or not isinstance(channel, _POSTABLE):
                await interaction.followup.send(
                    "❌ I can only post to a channel from inside a server.",
                    ephemeral=True,
                )
                return
            if not can_post_public(member):
                await interaction.followup.send(
                    "❌ Only mods can post an answer to the channel. Run "
                    "`/ask` again without **public** and I'll answer just for "
                    "you.",
                    ephemeral=True,
                )
                return
            # Being a mod isn't permission to speak everywhere: read-only
            # #rules and #announcements are exactly the channels a mod can see
            # but shouldn't post in, and the bot must not become the way round
            # that. Send Messages is a separate permission from the mod powers
            # can_post_public accepts, so this is not implied by the check above.
            if member is None or not channel.permissions_for(member).send_messages:
                await interaction.followup.send(
                    "❌ You can't post in this channel, so neither will I. "
                    "Run `/ask` again without **public** for a private answer.",
                    ephemeral=True,
                )
                return
            post_to = channel

        model = MODEL
        assistant_name = DEFAULT_ASSISTANT_NAME
        guild_context: str | None = None
        tools: AdvisorTools | None = None
        proposals: list[ConfigProposal] = []
        if guild is not None:
            db_path = self.bot.ctx.db_path
            with open_db(db_path) as conn:
                # Staff asks get the stronger model whether or not live context
                # is on — the tiering is about answer quality, not context.
                model = resolve_advisor_model(conn, guild.id, staff=is_staff(member))
                assistant_name = resolve_assistant_name_conn(conn, guild.id)
                context_on = get_advisor_context_enabled(conn, guild.id)
                tools_on = get_advisor_tools_enabled(conn, guild.id)
            if context_on:
                if public:
                    # A public answer is read by the whole channel, so it is
                    # built at @everyone visibility however senior the asker
                    # is: no staff-only channel names or topics, and no config
                    # tools (which would also attach Apply buttons the room
                    # could see). Neither guild toggle can turn this off.
                    guild_context = build_asker_context(
                        guild, None, db_path, include_config=False
                    )
                else:
                    if tools_on and member is not None and can_see_config(member):
                        tools = _make_tools(guild, member, db_path, proposals)
                    # Tools replace the inline settings dump; the rest of the
                    # context (what the asker can do, channels, docs) stays inline.
                    guild_context = build_asker_context(
                        guild, member, db_path, include_config=tools is None
                    )

        result = await answer_advisor(
            question, model=model, guild_context=guild_context, tools=tools,
            assistant_name=assistant_name, public_tutorial=public,
        )
        answer = result.answer
        if len(answer) > _MAX_DESC:
            answer = answer[:_MAX_DESC].rstrip() + "…"

        color = (
            await safe_resolve_accent(self.bot.ctx, interaction.guild, log_label="advisor")
            if interaction.guild
            else None
        )
        embed = _answer_embed(
            question=question,
            answer=answer,
            assistant_name=assistant_name,
            color=color,
            asker=interaction.user if public else None,
        )
        if post_to is not None:
            if not result.ok:
                # Don't offer to publish an "I couldn't reach the model" notice.
                await interaction.followup.send(result.answer, ephemeral=True)
                return
            await interaction.followup.send(
                content=(
                    "Preview — only you can see this. **Post to channel** "
                    "publishes it here exactly as shown."
                ),
                embed=embed,
                view=_PublicPostView(
                    channel=post_to,
                    embed=embed,
                    asker_id=interaction.user.id,
                ),
                ephemeral=True,
            )
            return
        if proposals and guild is not None:
            _proposal_fields(embed, proposals)
        view = (
            _ApplyConfigView(self.bot, self.bot.ctx.db_path, guild, proposals)
            if proposals and guild is not None
            else discord.utils.MISSING
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"❌ Give me a sec — try again in {error.retry_after:.0f}s."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        log.exception("Unexpected /ask error", exc_info=error)


async def setup(bot: Bot) -> None:
    await bot.add_cog(AdvisorCog(bot))
