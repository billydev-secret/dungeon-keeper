# Privacy — Feature Spec

Two slash commands that erase a member's **Discord messages**: `/delete_me` for self-service and `/delete_user` for mod-run erasure of another member. Since 2026-07-16, both commands delete Discord messages **only**, in every mode — an authoritative Discord-side scan + delete that walks every readable channel and thread looking for messages by the target. Server-side data (XP, activity, profile, wellness, and the bot's own message records) is always retained for moderation, and the confirmation prompt says so before anyone confirms. Under the default storage level the retained message records are mostly ingest-time metadata, not content — see [Stored data](#stored-data).

The genuine hard-erasure path, `purge_user_data`, still exists but is deliberately **unwired from any command**. It is retained for a manual, out-of-band legal (e.g. GDPR) erasure run — see [Out-of-band erasure](#phase-3--db-purge-retired-out-of-band-erasure).

Both commands take an optional `mode` that narrows the scope to just images/files or just text — see [Modes](#modes). Every mode is a scrub of Discord messages; the account is untouched regardless.

The channel-walking scanner is shared with [[events-spec]]'s backfill so both features cover the same channel set.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/delete_me [mode]` | Slash | Everyone (guild only) | Delete your own Discord messages — all of them, or just images/text; server-side data always stays |
| `/delete_user member:<user> [mode]` | Slash | Manage Server + mod | Delete any user's Discord messages (including users who have left) — all, or just images/text; server-side data always stays |

There is no web dashboard surface — erasure is destructive and intentionally gated behind a slash-only ephemeral confirmation.

## Modes

`mode` is an optional choice on both commands — "All my messages" (or "All their messages" on `/delete_user`), "Images & files only", "Text messages only" — and omitting it defaults to all.

| Mode | Value | Deletes from Discord | Account data (XP, activity, profile) | Local archive |
|---|---|---|---|---|
| All messages | `all` (default) | every message | untouched | untouched |
| Images & files only | `media` | messages carrying media | untouched | untouched |
| Text messages only | `text` | messages carrying no media | untouched | untouched |

The modes differ **solely in which Discord messages go** — every mode leaves the account and the server's own records intact, so clearing your photos (or everything) doesn't cost you your level. No mode reaches the DB purge on either command.

The two partial modes partition a member's messages exactly: every message is in `media` or `text`, never both.

**What counts as media** (`message_has_media`): attachments, stickers, and embeds of type `image`/`video`/`gifv`. Discord auto-generates an embed for any posted link, so `link`/`article`/`rich` previews deliberately do **not** count — otherwise ordinary chatter with a URL would be swept into "clear my images". A posted image *URL* has no attachment but does produce an `image` embed, so it counts.

Selection happens **during the scan** (`find_user_messages(predicate=...)`): the scan returns only `(message_id, channel_id)`, so a mode that selects on message content has to decide while the `discord.Message` is still in hand. A message the predicate rejects is never collected and therefore never deleted.

## Behavior

### Confirmation
Both commands open an ephemeral confirm view with a danger button and **Cancel**. The view checks that the clicker is the original invoker, times out silently after **60 seconds**, and disables itself on click. Until the user confirms, nothing is touched.

The prompt and button name the *real* scope — a media scrub's button reads "Yes, delete my images & files" (or "their" when a mod acts on someone else). The prompt is also where retention is disclosed, on both commands: XP, activity, and profile stay exactly as they are, and the server keeps its own records of the messages for moderation (under the default storage level those records are mostly ingest-time metadata, not content). The person is told that **before** the irreversible click rather than discovering it in the summary afterwards.

A per-target lock prevents racing: while a deletion for user X is running, neither `/delete_me` (by X) nor `/delete_user @X` (by a mod) will start a second one — the second invocation sees a "deletion already running" ephemeral and bails.

### Phase 1 — Discord-side scan
The scanner walks every channel the bot can read: text channels, voice and stage chat channels, active forum threads, and archived threads (including private archived threads when the bot has Manage Threads). Anything the bot can't see is silently skipped — it won't be deleted, but the rest of the run continues. Progress updates throttle to roughly one edit every 2 seconds and read: "Scanning the server for your messages — channel **D/T** (**F** found so far)…". The final update is always sent.

If the scan finds zero matching messages, the delete phase is skipped entirely and the user gets a single completion message confirming that nothing was found and nothing else was touched.

### Phase 2 — Discord-side delete
For each channel with hits:
- **Channels the bot can no longer reach** count their messages as "already gone" — Discord agrees they're not there.
- **Archived threads** are unarchived before processing and re-archived after — Discord refuses sends and deletes against archived threads.
- **Forum thread OPs** (the post that anchors the thread) get deleted, then the bot posts a `[deleted]` tombstone so the thread itself — and other members' replies — survive under the bot's name. These count as "replaced", not "deleted".
- **Recent messages (≤14 days)** use bulk delete in batches of 100, with a 1-second pause between batches.
- **Older messages (>14 days)** are deleted one at a time, with a half-second pause between calls — Discord's API has no bulk-delete for older messages.

Per-message failures (permissions denied, transient HTTP errors) are counted as "failed" and logged; the run continues. Progress is rendered as a 20-character bar like `[████████░░░░░░░░░░░░] 42/100`, throttled to one edit every 1.5 seconds.

### Phase 3 — DB purge (retired; out-of-band erasure)
**No longer runs from either command, in any mode** (since commit `e63e728`, 2026-07-16). Both commands stop after the Discord-side delete; no DB row is touched.

The purge implementation, `purge_user_data` (`src/bot_modules/services/privacy_service.py`), is deliberately **unwired** rather than deleted. It is the out-of-band path for a genuine legal erasure request (e.g. GDPR): an operator runs it manually against the database. It removes the user's rows from XP, voice sessions, member activity, quality-score history, gender, member events, the interaction graph (both directions), wellness state and counters, and audit-event tables; with `keep_messages=False` it also drops the `messages` archive and its children (attachments, mentions, embeds, reactions, sentiment, and the per-user dedup table). Wellness tables vary by guild deployment age; missing tables are tolerated — a missing-table error logs a warning and the rest of the purge proceeds.

A full legal erasure is therefore two runs: the slash command (or the same channel walker) for the Discord side, plus a manual `purge_user_data` call for the DB side.

### Phase 4 — Final report
If the interaction token is still alive, the ephemeral message is edited with the summary. Long scans can outlive Discord's 15-minute interaction lifetime; in that case the bot DMs the actor instead. A closed-DM actor gets nothing user-facing — only log lines.

A typical summary (the noun follows the mode — "Images & files deleted from Discord" for a media scrub; the tombstone and failure lines appear only when non-zero):

```
All done. Here's what was removed:
Messages deleted from Discord: **N**
XP, activity, profile, and the server's own message records: **kept for moderation**.
Forum posts replaced with tombstone: **R**
Messages that couldn't be deleted (no access): **M**
```

The copy is deliberately neutral (no "your") because `/delete_user` shows this summary to the acting mod, not to the subject.

## Permissions

- The bot needs **Manage Messages** to delete messages, **Read Message History** + **View Channel** on every channel it scans, **Manage Threads** to surface unjoined private archived threads, and **Send Messages** in forum threads where it has to post the `[deleted]` tombstone.
- `/delete_me` is open to everyone; rejects DMs.
- `/delete_user` requires the user's **Manage Server** permission **and** the bot's mod check (defence in depth in case a guild has hand-edited the default permissions).
- The confirm buttons hard-check that the clicker is the actor — for `/delete_user` that's the mod, not the target.

## User-visible errors

| When | The user sees |
|---|---|
| Invoked in DMs | "This command only works in a server." |
| `/delete_user` by a non-mod | "You don't have permission to use this command." |
| `/delete_me` already running for the actor | "A deletion is already running for your account — please wait for it to finish." |
| `/delete_user` already running for the target | "A deletion is already running for @user — please wait for it to finish." |
| Wrong user clicks the confirm button | "This isn't your confirmation." |
| Confirm view times out / cancelled | "Cancelled." (or no message on timeout) |
| Scan-of-empty | "All done. No messages found in any channel I can read. Nothing else was touched — XP, profile, and the server's records stay as they are." |
| Discord-side delete partially blocked | Summary still posts; counters show how many couldn't be deleted |
| Interaction token expired mid-run | Final summary is DM'd to the actor instead of edited in place |

## Non-goals

- **No undo.** The confirm view is the only safety net; deletion is permanent.
- **No partial selectors.** Users can't say "delete only my XP" or "delete only my messages in #channel". The only switches are which command (self vs other) and the `mode` (all / media / text).
- **No web dashboard.** Mods must run the slash command — the destructive scope and confirm-view UX don't translate cleanly.
- **No cross-guild delete.** `/delete_me` clears one guild only; a user in three servers must run it in each.
- **No DB deletion from the commands.** Neither command touches any server-side row — [[dm-perms-spec]] audit / consent rows included. DB erasure is the manual, out-of-band `purge_user_data` run (Phase 3 above), and even that deliberately preserves the consent/audit forensic record.
- **No export.** Right-to-portability is intentionally deferred.
- **No retry queue.** Failed deletions surface in the summary but don't reschedule. Re-running the command after fixing perms is the path.
- **No notification to the target.** `/delete_user @alice` does not DM Alice; the action is silent except for the actor's ephemeral progress.

## Configuration

Privacy has no per-guild configuration. The behavioral constants — 60-second confirm timeout, the 14-day cutoff between bulk and one-at-a-time delete, the throttle cadences — mirror Discord's own constraints and are not exposed.

The only per-call switches are the `mode` (which slice of Discord messages is targeted) and whether the channel walker tries to surface unjoined private archived threads (on for privacy, off for the [[events-spec]] backfill caller).

## Stored data

Privacy is a **pure deleter of Discord messages** — it owns no tables of its own, and the commands purge none. Deletion enumerates the target's messages by walking Discord itself (a live `channel.history` scan across every readable channel, as Phase 1 describes) — **not** by reading the local `messages` archive, so gaps in the archive don't limit what gets deleted.

The `messages` archive itself holds less than the name suggests: `message_storage_level` defaults to `"none"`, under which message content and attachments are dropped at ingest and only metadata derived at ingest time (author, channel, timestamps, media kind, sentiment) is kept. The "server keeps its own records" disclosure is about that metadata unless a guild has opted into `"all"`.

**Preserved by the commands**: everything server-side — the commands write no DB deletions at all. For the out-of-band `purge_user_data` run, the preserve list is implicit: a table is purged iff it's named in the purge (XP, activity, gender, wellness, interactions, member events, audit events, and optionally the archive), and deliberately preserved otherwise — [[dm-perms-spec]] consent and audit data, [[pressure-cooker-spec]] game history, [[starboard-spec]] quote audit, [[guess-spec]] / [[whisper-spec]] / [[jail-spec]] tables, [[reporting-spec]] invite tracker and raid records, and [[confessions-spec]] audit and submissions. New per-user tables landing in other features must make an explicit decision — join the purge or document why they're kept.

In-memory only: the set of currently-running deletions (keyed by target user id), cleared in a finally-block whether the run succeeded, failed, or was cancelled.
