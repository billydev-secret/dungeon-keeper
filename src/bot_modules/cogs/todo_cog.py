from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours, open_db, open_db_immediate
from bot_modules.core.sticky import PanelContent, StickyPanel
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
        self.board = StickyPanel(
            "todo board",
            bot,
            load_ids=self._read_ids,
            save_ids=self._write_ids,
            build=self.build_panel,
        )
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.add_view(TodoBoardView())

    async def cog_unload(self) -> None:
        self.board.cancel_all()

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

    # ── board plumbing ───────────────────────────────────────────────────
    #
    # The sticky machinery (locks, debounce, id cache, delete-and-repost) lives
    # in core.sticky.StickyPanel; this cog only says where the ids are stored
    # and what the panel should look like.

    def _read_ids(self, guild_id: int) -> tuple[int, int]:
        with self.ctx.open_db() as conn:
            board = get_board(conn, guild_id)
        return board.channel_id, board.message_id

    def _write_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self.ctx.open_db() as conn:
            if channel_id and message_id:
                save_board(conn, guild_id, channel_id, message_id)
            else:
                clear_board(conn, guild_id)

    def _read_rows(self, guild_id: int) -> tuple[list[dict], int]:
        with self.ctx.open_db() as conn:
            # One screenful plus a sentinel: enough to render and to know the
            # list overflows, without hauling every pending row.
            rows = [
                dict(r)
                for r in pending_todos(conn, guild_id, limit=MAX_BOARD_ROWS + 1)
            ]
            total = pending_count(conn, guild_id)
        return rows, total

    async def build_panel(self, guild: discord.Guild) -> PanelContent:
        rows, total = await asyncio.to_thread(self._read_rows, guild.id)
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = discord.Embed(
            title="📋 Server Todo",
            description=render_rows(rows, total=total),
            color=accent,
        )
        embed.set_footer(text=render_footer(total))
        return PanelContent(
            embed=embed,
            view=TodoBoardView(),
            signature=board_signature(rows, total),
        )

    # Named pass-throughs: the routes, the loop and the tests all speak
    # "board", not "panel".

    async def place_board(
        self, guild: discord.Guild, target: discord.TextChannel
    ) -> discord.Message | None:
        return await self.board.place(guild, target)

    async def unpost_board(self, guild: discord.Guild) -> bool:
        return await self.board.unpost(guild)

    async def refresh_board(self, guild_id: int) -> bool:
        return await self.board.refresh(guild_id)

    @commands.Cog.listener("on_message")
    async def _restick_board(self, message: discord.Message) -> None:
        await self.board.on_message(message)


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
                cog.board.set_known_guilds(boards)
                # Repaint sequentially: these are edits against N different
                # channels and a 60s cadence gives no reason to burst them.
                for guild_id in sorted((spawned | cog.board.retry) & boards):
                    cog.board.retry.discard(guild_id)
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
