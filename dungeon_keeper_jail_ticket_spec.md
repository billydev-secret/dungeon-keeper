# Dungeon Keeper — Jail & Ticket System Module Spec

   **Bot:** Dungeon Keeper (Discord Management Bot)
**Stack:** Python 3.11+ / discord.py 2.x / aiosqlite / aiohttp (REST API)

---

## 1. Design Philosophy

This system manages two of the most emotionally charged interactions in a community: being disciplined and asking for help. Every design decision should serve two goals:

1. **The member should feel heard, not processed.** A jailed user who feels dehumanized will leave or escalate. A member opening a ticket who feels ignored will stop trusting staff. Every touchpoint — the embed wording, the DM tone, the channel name — should communicate "we take you seriously."

2. **The mod should feel empowered, not burdened.** Moderation is volunteer labor. Every extra click, every ambiguous state, every "wait, who was handling this?" is friction that burns people out. The system should make the right action the easy action.

**Key UX principles:**

- **No silent failures.** If something goes wrong (DMs blocked, role deleted, permissions missing), the mod sees a clear message — never a mystery.
- **Reversibility over confirmation dialogs.** The two-step close → delete flow for tickets exists because "are you sure?" modals train people to click "yes" without reading. Instead, we let them undo.
- **Context travels with the conversation.** Transcripts, `/modinfo`, and audit logs exist so no mod ever has to ask "what happened before I got here?"
- **Human decisions stay human.** Warnings don't auto-jail. Threshold alerts notify admins but don't act. Revoking a warning is a conscious choice, not a timer.

---

## 2. Setup & Configuration

### 2.1 First-Run Setup Wizard

**Command:** `/setup`  (Admin-only)

On first use (or if core config is missing), the bot walks the admin through an interactive wizard using embeds and buttons/selects:

1. **Mod roles** — "Which roles should have moderator access?" → Role select menu
2. **Admin roles** — "Which roles are senior staff? (used for escalations and warning alerts)" → Role select menu
3. **Jail category** — "Where should jail channels be created?" → Option to select existing category or let the bot create one
4. **Ticket category** — "Where should ticket channels be created?" → Same as above
5. **Log channel** — "Where should audit logs go?" → Channel select (uses existing mod-log)
6. **Transcript channel** — "Where should transcripts be posted?" → Channel select, with a "Same as log channel" shortcut
7. **Confirmation** — Summary of all choices with a "Looks good!" button

The wizard creates the `@Jailed` role automatically with correct server-wide deny overrides. The bot also verifies it has the permissions it needs and warns the admin if anything is missing.

**After setup**, individual settings can be tweaked with `/config set <key> <value>`, `/config get <key>`, and `/config list`.

### 2.2 Config Keys

| Key | Description | Set by wizard? |
|-----|-------------|:-:|
| `mod_roles` | Roles that grant moderator access | ✓ |
| `admin_roles` | Senior staff roles (escalation + warning alerts) | ✓ |
| `jail_category_id` | Category for jail channels | ✓ |
| `ticket_category_id` | Category for ticket channels | ✓ |
| `log_channel_id` | Channel for audit log embeds | ✓ |
| `transcript_channel_id` | Channel for transcript file posts | ✓ |
| `jailed_role_id` | The @Jailed role ID | Auto |
| `ticket_panel_channel_id` | Channel containing the ticket panel embed | Set by `/ticket panel` |
| `ticket_panel_message_id` | Message ID of the panel embed | Auto |
| `ticket_notify_on_create` | Whether to DM all mods with mod roles when a new ticket opens (default: true) | `/config` |
| `warning_threshold` | Active warning count that triggers an admin alert (default: 3) | `/config` |
| `api_port` | Port for REST API server (default: 8080) | `/config` |
| `api_secret` | Shared secret for API authentication | `/config` |

### 2.3 Discord Structure

**Roles:**
- `@Jailed` — Server-wide Deny View Channel and Send Messages. Auto-created by setup. Must sit below the bot's highest role.

**Categories (separate):**
- Jail Category — `@everyone` denied view.
- Ticket Category — `@everyone` denied view.

**Channels:**
- Ticket Panel Channel — Where the bot posts the persistent "Open Ticket" button.
- Log Channel — Existing mod-log, shared for all audit entries.
- Transcript Log Channel — Where transcript files land. Can be the same as log channel.

---

## 3. Jail System

*A jailed member should understand what's happening, why, and what comes next. The jail channel is not a punishment box — it's a structured conversation space where resolution can happen.*

### 3.1 Commands

| Command | Parameters | Access |
|---------|-----------|--------|
| `/jail <user> [duration] [reason]` | `user`: Member (required), `duration`: e.g. "24h", "7d" (optional), `reason`: String (optional) | Mod-only |
| `/unjail <user> [reason]` | `user`: Member (required), `reason`: String (optional) | Mod-only |

Also available as a **User Context Menu**: "Jail User" → opens a modal for duration and reason.

**Duration format:** `30m`, `2h`, `1d`, `7d`, `1w`. No value = indefinite.

### 3.2 Jail Flow

**What the mod does:** Right-clicks a user (or types `/jail`), optionally sets a duration and reason.

**What happens behind the scenes:**

1. Validate: target is not a bot, not already jailed, not a mod.
2. Snapshot all the user's current roles and store them in the database.
3. Strip all roles. Assign `@Jailed`.
4. Create a private channel: `jail-{username}-{timestamp}` (e.g. `jail-ben-0410-1423`). Permissions:
   - `@everyone`: Deny View, Deny Send
   - `@Jailed` role: Deny View (isolates jailed users from each other)
   - Target user: Allow View, Send, Read History, Attach Files
   - Each mod role: Allow View, Send, Read History, Manage Messages
   - Dungeon Keeper: Full access
5. Post a jail embed in the channel. Write audit log entry.

**What the member experiences:**

- They receive a DM: an embed explaining they've been jailed, by whom, why, and for how long. The tone should be firm but not hostile — "You've been placed in a moderation hold" not "You have been JAILED."
- They can see only their jail channel. They can speak there. The embed tells them a moderator will review their case.
- *Psychology: the private channel signals "we want to talk to you" rather than "we're ignoring you." This reduces escalation.*

**What the mod sees:** An audit embed in the log channel confirming the action.

### 3.3 Unjail Flow

**What happens:**

1. Restore all stored roles. If any roles were deleted in the meantime, log which ones and continue.
2. Remove `@Jailed`.
3. Generate a transcript of the jail channel (see §7).
4. Post transcript to the transcript log channel. DM transcript to the user.
5. Delete the jail channel.
6. Update DB record. Post audit embed.

**What the member experiences:** A DM explaining they've been released, with the full transcript attached. Their roles are back. They can see the server again.

*Psychology: delivering the transcript signals transparency — "here's the record of what happened." It also protects mods from "you never told me that" disputes.*

### 3.4 Auto-Expiry

A background task checks every 60 seconds for jails past their expiry. Expired jails run through the full unjail flow with reason "Jail duration expired."

*This exists so mods don't have to set reminders. A 24-hour jail at 11pm on Friday actually ends Saturday at 11pm, even if no mod is online.*

### 3.5 Rejoin Detection

If a jailed user leaves and rejoins, the bot re-strips roles, re-assigns `@Jailed`, and re-grants access to their existing jail channel. A note is posted in the channel.

*This prevents the "leave and rejoin to escape jail" exploit. The member sees continuity — the conversation picks up where it left off.*

---

## 4. Ticket System

*A member opening a ticket is asking for help. The experience should feel like walking up to a front desk — acknowledged, directed, and taken seriously — not like shouting into a void.*

### 4.1 Ticket Panel

**Command:** `/ticket panel <channel>` (Mod-only)

Posts a persistent embed with a "📩 Open Ticket" button (green). The embed should explain what tickets are for and set expectations ("A moderator will respond as soon as possible").

### 4.2 Opening a Ticket

| Trigger | Behavior |
|---------|----------|
| **Panel button** | Modal asks for a brief description → ticket created |
| **Slash command** `/ticket open [description]` | Ticket created directly |
| **Message context menu** "Open Ticket About This Message" | Modal asks for description; source message is linked in the ticket |

**What happens:**

1. Create a private channel: `ticket-{username}-{timestamp}`. Permissions mirror the jail channel pattern (creator + mod roles + bot).
2. Post a ticket embed: Opened By, Source, Description, Created At. If from context menu, include a jump link to the source message. Include a "🔒 Close Ticket" button (red).
3. DM the member confirming their ticket was created.
4. **DM all users with configured mod roles** that a new ticket has been opened, with a jump link to the ticket channel. (Configurable via `ticket_notify_on_create`.)
5. Write DB record. Post audit embed to log channel.

*Psychology: the DM confirmation tells the member "your request was received." The mod DM notification means no ticket sits unseen. The jump link takes the mod directly to the conversation — zero friction to first response.*

### 4.3 Ticket States

Tickets have three states: **Open → Closed → Deleted.** This is the most important UX decision in the system.

**Why not just close-and-delete?** Because mistakes happen. A mod closes a ticket thinking the issue is resolved, but the member replies "wait, one more thing." With immediate deletion, that message is gone and the mod has to ask the member to open a new ticket. With the intermediate state, the mod just clicks Reopen.

### 4.4 Closing a Ticket

**Trigger:** "Close Ticket" button OR `/ticket close [reason]`

1. Mod-only. Non-mods get an ephemeral "Only moderators can close tickets."
2. Confirmation modal with an optional reason field.
3. **Lock the channel** — the ticket creator can still *see* the conversation but can no longer *send* messages. Pulled users also lose Send.
4. Update the ticket embed: show "🔒 Closed" status, who closed it, reason, timestamp.
5. Replace the Close button with two new buttons:
   - "🔓 Reopen" (green)
   - "🗑️ Delete" (red)
6. Post a message: "This ticket has been closed by {moderator}."
7. DM the member that their ticket was closed and that they can still view the channel.
8. Update DB. Post audit embed.

*The member can still read the conversation. This matters — it lets them reference what was discussed without having to search DMs for a transcript.*

### 4.5 Reopening a Ticket

**Trigger:** "Reopen" button OR `/ticket reopen`

1. Restore Send permission for the creator (and any pulled users).
2. Swap buttons back to "Close Ticket."
3. Post a message: "This ticket has been reopened by {moderator}."
4. DM the member that their ticket was reopened.
5. Update DB. Log audit entry.

### 4.6 Deleting a Ticket

**Trigger:** "Delete" button OR `/ticket delete` (only available when ticket is closed)

This is permanent. This is where the transcript is generated.

1. Final confirmation with Confirm/Cancel buttons.
2. Generate transcript (see §7). Store in DB.
3. Post transcript file + summary to transcript log channel.
4. DM transcript file to the ticket creator.
5. Delete the channel.
6. Update DB. Post audit embed.

### 4.7 Ticket Commands Summary

| Command | Parameters | Access |
|---------|-----------|--------|
| `/ticket panel <channel>` | `channel`: TextChannel | Mod-only |
| `/ticket open [description]` | `description`: String (optional) | Everyone |
| `/ticket close [reason]` | `reason`: String (optional). Inside a ticket channel. | Mod-only |
| `/ticket reopen` | None. Inside a closed ticket channel. | Mod-only |
| `/ticket delete` | None. Inside a closed ticket channel. | Mod-only |

No limit on concurrent open tickets per user.

---

## 5. Context Menu Commands

| Menu Type | Name | Behavior |
|-----------|------|----------|
| User context menu | "Jail User" | Modal for duration + reason → jail flow |
| Message context menu | "Open Ticket About This Message" | Modal for description → ticket with jump link to source message |

*Context menus matter because they meet the mod where they already are. Spotting a problem in chat → right-click → act. No switching channels, no remembering command syntax.*

---

## 6. Pull & Remove

*Sometimes a jail conversation or ticket needs another person's input — a witness, another mod, or someone involved in the situation. Pull brings them in. Remove takes them out when the sensitive part of the conversation begins.*

| Command | Parameters | Access |
|---------|-----------|--------|
| `/pull <user>` | `user`: Member (required) | Mod-only |
| `/remove <user>` | `user`: Member (required) | Mod-only |

Both must be run inside a jail or ticket channel.

**Pull flow:** Grants the user View, Send, Read History, Attach Files on the channel. Posts a notification: "{user} has been added by {moderator}." They appear in the transcript's participant list.

**Remove flow:** Revokes the user's permission overwrite. Posts a notification: "{user} has been removed by {moderator}." Cannot remove the primary user (the jailed member or the ticket creator).

Pulled users are observers/participants — they do **not** get the `@Jailed` role or have roles stripped.

---

## 7. Transcripts

*Transcripts are the institutional memory of the moderation team. They exist so that any mod, at any future point, can understand exactly what happened — not a summary, not a retelling, but the actual conversation.*

Transcripts are generated when a jail channel is closed (unjail) or when a ticket is **deleted** (not just closed).

**What they capture:**
- Metadata: type (jail/ticket), record ID, guild info, channel name, timestamps, participant list
- Every message in chronological order: author, content, embeds, attachments (filename + URL), timestamp
- Summary: message count, duration, moderator, reason, outcome

**Where they go:**
1. Stored in the database as JSON (for the web dashboard).
2. Posted to the transcript log channel as a `.json` file with a summary embed.
3. DM'd to the jailed user or ticket creator as a file attachment.

---

## 8. Warning System

*Warnings are a paper trail. They let the mod team build a shared picture of a member's behavior over time, so that when a difficult decision needs to be made, it's based on documented history — not gut feeling or whoever happens to be online.*

### 8.1 Commands

| Command | Parameters | Access |
|---------|-----------|--------|
| `/warn <user> [reason]` | `user`: Member (required), `reason`: String (optional) | Mod-only |
| `/warnings <user>` | `user`: Member (required) | Mod-only |
| `/revokewarn <user> <warning_id>` | `user`: Member (required), `warning_id`: Integer (required) | Mod-only |

### 8.2 Warn Flow

1. Create a warning record in the database.
2. DM the member: moderator, reason, their current active warning count.
3. Post warning audit embed to log channel.
4. Count the user's active (non-revoked) warnings.
5. If the count reaches the configured threshold (default: 3), post a **highlighted alert** in the log channel that pings admin roles. The alert includes the user, their warning history, and a note that the threshold has been reached.

**This does not auto-jail.** It escalates to humans. The admins decide what to do next.

*Psychology: automated consequences feel arbitrary to the person receiving them. A human reviewing the situation and making a call — even if the outcome is the same — preserves the sense that someone actually looked at what happened.*

### 8.3 Warning Details

- `/warnings <user>` shows all warnings (active and revoked) with status indicators, dates, reasons, and issuing moderator.
- `/revokewarn` marks a warning as revoked (soft delete). The mod can provide a reason. *This is intentionally a manual action — if someone has genuinely changed, a mod recognizes that by revoking, not a timer.*
- The threshold alert fires only when the count **reaches** the threshold, not on every warning above it.

---

## 9. Mod Info

*One command, full picture. No more asking around "has anyone dealt with this person before?"*

| Command | Parameters | Access |
|---------|-----------|--------|
| `/modinfo <user>` | `user`: Member (required) | Mod-only |

Displays a comprehensive embed for the target user:

- **Jail status:** Active jail details or "Not currently jailed"
- **Jail history:** Past jails with most recent shown (date, duration, reason, outcome)
- **Active warnings:** List with dates, reasons, issuing moderators
- **Warning history:** Total issued (active + revoked)
- **Ticket history:** Open and closed ticket counts, most recent summary

*This is the single most important mod tool in the system. It turns "who is this person?" into an immediate answer instead of a 10-minute investigation across DMs and memory.*

---

## 10. Ticket Claiming

*Claiming answers the question "whose job is this?" Without it, tickets get the bystander effect — everyone assumes someone else is handling it.*

| Command | Parameters | Access |
|---------|-----------|--------|
| `/ticket claim` | None. Inside a ticket channel. | Mod-only |

**What claiming does:** Subscribes the mod to DM notifications for new activity in the ticket. When someone other than the claimer posts a message (after a 5-minute cooldown to avoid spam), the claimer gets a DM with a jump link.

**What claiming doesn't do:** It doesn't lock other mods out. Everyone can still see and respond. Claiming signals ownership and enables alerts — not exclusivity.

- One mod claims at a time. Another mod can reassign with a confirmation prompt.
- The claimer is recorded in the ticket embed and transcript summary.

---

## 11. Ticket Escalation

*Escalation exists because not every mod should have to handle every situation. Some issues need admin-level judgment — policy questions, sensitive interpersonal conflicts, edge cases. Escalation makes that handoff clean.*

| Command | Parameters | Access |
|---------|-----------|--------|
| `/ticket escalate [reason]` | `reason`: String (optional). Inside a ticket channel. | Mod-only |

**What happens:**

1. Admin roles gain visibility into the ticket channel.
2. The ticket embed updates to show "⚠️ Escalated" status.
3. Admin roles are pinged in the ticket channel.
4. Audit log entry.

**Admin claim (second-level):** After escalation, an admin can run `/ticket claim`. This creates a dual-notification setup — **both** the original mod claimer and the admin get DM alerts. The ticket embed shows both names.

- A ticket can only be escalated once.
- Both mods and admins can close escalated tickets.

---

## 12. Audit Logging

*Every action is logged to both the database and the mod-log channel. This isn't surveillance — it's accountability and continuity. When a mod picks up a situation mid-stream, the log tells them exactly where things stand.*

### Action Types

| Action | Actor | Target | Extra Details |
|--------|-------|--------|---------------|
| `jail_create` | Moderator | User | reason, duration, channel ID |
| `jail_release` | Moderator or Bot | User | reason, duration served |
| `jail_expire` | Bot | User | original duration |
| `ticket_open` | User | — | source, description |
| `ticket_close` | Moderator | User (creator) | reason |
| `ticket_reopen` | Moderator | User (creator) | ticket ID |
| `ticket_delete` | Moderator | User (creator) | ticket ID, message count |
| `channel_pull` | Moderator | Pulled user | channel type, record ID |
| `channel_remove` | Moderator | Removed user | channel type, record ID |
| `warning_issue` | Moderator | User | reason, warning count, threshold reached |
| `warning_revoke` | Moderator | User | reason, remaining count |
| `ticket_claim` | Moderator | — | ticket ID |
| `ticket_escalate` | Moderator | — | ticket ID, escalation role |
| `ticket_admin_claim` | Admin | — | ticket ID, original claimer |
| `config_update` | Moderator | — | key, old value, new value |

### Embed Colors

| Context | Color | Hex |
|---------|-------|-----|
| Jail actions | Red | `0xE74C3C` |
| Ticket actions | Blue | `0x3498DB` |
| Success (unjail, release) | Green | `0x2ECC71` |
| Audit/info | Gray | `0x95A5A6` |

*(Placeholder — replace with TGM server palette)*

---

## 13. DM Notifications

*DMs are how the system communicates with members who may not be able to see the relevant channels. Every DM should be informative and warm — not robotic.*

| Event | DM to Member |
|-------|------------|
| Jailed | Embed: moderator, reason, duration, server name. Tone: "You've been placed in a moderation hold." |
| Unjailed | Embed: released by, reason, duration served + transcript attachment |
| Ticket created | Confirmation with channel jump link |
| Ticket closed | Notification that it's been closed, reason, and that they can still view the channel |
| Ticket reopened | Notification that it's been reopened |
| Ticket deleted | Summary embed + transcript attachment |
| Warning issued | Embed: moderator, reason, current warning count |

| Event | DM to Mod |
|-------|-----------|
| New ticket opened | Jump link to ticket channel (if `ticket_notify_on_create` is true) |
| Activity in claimed ticket | Jump link to new message (5-minute cooldown between notifications) |

All DMs wrapped in error handling. If a DM fails, post a note in the relevant channel so the mod knows the user didn't receive it.

---

## 14. REST API

An aiohttp server in the same process as the bot, authenticated via Bearer token (`api_secret`).

### Endpoints

| Method | Path | Description | Notes |
|--------|------|-------------|-------|
| `GET` | `/api/jails` | List jail records | Params: `guild_id` (required), `status`, `user_id`, `page`, `per_page` |
| `GET` | `/api/jails/{jail_id}` | Get single jail | Includes stored roles |
| `POST` | `/api/jails/{jail_id}/release` | Release a jailed user | Body: `reason`, `actor_id`. Triggers full unjail flow. |
| `GET` | `/api/tickets` | List ticket records | Params: `guild_id` (required), `status` (open/closed/deleted/all), `user_id`, `page`, `per_page` |
| `GET` | `/api/tickets/{ticket_id}` | Get single ticket | |
| `POST` | `/api/tickets/{ticket_id}/close` | Close (lock channel) | Body: `reason`, `actor_id` |
| `POST` | `/api/tickets/{ticket_id}/reopen` | Reopen | Body: `actor_id` |
| `POST` | `/api/tickets/{ticket_id}/delete` | Delete (permanent) | Body: `reason`, `actor_id`. Triggers transcript + channel delete. |
| `GET` | `/api/transcripts/{type}/{record_id}` | Get transcript | `type` is "jail" or "ticket" |
| `GET` | `/api/audit` | List audit entries | Params: `guild_id`, `action`, `actor_id`, `target_id`, `after`, `before`, `page`, `per_page` |
| `GET` | `/api/stats` | Dashboard stats | Params: `guild_id`, `period` (7d/30d/90d/all). Returns jail/ticket counts, averages, top moderators. |

All `POST` endpoints trigger full Discord-side flows — role changes, permissions, channel operations, transcripts, DMs, audit logs.

---

## 15. Bot Restart Recovery

1. **Re-attach ticket panel button** using stored message ID.
2. **Re-attach ticket channel buttons** — "Close" buttons on open tickets, "Reopen / Delete" buttons on closed tickets.
3. **Catch up on expired jails** — expiry task queries by timestamp, so missed expirations process immediately.
4. **Validate channels** — check all active jail/ticket channel IDs still exist. If manually deleted, mark record accordingly.

---

## 16. Edge Cases

| Scenario | Handling |
|----------|----------|
| User has DMs disabled | Continue flow, post note in channel so mod knows |
| Roles deleted while user jailed | Restore only valid roles, log which were missing |
| @Jailed role deleted | Re-create on next jail action |
| Category full (50 channels) | Notify mod, suggest archiving |
| User leaves while jailed | Re-jail on rejoin (§3.5) |
| Channel manually deleted | Caught on restart, mark record closed |
| Concurrent jail attempts | DB check + per-user lock |
| Bot lacks permissions | Descriptive error to mod before attempting |

---

## 17. Implementation Priority

1. Database schema & migrations
2. `/setup` wizard + `/config` commands
3. Jail core — `/jail`, `/unjail`, role management, channel creation, `@Jailed` role
4. Jail extras — context menu, auto-expiry, duration parsing, rejoin detection
5. Transcript system — collection, JSON formatting, DB storage, file posting
6. Ticket core — `/ticket open`, close/reopen/delete flows, panel embed + buttons
7. Ticket extras — context menu, persistent button views
8. `/pull` and `/remove`
9. DM notifications — all member and mod DMs
10. Audit logging — DB writes + log channel embeds
11. Warning system — `/warn`, `/warnings`, `/revokewarn`, threshold alerts
12. `/modinfo` — unified user history view
13. Ticket claiming — `/ticket claim`, DM subscriptions
14. Ticket escalation — `/ticket escalate`, admin claim
15. Restart recovery — view re-registration, channel validation, expiry catchup
16. REST API — all endpoints
17. Testing & edge case hardening
