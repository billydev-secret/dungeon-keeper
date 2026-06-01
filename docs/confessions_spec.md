# Confessions — Feature Spec

Anonymous confession box with a persistent **Confess** launcher button at the bottom of a configured channel. Submitters open a modal; the bot reposts the text into a destination channel (or a forum thread) and seeds it with an anonymous-reply button bar. Replies are themselves anonymous — each replier gets a stable name + colour per thread, or a fresh "someone new" identity on demand. Every confession and reply is mirrored to a moderator log channel.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/confess` | Slash | Everyone (any channel) | Open the confession modal (long-form text + notify-pref input) |
| **Confess** (launcher button) | Persistent button | Everyone | Open the same confession modal as `/confess` |
| **🎭 Reply Anonymously** | Persistent button | Everyone | Open the reply modal with the user ' s **stable** identity for this thread |
| **🎲 Reply as Someone New** | Persistent button | Everyone | Open the reply modal with a fresh **ephemeral** identity (not stored) |
| **❓ What ' s this?** | Persistent button | Everyone | Ephemeral help text comparing the two reply modes |
| Confessions config | Web | Admin reads; Game host writes | Edit destination / log channel, cooldown, character cap, panic / replies flags, per-day limit |
| Confessions block list | Web | Admin | Add / remove per-guild blocklist entries |
| Launcher placement | Web | Game host | Post / move the launcher button to a specified channel |
| Confessions audit log | Web | Admin | Recent confessions joined with archived bodies for moderator review |

Bot-side perms required in the destination channel: **Send Messages**, **Embed Links** (for the log embed), **Create Public Threads** (text-channel destination — the bot creates a thread for the reply bar) or **Send Messages in Threads** (forum destination). All modals reject DMs implicitly.

## Behaviour

### Submitting a confession

The modal accepts the body plus a notify-pref textbox (yes / no / unset). On submit, the bot rejects when:

- the guild has no confessions config row,
- panic mode is on,
- the submitter is on the per-guild block list,
- the body is empty after trimming,
- the body exceeds the per-guild character cap,
- the submitter is still inside the cooldown,
- or the submitter has reached the per-day limit.

On success the bot posts the body to the destination channel. For a forum destination it creates a new thread (using the first available tag if the forum requires one); for a text channel it sends the message then creates a thread on it for the reply bar. The reply button bar (the three buttons above) is posted into the thread. The mod log channel receives a mirror embed. The launcher button is re-pinned to the bottom of the launcher channel.

### Reply identity model

Each confession thread maintains two shuffled pools: a **name pool** of 660 entries (20 adjectives × 33 animals — e.g. "Brave Aardvark") and a **colour pool** of 22 unicode circles. Both pools are popped without replacement; when a pool is exhausted, it reshuffles and a cycle counter advances. Persistent and ephemeral replies share the same pools — once a colour or name has been handed out in a cycle, neither path hands it out again until the pool refills.

- **🎭 Reply Anonymously** (persistent) — the user ' s identity for this thread is stored and stable across every reply they make in it. Older threads predating the pool system lazy-backfill from the original hash-based mapping so the identity stays visually consistent.
- **🎲 Reply as Someone New** (ephemeral) — a fresh name and circle are popped from the pools just for this reply. Nothing is stored against the user; subsequent ephemeral replies give different identities.
- **OP badge** — the original confessor ' s persistent replies are tagged with a **⭐ [OP]** marker instead of the name + circle. Ephemeral replies never get the OP badge, even from the original confessor — that ' s the point of the "someone new" button.

### Submitting a reply

Same set of guards as a confession, plus a check that replies are enabled in this guild. The reply cooldown is half the post cooldown with a 30-second floor. There is no per-day limit on replies. The reply is posted in the spawned Discord thread when known, otherwise as a Discord reply to the parent message. If the original confessor opted into reply notifications, the bot DMs them with jump links to the reply and the original confession; closed DMs and other DM failures are silent.

`@everyone` and `@here` in any confession or reply body are defanged before posting. Bodies are hard-truncated to 2 000 characters after the identity prefix.

### Launcher button maintenance

The launcher gets re-pinned to the bottom of the launcher channel after every confession, every reply, every non-bot message in the launcher channel, and any explicit dashboard re-post. A per-guild lock serialises the re-pin so concurrent activity doesn ' t spawn duplicates. Stale launcher buttons in the last 50 channel messages are swept after each post.

### What ' s this? button

Pure help text. Posts an ephemeral comparison of the two reply modes. No database writes.

## Permissions

- **Discord side** — every entry point is open to all guild members; the cog only enforces the per-guild blocklist and the global panic flag. Both modals reject DMs implicitly by checking guild context.
- **Dashboard** — reading the confessions config requires admin; editing the config and posting the launcher require the game-host tier; block / unblock requires admin; the audit log requires admin.

## User-visible errors

| When | The user sees |
|---|---|
| Confessions not configured for this guild | "Bot is not configured. Ask an admin " |
| Panic mode is on | "Confessions are temporarily disabled." |
| Notify-pref textbox contains something other than yes / no / empty | "Invalid notify setting. Use `yes` or `no`." |
| Body is empty after trim | "Confession/Reply can ' t be empty." |
| Body exceeds the character cap | Ephemeral with the computed cap (per-guild) |
| Inside cooldown | "Slow down — you can post/reply again in **{remaining}s**." |
| Per-day limit reached (confessions only) | "You ' ve hit today ' s limit (**{N}**). Try again tomorrow." |
| Submitter on the per-guild block list (confession path) | "You can ' t submit confessions on this server." |
| Submitter on the per-guild block list (reply path) | "You can ' t submit anonymous replies on this server." |
| Replies disabled by config | Ephemeral: replies-disabled message |
| Destination channel rejects the post (perms) | "Failed to post confession/reply (missing perms?)." |
| Reply parent message gone | "That message no longer exists." |
| Reply thread locked | "This confession thread is locked." |
| Generic button interaction error | "Something went wrong handling that {action}." |
| Bot lacks access to act on a button | "I don ' t have enough access to handle that action." |
| Slash command raises | "An unexpected error occurred. Please try again." |

Stale-interaction races (Discord internal-defer collisions) silently no-op — the user ' s click is treated as already handled.

## Non-goals

- **No anonymous DMs to the bot.** Every entry point requires a guild context.
- **No author edit or delete.** Once posted, only mods can remove a confession via Discord directly; the bot offers no command for that.
- **No separate identities for replies-to-replies.** Replying to a reply inherits the root thread ' s identity pool, so the same person keeps the same name and colour throughout.
- **No backfill for deleted spawned threads.** If the thread was deleted manually, the reply button still works but posts as a direct Discord reply in the destination channel.
- **No web-side authoring.** The dashboard configures the feature; submission is Discord-only.
- **No per-channel destination override.** One destination channel per guild.
- **No attachment support today.** Text bodies only.

## Configuration

Per-guild settings, editable from the dashboard:

- **Destination channel** — text channel or forum channel where confessions are reposted.
- **Log channel** — mod-only mirror; logging is best-effort and won ' t block a post.
- **Post cooldown** — seconds between confessions per user (default 120).
- **Reply cooldown** — derived as half the post cooldown, floor 30 s; not directly configurable.
- **Character cap** — per-body cap (default 2000, clamped to Discord ' s actual limit).
- **Panic mode** — kill switch; every modal short-circuits when on.
- **Replies enabled** — disables the three reply buttons globally when off.
- **Notify-OP-on-reply default** — default value of the notify-pref textbox in the confession modal.
- **Per-day limit** — UTC-day cap on new confessions per user; 0 means unlimited.
- **Launcher channel** — where the persistent **Confess** button lives.
- **Block list** — per-guild list of user ids barred from posting confessions and replies, managed via dashboard.

## Stored data

Per-guild: a config row (settings + block list), the per-user rate-limit row (last-confess and last-reply timestamps plus the UTC-day key and counter), thread metadata for every bot-posted message (root or reply, with the real author id kept internal and the spawned Discord thread id), persistent identity assignments keyed by (guild, root message, user), and the shuffled identity pools (name and colour) keyed by (guild, root message). Thread metadata is auto-purged after seven days. No DM data is ever stored.

Ephemeral identity replies pop the shared pools but never write an assignment row, by design — that ' s what makes them ephemeral. Launcher state lives in memory as per-guild locks; pool state lives in the database so identities survive bot restarts.
