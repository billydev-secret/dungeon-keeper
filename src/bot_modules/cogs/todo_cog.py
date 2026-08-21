from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import get_tz_offset_hours, open_db, open_db_immediate
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.services.todo_recurring_service import (
    chore_board_rows,
    due_recurring,
    spawn_due,
)
from bot_modules.services.todo_service import (
    BOARD_ALL,
    BOARD_CHORES,
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
    chore_signature,
    complete_option_label,
    nothing_to_tick_message,
    render_chore_footer,
    render_chore_rows,
    render_footer,
    render_rows,
    tickable_chores,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger(__name__)

#: How often the background loop spawns due recurring tasks and repaints boards.
_LOOP_INTERVAL = 60.0

#: How many chores the chore board reads. It renders ``MAX_BOARD_ROWS`` and
#: reports the rest as a count, and the footer's "N of M done" has to count the
#: overflow too — so unlike the all-todos board this fetches a real slice
#: rather than one sentinel row past the window. Active recurring definitions
#: are a handful per guild; a server past this many has a dashboard problem,
#: not a board problem.
_CHORE_FETCH = 50



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
        guild_id = interaction.guild.id
        todo_id = await cog.add_todo(
            guild_id,
            interaction.user.id,
            text,
            description=str(self.notes.value).strip() or None,
        )
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {text}", ephemeral=True
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
            with cog.bot.ctx.open_db() as conn:
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
        # Both boards: a recurring row shows on the chore board *and*, while it
        # is outstanding, on the all-todos board.
        await cog.refresh_boards(guild_id)


class TodoBoardView(discord.ui.View):
    """The board's persistent buttons.

    Carries no per-message state, so it's a static-custom_id view (the
    ShopPanelView pattern) re-registered in ``cog_load`` rather than a
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
        cog = _resolve_cog(interaction)
        if cog is None:
            await _unavailable(interaction)
            return
        if not await _require_mod(interaction, cog.bot.ctx):
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
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        if not await _require_mod(interaction, cog.bot.ctx):
            return
        guild_id = interaction.guild.id

        def _load() -> list[dict]:
            with cog.bot.ctx.open_db() as conn:
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


class TodoChoreBoardView(discord.ui.View):
    """The chore board's one button.

    No Add here on purpose. A chore is a *recurring definition*, created on the
    dashboard with a cadence and a time of day — the thing this button's
    neighbour on the other board adds is a one-off task, which is exactly what
    this board is for not showing. Ticking, though, has to be possible where
    the chores are: the whole point of a second board in the mods' own channel
    is that they never have to go and find the first one.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Mark Done",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="todo_chore_board_complete",
    )
    async def _complete(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        cog = _resolve_cog(interaction)
        if cog is None or interaction.guild is None:
            await _unavailable(interaction)
            return
        if not await _require_mod(interaction, cog.bot.ctx, "tick off chores"):
            return
        guild_id = interaction.guild.id

        def _load() -> tuple[list[dict], str]:
            with cog.bot.ctx.open_db() as conn:
                # The same slice the board renders. Reading fewer than the
                # board shows lets "Every chore is already ticked off" appear
                # while open rows are visible in the message above it.
                # TodoCompleteSelect caps the options at Discord's 25.
                rows = chore_board_rows(conn, guild_id, limit=_CHORE_FETCH)
            # Both the filter and the empty-case wording come from board_logic,
            # alongside the chore_state the board renders with — the two used
            # to disagree about a definition with no instance behind it.
            options = [
                {"id": row["todo_id"], "task": row["task"], "description": None}
                for row in tickable_chores(rows)
            ]
            return options, nothing_to_tick_message(rows)

        rows, empty_message = await asyncio.to_thread(_load)
        if not rows:
            await interaction.response.send_message(empty_message, ephemeral=True)
            return
        view = discord.ui.View(timeout=180)
        view.add_item(TodoCompleteSelect(rows))
        await interaction.response.send_message(
            "Which chore did you do?", view=view, ephemeral=True
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
    interaction: discord.Interaction,
    ctx: AppContext,
    action: str = "manage the todo list",
) -> bool:
    """The one moderator gate for every Discord surface of the todo list —
    `/todo` and both board buttons.

    Delegates to ``AppContext.is_mod``: Discord's administrator/manage_guild
    short-circuit, then the guild's configured ``mod_role_ids``/
    ``admin_role_ids``. That is the bot's house definition of a moderator, and
    ``web_server.auth.resolve_guild_perms`` resolves the dashboard's
    ``moderator`` tier the same way — so a mod refused here is refused there,
    and a mod who can tick a chore off on the dashboard can tick it off in the
    channel. This gate used to read the games cogs'
    ``has_mod_or_admin_permissions`` (administrator/manage_guild/manage_channels
    and nothing else), which refused real moderators the dashboard let in.
    """
    user = interaction.user
    if not isinstance(user, discord.Member) or not ctx.is_mod(interaction):
        await interaction.response.send_message(
            f"❌ Only moderators can {action}.", ephemeral=True
        )
        return False
    return True


class TodoCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.board = StickyPanel(
            "todo board",
            bot,
            load_ids=self._read_ids,
            save_ids=self._write_ids,
            build=self.build_panel,
        )
        # A second, independent sticky panel. Neither sets restick_on_bot, so
        # they cannot chase each other's reposts — and they can never share a
        # channel anyway: todo_service.conflicting_board refuses that at
        # configuration time, which is the only place the collision is legible.
        self.chore_board = StickyPanel(
            "todo chore board",
            bot,
            load_ids=self._read_chore_ids,
            save_ids=self._write_chore_ids,
            build=self.build_chore_panel,
        )
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.add_view(TodoBoardView())
        self.bot.add_view(TodoChoreBoardView())

    async def cog_unload(self) -> None:
        self.board.cancel_all()
        self.chore_board.cancel_all()

    # ── slash command ────────────────────────────────────────────────────

    @app_commands.command(name="todo", description="Add a task to the server todo list.")
    @app_commands.describe(task="The task to add.")
    async def todo(self, interaction: discord.Interaction, task: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        # The todo list is a mod worklist, curated from the dashboard — only
        # moderators may add to it (the web endpoints are mod-gated too).
        if not await _require_mod(interaction, self.bot.ctx, "add to the todo list"):
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
        todo_id = await self.add_todo(guild_id, interaction.user.id, task)
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {task}", ephemeral=True
        )
        await self.refresh_board(guild_id)

    async def add_todo(
        self,
        guild_id: int,
        user_id: int,
        task: str,
        *,
        description: str | None = None,
    ) -> int:
        """Create a task. Shared by `/todo` and the board's Add button.

        Deliberately does *not* repaint the board: callers must answer the
        interaction first. A repaint is a REST edit, and under per-channel rate
        limiting discord.py sleeps until reset — long enough to burn the
        three-second window and show "This interaction failed" for a task that
        was in fact saved.
        """

        def _create() -> int:
            with self.bot.ctx.open_db() as conn:
                return create_todo(
                    conn, guild_id, user_id, task, description=description
                )

        return await asyncio.to_thread(_create)

    # ── board plumbing ───────────────────────────────────────────────────
    #
    # The sticky machinery (locks, debounce, id cache, delete-and-repost) lives
    # in core.sticky.StickyPanel; this cog only says where the ids are stored
    # and what the panel should look like.

    def _read_ids(self, guild_id: int) -> tuple[int, int]:
        with self.bot.ctx.open_db() as conn:
            board = get_board(conn, guild_id, BOARD_ALL)
        return board.channel_id, board.message_id

    def _write_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self.bot.ctx.open_db() as conn:
            if channel_id and message_id:
                save_board(conn, guild_id, channel_id, message_id, kind=BOARD_ALL)
            else:
                clear_board(conn, guild_id, kind=BOARD_ALL)

    def _read_chore_ids(self, guild_id: int) -> tuple[int, int]:
        with self.bot.ctx.open_db() as conn:
            board = get_board(conn, guild_id, BOARD_CHORES)
        return board.channel_id, board.message_id

    def _write_chore_ids(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        with self.bot.ctx.open_db() as conn:
            if channel_id and message_id:
                save_board(conn, guild_id, channel_id, message_id, kind=BOARD_CHORES)
            else:
                clear_board(conn, guild_id, kind=BOARD_CHORES)

    def _read_rows(self, guild_id: int) -> tuple[list[dict], int]:
        with self.bot.ctx.open_db() as conn:
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
        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="todo")
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

    def _display_name(self, guild: discord.Guild, user_id) -> str:
        """Who ticked it, as the mods know them. Never a raw id on the board."""
        if not user_id:
            return ""
        member = guild.get_member(int(user_id))
        return member.display_name if member is not None else "someone"

    def _read_chore_rows(self, guild_id: int) -> list[dict]:
        with self.bot.ctx.open_db() as conn:
            return chore_board_rows(conn, guild_id, limit=_CHORE_FETCH)

    async def build_chore_panel(self, guild: discord.Guild) -> PanelContent:
        rows = await asyncio.to_thread(self._read_chore_rows, guild.id)
        for row in rows:
            row["completed_by_name"] = self._display_name(guild, row.get("completed_by"))
        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="todo")
        embed = discord.Embed(
            title="🔁 Mod Chores",
            description=render_chore_rows(rows),
            color=accent,
        )
        embed.set_footer(text=render_chore_footer(rows))
        return PanelContent(
            embed=embed,
            view=TodoChoreBoardView(),
            signature=chore_signature(rows),
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

    async def place_chore_board(
        self, guild: discord.Guild, target: discord.TextChannel
    ) -> discord.Message | None:
        return await self.chore_board.place(guild, target)

    async def unpost_chore_board(self, guild: discord.Guild) -> bool:
        return await self.chore_board.unpost(guild)

    async def refresh_chore_board(self, guild_id: int) -> bool:
        return await self.chore_board.refresh(guild_id)

    async def refresh_boards(self, guild_id: int) -> None:
        """Repaint both boards. Each is signature-guarded, so the one nothing
        changed on costs a DB read and no API call."""
        await self.refresh_board(guild_id)
        await self.refresh_chore_board(guild_id)

    @commands.Cog.listener("on_message")
    async def _restick_board(self, message: discord.Message) -> None:
        await self.board.on_message(message)
        await self.chore_board.on_message(message)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_board_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """Clear either board's ids if its channel was deleted."""
        await self.board.on_channel_delete(channel)
        await self.chore_board.on_channel_delete(channel)


def _tick(ctx) -> tuple[set[int], set[int], set[int]]:
    """One scheduler pass: spawn what is due, report what needs repainting.

    Returns ``(guilds_with_an_all_board, guilds_with_a_chore_board,
    guilds_that_changed)``. Runs in a worker thread — all of it is blocking
    sqlite.

    "Changed" now covers a **reset that only wrote a row off**. A day where the
    chore was not done spawns a fresh row *and* marks the old one missed, and
    even in the edge case where the spawn is what fails, the write-off alone
    has moved both boards — so keying the repaint on "did a row spawn" would
    leave the miss invisible until something else happened to repaint.
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
        boards = set(guilds_with_board(conn, BOARD_ALL))
        chore_boards = set(guilds_with_board(conn, BOARD_CHORES))
    if not due:
        return boards, chore_boards, set()

    with open_db_immediate(ctx.db_path) as conn:
        spawned = spawn_due(
            conn,
            now_ts=now,
            offset_hours_for=lambda gid: get_tz_offset_hours(conn, gid),
        )
        boards = set(guilds_with_board(conn, BOARD_ALL))
        chore_boards = set(guilds_with_board(conn, BOARD_CHORES))
    changed = {
        r.guild_id
        for r in spawned
        if r.todo_id is not None or r.missed_todo_id is not None
    }
    return boards, chore_boards, changed


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
            boards, chore_boards, changed = await asyncio.to_thread(_tick, ctx)
            cog = cast("TodoCog | None", bot.get_cog("TodoCog"))
            if cog is not None:
                # Publish each board set so the on_message listener can reject
                # boardless guilds without touching the DB. The two panels keep
                # separate sets: a guild may run the chore board alone.
                cog.board.set_known_guilds(boards)
                cog.chore_board.set_known_guilds(chore_boards)
                # Repaint sequentially: these are edits against N different
                # channels and a 60s cadence gives no reason to burst them.
                # take_retries() is drained once per panel — leaving a panel's
                # undrained is how pen pals lost its failed edits (F5 in
                # docs/reviews/2026-08-06-sticky-panel-machinery.md).
                for guild_id in sorted((changed | cog.board.take_retries()) & boards):
                    try:
                        await cog.refresh_board(guild_id)
                    except Exception:
                        log.exception("todo board refresh failed for %s", guild_id)
                chore_work = (changed | cog.chore_board.take_retries()) & chore_boards
                for guild_id in sorted(chore_work):
                    try:
                        await cog.refresh_chore_board(guild_id)
                    except Exception:
                        log.exception(
                            "todo chore board refresh failed for %s", guild_id
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("todo board loop tick failed")
        await asyncio.sleep(_LOOP_INTERVAL)


async def setup(bot: Bot) -> None:
    await bot.add_cog(TodoCog(bot))
