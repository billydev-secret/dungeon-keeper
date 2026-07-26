from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours, open_db_immediate
from bot_modules.economy.guide import should_restick_guide
from bot_modules.games_config.logic import has_mod_or_admin_permissions
from bot_modules.services.todo_recurring_service import spawn_due
from bot_modules.services.todo_service import (
    TASK_MAX_LEN,
    clear_board,
    complete_todo,
    create_todo,
    get_board,
    guilds_with_board,
    pending_todos,
    save_board,
)
from bot_modules.todo.board_logic import (
    MAX_BOARD_ROWS,
    board_signature,
    complete_option_label,
    render_footer,
    render_rows,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger(__name__)

#: Cache TTL for the board's (channel_id, message_id) so the on_message listener
#: is a dict lookup rather than a DB read on every message.
_BOARD_CACHE_TTL = 300.0

#: Debounce before re-sticking, so a burst of chat costs one repost, not twenty.
_RESTICK_DELAY = 6.0

#: How often the background loop spawns due recurring tasks and repaints boards.
_LOOP_INTERVAL = 60.0

_MOD_ONLY_MSG = "❌ Only moderators can manage the todo list."


class TodoAddModal(discord.ui.Modal, title="Add a Task"):
    """The board's Add button — headline plus optional notes."""

    task = discord.ui.TextInput(
        label="Task",
        placeholder="What needs doing?",
        max_length=TASK_MAX_LEN,
        required=True,
    )
    notes = discord.ui.TextInput(
        label="Notes (optional)",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        text = str(self.task.value).strip()
        if not text:
            await interaction.response.send_message(
                "❌ Task cannot be empty.", ephemeral=True
            )
            return
        description = str(self.notes.value).strip() or None
        guild_id = interaction.guild.id
        user_id = interaction.user.id

        def _create() -> int:
            with cog.ctx.open_db() as conn:
                return create_todo(
                    conn, guild_id, user_id, text, description=description
                )

        todo_id = await asyncio.to_thread(_create)
        await interaction.response.send_message(
            f"Added todo #{todo_id}: {text}", ephemeral=True
        )
        await cog.refresh_board(guild_id)


class TodoCompleteSelect(discord.ui.Select):
    """Ephemeral picker listing the pending tasks a mod can tick off."""

    def __init__(self, rows: list[dict]) -> None:
        options = []
        for row in rows[:25]:  # Discord caps a select at 25 options
            label, desc = complete_option_label(row)
            options.append(
                discord.SelectOption(
                    label=label, value=str(row["id"]), description=desc or None
                )
            )
        super().__init__(
            placeholder="Pick the task you finished…",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        ids = [int(v) for v in self.values]

        def _complete() -> int:
            done = 0
            with cog.ctx.open_db() as conn:
                for todo_id in ids:
                    if complete_todo(conn, todo_id, guild_id, user_id):
                        done += 1
            return done

        done = await asyncio.to_thread(_complete)
        if done:
            noun = "task" if done == 1 else "tasks"
            msg = f"Marked {done} {noun} complete."
        else:
            msg = "Those were already completed by someone else."
        await interaction.response.edit_message(content=msg, view=None)
        await cog.refresh_board(guild_id)


class TodoBoardView(discord.ui.View):
    """The board's persistent buttons.

    Carries no per-message state, so it's a static-custom_id view (the
    GuideView/ShopPanelView pattern) re-registered in ``cog_load`` rather than a
    DynamicItem — one board per guild, and the ids never vary.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Add Task",
        emoji="➕",
        style=discord.ButtonStyle.primary,
        custom_id="todo_board_add",
    )
    async def _add(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not await _require_mod(interaction):
            return
        await interaction.response.send_modal(TodoAddModal())

    @discord.ui.button(
        label="Complete",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="todo_board_complete",
    )
    async def _complete(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not await _require_mod(interaction):
            return
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        guild_id = interaction.guild.id

        def _load() -> list[dict]:
            with cog.ctx.open_db() as conn:
                return [dict(r) for r in pending_todos(conn, guild_id, limit=25)]

        rows = await asyncio.to_thread(_load)
        if not rows:
            await interaction.response.send_message(
                "Nothing pending — the list is already clear. ✨", ephemeral=True
            )
            return
        view = discord.ui.View(timeout=180)
        view.add_item(TodoCompleteSelect(rows))
        await interaction.response.send_message(
            "Which task did you finish?", view=view, ephemeral=True
        )


def _resolve_cog(interaction: discord.Interaction) -> "TodoCog | None":
    """Resolve the cog at click time — the board outlives cog reloads."""
    bot = cast("Bot", interaction.client)
    return cast("TodoCog | None", bot.get_cog("TodoCog"))


async def _unavailable(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "❌ The todo list isn't available right now.", ephemeral=True
    )


async def _require_mod(interaction: discord.Interaction) -> bool:
    """Board buttons are moderator-only, matching `/todo` and the web routes."""
    user = interaction.user
    if not isinstance(user, discord.Member) or not has_mod_or_admin_permissions(
        user.guild_permissions
    ):
        await interaction.response.send_message(_MOD_ONLY_MSG, ephemeral=True)
        return False
    return True


class TodoCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        # guild → (monotonic expiry, channel_id, message_id)
        self._board_ref: dict[int, tuple[float, int, int]] = {}
        self._board_locks: dict[int, asyncio.Lock] = {}
        self._restick_tasks: dict[int, asyncio.Task[None]] = {}
        # guild → signature of what the board last rendered, so an unchanged
        # board costs no API call.
        self._board_sig: dict[int, tuple] = {}
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.add_view(TodoBoardView())

    async def cog_unload(self) -> None:
        for task in self._restick_tasks.values():
            task.cancel()
        self._restick_tasks.clear()

    # ── slash command ────────────────────────────────────────────────────

    @app_commands.command(name="todo", description="Add a task to the server todo list.")
    @app_commands.describe(task="The task to add.")
    async def todo(self, interaction: discord.Interaction, task: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        # The todo list is a mod worklist, curated from the dashboard — only
        # moderators may add to it (the web endpoints are mod-gated too).
        if not isinstance(
            interaction.user, discord.Member
        ) or not has_mod_or_admin_permissions(interaction.user.guild_permissions):
            await interaction.response.send_message(
                "❌ Only moderators can add to the todo list.", ephemeral=True
            )
            return
        task = task.strip()
        if not task:
            await interaction.response.send_message("❌ Task cannot be empty.", ephemeral=True)
            return
        if len(task) > TASK_MAX_LEN:
            await interaction.response.send_message(
                f"❌ Task must be {TASK_MAX_LEN} characters or fewer.", ephemeral=True
            )
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id

        def _do_create_todo():
            with self.ctx.open_db() as conn:
                return create_todo(conn, guild_id, user_id, task)

        todo_id = await asyncio.to_thread(_do_create_todo)
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {task}", ephemeral=True
        )
        await self.refresh_board(guild_id)

    # ── board rendering ──────────────────────────────────────────────────

    async def build_board_embed(
        self, guild: discord.Guild, rows: list[dict]
    ) -> discord.Embed:
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title="📋 Server Todo",
            description=render_rows(rows, limit=MAX_BOARD_ROWS),
            color=accent,
        )
        embed.set_footer(text=render_footer(rows))
        return embed

    def _load_pending(self, guild_id: int) -> list[dict]:
        with self.ctx.open_db() as conn:
            return [dict(r) for r in pending_todos(conn, guild_id)]

    # ── placement + sticky repost ────────────────────────────────────────

    async def place_board(
        self, guild: discord.Guild, target: discord.TextChannel
    ) -> discord.Message | None:
        """Delete the old board (if any) and post a fresh one at the bottom of
        ``target``, persisting the new ids. Returns None when posting is
        forbidden. Serialised per guild so a dashboard post and a sticky repost
        can't race into two boards.
        """
        lock = self._board_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            # Re-read the stored ids INSIDE the lock — a caller's pre-lock
            # snapshot can be stale after a racing post, and deleting it would
            # orphan the current live board.
            def _load() -> tuple[tuple[int, int], list[dict]]:
                with self.ctx.open_db() as conn:
                    board = get_board(conn, guild.id)
                    rows = [dict(r) for r in pending_todos(conn, guild.id)]
                return (board.channel_id, board.message_id), rows

            (old_channel_id, old_message_id), rows = await asyncio.to_thread(_load)
            embed = await self.build_board_embed(guild, rows)

            if old_channel_id and old_message_id:
                old_channel = guild.get_channel(old_channel_id)
                if isinstance(old_channel, discord.TextChannel):
                    try:
                        old = await old_channel.fetch_message(old_message_id)
                        await old.delete()
                    except discord.HTTPException:
                        pass

            try:
                message = await target.send(embed=embed, view=TodoBoardView())
            except discord.Forbidden:
                return None

            # Record the new id before the DB-save await so our own repost's
            # gateway event is recognised (and skipped) by the sticky listener.
            self._board_ref[guild.id] = (
                time.monotonic() + _BOARD_CACHE_TTL,
                target.id,
                message.id,
            )
            self._board_sig[guild.id] = board_signature(rows)

            def _save() -> None:
                with self.ctx.open_db() as conn:
                    save_board(conn, guild.id, target.id, message.id)

            await asyncio.to_thread(_save)
            return message

    async def unpost_board(self, guild: discord.Guild) -> bool:
        """Delete the board and forget its placement. True if one was removed."""
        lock = self._board_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:

            def _load() -> tuple[int, int]:
                with self.ctx.open_db() as conn:
                    board = get_board(conn, guild.id)
                return board.channel_id, board.message_id

            channel_id, message_id = await asyncio.to_thread(_load)
            if channel_id and message_id:
                channel = guild.get_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    try:
                        message = await channel.fetch_message(message_id)
                        await message.delete()
                    except discord.HTTPException:
                        pass

            self._board_ref.pop(guild.id, None)
            self._board_sig.pop(guild.id, None)

            def _clear() -> None:
                with self.ctx.open_db() as conn:
                    clear_board(conn, guild.id)

            await asyncio.to_thread(_clear)
            return bool(channel_id and message_id)

    async def refresh_board(self, guild_id: int) -> bool:
        """Edit the board in place to match the current task list.

        Skips the API call when the rendered content is unchanged — ages are
        `<t:…:R>` timestamps that tick client-side, so "2h → 3h" needs no edit.
        Returns True when an edit was actually issued.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False

        def _load() -> tuple[tuple[int, int], list[dict]]:
            with self.ctx.open_db() as conn:
                board = get_board(conn, guild_id)
                rows = [dict(r) for r in pending_todos(conn, guild_id)]
            return (board.channel_id, board.message_id), rows

        (channel_id, message_id), rows = await asyncio.to_thread(_load)
        if not channel_id or not message_id:
            return False

        signature = board_signature(rows)
        if self._board_sig.get(guild_id) == signature:
            return False

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        embed = await self.build_board_embed(guild, rows)
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed, view=TodoBoardView())
        except discord.NotFound:
            # The board was deleted out from under us — re-post it so the
            # feature heals itself rather than going quietly dead.
            await self.place_board(guild, channel)
            return True
        except discord.HTTPException:
            return False
        self._board_sig[guild_id] = signature
        return True

    @commands.Cog.listener("on_message")
    async def _restick_board(self, message: discord.Message) -> None:
        """Arm a debounced repost when a member posts below the board — the
        same bottom-sticky behaviour as the economy guide/shop panels."""
        if message.guild is None or message.author.bot:
            return
        guild_id = message.guild.id
        channel_id, message_id = await self._board_panel_ref(guild_id)
        if not should_restick_guide(
            message_channel_id=message.channel.id,
            message_id=message.id,
            panel_channel_id=channel_id,
            panel_message_id=message_id,
        ):
            return
        self._schedule_restick(guild_id)

    async def _board_panel_ref(self, guild_id: int) -> tuple[int, int]:
        """Cached ``(channel_id, message_id)`` of the board, or ``(0, 0)``."""
        entry = self._board_ref.get(guild_id)
        now = time.monotonic()
        if entry is not None and entry[0] > now:
            return entry[1], entry[2]

        def _load() -> tuple[int, int]:
            with self.ctx.open_db() as conn:
                board = get_board(conn, guild_id)
            return board.channel_id, board.message_id

        channel_id, message_id = await asyncio.to_thread(_load)
        self._board_ref[guild_id] = (now + _BOARD_CACHE_TTL, channel_id, message_id)
        return channel_id, message_id

    def _schedule_restick(self, guild_id: int) -> None:
        existing = self._restick_tasks.get(guild_id)
        if existing and not existing.done():
            existing.cancel()
        self._restick_tasks[guild_id] = asyncio.create_task(
            self._delayed_restick(guild_id)
        )

    async def _delayed_restick(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(_RESTICK_DELAY)
        except asyncio.CancelledError:
            return
        try:
            await self._restick_now(guild_id)
        except Exception:
            log.exception("todo board restick failed for guild %s", guild_id)

    async def _restick_now(self, guild_id: int) -> None:
        """Move an already-posted board back to the channel bottom.

        Only ever maintains an existing board — it never creates one, so a guild
        that has not configured a board channel stays untouched.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        def _load() -> tuple[int, int]:
            with self.ctx.open_db() as conn:
                board = get_board(conn, guild_id)
            return board.channel_id, board.message_id

        channel_id, message_id = await asyncio.to_thread(_load)
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        await self.place_board(guild, channel)


async def todo_board_loop(bot: Bot) -> None:
    """Spawn due recurring tasks, then repaint any board whose content changed.

    This is also what picks up dashboard-side edits — the web routes and the bot
    share a process, but a route mutation has no gateway event to react to.
    """
    await bot.wait_until_ready()
    ctx = bot.ctx
    while not bot.is_closed():
        try:
            def _tick() -> list[int]:
                now = time.time()
                # BEGIN IMMEDIATE: spawn_due reads (is the last instance still
                # open?) and then writes. A racing "Run now" from the dashboard
                # on a deferred transaction could read the same "nothing open"
                # snapshot and insert a second copy, defeating skip-if-pending.
                with open_db_immediate(ctx.db_path) as conn:
                    offsets: dict[int, float] = {}

                    def _offset_for(guild_id: int) -> float:
                        if guild_id not in offsets:
                            offsets[guild_id] = get_tz_offset_hours(conn, guild_id)
                        return offsets[guild_id]

                    # Spawning is guild-wide: a recurring task is due whether or
                    # not that guild has posted a board.
                    spawn_due(conn, now_ts=now, offset_hours_for=_offset_for)
                    # Then refresh every board, not only guilds that spawned —
                    # tasks also arrive from the dashboard and from `/todo`.
                    # refresh_board() no-ops when nothing actually changed.
                    return sorted(guilds_with_board(conn))

            guild_ids = await asyncio.to_thread(_tick)
            cog = cast("TodoCog | None", bot.get_cog("TodoCog"))
            if cog is not None:
                for guild_id in guild_ids:
                    try:
                        await cog.refresh_board(guild_id)
                    except Exception:
                        log.exception("todo board refresh failed for %s", guild_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("todo board loop tick failed")
        await asyncio.sleep(_LOOP_INTERVAL)


async def setup(bot: Bot) -> None:
    await bot.add_cog(TodoCog(bot, bot.ctx))
