# Jail & Ticket System — Feature Spec

Two of the most emotionally charged moderator workflows — disciplining a member and answering a help request — share one system. Jail places a member into a private channel with their roles stripped while mods talk to them. Tickets open a private channel between a member and the mod team. Both flows produce a transcript on close and feed a unified `/modinfo` history view. A companion warning system creates a paper trail without auto-acting.

## Commands

### Slash & context-menu

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/setup` | Slash | Administrator | First-run interactive wizard for roles, categories, log channels |
| `/config set <key> <value>` / `/config get <key>` / `/config list` | Slash | Administrator | Tweak individual settings after setup |
| `/jail <user> [duration] [reason]` | Slash | Mod | Jail a member, optionally with a duration like `24h` or `7d` |
| `/unjail <user> [reason]` | Slash | Mod | Release a jailed member |
| `Jail User` | User context menu | Mod | Modal for duration + reason, then runs the jail flow |
| `/ticket panel <channel>` | Slash | Mod | Post the persistent "Open Ticket" button in a channel |
| `/ticket open [description]` | Slash | Everyone | Open a ticket directly from chat |
| `Open Ticket About This Message` | Message context menu | Everyone | Open a ticket that links the source message |
| `/ticket close [reason]` | Slash | Mod | Lock a ticket channel (still readable) |
| `/ticket reopen` | Slash | Mod | Unlock a closed ticket |
| `/ticket delete` | Slash | Mod | Permanently delete a closed ticket (generates transcript) |
| `/ticket claim` | Slash | Mod | Subscribe to DM alerts on new activity in this ticket |
| `/ticket escalate [reason]` | Slash | Mod | Bring admin roles into the ticket |
| `/pull <user>` | Slash | Mod | Add a user into the current jail or ticket channel |
| `/remove <user>` | Slash | Mod | Remove a previously pulled user |
| `/warn <user> [reason]` | Slash | Mod | Record a warning and DM the member |
| `/warnings <user>` | Slash | Mod | Show the user's full warning history |
| `/revokewarn <user> <warning_id>` | Slash | Mod | Soft-delete a warning |
| `/modinfo <user>` | Slash | Mod | Unified view: jail status, jail history, warnings, ticket history |

### Web dashboard

The dashboard mirrors the moderator surface. Jail endpoints are read-only — releases happen in Discord.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/moderation/stats` | Counts, averages, top moderators |
| `GET` | `/api/moderation/jails` | List jail records |
| `GET` | `/api/moderation/tickets` | List tickets (filter by status) |
| `GET` | `/api/moderation/tickets/{id}` | Single-ticket detail |
| `POST` | `/api/moderation/tickets/{id}/claim` | Claim a ticket |
| `POST` | `/api/moderation/tickets/{id}/close` | Close (lock the channel) |
| `POST` | `/api/moderation/tickets/{id}/reopen` | Reopen a closed ticket |
| `POST` | `/api/moderation/tickets/{id}/dismiss` | Dismiss with no action |
| `POST` | `/api/moderation/tickets/{id}/escalate` | Escalate to senior mods |
| `POST` | `/api/moderation/tickets/{id}/warn` | Issue a warning from the ticket |
| `POST` | `/api/moderation/tickets/{id}/jail` | Jail the ticket subject |
| `POST` | `/api/moderation/tickets/{id}/note` | Add an internal mod-only note |
| `GET` | `/api/moderation/warnings` | List warnings |
| `GET` | `/api/moderation/policy-tickets` | List escalated tickets |
| `GET` | `/api/moderation/transcript` | Fetch a transcript |
| `GET` | `/api/moderation/audit` | List audit log entries |

Discord and dashboard actions are *intended* to produce identical side-effects
(role changes, channel ops, DMs, transcripts, audit entries), and some do:
`POST .../jail` routes through the same `apply_jail` flow as the slash command
(real role + jail channel + DM), and `.../warn` and `.../claim` match their
Discord counterparts (record-only).

**Known gap (as of 2026-07-14):** the ticket-lifecycle routes
`POST .../close`, `.../reopen`, `.../dismiss`, and `.../escalate` are currently
**DB-only** — they flip the ticket row and write an audit entry but do **not**
reach Discord. Closing (or dismissing/reopening/escalating) a ticket from the
dashboard leaves the Discord channel untouched: the embed still shows its old
status, the Close↔Reopen/Delete buttons don't swap, the channel isn't
locked/unlocked, no note is posted, the creator isn't DM'd, and escalate
neither adds admin roles nor pings them. The bridge to fix this exists
(`ctx.bot` on the web server's event loop, as `.../jail` uses); wiring it up is
deferred. Until then, drive close/reopen/escalate from **Discord** if you need
the channel-side effects.

## Behaviour

### First-run setup

`/setup` walks an admin through a short interactive wizard with role and channel pickers: mod roles, admin roles, jail category, ticket category, log channel, transcript channel (with a "same as log" shortcut), then a confirmation summary. The bot auto-creates a `@Jailed` role with server-wide Deny View Channel and Deny Send Messages overrides — that role must sit below the bot's top role. Two separate categories are used (jail and ticket); both deny `@everyone` view. A persistent "Open Ticket" button is published into the ticket panel channel by `/ticket panel`.

### Jail flow

Mod runs `/jail`, the right-click "Jail User" menu, or the dashboard. Rejects if the target is a bot, already jailed, or a mod. The flow snapshots every current role, strips them, assigns `@Jailed`, and creates a fresh private channel named `jail-{username}-{timestamp}` visible to the target and mod roles only. The `@Jailed` role itself is denied view on this channel so jailed users can't see each other's channels. The bot posts an intake embed in the channel and DMs the member: who jailed them, why, and how long the hold lasts (or "indefinite" if no duration was given). Tone is firm but neutral — "you've been placed in a moderation hold." Durations accept `30m`, `2h`, `1d`, `7d`, `1w`; no value means indefinite.

A background task checks once a minute for expired jails and runs the unjail flow automatically with reason "Jail duration expired." A 24-hour jail set at 11 pm Friday actually ends at 11 pm Saturday even if no mod is online.

If a jailed user leaves and rejoins, they're re-jailed automatically: roles re-stripped, `@Jailed` re-assigned, channel access restored, with a note posted in the jail channel that they returned.

### Unjail flow

Restores every snapshotted role (logging any that were deleted in the meantime), removes `@Jailed`, generates a transcript of the jail channel, posts the transcript file to the transcript log channel, DMs the transcript to the member, then deletes the jail channel. The member's DM tells them they've been released and reattaches the transcript so they have the conversation record.

### Ticket flow

A member opens a ticket via the persistent panel button, `/ticket open`, or the message context menu. The button opens a description modal; the slash command takes an optional description argument; the context menu both prompts for a description and embeds a jump link to the source message. A private channel named `ticket-{username}-{timestamp}` is created, visible to the creator, mod roles, and the bot. The bot posts a ticket embed with Opened By / Source / Description / Created At fields and a red "Close Ticket" button, DMs the creator confirming the ticket, and (when `ticket_notify_on_create` is on) DMs every mod with a jump link.

No cap on concurrent open tickets per user.

### Close, reopen, delete

Tickets pass through three states: **Open → Closed → Deleted**. Close locks the channel — the creator can still read everything but can't post; the embed updates to a "🔒 Closed" status with the closing mod and reason; the Close button is replaced with green "🔓 Reopen" and red "🗑️ Delete" buttons. Reopen restores send permissions and swaps the buttons back. Delete is permanent and runs through a final confirm; it generates the transcript, posts it to the transcript channel, DMs it to the creator, then deletes the channel.

The intermediate Closed state exists so that a "wait, one more thing" message after close doesn't require opening a new ticket — the mod just clicks Reopen.

A ticket left **closed for 24 hours** is deleted automatically. An hourly background sweep finds tickets whose close time is more than 24 h in the past and runs the same delete path as the button: it generates the transcript, posts it to the transcript channel, DMs it to the creator, then deletes the channel. Reopening resets the clock — reopen clears the close timestamp, so a reopened ticket drops out of the sweep until it's closed again (and a re-close starts a fresh 24 h). If the transcript can't be generated, the channel is left intact and the sweep retries on its next pass, so a ticket is never destroyed without an archive. Manual `/ticket delete` (or the Delete button) still works at any time for an immediate, mod-triggered delete.

### Claim & escalate

`/ticket claim` subscribes the claiming mod to DM alerts whenever someone other than them posts in the ticket; alerts are coalesced with a 5-minute cooldown so a flurry of messages produces one DM. Claiming is advisory — it doesn't lock other mods out, it just signals ownership and routes alerts. Another mod can reassign with a confirmation prompt. The claimer's name lands on the ticket embed and the final transcript summary.

`/ticket escalate` adds admin roles to the channel's viewer list, swaps the embed status to "⚠️ Escalated", pings admin roles in-channel, and writes an audit entry. After escalation an admin can run `/ticket claim` to layer admin-level alerts on top of the original mod claim — both get DM'd on new activity. A ticket can be escalated only once.

### Pull & remove

`/pull <user>` and `/remove <user>` add or revoke per-user view + send overwrites inside a jail or ticket channel. Pulled users appear in the transcript's participant list and are not assigned `@Jailed` — they're observers, not subjects. The primary user (the jailed member or ticket creator) cannot be removed.

### Transcripts

Generated when a jail channel is closed via unjail or when a ticket is **deleted** (not just closed). Captures every message in order — author, content, embeds, attachment names + URLs, timestamps — plus participant list, duration, moderator, reason, and outcome. The file lands in three places: stored as JSON in the database (for the dashboard), posted to the transcript log channel with a summary embed, and DM'd to the affected member.

Transcripts are delivered as Markdown (`.md`) files. They're readable in any text editor without rendering, copy-paste cleanly, and survive archival in plain-text tools without losing structure.

### Warnings

`/warn` records a warning, DMs the member with the reason and their current active warning count, and writes an audit embed. When a member's active warning count **reaches** the configured threshold (default 3) the bot posts a highlighted alert in the log channel that pings admin roles. **The threshold never auto-jails** — it escalates to humans, who decide what happens next. `/warnings <user>` shows active and revoked warnings with dates, reasons, and the issuing mod. `/revokewarn` is a deliberate manual soft-delete — no timed expiry.

### Modinfo

`/modinfo <user>` produces one embed with current jail status, jail history, active warnings, total warnings issued, and ticket counts with the most recent ticket summary. It's the single tool that answers "what's the history with this person?" without an investigation across DMs.

### Auditing & DMs

Every state-changing action — jail, unjail, ticket open/close/reopen/delete, pull/remove, warn/revoke, claim, escalate, config change — writes an audit embed to the configured log channel and stores a record in the database. The member receives a DM at each major step (jailed, unjailed with transcript, ticket created/closed/reopened/deleted, warning issued). If a DM fails (member has DMs off), the bot posts a note in the relevant channel so the mod isn't left guessing.

### Restart recovery

After a restart, persistent ticket panel buttons and per-ticket Close / Reopen / Delete buttons re-attach to their stored messages, the expiry sweep catches up on any jails that lapsed while the bot was offline, and channel IDs are validated — manually deleted channels mark their record closed.

## Permissions

**Bot needs:** Manage Roles (to assign / strip / restore roles and create `@Jailed`), Manage Channels (jail and ticket channel creation, lock/unlock, permission overwrites), View Channels and Send Messages in the configured log channels, Read Message History and Embed Links for embeds, Attach Files for transcript delivery. The bot's top role must sit above `@Jailed` for role-strip to work.

**User needs:**
- Mod role (configured via `/setup` or `/config`): `/jail`, `/unjail`, `/ticket close|reopen|delete|claim|escalate`, `/pull`, `/remove`, `/warn`, `/warnings`, `/revokewarn`, `/modinfo`, `/ticket panel`, and the dashboard moderation routes.
- Admin role: `/setup`, `/config`, dashboard config writes. Admin roles are also the ones pinged on warning threshold and ticket escalation.
- Everyone: `/ticket open`, the panel button, and the "Open Ticket About This Message" menu.

## User-visible errors

| When | The user sees |
|---|---|
| Non-mod tries to close a ticket | "Only moderators can close tickets." |
| Bot lacks a required permission | A descriptive message identifying the missing permission, before any state changes |
| DM fails (recipient has DMs off) | Channel note: "(Couldn't DM {member} — they have DMs disabled.)" |
| Jail target is a bot, a mod, or already jailed | "Can't jail that user: {reason}" |
| Roles were deleted while a member was jailed | Restored roles are applied; the missing ones are noted in the audit entry |
| Jail or ticket category is full (Discord's 50-channel cap) | "That category is full — please archive a channel or pick a new category." |
| User tries to remove the primary user from their own ticket / jail | "You can't remove the {creator|jailed member} from their own channel." |
| Action runs in a channel that isn't a jail or ticket | "This command only works inside a jail or ticket channel." |
| `/ticket reopen` or `/ticket delete` on an open ticket | "This ticket isn't closed." |
| `/ticket escalate` runs twice | "This ticket has already been escalated." |
| `/revokewarn` warning_id not found | "I couldn't find that warning." |

## Non-goals

- **No auto-jail.** Warning thresholds never trigger jail automatically — they ping admins.
- **No auto-revoke for warnings.** Warnings only clear when a mod manually revokes them.
- **No appeal system.** There's no member-facing "appeal my jail" flow; the jail channel itself is the conversation.
- **No web-side jail release or ticket delete.** Both destructive actions happen only in Discord.
- **No per-user ticket cap.** A member can open as many tickets as they want.
- **No spectator visibility on tickets or jails.** Only the primary user, mods, pulled participants, and (after escalation) admins can see the channel.

## Configuration

Setup wizard sets most keys; the rest are tweaked with `/config set`.

| Key | Purpose | Default |
|---|---|---|
| `mod_roles` | Roles granting moderator access | unset (set by wizard) |
| `admin_roles` | Senior staff roles — receive escalation pings and warning threshold alerts | unset (set by wizard) |
| `jail_category_id` | Category where jail channels are created | unset (set by wizard) |
| `ticket_category_id` | Category where ticket channels are created | unset (set by wizard) |
| `log_channel_id` | Channel for audit log embeds | unset (set by wizard) |
| `transcript_channel_id` | Channel where transcript files are posted | unset (set by wizard) |
| `jailed_role_id` | The `@Jailed` role | auto-created |
| `ticket_panel_channel_id` / `ticket_panel_message_id` | Where the persistent ticket button lives | set by `/ticket panel` |
| `ticket_notify_on_create` | DM all mods on every new ticket | on |
| `warning_threshold` | Active-warning count that triggers an admin alert | 3 |
| `api_port` / `api_secret` | Dashboard API port and shared secret | platform defaults |

## Stored data

The system keeps per-guild records of every jail, ticket, warning, audit action, and transcript. Jail records carry the snapshotted role list so an unjail can restore the member exactly as they were. Tickets track creator, opener source, current state, claim assignment, and (after escalation) the escalating mod and admin claimer. Warnings store the issuing mod, reason, and active/revoked status; revocation is soft so history stays intact for `/modinfo`. Transcripts are stored as JSON for the dashboard and also written as files in the transcript channel and the member's DMs.

Audit entries cover every state change (jail/unjail, ticket lifecycle, pull/remove, warn/revoke, claim, escalate, config update) with actor, target, and contextual details. The audit log is the system of record for "what happened and who did it."

No DM content is persisted beyond what's already in the channel the DM was sent from; transcript files attached to DMs are the same files written to the transcript channel and database.

See also: [[dm-perms-spec]], [[whisper-spec]], [[confessions-spec]] for adjacent moderator surfaces that share the same `/api/moderation/` namespace.
