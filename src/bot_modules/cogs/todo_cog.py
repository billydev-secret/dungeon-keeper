from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours, open_db, open_db_immediate
from bot_modules.core.sticky import should_restick_guide
from bot_modules.games_config.logic import has_mod_or_admin_permissions
from bot_modules.services.todo_recurring_service import due_recurring, spawn_due
from bot_modules.services.todo_service import (
    TASK_MAX_LEN,
    clear_board,
    complete_todo,
    create_todo,
    get_board,
    guilds_with_board,
    pending_count,
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
        todo_id = await cog.add_todo(
            interaction.guild.id,
            interaction.user.id,
            text,
            description=str(self.notes.value).strip() or None,
        )
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {text}", ephemeral=True
        )


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


async def _require_mod(
    interaction: discord.Interaction, action: str = "manage the todo list"
) -> bool:
    """The one moderator gate for every Discord surface of the todo list —
    `/todo` and both board buttons. The web routes enforce the same tier."""
    user = interaction.user
    if not isinstance(user, discord.Member) or not has_mod_or_admin_permissions(
        user.guild_permissions
    ):
        await interaction.response.send_message(
            f"❌ Only moderators can {action}.", ephemeral=True
        )
        return False
    return True


class TodoCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        # guild → (monotonic expiry, channel_id, message_id)
        self._board_ref: dict[int, tuple[float, int, int]] = {}
        # defaultdict, not setdefault: the latter builds a throwaway Lock on
        # every placement call just to discard it.
        self._board_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._restick_tasks: dict[int, asyncio.Task[None]] = {}
        # Guilds that actually have a board, republished each tick. Lets the
        # on_message listener reject the common case with a set lookup.
        self._boards: set[int] = set()
        self._boards_known = False
        # Guilds whose in-place edit failed; the loop retries these next tick
        # so a transient Discord error doesn't strand a stale board.
        self._retry_refresh: set[int] = set()
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
        if not await _require_mod(interaction, "add to the todo list"):
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
        todo_id = await self.add_todo(
            interaction.guild.id, interaction.user.id, task
        )
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {task}", ephemeral=True
        )

    async def add_todo(
        self,
        guild_id: int,
        user_id: int,
        task: str,
        *,
        description: str | None = None,
    ) -> int:
        """Create a task and repaint the board. Shared by `/todo` and the
        board's Add button so the two can't drift."""

        def _create() -> int:
            with self.ctx.open_db() as conn:
                return create_todo(
                    conn, guild_id, user_id, task, description=description
                )

        todo_id = await asyncio.to_thread(_create)
        await self.refresh_board(guild_id)
        return todo_id

    # ── board rendering ──────────────────────────────────────────────────

    async def build_board_embed(
        self, guild: discord.Guild, rows: list[dict], total: int
    ) -> discord.Embed:
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title="📋 Server Todo",
            description=render_rows(rows, total=total),
            color=accent,
        )
        embed.set_footer(text=render_footer(total))
        return embed

    # ── board state reads ────────────────────────────────────────────────
    #
    # Every placement/refresh path needs the stored ids, and most also need the
    # rows to render. These two helpers are the only places that know how that
    # is loaded, so the storage shape lives in one spot rather than five.

    def _read_ids(self, guild_id: int) -> tuple[int, int]:
        with self.ctx.open_db() as conn:
            board = get_board(conn, guild_id)
        return board.channel_id, board.message_id

    def _read_board(self, guild_id: int) -> tuple[tuple[int, int], list[dict], int]:
        with self.ctx.open_db() as conn:
            board = get_board(conn, guild_id)
            # One screenful plus a sentinel: enough to render and to know the
            # list overflows, without hauling every pending row.
            rows = [
                dict(r)
                for r in pending_todos(conn, guild_id, limit=MAX_BOARD_ROWS + 1)
            ]
            total = pending_count(conn, guild_id)
        return (board.channel_id, board.message_id), rows, total

    async def _board_ids(self, guild_id: int) -> tuple[int, int]:
        return await asyncio.to_thread(self._read_ids, guild_id)

    def _resolve_channel(
        self, guild: discord.Guild, channel_id: int
    ) -> discord.TextChannel | None:
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    # ── placement + sticky repost ────────────────────────────────────────

    async def place_board(
        self, guild: discord.Guild, target: discord.TextChannel
    ) -> discord.Message | None:
        """Post a fresh board at the bottom of ``target`` and remove the old
        one, persisting the new ids. Returns None when posting fails, leaving
        any existing board untouched. Serialised per guild so a dashboard post
        and a sticky repost can't race into two boards.
        """
        async with self._board_locks[guild.id]:
            # Re-read the stored ids INSIDE the lock — a caller's pre-lock
            # snapshot can be stale after a racing post, and deleting it would
            # orphan the current live board.
            (old_channel_id, old_message_id), rows, total = await asyncio.to_thread(
                self._read_board, guild.id
            )
            embed = await self.build_board_embed(guild, rows, total)

            # Post the replacement BEFORE removing the old one. Deleting first
            # would destroy a working board when the new channel turns out to
            # be unpostable (no Send Messages, embed rejected, transient 5xx) —
            # and if the target *is* the old channel there'd be nothing left to
            # heal from. Worst case here is two boards for a moment.
            try:
                message = await target.send(embed=embed, view=TodoBoardView())
            except discord.HTTPException:
                return None

            old_channel = self._resolve_channel(guild, old_channel_id)
            if old_channel is not None and old_message_id:
                try:
                    await old_channel.get_partial_message(old_message_id).delete()
                except discord.HTTPException:
                    pass

            # Record the new id before the DB-save await so our own repost's
            # gateway event is recognised (and skipped) by the sticky listener.
            self._board_ref[guild.id] = (
                time.monotonic() + _BOARD_CACHE_TTL,
                target.id,
                message.id,
            )
            self._board_sig[guild.id] = board_signature(rows, total)
            self._boards.add(guild.id)
            message_id = message.id

            def _save() -> None:
                with self.ctx.open_db() as conn:
                    save_board(conn, guild.id, target.id, message_id)

            await asyncio.to_thread(_save)
            return message

    async def unpost_board(self, guild: discord.Guild) -> bool:
        """Delete the board and forget its placement. True if one was removed."""
        async with self._board_locks[guild.id]:
            channel_id, message_id = await self._board_ids(guild.id)
            channel = self._resolve_channel(guild, channel_id)
            if channel is not None and message_id:
                try:
                    await channel.get_partial_message(message_id).delete()
                except discord.HTTPException:
                    pass

            self._board_ref.pop(guild.id, None)
            self._board_sig.pop(guild.id, None)
            self._boards.discard(guild.id)

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

        (channel_id, message_id), rows, total = await asyncio.to_thread(
            self._read_board, guild_id
        )
        if not channel_id or not message_id:
            return False

        signature = board_signature(rows, total)
        if self._board_sig.get(guild_id) == signature:
            return False

        channel = self._resolve_channel(guild, channel_id)
        if channel is None:
            return False
        embed = await self.build_board_embed(guild, rows, total)
        try:
            await channel.get_partial_message(message_id).edit(
                embed=embed, view=TodoBoardView()
            )
        except discord.NotFound:
            # The board was deleted out from under us — re-post it so the
            # feature heals itself rather than going quietly dead.
            return await self.place_board(guild, channel) is not None
        except discord.HTTPException:
            # Leave the signature stale and ask the loop to try again, so a
            # transient error doesn't strand the board until the next mutation.
            self._retry_refresh.add(guild_id)
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
        # Fast path: the loop publishes which guilds actually have a board, so
        # the overwhelming majority of messages cost one set lookup and no I/O.
        # Falls through to the cached read until the first tick populates it.
        if self._boards_known and guild_id not in self._boards:
            return
        channel_id, board_message_id = await self._board_panel_ref(guild_id)
        if not should_restick_guide(
            message_channel_id=message.channel.id,
            message_id=message.id,
            panel_channel_id=channel_id,
            panel_message_id=board_message_id,
        ):
            return
        self._schedule_restick(guild_id)

    async def _board_panel_ref(self, guild_id: int) -> tuple[int, int]:
        """Cached ``(channel_id, message_id)`` of the board, or ``(0, 0)``."""
        entry = self._board_ref.get(guild_id)
        now = time.monotonic()
        if entry is not None and entry[0] > now:
            return entry[1], entry[2]
        channel_id, message_id = await self._board_ids(guild_id)
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
        """After the debounce, move an already-posted board back to the bottom.

        Only ever maintains an existing board — it never creates one, so a
        guild that has not configured a board channel stays untouched.
        """
        try:
            await asyncio.sleep(_RESTICK_DELAY)
        except asyncio.CancelledError:
            return
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            channel_id, message_id = await self._board_ids(guild_id)
            if not message_id:
                return
            channel = self._resolve_channel(guild, channel_id)
            if channel is not None:
                await self.place_board(guild, channel)
        except Exception:
            log.exception("todo board restick failed for guild %s", guild_id)

def _tick(ctx) -> tuple[set[int], set[int]]:
    """One scheduler pass: spawn what is due, report what needs repainting.

    Returns ``(guilds_with_a_board, guilds_that_gained_a_task)``. Runs in a
    worker thread — all of it is blocking sqlite.
    """
    now = time.time()
    # Probe on a plain (deferred) transaction first. spawn_due is a
    # read-then-write, so it needs BEGIN IMMEDIATE to keep skip-if-pending
    # honest against a racing "Run now" — but taking the global write lock
    # every 60s when nothing is due would make every other writer queue behind
    # a no-op. Escalate only on a hit; the escalated transaction re-reads under
    # the lock, so the race the lock guards is still covered.
    with open_db(ctx.db_path) as conn:
        due = bool(due_recurring(conn, now))
        boards = set(guilds_with_board(conn))
    if not due:
        return boards, set()

    with open_db_immediate(ctx.db_path) as conn:
        spawned = spawn_due(
            conn,
            now_ts=now,
            offset_hours_for=lambda gid: get_tz_offset_hours(conn, gid),
        )
        boards = set(guilds_with_board(conn))
    return boards, {r.guild_id for r in spawned if r.status == "spawned"}


async def todo_board_loop(bot: Bot) -> None:
    """Spawn due recurring tasks, then repaint the boards that changed.

    Every user-facing mutation (`/todo`, the board buttons, the dashboard)
    repaints the board itself, so on a quiet tick there is nothing to do. The
    two things this loop owes a repaint are tasks it just spawned and edits a
    previous tick failed to apply — anything else would be N pointless DB reads
    and signature comparisons per minute, forever.
    """
    await bot.wait_until_ready()
    ctx = bot.ctx
    while not bot.is_closed():
        try:
            boards, spawned = await asyncio.to_thread(_tick, ctx)
            cog = cast("TodoCog | None", bot.get_cog("TodoCog"))
            if cog is not None:
                # Publish the board set so the on_message listener can reject
                # boardless guilds without touching the DB.
                cog._boards = boards
                cog._boards_known = True
                # Repaint sequentially: these are edits against N different
                # channels and a 60s cadence gives no reason to burst them.
                for guild_id in sorted((spawned | cog._retry_refresh) & boards):
                    cog._retry_refresh.discard(guild_id)
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
