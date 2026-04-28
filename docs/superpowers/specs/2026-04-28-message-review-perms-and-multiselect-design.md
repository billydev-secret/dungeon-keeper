# Message Review: moderator perms + multi-select author/channel

## Background

The "Message Review" panel (`web/static/js/panels/message-search.js`, registered
in `web/static/js/app.js:82`) lives under the Moderation section, which is gated
on the `moderator` perm. Its backing routes in `web/routes/messages.py`
currently require `admin`, so any non-admin moderator who clicks the panel hits
a 403. The author and channel filters each accept only one value.

Two changes:

1. Align the route perms with the section: require `moderator`, not `admin`.
2. Let users filter by multiple authors and multiple channels at once.

`mentions` and `reply_to` stay single-select — out of scope.

## Permission change

`web/routes/messages.py`, four endpoints:

| Line | Endpoint | Current | New |
|------|----------|---------|-----|
| 33 | `GET /messages/search` | `require_perms({"admin"})` | `require_perms({"moderator"})` |
| 380 | `GET /messages/search/export` | `require_perms({"admin"})` | `require_perms({"moderator"})` |
| 711 | `GET /messages/ai-models` | `require_perms({"admin"})` | `require_perms({"moderator"})` |
| 734 | `POST /messages/ai-query` | `require_perms({"admin"})` | `require_perms({"moderator"})` |

No frontend gating change. The panel is already accessible to `moderator` via
the Moderation section in `app.js:74`.

## Multi-select: backend

Both `/messages/search` and `/messages/search/export` change:

- `author: str | None` → `author: list[str] | None = Query(None)`.
- `channel: str | None` → `channel: list[str] | None = Query(None)`.

`mentions` and `reply_to` remain `str | None`.

### Author resolution

Each value in `author` is independently resolved with the existing
`_resolve_user(conn, value)` helper (numeric ID → `[int(value)]`; otherwise name
substring lookup against the guild cache, then `known_users`). The resolved ID
sets are unioned into one list.

Behavior:

- If the union is empty (every supplied value resolved to nothing), the search
  returns the empty result, matching today's behavior for a single
  unresolvable name.
- If the union has one ID, emit `m.author_id = ?` (preserves the current
  fast path).
- If the union has multiple IDs, emit `m.author_id IN (?, ?, ...)`.

This mirrors the existing branch at `messages.py:119-125`.

### Channel resolution

Channel values are still treated as numeric IDs (the UI sends IDs from the
populated dropdown). Each value is `int()`-cast and emitted as
`m.channel_id IN (?, ?, ...)` (or a `=` for the single-value case). A
non-integer channel value is a 422 from FastAPI, same as today's
single-channel parsing path.

### AI query

`AI_QUERY_SYSTEM_PROMPT` (the `Available filters` section) updates `author` and
`channel` to read:

- `"author": user ID/name string OR array of user ID/name strings`
- `"channel": channel ID string OR array of channel ID strings`

The example output stays single-value for clarity. The route returns the AI's
filters object verbatim — no server-side normalization needed; the frontend's
`applyFilters` handles both shapes (see below).

## Multi-select: frontend

`web/static/js/panels/message-search.js`:

### New helper: `multiFilterSelect(placeholder, options)`

Built next to `filterSelect`. Same dropdown-on-focus / type-to-filter UX, but
selection appends a chip above the input rather than replacing the input value.

API:

```
{
  el,                  // container DOM node
  getValues(): string[],
  setValues(ids: string[]): void,
  getInput(): HTMLInputElement,
}
```

Chip rendering: each chip shows the option label and an `×` button. Clicking
`×` removes that ID from the selection. Selecting the same option twice is a
no-op. The "(any)" sentinel from `filterSelect` is removed for the multi
version — selecting nothing already means "any."

### Author filter

Replace the placeholder `authorFS = filterSelect(...)` and its post-load
replacement with `multiFilterSelect`. The `author` slot in the controls grid is
unchanged. Enter inside the input still triggers `doSearch(1)`.

### Channel filter

Replace the native `<select data-field="channel">` with a `multiFilterSelect`.
Channel options are loaded from `/api/meta/channels` in the existing
`Promise.all`; build them as `{ id: ch.id, label: '#' + ch.name }`.

The `<label>Channel ...</label>` markup changes to a slot
(`<span data-slot="channel"></span>`) and the widget is mounted into it,
matching the author/mentions/reply_to pattern.

### Enter-to-search

The existing wiring (`message-search.js:263-267`) loops over
`[authorFS, mentionsFS, replyFS]` to attach Enter handlers. Add `channelFS` to
that loop.

### `buildFilterParams`

```
for (const id of authorFS.getValues()) params.append("author", id);
for (const id of channelFS.getValues()) params.append("channel", id);
```

`URLSearchParams.append` produces the `?author=1&author=2` form FastAPI parses
into a `list[str]`.

### `applyFilters` (AI prefill)

For `author` and `channel`, accept either string or array:

```
const toArray = (v) => v == null ? [] : Array.isArray(v) ? v.map(String) : [String(v)];
authorFS.setValues(toArray(f.author));
channelFS.setValues(toArray(f.channel));
```

The reset block at the top of `applyFilters` calls `setValues([])` for both.

## Result rendering

No change. Each row already shows author and channel names individually; with
multi-select, results are simply the union across selected authors/channels.
Pagination, sorting, and sentiment/emotion badges all work as-is.

## Out of scope

- `mentions` and `reply_to` multi-select.
- Backend AND-mode (e.g., "messages mentioning both X and Y"). Multi-select is
  OR by design.
- Persisting selected filters across navigations.
- Visual changes to the dropdown beyond chip rendering.

## Testing

- Manual: pick two authors → results contain messages from either; remove one
  chip → results narrow. Same for channels.
- Manual: a non-admin user with the Discord "Manage Messages" permission (which
  grants the `moderator` perm) can open the panel and run a search without a
  403.
- Manual: AI query "messages from alice or bob in #general" populates two
  author chips and one channel chip, then runs.
- Manual: AI query "messages from alice in #general" still works with a single
  chip on each.
- Existing message-search tests still pass (the single-value path is preserved
  for both filters).
