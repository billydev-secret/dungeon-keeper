# Tools — Feature Spec

Internal owner-only commands, an admin-only Bot Identity panel on the dashboard, the open `/support` command, and the slash-command sync gate that runs on every startup.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/reload_cog extension:<ext>` | Slash | Bot owner | Hot-reload an extension and its imported modules, then resync the command tree if it changed |
| `/spotify_authorize` | Slash | Bot owner | Ephemeral link to the dashboard's Spotify OAuth flow |
| `/support` | Slash | Everyone | Ephemeral message with the support-server invite |
| Bot Identity panel | Web (dashboard) | Admin | Change the bot's per-server nickname and guild avatar without touching the global Discord account |

The command-tree sync gate is invoked automatically — once on bot startup, again from `/reload_cog`. It has no member-facing surface.

## Behaviour

### `/reload_cog`

1. Refuse the call unless the invoker is the bot owner.
2. Refuse unknown extensions.
3. Reload the extension's imported `bot_modules.*` dependencies (preserving any singletons, caches, or background tasks that were bound at module level), then reload the extension itself.
4. Recompute the slash-command tree signature. If it changed, push the update to Discord. Reply ephemerally with "Reloaded {ext}. (commands resynced)" or "... (commands unchanged)".
5. On any error during the reload or resync, reply ephemerally with the exception class and message.

### `/spotify_authorize`

Owner-gated. Replies ephemerally with a link to the dashboard's `/spotify/authorize` route. The dashboard handles the actual OAuth dance — this command is just a convenience shortcut for the owner so they don't have to navigate manually.

### `/support`

Open to every member, works in DMs and guilds. Replies ephemerally with a "Click here to join the support server" link to the support-server invite.

### Bot Identity panel

Lives in the Global Config section of the dashboard as a "Bot Identity (this server)" block. An admin can change two things, per guild, without leaving the dashboard:

- **Nickname** — the bot's display name in this server. Submitting an empty value clears the nickname back to the global account name.
- **Avatar** — the bot's guild-specific avatar. Either paste an image URL or upload a file directly. File upload takes priority when both are provided.

Changes are applied live through Discord and are reflected immediately on member lists. The dashboard pre-loads the current nickname and a 64-px avatar preview when the panel opens; on submit it shows an inline status ("Applied" / error detail) next to the Apply button. There's no database record — Discord owns the truth here, and the panel reads it back fresh each time.

The bot's **global** account name and avatar (the ones visible across every server it's in) are managed only via the Discord Developer Portal, not this panel.

### Command sync (startup and post-reload)

The bot keeps a hash of the slash-command tree as it was last synced. On startup, and again after `/reload_cog`, the bot rehashes the tree:

- **Hash unchanged** — skip the network call. Discord still has what it had before.
- **Hash changed** — push the updated tree and store the new hash.

The bot picks **guild-scoped sync** in dev mode (changes appear instantly in the configured dev guild) and **global sync** in prod mode (visible everywhere after up to a one-hour Discord cache). On a dev/prod switch, the bot also clears the other mode's leftover commands so the same command doesn't appear twice.

If the dev guild sync hits "missing access" (the bot wasn't invited with the `applications.commands` scope), the bot logs a warning and continues without retrying.

## Permissions

- `/reload_cog` and `/spotify_authorize`: bot owner only (Application Owner or any id in the bot's explicit owner list).
- `/support`: everyone.
- Bot Identity panel: dashboard admin perm.
- Bot-side: standard slash-command perms only. The Bot Identity panel additionally needs **Change Nickname** in the target guild for the nickname edit to land.

## User-visible errors

| When | The user sees |
|---|---|
| Non-owner runs `/reload_cog` or `/spotify_authorize` | "Bot owner only." |
| `/reload_cog` extension isn't loaded | "Unknown extension `{ext}`." |
| Reload or resync raises | "Reload failed: `{ExcType}: {message}`" |
| Bot Identity panel: bot or guild unavailable | "Bot not available" (503) |
| Bot Identity panel: avatar URL fetch fails or Discord rejects the payload | The error detail from the response (400) |

## Non-goals

- **No user-facing health / status / version command.** Service health is exposed via the dashboard.
- **No live config reload.** `/reload_cog` reloads code; per-guild config is read fresh from the DB on the next request that needs it.
- **No DB migration runner from inside the bot.** Migrations apply at process start before the bot connects.
- **No dev-mode → prod-mode promotion without a restart.**
- **No shard-aware sync.** The sync gate operates on a single command tree.
- **No editing the global Discord account identity** (username, account avatar). The Bot Identity panel is per-guild only; the global account is managed through the Discord Developer Portal.
- **No nickname/avatar history** — overwriting is destructive; Discord doesn't expose the prior values.

## Configuration

| Key | Purpose |
|---|---|
| `DASHBOARD_BASE_URL` (env) | Where `/spotify_authorize` links to. Defaults to localhost |
| Dev / prod mode + dev guild id (env) | Picks guild-scoped vs global sync. Documented in [[dungeon-keeper-test-env-spec]] |

The support-server invite is a hard-coded constant — changing it needs a redeploy.

## Stored data

Two per-guild rows in the shared config table for the slash-command tree hash (one for the global hash, one for each guild the bot has ever synced to). These are a performance optimisation — losing them just forces one extra resync on next startup.

No per-user data. No filesystem cache. The `/support` invite URL is in code, not in the DB.
