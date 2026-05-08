# Anonymous Confession Identity — Design Spec

**Date:** 2026-05-08
**Branch:** feat/veil-phase-1 (to be implemented separately)

## Overview

Two changes to anonymous confessions:

1. **Ephemeral identity button** — a second "Reply as Someone New" button that generates a fresh one-shot anonymous identity (name + color) on every click, unrelated to the user's persistent thread identity.
2. **No-repeat pool** — names and colors are drawn from a per-thread shuffled pool so no two participants share a name or color until all options are exhausted, then the pool reshuffles (new random permutation).
3. **Help button** — a third button explaining the difference between the two reply buttons.

## Pool System

### New table: `confession_pools`

```sql
confession_pools(
    guild_id        INTEGER NOT NULL,
    root_message_id INTEGER NOT NULL,
    pool_type       TEXT NOT NULL,   -- "name" or "color"
    remaining_json  TEXT NOT NULL,   -- JSON array of remaining indices
    cycle           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, root_message_id, pool_type)
)
```

- **Name pool** — indices 0–659 into a flat adjective×animal grid. Index `i` maps to adjective `_ANON_ADJECTIVES[i // 33]` and animal `_ANON_ANIMALS[i % 33]`.
- **Color pool** — indices 0–23 into `_ANON_CIRCLES`.

On first access for a thread, both pool rows are created with a full `random.shuffle()` of all indices. When `remaining_json` is exhausted, reshuffle all indices and increment `cycle`.

### Changes to `confession_emoji_assignments`

Add a `name_index INTEGER` column to store the persistent name assignment alongside the existing `emoji_index`.

**Migration:** backfill `name_index` for existing rows using the current hash function (`anon_id` → derive adjective/animal indices) so existing threads display unchanged.

### Pool pop logic (shared helper)

```
def pop_pool_index(conn, guild_id, root_message_id, pool_type, pool_size) -> int:
    load row; if missing, create with shuffle(range(pool_size))
    pop index 0 from remaining; write back
    if remaining now empty: reshuffle all indices, cycle += 1
    return popped index
```

Called for both persistent and ephemeral assignments.

## Identity Assignment

### Persistent identity (button 1 — "Reply Anonymously")

1. Check `confession_emoji_assignments` for `(guild_id, root_message_id, user_id)`.
2. If row exists: return stored `(name_index, emoji_index)`.
3. If not: pop from name pool and color pool → write new row → return indices.

Same user always gets the same name+color within a thread.

### Ephemeral identity (button 2 — "Reply as Someone New")

1. Pop from name pool and color pool.
2. Use `(name_index, emoji_index)` for this message only — **do not write to `confession_emoji_assignments`**.

Each button click produces a different identity. No persistence.

### OP identity

Unchanged. OP gets `⭐ [OP]` header. OP's persistent identity is assigned normally (for pool consistency) but the circle/name are not displayed in their messages.

## Button UI

`build_reply_button_view` returns a three-button `discord.ui.View`:

| Button | Label | Style | custom_id |
|--------|-------|-------|-----------|
| Reply Anonymously | "🎭 Reply Anonymously" | secondary | `cr\|{root_message_id}` |
| Reply as Someone New | "🎲 Reply as Someone New" | secondary | `crn\|{root_message_id}` |
| Help | "❓ What's this?" | secondary | `crh\|{root_message_id}` |

### Help button response

Ephemeral message visible only to the clicker:

> **🎭 Reply Anonymously** — gives you a consistent identity in this thread. Your name and color stay the same across all your replies here.
>
> **🎲 Reply as Someone New** — gives you a one-time random identity for just that message. A fresh name and color every time you click it.

### Interaction routing (`on_interaction`)

Add two new `custom_id` prefixes alongside the existing `cr|` handler:

- `crn|{root_message_id}` — same modal as `cr|` but passes `ephemeral=True` flag so the service generates an ephemeral identity instead of a persistent one
- `crh|{root_message_id}` — no modal; respond immediately with ephemeral help text

## Service API changes

```python
# existing — unchanged signature, internal behavior changes to use pool
def get_or_assign_anon_identity(conn, guild_id, root_message_id, user_id) -> tuple[int, int]:
    ...  # returns (name_index, emoji_index), persistent

# new
def get_ephemeral_anon_identity(conn, guild_id, root_message_id) -> tuple[int, int]:
    ...  # returns (name_index, emoji_index), one-shot, no DB write to assignments
```

Two pure helpers convert indices to display strings (replacing the old hash-based `anon_id` / `anon_circle` functions):

```python
def anon_name_from_index(name_index: int) -> str:
    return f"{_ANON_ADJECTIVES[name_index // 33]} {_ANON_ANIMALS[name_index % 33]}"

def anon_circle_from_index(emoji_index: int) -> str:
    return _ANON_CIRCLES[emoji_index]
```

`format_confession_header` already accepts `(circle, anon_name)` strings — no signature change needed there.

## Migration

1. Add `name_index INTEGER` column to `confession_emoji_assignments` (nullable, backfilled).
2. Create `confession_pools` table.
3. Backfill `name_index` for existing rows: derive from current `anon_id` hash (compute which adjective+animal the hash maps to, store as index).
4. No pool rows created during migration — pools are created lazily on first access.

## Testing

- Pool pop returns unique indices until exhausted, then reshuffles (new order).
- Two users in same thread get different name indices and different color indices.
- Ephemeral assignment does not write to `confession_emoji_assignments`.
- Persistent assignment for same user in same thread returns same indices on repeat call.
- Help button returns ephemeral response, no modal.
- `crn|` prefix routes to ephemeral identity path.
- Migration backfill produces valid `name_index` for all existing rows.
