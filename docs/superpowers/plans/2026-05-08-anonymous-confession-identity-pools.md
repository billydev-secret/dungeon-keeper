# Anonymous Confession Identity Pools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-thread shuffled identity pools to anonymous confessions so no two users share a name or color until all are exhausted, plus an ephemeral "Reply as Someone New" button and a "What's this?" help button.

**Architecture:** A new `confession_pools` table stores one shuffled remaining-index list per `(thread, pool_type)`; `pop_pool_index` pops from it, reshuffling when empty. Two new service functions replace the old hash- and count-based identity assignment: `get_or_assign_anon_identity` (persistent) and `get_ephemeral_anon_identity` (one-shot, no DB write to `confession_emoji_assignments`). The cog gains two new button `custom_id` prefixes (`crn|`, `crh|`) alongside the existing `cr|`.

**Tech Stack:** Python 3.10, discord.py, sqlite3 via `open_db`, pytest, `sync_db_path` fixture

---

## File Map

- **Create:** `migrations/010_confession_pools.sql` — adds `confession_pools` table and `name_index` column
- **Modify:** `services/confessions_service.py` — add `import random`, pool constants/helpers, new identity API, simplified `build_anon_reply`, `_create_tables` update, legacy backfill
- **Modify:** `cogs/confessions_cog.py` — three-button view, `crn|`/`crh|` routing, `ephemeral` flag on `ReplyModal`
- **Create:** `tests/test_confessions_service.py` — service-level tests for all new functions

---

### Task 1: Schema migration

**Files:**
- Create: `migrations/010_confession_pools.sql`
- Modify: `services/confessions_service.py`

- [ ] **Step 1: Create the migration file**

Create `migrations/010_confession_pools.sql`:

```sql
-- Migration 010: anonymous confession identity pools

ALTER TABLE confession_emoji_assignments ADD COLUMN name_index INTEGER NOT NULL DEFAULT -1;

CREATE TABLE IF NOT EXISTS confession_pools (
    guild_id        INTEGER NOT NULL,
    root_message_id INTEGER NOT NULL,
    pool_type       TEXT NOT NULL,
    remaining_json  TEXT NOT NULL DEFAULT '[]',
    cycle           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, root_message_id, pool_type)
);
```

- [ ] **Step 2: Add `confession_pools` to `_create_tables` in `services/confessions_service.py`**

In `_create_tables` (line 137), after the `confession_emoji_assignments` `conn.execute` block (after line 191), add:

```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS confession_pools (
            guild_id        INTEGER NOT NULL,
            root_message_id INTEGER NOT NULL,
            pool_type       TEXT NOT NULL,
            remaining_json  TEXT NOT NULL DEFAULT '[]',
            cycle           INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, root_message_id, pool_type)
        )
    """)
```

- [ ] **Step 3: Verify migration applies cleanly**

Run: `pytest tests/ -x -q --tb=short 2>&1 | head -40`

Expected: all existing tests PASS.

Also verify: `python -c "from migrations import apply_migrations_sync; from pathlib import Path; apply_migrations_sync(Path('dk_dev.db'))"`

Expected: no traceback.

- [ ] **Step 4: Commit**

```
git add migrations/010_confession_pools.sql services/confessions_service.py
git commit -m "feat(confessions): add confession_pools table and name_index column (migration 010)"
```

---

### Task 2: Pool helpers and pure index converters

**Files:**
- Modify: `services/confessions_service.py`
- Create: `tests/test_confessions_service.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_confessions_service.py`:

```python
"""Tests for confession service identity pool helpers."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from services.confessions_service import (
    _ANON_ADJECTIVES,
    _ANON_ANIMALS,
    _ANON_CIRCLES,
    _COLOR_POOL_SIZE,
    _NAME_POOL_SIZE,
    anon_circle_from_index,
    anon_name_from_index,
    pop_pool_index,
)


def test_anon_name_from_index_first():
    assert anon_name_from_index(0) == f"{_ANON_ADJECTIVES[0]} {_ANON_ANIMALS[0]}"


def test_anon_name_from_index_last():
    last = len(_ANON_ADJECTIVES) * len(_ANON_ANIMALS) - 1
    assert anon_name_from_index(last) == f"{_ANON_ADJECTIVES[-1]} {_ANON_ANIMALS[-1]}"


def test_anon_name_from_index_second_row():
    n_animals = len(_ANON_ANIMALS)
    idx = n_animals + 3  # second adjective, fourth animal
    assert anon_name_from_index(idx) == f"{_ANON_ADJECTIVES[1]} {_ANON_ANIMALS[3]}"


def test_anon_circle_from_index_all():
    for i, circle in enumerate(_ANON_CIRCLES):
        assert anon_circle_from_index(i) == circle


def test_pop_pool_index_no_repeats_color(sync_db_path: Path):
    """Color pool yields all unique indices before repeating."""
    pool_size = _COLOR_POOL_SIZE
    seen: set[int] = set()
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for _ in range(pool_size):
            idx = pop_pool_index(conn, guild_id=1, root_message_id=100, pool_type="color", pool_size=pool_size)
            assert idx not in seen, f"Duplicate index {idx} before pool exhausted"
            seen.add(idx)
    assert seen == set(range(pool_size))


def test_pop_pool_index_refills_after_exhaustion(sync_db_path: Path):
    """After pool is exhausted the next call returns a valid index."""
    pool_size = _COLOR_POOL_SIZE
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for _ in range(pool_size):
            pop_pool_index(conn, guild_id=1, root_message_id=200, pool_type="color", pool_size=pool_size)
        extra = pop_pool_index(conn, guild_id=1, root_message_id=200, pool_type="color", pool_size=pool_size)
    assert 0 <= extra < pool_size


def test_pop_pool_index_increments_cycle(sync_db_path: Path):
    """cycle column increments when pool refills."""
    pool_size = _COLOR_POOL_SIZE
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for _ in range(pool_size + 1):
            pop_pool_index(conn, guild_id=1, root_message_id=300, pool_type="color", pool_size=pool_size)
        row = conn.execute(
            "SELECT cycle FROM confession_pools WHERE guild_id=1 AND root_message_id=300 AND pool_type='color'"
        ).fetchone()
    assert row["cycle"] == 1


def test_pop_pool_index_separate_threads_independent(sync_db_path: Path):
    """Pools for different root_message_ids are fully independent."""
    pool_size = _COLOR_POOL_SIZE
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        idx_a = pop_pool_index(conn, guild_id=1, root_message_id=400, pool_type="color", pool_size=pool_size)
        idx_b = pop_pool_index(conn, guild_id=1, root_message_id=500, pool_type="color", pool_size=pool_size)
    assert 0 <= idx_a < pool_size
    assert 0 <= idx_b < pool_size
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `pytest tests/test_confessions_service.py -v`

Expected: ImportError (`_COLOR_POOL_SIZE`, `anon_name_from_index`, etc. not yet defined).

- [ ] **Step 3: Add `import random` to `services/confessions_service.py`**

After `import time` (line 8), add:

```python
import random
```

- [ ] **Step 4: Add pool constants after `_OP_CIRCLE` (line 72)**

```python
_NAME_POOL_SIZE = len(_ANON_ADJECTIVES) * len(_ANON_ANIMALS)
_COLOR_POOL_SIZE = len(_ANON_CIRCLES)
```

- [ ] **Step 5: Add `anon_name_from_index` and `anon_circle_from_index` after the existing `anon_circle` function (after line 84)**

```python
def anon_name_from_index(name_index: int) -> str:
    return f"{_ANON_ADJECTIVES[name_index // len(_ANON_ANIMALS)]} {_ANON_ANIMALS[name_index % len(_ANON_ANIMALS)]}"


def anon_circle_from_index(emoji_index: int) -> str:
    return _ANON_CIRCLES[emoji_index]
```

- [ ] **Step 6: Add `pop_pool_index` after `anon_circle_from_index`**

```python
def pop_pool_index(
    conn: sqlite3.Connection,
    guild_id: int,
    root_message_id: int,
    pool_type: str,
    pool_size: int,
) -> int:
    """Pop the next index from a per-thread shuffled pool, reshuffling when exhausted."""
    row = conn.execute(
        "SELECT remaining_json, cycle FROM confession_pools "
        "WHERE guild_id = ? AND root_message_id = ? AND pool_type = ?",
        (guild_id, root_message_id, pool_type),
    ).fetchone()
    cycle = row["cycle"] if row else 0
    remaining: list[int] = json.loads(row["remaining_json"]) if row else []
    if not remaining:
        remaining = list(range(pool_size))
        random.shuffle(remaining)
        if row:
            cycle += 1
    idx = remaining.pop()
    conn.execute("""
        INSERT INTO confession_pools (guild_id, root_message_id, pool_type, remaining_json, cycle)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, root_message_id, pool_type) DO UPDATE SET
            remaining_json = excluded.remaining_json,
            cycle = excluded.cycle
    """, (guild_id, root_message_id, pool_type, json.dumps(remaining), cycle))
    return idx
```

- [ ] **Step 7: Run tests and confirm they pass**

Run: `pytest tests/test_confessions_service.py -v`

Expected: all 9 tests PASS.

- [ ] **Step 8: Commit**

```
git add services/confessions_service.py tests/test_confessions_service.py
git commit -m "feat(confessions): add pool helpers — anon_name_from_index, anon_circle_from_index, pop_pool_index"
```

---

### Task 3: Identity APIs

**Files:**
- Modify: `services/confessions_service.py`
- Modify: `tests/test_confessions_service.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_confessions_service.py`:

```python
from services.confessions_service import (
    get_ephemeral_anon_identity,
    get_or_assign_anon_identity,
)


def test_persistent_identity_stable(sync_db_path: Path):
    """Same user same thread always returns same (name_idx, emoji_idx)."""
    a = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1000, user_id=42)
    b = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1000, user_id=42)
    assert a == b


def test_persistent_identity_unique_per_user(sync_db_path: Path):
    """Two different users in the same thread get different name and color indices."""
    a = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1001, user_id=10)
    b = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1001, user_id=11)
    assert a[0] != b[0], "name_index should differ"
    assert a[1] != b[1], "emoji_index should differ"


def test_persistent_identity_valid_range(sync_db_path: Path):
    name_idx, emoji_idx = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1002, user_id=99)
    assert 0 <= name_idx < _NAME_POOL_SIZE
    assert 0 <= emoji_idx < _COLOR_POOL_SIZE


def test_persistent_identity_writes_to_db(sync_db_path: Path):
    get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=1003, user_id=77)
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT name_index, emoji_index FROM confession_emoji_assignments "
            "WHERE guild_id=1 AND root_message_id=1003 AND user_id=77"
        ).fetchone()
    assert row is not None
    assert row["name_index"] >= 0
    assert row["emoji_index"] >= 0


def test_persistent_identity_legacy_backfill(sync_db_path: Path):
    """Existing rows with name_index=-1 get a valid name_index on next call."""
    with sqlite3.connect(str(sync_db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO confession_emoji_assignments (guild_id, root_message_id, user_id, emoji_index, name_index) "
            "VALUES (1, 9999, 55, 3, -1)"
        )
        conn.commit()
    name_idx, emoji_idx = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=9999, user_id=55)
    assert name_idx >= 0
    assert emoji_idx == 3  # original emoji preserved


def test_ephemeral_identity_does_not_write_to_assignments(sync_db_path: Path):
    get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2000)
    with sqlite3.connect(str(sync_db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM confession_emoji_assignments WHERE guild_id=1 AND root_message_id=2000"
        ).fetchall()
    assert len(rows) == 0


def test_ephemeral_identity_valid_range(sync_db_path: Path):
    name_idx, emoji_idx = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2001)
    assert 0 <= name_idx < _NAME_POOL_SIZE
    assert 0 <= emoji_idx < _COLOR_POOL_SIZE


def test_ephemeral_identity_advances_pool(sync_db_path: Path):
    """Two consecutive ephemeral calls to the same thread return different indices."""
    a = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2002)
    b = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=2002)
    assert a != b


def test_ephemeral_and_persistent_share_pool(sync_db_path: Path):
    """Ephemeral and persistent calls compete for the same color pool (no reuse within a cycle)."""
    pool_size = _COLOR_POOL_SIZE
    seen_colors: set[int] = set()
    root = 3000
    for user_id in range(pool_size // 2):
        _, emoji_idx = get_or_assign_anon_identity(sync_db_path, guild_id=1, root_message_id=root, user_id=user_id)
        seen_colors.add(emoji_idx)
    for _ in range(pool_size - pool_size // 2):
        _, emoji_idx = get_ephemeral_anon_identity(sync_db_path, guild_id=1, root_message_id=root)
        seen_colors.add(emoji_idx)
    assert seen_colors == set(range(pool_size))
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `pytest tests/test_confessions_service.py -v -k "persistent or ephemeral"`

Expected: ImportError (`get_or_assign_anon_identity` not yet defined).

- [ ] **Step 3: Add `get_or_assign_anon_identity` to `services/confessions_service.py`**

Add after `pop_pool_index`:

```python
def get_or_assign_anon_identity(
    db_path: Path, guild_id: int, root_message_id: int, user_id: int
) -> tuple[int, int]:
    """Return (name_index, emoji_index) for a persistent anonymous identity in a thread.

    Legacy rows with name_index=-1 are backfilled using the original hash algorithm.
    """
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT name_index, emoji_index FROM confession_emoji_assignments "
            "WHERE guild_id = ? AND root_message_id = ? AND user_id = ?",
            (guild_id, root_message_id, user_id),
        ).fetchone()
        if row:
            name_idx = int(row["name_index"])
            emoji_idx = int(row["emoji_index"])
            if name_idx == -1:
                # Legacy row: derive name_index from original hash, preserve emoji_index
                digest = hashlib.sha256(f"{user_id}:{root_message_id}".encode()).digest()
                adj_idx = int.from_bytes(digest[0:2], "big") % len(_ANON_ADJECTIVES)
                animal_idx = int.from_bytes(digest[2:4], "big") % len(_ANON_ANIMALS)
                name_idx = adj_idx * len(_ANON_ANIMALS) + animal_idx
                conn.execute(
                    "UPDATE confession_emoji_assignments SET name_index = ? "
                    "WHERE guild_id = ? AND root_message_id = ? AND user_id = ?",
                    (name_idx, guild_id, root_message_id, user_id),
                )
            return name_idx, emoji_idx
        name_idx = pop_pool_index(conn, guild_id, root_message_id, "name", _NAME_POOL_SIZE)
        emoji_idx = pop_pool_index(conn, guild_id, root_message_id, "color", _COLOR_POOL_SIZE)
        conn.execute(
            "INSERT OR IGNORE INTO confession_emoji_assignments "
            "(guild_id, root_message_id, user_id, emoji_index, name_index) VALUES (?, ?, ?, ?, ?)",
            (guild_id, root_message_id, user_id, emoji_idx, name_idx),
        )
        row = conn.execute(
            "SELECT name_index, emoji_index FROM confession_emoji_assignments "
            "WHERE guild_id = ? AND root_message_id = ? AND user_id = ?",
            (guild_id, root_message_id, user_id),
        ).fetchone()
        return int(row["name_index"]), int(row["emoji_index"])
```

- [ ] **Step 4: Add `get_ephemeral_anon_identity`**

```python
def get_ephemeral_anon_identity(db_path: Path, guild_id: int, root_message_id: int) -> tuple[int, int]:
    """Return (name_index, emoji_index) for a one-shot ephemeral identity; not stored."""
    with open_db(db_path) as conn:
        name_idx = pop_pool_index(conn, guild_id, root_message_id, "name", _NAME_POOL_SIZE)
        emoji_idx = pop_pool_index(conn, guild_id, root_message_id, "color", _COLOR_POOL_SIZE)
    return name_idx, emoji_idx
```

- [ ] **Step 5: Run all tests and confirm they pass**

Run: `pytest tests/test_confessions_service.py -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```
git add services/confessions_service.py tests/test_confessions_service.py
git commit -m "feat(confessions): add get_or_assign_anon_identity and get_ephemeral_anon_identity"
```

---

### Task 4: Update `build_anon_reply` and cog caller

**Files:**
- Modify: `services/confessions_service.py`
- Modify: `tests/test_confessions_service.py`
- Modify: `cogs/confessions_cog.py`

- [ ] **Step 1: Write failing tests for updated `build_anon_reply`**

Append to `tests/test_confessions_service.py`:

```python
from services.confessions_service import MAX_DISCORD_MESSAGE_LENGTH, _OP_CIRCLE, build_anon_reply


def test_build_anon_reply_op():
    result = build_anon_reply("hello world", is_op=True)
    assert result.startswith(f"{_OP_CIRCLE} [OP]\n")
    assert "hello world" in result


def test_build_anon_reply_non_op():
    result = build_anon_reply("secret stuff", is_op=False, circle="🔴", anon_name="Brave Owl")
    assert result.startswith("🔴 Brave Owl\n")
    assert "secret stuff" in result


def test_build_anon_reply_truncates():
    long_content = "x" * (MAX_DISCORD_MESSAGE_LENGTH + 100)
    result = build_anon_reply(long_content, is_op=False, circle="🔵", anon_name="Tiny Fox")
    assert len(result) <= MAX_DISCORD_MESSAGE_LENGTH


def test_build_anon_reply_defangs_everyone():
    result = build_anon_reply("@everyone listen up", is_op=False, circle="🟢", anon_name="Quiet Yak")
    assert "@everyone" not in result
    assert "@​everyone" in result
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `pytest tests/test_confessions_service.py -v -k "build_anon"`

Expected: FAIL — current `build_anon_reply` requires positional `user_id` and `root_message_id`.

- [ ] **Step 3: Replace `build_anon_reply` in `services/confessions_service.py`**

Replace the entire function (lines 87–105):

```python
def build_anon_reply(
    content: str,
    *,
    is_op: bool,
    circle: Optional[str] = None,
    anon_name: Optional[str] = None,
) -> str:
    safe = defang_everyone_here(content)
    if is_op:
        prefix = f"{_OP_CIRCLE} [OP]"
    else:
        prefix = f"{circle} {anon_name}"
    msg = f"{prefix}\n{safe}"
    if len(msg) > MAX_DISCORD_MESSAGE_LENGTH:
        msg = f"{prefix}\n{safe[:MAX_DISCORD_MESSAGE_LENGTH - len(prefix) - 1]}"
    return msg
```

- [ ] **Step 4: Run `build_anon_reply` tests**

Run: `pytest tests/test_confessions_service.py -v -k "build_anon"`

Expected: PASS.

- [ ] **Step 5: Update imports in `cogs/confessions_cog.py`**

Replace the entire `from services.confessions_service import (...)` block (lines 13–38):

```python
from services.confessions_service import (
    ERROR_CONFIG_INVALID,
    ERROR_NOT_CONFIGURED,
    ERROR_PANIC_MODE,
    ERROR_REPLIES_DISABLED,
    ERROR_USER_BLOCKED,
    MAX_DISCORD_MESSAGE_LENGTH,
    MIN_REPLY_COOLDOWN_SECONDS,
    anon_circle_from_index,
    anon_name_from_index,
    build_anon_reply,
    check_and_bump_limits,
    defang_everyone_here,
    get_config,
    get_discord_thread_id,
    get_ephemeral_anon_identity,
    get_or_assign_anon_identity,
    get_thread_info,
    init_db,
    jump_link,
    log_confession,
    log_reply,
    purge_old_thread_posts,
    thread_name_from_content,
    update_discord_thread_id,
    upsert_config,
    upsert_thread_post,
)
```

- [ ] **Step 6: Add `ephemeral: bool = False` to `ReplyModal.__init__`**

Replace the `__init__` method of `ReplyModal` (lines 255–268):

```python
    def __init__(
        self,
        cog: ConfessionsCog,
        cfg: GuildConfig,
        parent_channel_id: int,
        parent_message_id: int,
        thread_id: int = 0,
        ephemeral: bool = False,
    ) -> None:
        super().__init__()
        self.cog = cog
        self.cfg = cfg
        self.parent_channel_id = parent_channel_id
        self.parent_message_id = parent_message_id
        self.thread_id = thread_id
        self.ephemeral = ephemeral
```

- [ ] **Step 7: Update identity assignment in `ReplyModal.on_submit`**

Replace lines 335–340 (from `is_op = ...` through `reply_content = build_anon_reply(...)`):

```python
        is_op = parent_author_id > 0 and interaction.user.id == parent_author_id
        circle = None
        anon_name = None
        if not is_op:
            if self.ephemeral:
                name_idx, emoji_idx = get_ephemeral_anon_identity(
                    db_path, interaction.guild.id, root_message_id
                )
            else:
                name_idx, emoji_idx = get_or_assign_anon_identity(
                    db_path, interaction.guild.id, root_message_id, interaction.user.id
                )
            circle = anon_circle_from_index(emoji_idx)
            anon_name = anon_name_from_index(name_idx)
        reply_content = build_anon_reply(content, is_op=is_op, circle=circle, anon_name=anon_name)
```

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -x -q --tb=short`

Expected: all tests PASS.

- [ ] **Step 9: Commit**

```
git add services/confessions_service.py cogs/confessions_cog.py tests/test_confessions_service.py
git commit -m "feat(confessions): wire identity pool into ReplyModal, simplify build_anon_reply"
```

---

### Task 5: New button UI and interaction routing

**Files:**
- Modify: `cogs/confessions_cog.py`

- [ ] **Step 1: Replace `build_reply_button_view` (lines 532–540)**

```python
    @staticmethod
    def build_reply_button_view(root_message_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="🎭 Reply Anonymously",
            style=discord.ButtonStyle.secondary,
            custom_id=f"cr|{root_message_id}",
        ))
        view.add_item(discord.ui.Button(
            label="🎲 Reply as Someone New",
            style=discord.ButtonStyle.secondary,
            custom_id=f"crn|{root_message_id}",
        ))
        view.add_item(discord.ui.Button(
            label="❓ What's this?",
            style=discord.ButtonStyle.secondary,
            custom_id=f"crh|{root_message_id}",
        ))
        return view
```

- [ ] **Step 2: Update the early-exit guard in `_on_interaction_buttons`**

Replace line 654:
```python
            if custom_id != "cr" and not custom_id.startswith("cr|"):
                return
```

With:
```python
            if (
                custom_id != "cr"
                and not custom_id.startswith("cr|")
                and not custom_id.startswith("crn|")
                and not custom_id.startswith("crh|")
            ):
                return
```

- [ ] **Step 3: Add `crh|` help button handler**

After the closing `return` of the `cr|` handler block (after line 700), before the legacy `# Legacy plain "cr" button` comment, insert:

```python
            if custom_id.startswith("crh|"):
                action = "help request"
                await self._safe_ephemeral(
                    interaction,
                    "**🎭 Reply Anonymously** — gives you a consistent identity in this thread. "
                    "Your name and color stay the same across all your replies here.\n\n"
                    "**🎲 Reply as Someone New** — gives you a one-time random identity for just "
                    "that message. A fresh name and color every time you click it.",
                )
                return

```

- [ ] **Step 4: Add `crn|` ephemeral reply handler**

After the `crh|` handler block (just added), insert:

```python
            if custom_id.startswith("crn|"):
                action = "ephemeral anonymous reply"
                parts = custom_id.split("|")
                if len(parts) != 2 or not parts[1].isdigit():
                    await self._safe_ephemeral(interaction, "Invalid reply button.")
                    return
                root_message_id = int(parts[1])
                if not get_thread_info(self.ctx.db_path, interaction.guild.id, root_message_id):
                    await self._safe_ephemeral(interaction, "This confession can no longer be replied to.")
                    return
                discord_thread_id = get_discord_thread_id(self.ctx.db_path, interaction.guild.id, root_message_id)
                if discord_thread_id:
                    thread_obj = self.bot.get_channel(discord_thread_id)
                    if isinstance(thread_obj, discord.Thread) and thread_obj.locked:
                        await self._safe_ephemeral(interaction, "This confession thread is locked.")
                        return
                if not interaction.response.is_done():
                    await interaction.response.send_modal(
                        ReplyModal(
                            self, cfg,
                            parent_channel_id=cfg.dest_channel_id,
                            parent_message_id=root_message_id,
                            thread_id=discord_thread_id,
                            ephemeral=True,
                        )
                    )
                return

```

- [ ] **Step 5: Update `is_valid_reply_target_message` to accept `crn|` buttons**

Replace the final `return any(...)` in `is_valid_reply_target_message` (lines 606–610):

```python
        return any(
            isinstance(_cid := getattr(child, "custom_id", None), str)
            and (_cid.startswith("cr|") or _cid.startswith("crn|"))
            for row in msg.components
            for child in getattr(row, "children", [])
        )
```

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -x -q --tb=short`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```
git add cogs/confessions_cog.py
git commit -m "feat(confessions): add ephemeral reply button (crn|), help button (crh|), three-button view"
```

---

### Task 6: Cleanup — remove old functions

**Files:**
- Modify: `services/confessions_service.py`

- [ ] **Step 1: Delete `anon_id` (lines 75–79), `anon_circle` (lines 82–84), and `get_or_assign_emoji_index` (lines 334–355) from `services/confessions_service.py`**

These are fully replaced by `anon_name_from_index`, `anon_circle_from_index`, and `get_or_assign_anon_identity`. Verify no remaining imports reference them first:

Run: `grep -rn "anon_id\|anon_circle\b\|get_or_assign_emoji_index" --include="*.py" .`

Expected: no matches outside `services/confessions_service.py` itself.

Then delete the three functions.

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -x -q --tb=short`

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```
git add services/confessions_service.py
git commit -m "refactor(confessions): remove hash-based anon_id/anon_circle and count-based get_or_assign_emoji_index"
```
