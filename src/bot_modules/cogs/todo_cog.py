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
from bot_modules.confessions.approval_views import open_confessions_picker
from bot_modules.economy.approval_views import open_approvals_picker
from bot_modules.economy.quest_views import open_signoff_picker
from bot_modules.services.confessions_service import (
    pending_confession_count,
    pending_confessions,
)
from bot_modules.services.economy_approvals_service import (
    pending_approval_count,
    pending_approvals,
)
from bot_modules.services.economy_quests_service import (
    pending_signoff_count,
    pending_signoff_rows,
)
from bot_modules.services.economy_service import load_econ_settings
from bot_modules.services.todo_recurring_service import (
    chore_board_rows,
    due_recurring,
    spawn_due,
)
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
    MAX_APPROVAL_ROWS,
    MAX_BOARD_ROWS,
    MAX_CONFESSION_ROWS,
    MAX_SIGNOFF_ROWS,
    board_content_signature,
    complete_option_label,
    completable_options,
    nothing_to_tick_message,
    render_board,
    render_board_footer,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger(__name__)

#: How often the background loop spawns due recurring tasks and repaints boards.
_LOOP_INTERVAL = 60.0

#: How many chores the board reads. It renders a bounded slice and reports the
#: rest as a count, and the footer's "N of M done" has to count the overflow
#: too — so unlike the task list this fetches a real slice rather than one
#: sentinel row past the window. Active recurring definitions are a handful per
#: guild; a server past this many has a dashboard problem, not a board problem.
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
        await cog.refresh_board(guild_id)


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

        def _load() -> tuple[list[dict], str]:
            """Everything tickable, in the order the board shows it.

            One button over both sections: with a single board there is no
            longer a "which list was it on?" for a mod to answer first, and the
            thing being completed is a todo row either way.
            """
            with cog.bot.ctx.open_db() as conn:
                # The same slice the board renders — reading fewer than it
                # shows lets an "already clear" message appear over visibly
                # open rows. TodoCompleteSelect caps at Discord's 25.
                chores = [
                    dict(r) for r in chore_board_rows(conn, guild_id, limit=_CHORE_FETCH)
                ]
                tasks = [
                    dict(r)
                    for r in pending_todos(
                        conn, guild_id, limit=25, exclude_chores=True
                    )
                ]
            options = [dict(o) for o in completable_options(chores, tasks)]
            # The chore-aware wording only earns its place when chores are the
            # reason there is nothing to offer; with tasks outstanding the list
            # is never empty anyway.
            empty = (
                nothing_to_tick_message(chores)
                if chores
                else "Nothing pending — the list is already clear. ✨"
            )
            return options, empty

        rows, empty_message = await asyncio.to_thread(_load)
        if not rows:
            await interaction.response.send_message(empty_message, ephemeral=True)
            return
        view = discord.ui.View(timeout=180)
        view.add_item(TodoCompleteSelect(rows))
        await interaction.response.send_message(
            "What did you finish?", view=view, ephemeral=True
        )

    @discord.ui.button(
        label="Sign-Offs",
        emoji="✍️",
        style=discord.ButtonStyle.secondary,
        custom_id="todo_board_signoffs",
    )
    async def _signoffs(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """Review the quest claims waiting on a mod.

        Deliberately *not* behind ``_require_mod``: approving pays real
        currency, so the economy's own manager gate applies, and it lives with
        the rest of the sign-off flow in ``economy/quest_views.py`` — this
        button is only the door onto the board.
        """
        if _resolve_cog(interaction) is None:
            await _unavailable(interaction)
            return
        await open_signoff_picker(interaction)

    @discord.ui.button(
        label="Approvals",
        emoji="🧾",
        style=discord.ButtonStyle.secondary,
        custom_id="todo_board_approvals",
    )
    async def _approvals(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """Review the paid requests waiting on a mod — themes, questions, pins.

        Same deal as Sign-Offs: deliberately *not* behind ``_require_mod``,
        because every decision behind it moves currency and the economy's own
        manager gate applies. This button is only the door onto the board.
        """
        if _resolve_cog(interaction) is None:
            await _unavailable(interaction)
            return
        await open_approvals_picker(interaction)

    @discord.ui.button(
        label="Confessions",
        emoji="🕵️",
        style=discord.ButtonStyle.secondary,
        custom_id="todo_board_confessions",
    )
    async def _confessions(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """Review the confessions mod-approve mode is holding.

        The fifth and last button the board can carry — Discord allows five per
        action row, and this fills the row exactly.

        Unlike Sign-Offs and Approvals this one *is* the board's own moderator
        tier, applied inside the picker: no currency moves behind it, so there
        is no narrower economy gate to defer to. The picker gates itself rather
        than relying on this button, because the ephemeral card it opens
        outlives the click.
        """
        if _resolve_cog(interaction) is None:
            await _unavailable(interaction)
            return
        await open_confessions_picker(interaction)


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
            board = get_board(conn, guild_id)
        return board.channel_id, board.message_id

    def _write_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self.bot.ctx.open_db() as conn:
            if channel_id and message_id:
                save_board(conn, guild_id, channel_id, message_id)
            else:
                clear_board(conn, guild_id)

    def _read_rows(self, guild_id: int) -> dict:
        with self.bot.ctx.open_db() as conn:
            # Pending sign-offs are read straight from the claims table rather
            # than mirrored into todos: nothing to keep in sync, and the
            # Complete button can never offer one (a claim is approved, not
            # ticked off). One row past the visible window, same sentinel
            # trick the task list uses, plus the true total for the footer.
            signoffs = [
                dict(r)
                for r in pending_signoff_rows(
                    conn, guild_id, limit=MAX_SIGNOFF_ROWS + 1
                )
            ]
            # Both only matter when something is waiting: a guild with no
            # claims — the normal state, and every guild with the economy off
            # — pays nothing for the section it isn't rendering.
            signoff_total = pending_signoff_count(conn, guild_id) if signoffs else 0
            # The three paid queues, read the same way and for the same
            # reason: a submission is not a todo row, so nothing is mirrored
            # and resolving it anywhere takes it off the board.
            approvals = pending_approvals(
                conn, guild_id, limit=MAX_APPROVAL_ROWS + 1
            )
            approval_total = (
                pending_approval_count(conn, guild_id) if approvals else 0
            )
            # Confessions held by mod-approve mode, read the same way again.
            # A guild with the mode off never has a row here, so the section
            # and its two queries cost nothing until somebody turns it on.
            confessions = pending_confessions(
                conn, guild_id, limit=MAX_CONFESSION_ROWS + 1
            )
            confession_total = (
                pending_confession_count(conn, guild_id) if confessions else 0
            )
            currency_emoji = (
                load_econ_settings(conn, guild_id).currency_emoji
                if (signoffs or approvals)
                else ""
            )
            chores = [
                dict(r) for r in chore_board_rows(conn, guild_id, limit=_CHORE_FETCH)
            ]
            # One screenful plus a sentinel: enough to render and to know the
            # list overflows, without hauling every pending row. Chore-spawned
            # rows are excluded — the chores section above shows them, with
            # more state than a task line can carry.
            rows = [
                dict(r)
                for r in pending_todos(
                    conn, guild_id, limit=MAX_BOARD_ROWS + 1, exclude_chores=True
                )
            ]
            total = pending_count(conn, guild_id, exclude_chores=True)
        return {
            "signoffs": signoffs,
            "signoff_total": signoff_total,
            "approvals": approvals,
            "approval_total": approval_total,
            "confessions": confessions,
            "confession_total": confession_total,
            "currency_emoji": currency_emoji,
            "chores": chores,
            "rows": rows,
            "total": total,
        }

    async def build_panel(self, guild: discord.Guild) -> PanelContent:
        data = await asyncio.to_thread(self._read_rows, guild.id)
        chores, rows = data["chores"], data["rows"]
        signoffs, total = data["signoffs"], data["total"]
        signoff_total = data["signoff_total"]
        approvals, approval_total = data["approvals"], data["approval_total"]
        confessions = data["confessions"]
        confession_total = data["confession_total"]
        for chore in chores:
            chore["completed_by_name"] = self._display_name(
                guild, chore.get("completed_by")
            )
        for row in rows:
            row["buyer_name"] = self._display_name(guild, row.get("buyer_id"))
        for claim in signoffs:
            claim["claimant_name"] = self._display_name(guild, claim.get("user_id"))
        for request in approvals:
            request["requester_name"] = self._display_name(
                guild, request.get("user_id")
            )
        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="todo")
        embed = discord.Embed(
            title="📋 Server Todo",
            description=render_board(
                chores,
                rows,
                signoff_rows=signoffs,
                signoff_total=signoff_total,
                approval_rows=approvals,
                approval_total=approval_total,
                # No name-resolution pass above for these, unlike every other
                # section: pending_confessions returns no author id at all, on
                # purpose. See confessions/approval_views.py.
                confession_rows=confessions,
                confession_total=confession_total,
                currency_emoji=data["currency_emoji"],
                task_total=total,
            ),
            color=accent,
        )
        embed.set_footer(
            text=render_board_footer(
                chores, total, signoff_total, approval_total, confession_total
            )
        )
        return PanelContent(
            embed=embed,
            view=TodoBoardView(),
            signature=board_content_signature(
                chores,
                rows,
                total,
                signoff_rows=signoffs,
                signoff_total=signoff_total,
                approval_rows=approvals,
                approval_total=approval_total,
                confession_rows=confessions,
                confession_total=confession_total,
            ),
        )

    def _display_name(self, guild: discord.Guild, user_id) -> str:
        """Who ticked it, as the mods know them. Never a raw id on the board."""
        if not user_id:
            return ""
        member = guild.get_member(int(user_id))
        return member.display_name if member is not None else "someone"

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

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_board_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """Clear the board's ids if its channel was deleted."""
        await self.board.on_channel_delete(channel)


async def repaint_board(bot, guild_id: int) -> None:
    """Repaint a guild's sticky board after an out-of-band mutation. Never raises.

    The single implementation: the quest sign-off path wraps this rather than
    keeping its own copy, so the two cannot drift into different defensive
    shapes and different log messages for the same failure.

    ``todo_board_loop`` deliberately does **not** poll for changes — it repaints
    only what it just spawned plus failed retries, because "every user-facing
    mutation repaints the board itself" (see its docstring). Automatic chore
    sign-off is such a mutation, and it happens in `events_cog` and in the game
    launch paths, neither of which holds the cog. Without this the QOTD chore
    would sit on the board looking outstanding until the next daily spawn — the
    row correct in the database, the surface a mod actually reads stale for
    most of a day, which to them is indistinguishable from the feature not
    working.
    """
    # ``bot`` may be annotated as the bare Client on some callers (the quest
    # expiry sweep holds one); only a commands.Bot carries cogs, which the
    # runtime one always is.
    get_cog = getattr(bot, "get_cog", None)
    cog = get_cog("TodoCog") if get_cog is not None else None
    refresh = getattr(cog, "refresh_board", None)
    if refresh is None:
        return
    try:
        await refresh(int(guild_id))
    except Exception:
        log.exception("todo board repaint failed for %s", guild_id)


def _tick(ctx) -> tuple[set[int], set[int]]:
    """One scheduler pass: spawn what is due, report what needs repainting.

    Returns ``(guilds_with_a_board, guilds_that_changed)``. Runs in a worker
    thread — all of it is blocking sqlite.

    "Changed" covers a **reset that only wrote a row off**. A day where the
    chore was not done spawns a fresh row *and* marks the old one missed, and
    even in the edge case where the spawn is what fails, the write-off alone
    has moved the board — so keying the repaint on "did a row spawn" would
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
    changed = {
        r.guild_id
        for r in spawned
        if r.todo_id is not None or r.missed_todo_id is not None
    }
    return boards, changed


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
    # Repaint every board once on boot. Normally the loop only touches guilds
    # where something spawned, but a board posted by a previous release can be
    # carrying a view this one no longer registers — after the 180 merge the
    # surviving message is the old chore board's, whose ✅ Mark Done button now
    # answers "This interaction failed" until something repaints it. An edit
    # replaces the view along with the content, so one pass on boot closes that
    # window instead of waiting for the next chat in the channel. It also heals
    # any board that drifted while the bot was down.
    first_pass = True
    while not bot.is_closed():
        try:
            boards, changed = await asyncio.to_thread(_tick, ctx)
            cog = cast("TodoCog | None", bot.get_cog("TodoCog"))
            if cog is not None:
                # Publish the board set so the on_message listener can reject
                # boardless guilds without touching the DB.
                cog.board.set_known_guilds(boards)
                # Repaint sequentially: these are edits against N different
                # channels and a 60s cadence gives no reason to burst them.
                # take_retries() is drained exactly once — leaving it undrained
                # is how pen pals lost its failed edits (F5 in
                # docs/reviews/2026-08-06-sticky-panel-machinery.md).
                work = (changed | cog.board.take_retries()) & boards
                if first_pass:
                    work |= boards
                    first_pass = False
                for guild_id in sorted(work):
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
    await bot.add_cog(TodoCog(bot))
