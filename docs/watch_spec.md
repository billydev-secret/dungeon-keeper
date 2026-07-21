# Watch List

**Flavor: Reference** — matches current behavior.

## Purpose

Lets a moderator quietly monitor a member: while a watch is active, the
member's public posts are relayed to the watching mod's DMs. When the local AI
is available it acts as a filter — only posts it flags as a rule concern are
forwarded; without AI, every post forwards. The watched member is never
notified.

## Commands

All three live under the `/watch` group (`WatchCog` in
`src/bot_modules/cogs/watch_cog.py`), guild-only in practice and gated twice:
Discord-side `default_permissions=Manage Server` on the group, plus the bot's
own `ctx.is_mod()` check (Manage Server / Administrator, or a configured mod
role via `GuildConfig.member_is_mod`). All responses are ephemeral.

| Command | Purpose |
|---|---|
| `/watch add user:<member>` | Start watching. Rejects bots and self-watching. The confirmation says whether AI screening is active or everything will be DM'd (checked live via `ollama_client.is_available()`). |
| `/watch remove user:<member>` | Stop watching (removes only *your* watch on them). |
| `/watch list` | Lists everyone **you** are watching — per-watcher, not a guild-wide roster. |

Watches are per (watched, watcher) pair: two mods can watch the same member
independently, and each manages only their own list.

## Behavior

`WatchCog.on_message` ignores bot authors, then checks the author against
`ctx.watched_users` — an in-memory `{watched_id: {watcher_ids}}` dict, so the
hot path costs one dict lookup. On a hit, `_notify_watchers` runs:

1. **AI gate.** If `ollama_client.is_available()`, the message is sent to
   `ai_check_watched_message` (`ai_moderation_service.py`): a single-message
   rule check whose system prompt and model are the dashboard-editable
   `ai_prompt_watch_check` / `ai_model_watch_check` keys (AI panel, via
   `list_prompts()` in `ai_config.py`). A `VIOLATION: …` reply forwards the
   message with the reason line; anything else suppresses it. **Fail-open:**
   if the AI call raises, the cog logs a warning and forwards anyway — a
   flaky model never hides a watched member's posts. If AI is unavailable
   entirely, there is no gate and every post forwards.
2. **DM relay.** Each watcher gets a DM containing the message text (or
   `*[no text content]*`), any attachment URLs, the optional
   `⚠️ Rule concern:` reason, author display/user name, guild + channel name,
   and a jump link. DM failures (closed DMs, fetch errors) are logged and
   skipped per-watcher — one unreachable mod doesn't block the others.

The relay is live-only: nothing about the forwarded message is stored, so the
feature coexists with `message_storage_level="none"` (the DM carries content
the DB never sees).

### Caveats (as built)

- The in-memory set is **not guild-scoped**. Rows are stored per guild, but
  `ctx.watched_users` keys only on user id and `on_message` never checks
  `message.guild` — a watched user's posts in any guild the bot shares are
  relayed. On restart, only the primary guild's rows are reloaded
  (`load_watched_users(conn, guild_id)` in `src/dungeonkeeper/__main__.py`),
  so watches added in another guild silently lapse at the next restart.
- `/watch list` reads the in-memory dict, so it reflects the same
  cross-guild runtime view.

## Configuration

No dashboard panel and no config keys of its own — the slash commands are the
entire management surface (mod-facing action, not admin config, per the
working agreement). The only adjacent dashboard surface is the AI panel's
"Watch — live rule check" prompt/model pair named above.

## Stored data

`watched_users` in the base schema (`src/migrations/000_init.sql`):

```
watched_users(guild_id, watched_user_id, watcher_user_id)
    PK (guild_id, watched_user_id, watcher_user_id)
```

Ids only — no timestamps, no reason field, no history of removed watches, and
never any message content. DB helpers live in
`src/bot_modules/services/watch_service.py` (`add_watched_user`,
`remove_watched_user`, `load_watched_users`); the cog mirrors every write into
`ctx.watched_users`.

## Non-goals

- No notification or visibility to the watched member.
- No audit trail of forwarded messages or of who watched whom historically.
- No guild-wide "all watches" view — each mod sees only their own list.
- No dashboard config panel; no per-watch expiry.
