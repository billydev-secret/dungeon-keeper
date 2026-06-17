# Privacy — Feature Spec

Two slash commands that erase a user's data: `/delete_me` for self-erasure and `/delete_user` for mod erasure of another member. Erasure runs in two phases — an authoritative Discord-side scan + delete that walks every readable channel and thread looking for messages by the target, then a DB purge that removes XP, activity, profile, wellness, and interaction-graph rows. The local `messages` archive is opt-in per command: `/delete_me` defaults to **keeping** the archive (the user can still see what they wrote even after Discord-side records are gone); `/delete_user` defaults to a **hard purge**.

The channel-walking scanner is shared with [[events-spec]]'s backfill so both features cover the same channel set.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/delete_me` | Slash | Everyone (guild only) | Erase your own data; the local archive is preserved by default |
| `/delete_user member:<user>` | Slash | Manage Server + mod | Erase any user's data (including users who have left); hard purge by default |

There is no web dashboard surface — erasure is destructive and intentionally gated behind a slash-only ephemeral confirmation.

## Behaviour

### Confirmation
Both commands open an ephemeral confirm view with **Yes, delete everything** and **Cancel**. The view checks that the clicker is the original invoker, times out silently after **60 seconds**, and disables itself on click. Until the user confirms, nothing is touched.

A per-target lock prevents racing: while a deletion for user X is running, neither `/delete_me` (by X) nor `/delete_user @X` (by a mod) will start a second one — the second invocation sees a "deletion already running" ephemeral and bails.

### Phase 1 — Discord-side scan
The scanner walks every channel the bot can read: text channels, voice and stage chat channels, active forum threads, and archived threads (including private archived threads when the bot has Manage Threads). Anything the bot can't see is silently skipped — it won't be deleted, but the rest of the run continues. Progress updates throttle to roughly one edit every 2 seconds and read: "Scanning the server for your messages — channel **D/T** (**F** found so far)…". The final update is always sent.

If the scan finds zero messages, the Discord-side phase is skipped entirely and the user gets a single completion message that confirms the DB-side data was cleared.

### Phase 2 — Discord-side delete
For each channel with hits:
- **Channels the bot can no longer reach** count their messages as "already gone" — Discord agrees they're not there.
- **Archived threads** are unarchived before processing and re-archived after — Discord refuses sends and deletes against archived threads.
- **Forum thread OPs** (the post that anchors the thread) get deleted, then the bot posts a `[deleted]` tombstone so the thread itself — and other members' replies — survive under the bot's name. These count as "replaced", not "deleted".
- **Recent messages (≤14 days)** use bulk delete in batches of 100, with a 1-second pause between batches.
- **Older messages (>14 days)** are deleted one at a time, with a half-second pause between calls — Discord's API has no bulk-delete for older messages.

Per-message failures (permissions denied, transient HTTP errors) are counted as "failed" and logged; the run continues. Progress is rendered as a 20-character bar like `[████████░░░░░░░░░░░░] 42/100`, throttled to one edit every 1.5 seconds.

### Phase 3 — DB purge
The purge runs unconditionally, even if the Discord-side delete is fully blocked by missing permissions. It removes the user's rows from XP, voice sessions, member activity, quality-score history, gender, member events, the interaction graph (both directions), wellness state and counters, and audit-event tables. When `keep_messages=False`, it also drops the `messages` archive and its children (attachments, mentions, embeds, reactions, sentiment, and the per-user dedup table).

Wellness tables vary by guild deployment age; missing tables are tolerated — a missing-table error logs a warning and the rest of the purge proceeds.

### Phase 4 — Final report
If the interaction token is still alive, the ephemeral message is edited with the summary. Long scans can outlive Discord's 15-minute interaction lifetime; in that case the bot DMs the actor instead. A closed-DM actor gets nothing user-facing — only log lines.

A typical summary:

```
All done. Here's what was removed:
Discord messages deleted: **N**
Server-side data (XP, activity, profile): **cleared**
Messages that couldn't be deleted (no access): **M**
```

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
| Scan-of-empty | "No messages found in any channel I can read. Server-side data: cleared." |
| Discord-side delete partially blocked | Summary still posts; counters show how many couldn't be deleted |
| Interaction token expired mid-run | Final summary is DM'd to the actor instead of edited in place |

## Non-goals

- **No undo.** The confirm view is the only safety net; deletion is permanent.
- **No partial selectors.** Users can't say "delete only my XP" or "delete only my messages in #channel". The only switches are which command (self vs other) and whether the archive is kept.
- **No web dashboard.** Mods must run the slash command — the destructive scope and confirm-view UX don't translate cleanly.
- **No cross-guild delete.** `/delete_me` clears one guild only; a user in three servers must run it in each.
- **No deletion of [[dm-perms-spec]] audit / consent rows.** The audit log retains the user's id; consent pairs are deliberately preserved as a forensic record. A user wanting to wipe these must revoke each pair first.
- **No export.** Right-to-portability is intentionally deferred; the `keep_messages=True` default on `/delete_me` is the workaround.
- **No retry queue.** Failed deletions surface in the summary but don't reschedule. Re-running the command after fixing perms is the path.
- **No notification to the target.** `/delete_user @alice` does not DM Alice; the action is silent except for the actor's ephemeral progress.

## Configuration

Privacy has no per-guild configuration. The behavioural constants — 60-second confirm timeout, the 14-day cutoff between bulk and one-at-a-time delete, the throttle cadences — mirror Discord's own constraints and are not exposed.

The only per-call switches are which command was run (which determines whether the `messages` archive is purged or kept) and whether the channel walker tries to surface unjoined private archived threads (on for privacy, off for the [[events-spec]] backfill caller).

## Stored data

Privacy is a **pure reader and deleter** — it owns no tables of its own. It reads the `messages` archive to enumerate what to delete on the Discord side, then purges rows the user owns across roughly two dozen tables spanning XP, activity, gender, wellness, interactions, and (optionally) the archive itself.

**Preserved on purpose**: [[dm-perms-spec]] consent and audit data, [[pressure-cooker-spec]] game history, [[starboard-spec]] quote audit, [[guess-spec]] / [[whisper-spec]] / [[jail-spec]] tables, [[reporting-spec]] invite tracker and raid records, and [[confessions-spec]] audit and submissions. The preserve list is implicit: a table is preserved iff it isn't named in the purge. New per-user tables landing in other features must make an explicit decision — either join the purge or document why they're kept.

In-memory only: the set of currently-running deletions (keyed by target user id), cleared in a finally-block whether the run succeeded, failed, or was cancelled.
