# Whisper — Feature Spec

An anonymous-message-with-guessing game. Members opt in to a per-guild role, then send anonymous DMs to other opted-in members. The recipient sees the message immediately and gets **three guesses** to identify the sender. A public feed channel hosts a persistent launcher (Send / My Inbox / My Sent) and announces whispers without spoiling content. Whispers can be replied to once, shared to the feed, deleted by the recipient, or — once correctly guessed — exposed.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/whisper send` | Slash | Whisper-role member (server only) | Opens the recipient picker (same as the launcher's Send button) |
| `/whisper sent` | Slash | Everyone (server only) | Ephemeral list of your active sent whispers |
| `/whisper optin` | Slash | Everyone (server only) | Consent embed; on Confirm grants the Whisper role |
| `/whisper optout` | Slash | Everyone (server only) | Removes the Whisper role; existing whispers are preserved |
| `/whisper forget-me` | Slash | Everyone (server only) | Two-step confirm; deletes all whispers + replies where you are sender or target |
| **Send Whisper** button | Persistent launcher | Whisper-role member | Opens the recipient picker |
| **My Inbox** button | Persistent launcher | Everyone | Ephemeral inbox of received whispers |
| **My Sent** button | Persistent launcher | Everyone | Ephemeral list of your sent whispers (active only) |
| Whisper audit log | Web (dashboard) | Admin | Per-guild log of every whisper with report counts |
| Whisper config | Web (dashboard) | Game host | Set the feed channel, opt-in role, and optional mod-log channel |

There is no one-shot `/whisper <target> <message>` — send is always picker + compose modal so the bot can pre-validate role membership.

## Behavior

### Persistent launcher
A single launcher message sits at the bottom of the feed channel. Any non-bot message in that channel bumps it: the previous launcher is deleted and a fresh one is posted. Concurrent bumps coalesce so a busy channel produces at most one delete-and-repost cycle at a time. Launcher buttons keep working across bot restarts.

### Sending a whisper
The send picker lists every opted-in member except the invoker, sorted by display name, paginated 25 at a time, with a filter modal for searching by name. Selecting a member opens a compose modal accepting up to **1000 characters**.

On submit:
- **Per-sender cooldown**: 30 seconds between any two sends.
- **Per-target hourly cap**: 5 whispers to the same recipient in a rolling hour.
- Timed-out targets are refused.
- The recipient is DM'd the content with Guess / Share / Reply / Delete buttons; if the DM fails (closed DMs), nothing is persisted.
- A best-effort announcement is posted to the feed: "Someone sent {target.mention} an anonymous message." — mention only, no content.

### Guessing
The target gets **three guesses**. The guess picker lists every opted-in member except the target, same paginated + filterable shape as the send picker. Guess consumption is atomic — two clicks racing on the same whisper can both pass pre-checks but only one will succeed; the other sees "This whisper was solved by another tab."

- **Correct**: the target sees "You solved it!"; a feed message announces "✅ {target} solved the whisper!" with an **Expose** button.
- **Wrong, with guesses left**: "Wrong! N guesses left."
- **Wrong, last guess**: the DM's Guess button is removed; the whisper remains active for Share / Reply / Delete.

### Sharing, replying, deleting, exposing
- **Share** (target only, before solved): the no-content feed post is replaced with one showing the full content (codefence escapes neutralised so user content can't break formatting). The DM keeps the Guess + Reply buttons if still applicable.
- **Reply** (sender or target, **one reply per whisper**): a modal collects up to 1000 characters; the other party is DM'd. The reply DM carries a Report button. If the recipient's DMs are closed the reply is rolled back and the writer sees "Couldn't deliver — they have DMs disabled."
- **Delete** (target only): soft-delete — the whisper disappears from the target's inbox but remains in the sender's sent list. Idempotent.
- **Expose** (target only, after correct guess): edits the target's DM to append `💥 Sender: @<sender>`.

### Inboxes
- **My Inbox** shows received whispers in pending or shared state (soft-deleted ones are hidden). Per-row actions: Guess (if eligible) + Share (if pending) + Reply + Report + Delete.
- **My Sent** shows your active sent whispers (excludes exposed, out-of-guesses, and 30-day age-locked rows). Per-row actions: Reply + Delete.

### Lifetime and locking
Whispers age-lock after **30 days** — no new guesses, no new replies. The row remains in the DB; only the inbox surfaces filter it out.

### Reporting
Recipients can report a whisper; reply recipients can report a reply. The reason field is free-form and optional (defaults to "(no reason provided)"). One report per reporter per item — second-clicks see "You've already reported this whisper." Reports always persist regardless of whether a mod-log channel is configured; the dashboard's audit log is the canonical view.

### Mod audit
The dashboard's audit log lists every whisper with its state, report count, sender, and target. Filters by state and reported-only. If a mod-log channel is set, every send, accept, reply, and report also fans out there as an embed; failures to post are logged and don't block the user action.

### Opt-out and forget-me
`/whisper optout` removes the role only — sent and received whispers stay intact. `/whisper forget-me` is a destructive nuke that requires a two-step confirm and deletes every whisper, reply, guess, and report where you are either party.

## Permissions

- The bot needs **Send Messages** + **Embed Links** in the feed channel, **Manage Messages** to bump the launcher, **Manage Roles** for opt-in / opt-out (with the bot's role above the Whisper role), and the ability to DM each target (Discord-side; not bot-grantable — closed DMs roll back the send).
- Slash commands have no Discord-side permission gate; they reject DMs and check role membership in-app.
- The Guess, Share, Delete, Expose, and Report buttons are **target-only**. Reply is sender-or-target. Report Reply is reply-recipient-only.
- Dashboard config requires the **game-host** role; the audit log requires **admin**.

## User-visible errors

| When | The user sees |
|---|---|
| Feed channel or opt-in role not configured | "Whisper isn't set up yet — ask an admin." |
| Opt-in role missing from the guild | "Whisper role no longer exists. Ask an admin to fix the config." |
| Invoker lacks the Whisper role | "You need the Whisper role first. Use `/whisper optin` to join." |
| No other opted-in members | "No other opted-in members to whisper to yet." |
| 30-second sender cooldown | "Slow down — wait Ns before sending another whisper." |
| Hourly per-target cap reached | "You've sent 5 whispers to that user in the last hour. Try again later." |
| Target is currently timed out | "Can't whisper a member who's currently timed out." |
| Recipient has DMs closed | "I couldn't deliver — they have DMs disabled." |
| Guess race lost | "This whisper was solved by another tab." |
| Wrong guess, with remaining | "Wrong! N guesses left." |
| Duplicate report | "You've already reported this whisper." |
| Duplicate reply report | "You've already reported this reply." |
| Opt-in confirm, bot can't assign role | "I don't have permission to assign that role." |
| Slash invoked in DMs | "This command can only be used in a server." |

## Non-goals

- **No second reply.** One reply per whisper, full stop.
- **No retraction.** Once sent, the sender can't un-send a single whisper — only `/whisper forget-me` to nuke their entire history.
- **No attachments or embeds.** Whispers and replies are text only.
- **No leaderboard or stats.** The audit log is forensic, not gamified.
- **No server-side blocklist.** A target who doesn't want whispers must opt out.
- **No one-shot slash form** that takes target + message directly.

## Configuration

Per-guild keys an admin sets via the dashboard:

- **Whisper opt-in role** — the role gating both send and receive. Required.
- **Feed channel** — where the launcher and announcements live. Required.
- **Mod-log channel** — optional; when set, sends/replies/reports also fan out here as embeds. Reports persist to the audit log regardless.

The launcher's current message id is bot-managed and not user-editable.

## Stored data

Per guild: every whisper (sender, target, content, state, guess count, timestamps, soft-delete flag), every guess attempt, every reply, and every report on a whisper or a reply. Sender and target ids are stored in plaintext — anonymity is a UI affordance, not a cryptographic property, and admins can see the full sender via the audit log.

Soft-delete preserves the row so the sender's history stays intact while the target's inbox hides it. `/whisper forget-me` is the only path that actually removes rows; it cascades through every whisper / reply / guess / report involving the user.

In-memory only and reset on restart: per-sender cooldown timestamps, per-target rolling counts, per-guild launcher locks. A restart resets these — by design, since a sender who restarts the bot gets at most one freebie.
