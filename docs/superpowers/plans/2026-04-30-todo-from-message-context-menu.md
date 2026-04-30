# Todo From Message — Context Menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Discord message context menu ("Apps → Add to Todo") that lets moderators turn any message into a todo with optional notes, persisting the message author/channel, content snapshot, and jump URL.

**Architecture:** Additive SQLite migration adds two nullable columns (`description`, `source_message_url`) to the existing `todos` table. The existing `services/todo_service.create_todo` is extended with optional kwargs so both old callers (slash command + web POST) and the new context-menu callback can use it. The cog registers a `discord.app_commands.ContextMenu` of type `message`, gates it with the project's `_is_mod` check, and pops a `discord.ui.Modal` with one paragraph TextInput for notes. The web `GET /todos` route is widened to return the new columns; no new POST endpoint is added in this iteration.

**Tech Stack:** Python 3, discord.py (`app_commands`, `ui.Modal`, `ui.TextInput`), SQLite (via the project's `migrations/` framework and `db_utils.open_db`), FastAPI (web routes), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-04-30-todo-from-message-context-menu-design.md`

---

## File Structure

**Create:**
- `migrations/007_todo_message_source.sql` — additive SQLite migration adding `description` and `source_message_url` columns to `todos`. Auto-discovered by `migrations/__init__.py` (no code change needed there).
- `tests/cogs/test_todo_cog.py` — callback/modal tests for the new context menu. **Important:** there must be **no `__init__.py`** in `tests/cogs/` — adding one breaks pytest because the directory name collides with the top-level `cogs/` package.

**Modify:**
- `services/todo_service.py` — extend `create_todo` with `description` and `source_message_url` keyword-only args. Existing callers keep working unchanged.
- `cogs/todo_cog.py` — add module-level helpers (`_format_task_label`, `_format_description`), register a message context menu in `cog_load`, define a `_TodoFromMessageModal`, and clean up in `cog_unload`. Mirrors the pattern in `cogs/jail_cog.py:182-219`.
- `web/routes/todo.py` — widen the `SELECT` and response dict in `list_todos` to include the new columns.
- `tests/test_todo_service.py` — add cases covering the new optional params (persistence + default-NULL regression).

**No changes:**
- `migrations/__init__.py` — discovers files via glob, picks up the new file automatically.
- `cogs/todo_cog.py`'s existing `/todo` slash command — it stays open to all and unchanged.
- `web/routes/todo.py`'s existing POST endpoint — unchanged.
- Dashboard UI — out of scope; will be wired later once the feature is exercised.

---

## Task 1: Migration — add `description` and `source_message_url` columns

**Files:**
- Create: `migrations/007_todo_message_source.sql`

- [ ] **Step 1: Write the migration SQL**

Create `migrations/007_todo_message_source.sql` with exactly:

```sql
ALTER TABLE todos ADD COLUMN description TEXT;
ALTER TABLE todos ADD COLUMN source_message_url TEXT;
```

Both columns are nullable; no backfill is needed.

- [ ] **Step 2: Verify the migration applies cleanly via the existing test fixture**

The `db` fixture in `tests/test_todo_service.py` calls `apply_migrations_sync` against a fresh tmp DB. Run:

```bash
pytest tests/test_todo_service.py -v
```

Expected: all existing tests still PASS (the migration is purely additive; the `service.create_todo` insert column list still works because the new columns are nullable).

- [ ] **Step 3: Verify the columns exist after migration**

Add a temporary one-shot check (do NOT keep this — it's a sanity check before continuing):

```bash
python -c "import sqlite3, tempfile, pathlib; from migrations import apply_migrations_sync; p = pathlib.Path(tempfile.mkdtemp()) / 't.db'; apply_migrations_sync(p); c = sqlite3.connect(str(p)); print([r[1] for r in c.execute('PRAGMA table_info(todos)')])"
```

Expected output includes: `'description'` and `'source_message_url'` alongside the existing columns.

- [ ] **Step 4: Commit**

```bash
git add migrations/007_todo_message_source.sql
git commit -m "feat(todos): add description + source_message_url columns"
```

---

## Task 2: Extend `create_todo` with optional `description` + `source_message_url`

**Files:**
- Modify: `services/todo_service.py`
- Test: `tests/test_todo_service.py`

- [ ] **Step 1: Write the failing tests**

Append the following tests to `tests/test_todo_service.py` (just before the `# ── list_todos` divider so they sit alongside the other create tests, but the exact placement is not load-bearing — the bottom of the file is fine too):

```python
# ── create_todo with new optional fields ─────────────────────────────


def test_create_with_description_and_source_url(db):
    with open_db(db) as conn:
        todo_id = create_todo(
            conn,
            GUILD,
            USER,
            "Message from @alice in #general",
            description="hello world\n\nfollow up next week",
            source_message_url="https://discord.com/channels/1/2/3",
        )
        row = conn.execute(
            "SELECT description, source_message_url FROM todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    assert row["description"] == "hello world\n\nfollow up next week"
    assert row["source_message_url"] == "https://discord.com/channels/1/2/3"


def test_create_without_new_fields_leaves_them_null(db):
    with open_db(db) as conn:
        todo_id = create_todo(conn, GUILD, USER, "Plain task")
        row = conn.execute(
            "SELECT description, source_message_url FROM todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
    assert row["description"] is None
    assert row["source_message_url"] is None
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
pytest tests/test_todo_service.py::test_create_with_description_and_source_url tests/test_todo_service.py::test_create_without_new_fields_leaves_them_null -v
```

Expected: `test_create_with_description_and_source_url` FAILS with a `TypeError` about unexpected keyword arguments `description` / `source_message_url`. The second test (`..._leaves_them_null`) will also FAIL because the `Row` lookup raises `IndexError: No item with that key`, since the columns aren't being read by the current SELECT — wait, columns DO exist (Task 1 added them). The `..._leaves_them_null` test should actually PASS already once Task 1 is in place. That's fine; the first test is the one driving this task.

- [ ] **Step 3: Extend `create_todo`**

Replace the current `create_todo` in `services/todo_service.py` with:

```python
def create_todo(
    conn: sqlite3.Connection,
    guild_id: int,
    added_by: int,
    task: str,
    *,
    description: str | None = None,
    source_message_url: str | None = None,
) -> int:
    """Insert a new to do and return its ID."""
    cur = conn.execute(
        "INSERT INTO todos (guild_id, added_by, task, description, source_message_url, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, added_by, task, description, source_message_url, time.time()),
    )
    return cur.lastrowid  # type: ignore[return-value]
```

The `*` makes the new params keyword-only so existing positional calls (e.g. `create_todo(conn, GUILD, USER, "task")`) keep working untouched.

- [ ] **Step 4: Run the full todo-service test suite**

```bash
pytest tests/test_todo_service.py -v
```

Expected: all tests PASS — both the new ones and the existing ones (the existing tests pass `task` positionally and don't supply the new kwargs).

- [ ] **Step 5: Commit**

```bash
git add services/todo_service.py tests/test_todo_service.py
git commit -m "feat(todos): create_todo accepts description and source_message_url"
```

---

## Task 3: Add `description` + `source_message_url` to `GET /todos`

**Files:**
- Modify: `web/routes/todo.py`
- Test: (no new test — `tests/test_web_routes.py` covers this if it exercises `/todos`; if it does not, no new test is required for this trivial widening. See Step 1.)

- [ ] **Step 1: Decide whether to add a web test**

Run:

```bash
grep -nE "todos|/todos" tests/test_web_routes.py tests/web/*.py
```

If a test already hits `/todos`, extend it to assert the new keys. If not, do NOT add one in this task — the read widening is trivial and is exercised end-to-end via the cog tests + manual smoke. Skip to Step 2.

- [ ] **Step 2: Modify the SELECT and response in `web/routes/todo.py`**

In `web/routes/todo.py`, find this block (currently around lines 47-65):

```python
        with ctx.open_db() as conn:
            rows = conn.execute(
                f"SELECT id, added_by, task, created_at, completed_at, completed_by"
                f" FROM todos WHERE {where} ORDER BY created_at DESC LIMIT 200",
                params,
            ).fetchall()

        todos = [
            {
                "id": r["id"],
                "added_by": str(r["added_by"]),
                "added_by_name": "",
                "task": r["task"],
                "created_at": r["created_at"],
                "completed_at": r["completed_at"],
                "completed_by": str(r["completed_by"]) if r["completed_by"] else None,
                "completed_by_name": "",
            }
            for r in rows
        ]
```

Replace with:

```python
        with ctx.open_db() as conn:
            rows = conn.execute(
                f"SELECT id, added_by, task, description, source_message_url,"
                f" created_at, completed_at, completed_by"
                f" FROM todos WHERE {where} ORDER BY created_at DESC LIMIT 200",
                params,
            ).fetchall()

        todos = [
            {
                "id": r["id"],
                "added_by": str(r["added_by"]),
                "added_by_name": "",
                "task": r["task"],
                "description": r["description"],
                "source_message_url": r["source_message_url"],
                "created_at": r["created_at"],
                "completed_at": r["completed_at"],
                "completed_by": str(r["completed_by"]) if r["completed_by"] else None,
                "completed_by_name": "",
            }
            for r in rows
        ]
```

- [ ] **Step 3: Run web tests and confirm nothing regressed**

```bash
pytest tests/test_web_routes.py tests/web/ -v
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add web/routes/todo.py
git commit -m "feat(todos): expose description and source_message_url in GET /todos"
```

---

## Task 4: Add label/description helpers to `cogs/todo_cog.py`

These are pure functions with no Discord dependencies, so they are testable directly. Adding them as a separate task means Task 5 (the modal/context menu) can call already-tested helpers.

**Files:**
- Modify: `cogs/todo_cog.py`
- Create: `tests/cogs/test_todo_cog.py`

- [ ] **Step 1: Confirm `tests/cogs/__init__.py` does NOT exist**

Run:

```bash
ls tests/cogs/__init__.py 2>&1
```

Expected: `ls: cannot access 'tests/cogs/__init__.py': No such file or directory`. If it exists, **delete it** — having one breaks pytest because `cogs/` is also a top-level package.

- [ ] **Step 2: Write the failing helper tests**

Create `tests/cogs/test_todo_cog.py`:

```python
"""Tests for cogs.todo_cog helpers and the context-menu modal."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cogs.todo_cog import _format_description, _format_task_label, _MAX_CONTENT_LEN  # noqa: E402


# ── _format_task_label ────────────────────────────────────────────────


def test_label_uses_display_name_and_channel_name():
    assert (
        _format_task_label(author_display="Alice", channel_name="general")
        == "Message from @Alice in #general"
    )


# ── _format_description ───────────────────────────────────────────────


def test_description_message_only():
    assert _format_description(message_content="hello world", notes="") == "hello world"


def test_description_notes_only_uses_no_text_marker():
    assert (
        _format_description(message_content="", notes="follow up next week")
        == "[no text content]\n\nfollow up next week"
    )


def test_description_both_joined_with_blank_line():
    assert (
        _format_description(message_content="hello", notes="follow up")
        == "hello\n\nfollow up"
    )


def test_description_neither_uses_no_text_marker():
    assert _format_description(message_content="", notes="") == "[no text content]"


def test_description_truncates_long_content():
    long = "x" * (_MAX_CONTENT_LEN + 50)
    out = _format_description(message_content=long, notes="")
    assert out.endswith("…")
    assert len(out) == _MAX_CONTENT_LEN + 1  # MAX chars + the ellipsis


def test_description_truncates_long_content_with_notes():
    long = "x" * (_MAX_CONTENT_LEN + 50)
    out = _format_description(message_content=long, notes="note")
    head, _, tail = out.partition("\n\n")
    assert head.endswith("…")
    assert len(head) == _MAX_CONTENT_LEN + 1
    assert tail == "note"
```

- [ ] **Step 3: Run the helper tests to verify they fail**

```bash
pytest tests/cogs/test_todo_cog.py -v
```

Expected: ALL FAIL with `ImportError: cannot import name '_format_description' from 'cogs.todo_cog'` (and the same for `_format_task_label`, `_MAX_CONTENT_LEN`).

- [ ] **Step 4: Add the helpers to `cogs/todo_cog.py`**

Open `cogs/todo_cog.py`. Just below the imports (above the `class TodoCog` line), add:

```python
_MAX_CONTENT_LEN = 1500
_NO_TEXT_MARKER = "[no text content]"


def _format_task_label(*, author_display: str, channel_name: str) -> str:
    """Build the headline shown in the todo list for a message-derived todo."""
    return f"Message from @{author_display} in #{channel_name}"


def _format_description(*, message_content: str, notes: str) -> str:
    """Build the description column: message content (truncated) then notes below.

    Either part may be empty. If the message has no text, '[no text content]' is
    used so the source link still has framing.
    """
    head = message_content
    if len(head) > _MAX_CONTENT_LEN:
        head = head[:_MAX_CONTENT_LEN] + "…"
    if not head:
        head = _NO_TEXT_MARKER
    notes = notes.strip() if notes else ""
    if not notes:
        return head
    return f"{head}\n\n{notes}"
```

- [ ] **Step 5: Run the helper tests to verify they pass**

```bash
pytest tests/cogs/test_todo_cog.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add cogs/todo_cog.py tests/cogs/test_todo_cog.py
git commit -m "feat(todos): add task-label and description formatting helpers"
```

---

## Task 5: Register the message context menu and modal

**Files:**
- Modify: `cogs/todo_cog.py`
- Test: `tests/cogs/test_todo_cog.py`

- [ ] **Step 1: Append failing modal-submission tests**

Append the following to `tests/cogs/test_todo_cog.py`:

```python
# ── _TodoFromMessageModal.on_submit ──────────────────────────────────

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

from tests.fakes import FakeChannel, FakeGuild, FakeRole, FakeUser, fake_interaction  # noqa: E402


def _build_message(content: str, jump_url: str = "https://discord.com/channels/1/2/3"):
    msg = MagicMock()
    msg.content = content
    msg.jump_url = jump_url
    msg.author = MagicMock()
    msg.author.display_name = "Alice"
    msg.channel = MagicMock()
    msg.channel.name = "general"
    return msg


def _build_mod_ctx():
    """A MagicMock AppContext where _is_mod will return True for our user."""
    ctx = MagicMock()
    ctx.mod_role_ids = {5001}
    ctx.admin_role_ids = set()
    return ctx


def _build_mod_user():
    role = FakeRole(id=5001, name="Mod")
    user = FakeUser(id=2001, name="mod_user", roles=[role])
    user.guild_permissions = MagicMock(manage_guild=False, administrator=False)
    return user


@pytest.mark.asyncio
async def test_modal_submit_calls_create_todo_with_formatted_args(monkeypatch):
    from cogs.todo_cog import _TodoFromMessageModal

    captured = {}

    def fake_create_todo(conn, guild_id, added_by, task, **kwargs):
        captured["guild_id"] = guild_id
        captured["added_by"] = added_by
        captured["task"] = task
        captured.update(kwargs)
        return 42

    monkeypatch.setattr("cogs.todo_cog.create_todo", fake_create_todo)

    ctx = _build_mod_ctx()
    ctx.open_db.return_value.__enter__ = lambda s: MagicMock()
    ctx.open_db.return_value.__exit__ = lambda s, a, b, c: False

    msg = _build_message("the original message text")
    modal = _TodoFromMessageModal(message=msg, ctx=ctx)
    modal.notes._value = "follow up tuesday"  # set the TextInput value directly

    user = _build_mod_user()
    guild = FakeGuild(id=9001)
    interaction = fake_interaction(user=user, guild=guild)

    await modal.on_submit(interaction)

    assert captured["guild_id"] == 9001
    assert captured["added_by"] == 2001
    assert captured["task"] == "Message from @Alice in #general"
    assert captured["description"] == "the original message text\n\nfollow up tuesday"
    assert captured["source_message_url"] == "https://discord.com/channels/1/2/3"
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "42" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_modal_submit_blank_notes_only_stores_message(monkeypatch):
    from cogs.todo_cog import _TodoFromMessageModal

    captured = {}

    def fake_create_todo(conn, guild_id, added_by, task, **kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr("cogs.todo_cog.create_todo", fake_create_todo)

    ctx = _build_mod_ctx()
    ctx.open_db.return_value.__enter__ = lambda s: MagicMock()
    ctx.open_db.return_value.__exit__ = lambda s, a, b, c: False

    modal = _TodoFromMessageModal(message=_build_message("just the message"), ctx=ctx)
    modal.notes._value = ""

    interaction = fake_interaction(user=_build_mod_user(), guild=FakeGuild(id=9001))
    await modal.on_submit(interaction)

    assert captured["description"] == "just the message"


@pytest.mark.asyncio
async def test_context_menu_callback_blocks_non_mods(monkeypatch):
    """Non-mods invoking the context menu get an ephemeral 'Mod only.' and no modal."""
    from cogs.todo_cog import TodoCog

    bot = MagicMock()
    bot.tree = MagicMock()
    ctx = _build_mod_ctx()
    cog = TodoCog.__new__(TodoCog)
    cog.bot = bot
    cog.ctx = ctx

    # Resolve the registered callback by exercising cog_load to register it,
    # then pull it out of bot.tree.add_command's call args.
    await cog.cog_load()
    add_command_calls = bot.tree.add_command.call_args_list
    # The context menu is one of the add_command arguments
    ctx_menu = next(
        c.args[0] for c in add_command_calls
        if getattr(c.args[0], "name", None) == "Add to Todo"
    )

    # Build a non-mod user and interaction
    non_mod = FakeUser(id=3001, name="regular", roles=[])
    non_mod.guild_permissions = MagicMock(manage_guild=False, administrator=False)
    interaction = fake_interaction(user=non_mod, guild=FakeGuild(id=9001))

    await ctx_menu.callback(interaction, _build_message("hi"))

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "Mod only" in args[0]
    assert kwargs.get("ephemeral") is True
    interaction.response.send_modal.assert_not_called()
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
pytest tests/cogs/test_todo_cog.py -v
```

Expected: the three new tests FAIL with `ImportError: cannot import name '_TodoFromMessageModal'` and the context-menu test FAILS because `cog_load` isn't registering anything yet.

- [ ] **Step 3: Implement the modal and context menu in `cogs/todo_cog.py`**

Replace the entire current contents of `cogs/todo_cog.py` with:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from commands.jail_commands import _is_mod
from services.todo_service import create_todo

if TYPE_CHECKING:
    from app_context import AppContext, Bot


_MAX_CONTENT_LEN = 1500
_NO_TEXT_MARKER = "[no text content]"
_NOTES_MAX_LEN = 1000
_TASK_MAX_LEN = 500


def _format_task_label(*, author_display: str, channel_name: str) -> str:
    """Build the headline shown in the todo list for a message-derived todo."""
    return f"Message from @{author_display} in #{channel_name}"


def _format_description(*, message_content: str, notes: str) -> str:
    """Build the description column: message content (truncated) then notes below.

    Either part may be empty. If the message has no text, '[no text content]' is
    used so the source link still has framing.
    """
    head = message_content
    if len(head) > _MAX_CONTENT_LEN:
        head = head[:_MAX_CONTENT_LEN] + "…"
    if not head:
        head = _NO_TEXT_MARKER
    notes = notes.strip() if notes else ""
    if not notes:
        return head
    return f"{head}\n\n{notes}"


class _TodoFromMessageModal(discord.ui.Modal, title="Add to Todo"):
    notes: discord.ui.TextInput = discord.ui.TextInput(
        label="Notes",
        style=discord.TextStyle.paragraph,
        max_length=_NOTES_MAX_LEN,
        required=False,
        placeholder="Optional context for this todo",
    )

    def __init__(self, *, message: discord.Message, ctx: AppContext) -> None:
        super().__init__()
        self._message = message
        self._ctx = ctx

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        author_display = getattr(self._message.author, "display_name", "unknown")
        channel_name = getattr(self._message.channel, "name", "unknown")
        task = _format_task_label(author_display=author_display, channel_name=channel_name)
        if len(task) > _TASK_MAX_LEN:
            task = task[: _TASK_MAX_LEN - 1] + "…"

        description = _format_description(
            message_content=self._message.content or "",
            notes=str(self.notes.value or ""),
        )

        with self._ctx.open_db() as conn:
            todo_id = create_todo(
                conn,
                interaction.guild.id,
                interaction.user.id,
                task,
                description=description,
                source_message_url=self._message.jump_url,
            )

        await interaction.response.send_message(
            f"Todo #{todo_id} added.", ephemeral=True
        )


class TodoCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        ctx = self.ctx

        async def _add_to_todo_ctx(
            interaction: discord.Interaction, message: discord.Message
        ) -> None:
            member = interaction.user
            if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
                await interaction.response.send_message("Mod only.", ephemeral=True)
                return
            await interaction.response.send_modal(
                _TodoFromMessageModal(message=message, ctx=ctx)
            )

        ctx_menu = app_commands.ContextMenu(
            name="Add to Todo", callback=_add_to_todo_ctx
        )
        ctx_menu.default_permissions = discord.Permissions(manage_messages=True)
        self.bot.tree.add_command(ctx_menu)
        self._add_to_todo_context_menu = ctx_menu

    async def cog_unload(self) -> None:
        if hasattr(self, "_add_to_todo_context_menu"):
            self.bot.tree.remove_command(
                "Add to Todo", type=discord.AppCommandType.message
            )

    @app_commands.command(name="todo", description="Add a task to the server todo list.")
    @app_commands.describe(task="The task to add.")
    async def todo(self, interaction: discord.Interaction, task: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        task = task.strip()
        if not task:
            await interaction.response.send_message("Task cannot be empty.", ephemeral=True)
            return
        if len(task) > _TASK_MAX_LEN:
            await interaction.response.send_message(
                "Task must be 500 characters or fewer.", ephemeral=True
            )
            return
        with self.ctx.open_db() as conn:
            todo_id = create_todo(conn, interaction.guild.id, interaction.user.id, task)
        await interaction.response.send_message(
            f"Todo #{todo_id} added: {task}", ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(TodoCog(bot, bot.ctx))
```

Notes for the engineer:
- We import `_is_mod` from `commands.jail_commands` because that's where the project's mod helper lives. The same helper is used by `cogs/jail_cog.py`.
- `discord.Permissions(manage_messages=True)` on the context menu sets the **default** Discord-side permission gate so non-mods don't even see the option in the UI. The in-callback `_is_mod` check is the authoritative gate (defense in depth — admins can rebind the default).
- `cog_load` is the right place to register: it runs once per cog load, both at startup and on reload. `cog_unload` cleans up so reloads don't leave stale registrations.
- The modal's `TextInput` is declared as a class attribute (per discord.py 2.x convention). The instance reads it via `self.notes.value`.

- [ ] **Step 4: Run the cog tests**

```bash
pytest tests/cogs/test_todo_cog.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Run the full project test suite to confirm no regressions**

```bash
pytest -x
```

Expected: all tests PASS. If anything else fails, do NOT skip — investigate. Likely culprits:
- Existing tests that import `cogs.todo_cog` may have new import-time side effects. There aren't any in the new file, but verify.
- A web test that asserts the exact response shape of `/todos` may need the new keys added to its fixture.

- [ ] **Step 6: Commit**

```bash
git add cogs/todo_cog.py tests/cogs/test_todo_cog.py
git commit -m "feat(todos): right-click message context menu with notes modal"
```

---

## Task 6: Manual smoke check (no commit)

This task verifies the wiring against a real Discord client. It's not automated.

**Files:** none (manual)

- [ ] **Step 1: Apply the migration to your dev DB**

If you use `dk_dev.db`, the bot applies migrations on startup automatically. Just start the bot:

```bash
python dungeonkeeper.py
```

Watch the log for:
```
applied migration 007_todo_message_source.sql
```

- [ ] **Step 2: Verify the context menu appears**

In a Discord client where the bot is installed and slash commands are synced:
1. Right-click any message in a server channel.
2. Hover **Apps**.
3. As a moderator, you should see **Add to Todo**. As a non-mod, the entry should be hidden (Discord respects `default_permissions`).

If syncing global commands is required, run whatever command the project uses to sync the tree (often a one-shot `/sync` or restart). Check `dungeonkeeper.py` for how the tree is synced on startup.

- [ ] **Step 3: Run the flow end-to-end**

1. Click **Add to Todo** on a message with text content.
2. Add a note in the modal and submit.
3. Confirm the ephemeral `"Todo #N added."` reply appears.
4. Hit `GET /api/todos` (or whatever path the dashboard uses) and confirm the new entry has:
   - `task`: `"Message from @<author> in #<channel>"`
   - `description`: message content + your note
   - `source_message_url`: a clickable Discord jump URL

- [ ] **Step 4: Edge-case smoke**

- Submit with the notes field blank — description should be only the message text.
- Right-click an embed-only / image-only message — description should start with `[no text content]`.

- [ ] **Step 5: As a non-mod (or with permissions revoked), verify the menu is hidden / blocked**

If you can sign in as a non-mod, confirm the option doesn't appear. If you bypass it (e.g. via the API), the in-callback check should still reject with `"Mod only."`.

If smoke passes, you're done. If anything looks off, file the gap and decide whether to fix in this branch or in a follow-up.

---

## Self-Review Notes

**Spec coverage:**
- ✅ Mod-gated message context menu — Task 5
- ✅ Modal with optional Notes field — Task 5
- ✅ Schema columns `description`, `source_message_url` — Task 1
- ✅ `task` formatted as `"Message from @<display> in #<channel>"` — Task 4 + 5
- ✅ Description formatting (message + notes, `[no text content]` marker, truncation at 1500 chars) — Task 4
- ✅ Service layer accepts the new params — Task 2
- ✅ Web read-side returns the new fields — Task 3
- ✅ Tests for service + cog — Tasks 2, 4, 5
- ✅ Cog cleanup on unload — Task 5
- ✅ DM-context guard — Task 5

**Out-of-scope items deliberately not implemented (per spec):** dashboard UI rendering, web POST mirror, edit-after-creation.

**Type/name consistency:** `_format_task_label`, `_format_description`, `_TodoFromMessageModal`, `_MAX_CONTENT_LEN` are referenced consistently across Tasks 4 and 5. The `notes` attribute is read via `self.notes.value` (instance) but tests poke `self.notes._value` (the underlying TextInput private), which is the standard pytest pattern for `discord.ui.TextInput` since it has no public setter.
