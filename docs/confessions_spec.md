# Confessions — Feature Spec

Anonymous confession box with a persistent **Confess** launcher button at the bottom of a configured channel. Submitters open a modal; the bot reposts the text into a destination channel (or a forum thread) and seeds it with an anonymous-reply button bar. Replies are themselves anonymous — each replier gets a stable name + color per thread, or a fresh "someone new" identity on demand. A guild can optionally put submissions behind **moderator approval**, in which case a confession waits on the mods' todo board until one of them approves it. Every confession and reply is recorded, with its real author, in the admin-gated **Confessions Audit Log** panel on the dashboard. A Discord mod-log channel can additionally mirror them, but it is optional and off by default — anyone who can read that channel can de-anonymise every confession in it, whereas the panel is admin-only.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/confess` | Slash | Everyone (any channel) | Open the confession modal (long-form text + notify-pref input) |
| **Confess** (launcher button) | Persistent button | Everyone | Open the same confession modal as `/confess` |
| **🎭 Reply Anonymously** | Persistent button | Everyone | Open the reply modal with the user's **stable** identity for this thread |
| **🎲 Reply as Someone New** | Persistent button | Everyone | Open the reply modal with a fresh **ephemeral** identity (not stored) |
| **❓ What's this?** | Persistent button | Everyone | Ephemeral help text comparing the two reply modes |
| Confessions config | Web | Admin reads; Game host writes | Edit destination channel, optional mod-log channel, cooldown, character cap, panic / replies / approval flags, per-day limit |
| **🕵️ Confessions** (todo-board button) | Persistent button | Moderator | Open the approval queue: an ephemeral pick-one select, then an Approve / Reject card |
| Confessions block list | Web | Admin | Add / remove per-guild blocklist entries |
| Launcher placement | Web | Game host | Post / move the launcher button to a specified channel |
| Confessions audit log | Web | Admin | Every confession and anonymous reply with its real author, joined with archived bodies for moderator review |

Bot-side perms required in the destination channel: **Send Messages**, **Embed Links** (for the log embed), **Create Public Threads** (text-channel destination — the bot creates a thread for the reply bar) or **Send Messages in Threads** (forum destination). All modals reject DMs implicitly.

## Behavior

### Submitting a confession

The modal accepts the body plus a notify-pref textbox (yes / no / unset). On submit, the bot rejects when:

- the guild has no confessions config row,
- panic mode is on,
- the submitter is on the per-guild block list,
- the body is empty after trimming,
- the body exceeds the per-guild character cap,
- the submitter is still inside the cooldown,
- or the submitter has reached the per-day limit.

When **mod approval** is on (below), a successful submission is instead written to `confession_pending` and the member is told it is waiting for review; nothing is posted, no audit row is written and no quest fires until a moderator approves it. Otherwise, and in the approval path once a mod says yes, the bot posts the body to the destination channel. For a forum destination it creates a new thread (using the first available tag if the forum requires one); for a text channel it sends the message then creates a thread on it for the reply bar. The reply button bar (the three buttons above) is posted into the thread. An audit row is written to `anon_audit_log`, and if a mod log channel is configured it also receives a mirror embed. The launcher button is re-pinned to the bottom of the launcher channel.

### Moderator approval

Off by default. With **require approval** on, every confession is held — there is
no exemption by account age, role or content, so there is no rule that can fail
open on the one submission that needed catching. The queue is worked from the
mods' sticky todo board, which gains a **🕵️ Confessions to approve** section and a
fifth button; the section shows each waiting confession's body, clipped, and how
long it has waited.

**The approver is never shown the author.** `pending_confessions` does not select
an author id, so no listing, board row, select option or review card can print
one even by accident. Putting a real name to an anonymous post stays one act, in
one place: the admin-gated Confessions Audit Log. That matters because the board's
gate is the *moderator* tier, which is a wider circle than admin — approving must
not become a second, wider way to de-anonymise. The rejection DM likewise names no
moderator; the mod team answers as a team.

**Panic mode and the block list cover this path too.** Approving is the one
confession surface that posts on a delay, so both guards are re-checked at
approval, not just at submission: with panic on nothing can be approved (the row
stays put, and rejecting still works — clearing a backlog is not posting), and a
member added to the block list after submitting has their queued confession
refused rather than posted, leaving the reject call to the moderator.

Approve claims the row and deletes it in a single immediate transaction, so two
moderators pressing Approve together cannot post the same confession twice; the
second is told it has already been handled. The confession then publishes exactly
as an unqueued one would — same embed, thread, audit row, mod-log mirror, launcher
re-pin — and the quest trigger fires **here**, on approval, so a confession that
never posts never pays. If the post fails — a permission gone since submission, or
anything at all raising out of the publish — the claimed row is put back rather
than lost, **keeping its original timestamp**: the seven-day sweep is a promise
to the member, and a row that restamped itself on every retry would outlive it
for as long as the failure lasted. Keeping the timestamp also keeps the
oldest-first queue honest.

Reject opens a modal with an optional reason and DMs the author a branded embed
saying their confession wasn't posted, quoting the reason if one was given. The
card reports whether that DM actually landed — a member with DMs closed is told
nothing at all, and a moderator assured otherwise has no reason to follow up.
Approval is not announced: the confession simply appears, which the member can
already see. A rejection does **not** refund the member's daily slot or reset
their cooldown — refunding would invite resubmit-until-approved.

A pending row is swept after **seven days** and its author DMed a distinct
"nobody reviewed it in time" message — worded as its own outcome, because nobody
judged it. Seven is not a tuning choice: the row holds a confession's text beside
its author's real id, and the privacy notice promises members that link
self-destructs after a week, so a queue the mods stop working must not become the
exception to it. An erasure request deletes any pending rows immediately, though it cannot
repaint the board — erasure runs out-of-band with no bot in hand — so a clipped
copy may stay rendered in the sticky embed until something else moves it. The
stored row is gone, and the Confessions button finds nothing.

There is deliberately **no dashboard queue** for this. Approving is a mod action
and mod actions live in Discord; a second approval surface would be a duplicated
control, and the board section's overflow line says "more waiting" rather than
pointing at a dashboard that doesn't have them.

### Reply identity model

Each confession thread maintains two shuffled pools: a **name pool** of 660 entries (20 adjectives × 33 animals — e.g. "Brave Aardvark") and a **color pool** of 22 unicode circles. Both pools are popped without replacement; when a pool is exhausted, it reshuffles and a cycle counter advances. Persistent and ephemeral replies share the same pools — once a color or name has been handed out in a cycle, neither path hands it out again until the pool refills.

- **🎭 Reply Anonymously** (persistent) — the user's identity for this thread is stored and stable across every reply they make in it. Older threads predating the pool system lazy-backfill from the original hash-based mapping so the identity stays visually consistent.
- **🎲 Reply as Someone New** (ephemeral) — a fresh name and circle are popped from the pools just for this reply. Nothing is stored against the user; subsequent ephemeral replies give different identities.
- **OP badge** — the original confessor's persistent replies are tagged with a **⭐ [OP]** marker instead of the name + circle. Ephemeral replies never get the OP badge, even from the original confessor — that's the point of the "someone new" button.

### Submitting a reply

Same set of guards as a confession, plus a check that replies are enabled in this guild. The reply cooldown is half the post cooldown with a 30-second floor. There is no per-day limit on replies. The reply is posted in the spawned Discord thread when known, otherwise as a Discord reply to the parent message. If the original confessor opted into reply notifications, the bot DMs them with jump links to the reply and the original confession; closed DMs and other DM failures are silent. That DM is a branded embed (`send_branded_dm` — guild accent, name and icon in the footer) and keeps `AllowedMentions.none()`, since the body carries member-authored text.

`@everyone` and `@here` in any confession or reply body are defanged before posting. Bodies are hard-truncated to 2 000 characters after the identity prefix.

### Launcher button maintenance

The launcher gets re-pinned to the bottom of the launcher channel after every confession, every reply, every non-bot message in the launcher channel, and any explicit dashboard re-post. A per-guild lock serialises the re-pin so concurrent activity doesn't spawn duplicates. Stale launcher buttons in the last 50 channel messages are swept after each post.

### What's this? button

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
| Body is empty after trim | "Confession/Reply can't be empty." |
| Body exceeds the character cap | Ephemeral with the computed cap (per-guild) |
| Inside cooldown | "Slow down — you can post/reply again in **{remaining}s**." |
| Per-day limit reached (confessions only) | "You've hit today's limit (**{N}**). Try again tomorrow." |
| Approval mode on, submission accepted | "Sent to the mods for review…" — not an error, but the one case where a successful submission does not appear |
| Submitter on the per-guild block list (confession path) | "You can't submit confessions on this server." |
| Submitter on the per-guild block list (reply path) | "You can't submit anonymous replies on this server." |
| Replies disabled by config | Ephemeral: replies-disabled message |
| Destination channel rejects the post (perms) | "Failed to post confession/reply (missing perms?)." |
| Reply parent message gone | "That message no longer exists." |
| Reply thread locked | "This confession thread is locked." |
| Generic button interaction error | "Something went wrong handling that {action}." |
| Bot lacks access to act on a button | "I don't have enough access to handle that action." |
| Slash command raises | "An unexpected error occurred. Please try again." |

Stale-interaction races (Discord internal-defer collisions) silently no-op — the user's click is treated as already handled.

## Non-goals

- **No anonymous DMs to the bot.** Every entry point requires a guild context.
- **No author edit or delete.** Once posted, only mods can remove a confession via Discord directly; the bot offers no command for that.
- **No separate identities for replies-to-replies.** Replying to a reply inherits the root thread's identity pool, so the same person keeps the same name and color throughout.
- **No backfill for deleted spawned threads.** If the thread was deleted manually, the reply button still works but posts as a direct Discord reply in the destination channel.
- **No web-side authoring.** The dashboard configures the feature; submission is Discord-only.
- **No dashboard approval queue.** Approving is a mod action, and it happens on the todo board only.
- **No rejected-confession archive.** A rejected body is deleted, not kept for later review.
- **No partial approval.** No trusted-member bypass, account-age threshold or word-filter trigger; it is on for everyone or off.
- **No per-channel destination override.** One destination channel per guild.
- **No attachment support today.** Text bodies only.

## Configuration

Per-guild settings, editable from the dashboard:

- **Destination channel** — text channel or forum channel where confessions are reposted.
- **Log channel** — *optional*, disabled by default. A mod-only Discord mirror
  of the audit trail; logging is best-effort and won't block a post. Confessions
  works fully without one — the audit panel is the moderation view, and unlike a
  channel it cannot be read by anyone below admin.
- **Post cooldown** — seconds between confessions per user (default 120).
- **Reply cooldown** — derived as half the post cooldown, floor 30 s; not directly configurable.
- **Character cap** — per-body cap (default 2000, clamped to Discord's actual limit).
- **Require approval** — hold every new confession for a moderator instead of
  posting it. Off by default. All-or-nothing; there is no partial mode.
- **Panic mode** — kill switch; every modal short-circuits when on.
- **Replies enabled** — disables the three reply buttons globally when off.
- **Notify-OP-on-reply default** — default value of the notify-pref textbox in the confession modal.
- **Per-day limit** — UTC-day cap on new confessions per user; 0 means unlimited.
- **Launcher channel** — where the persistent **Confess** button lives.
- **Block list** — per-guild list of user ids barred from posting confessions and replies, managed via dashboard.

## Stored data

Per-guild: a config row (settings + block list), the per-user rate-limit row (last-confess and last-reply timestamps plus the UTC-day key and counter), thread metadata for every bot-posted message (root or reply, with the real author id kept internal and the spawned Discord thread id), persistent identity assignments keyed by (guild, root message, user), and the shuffled identity pools (name and color) keyed by (guild, root message). Thread metadata is auto-purged after seven days. No DM data is ever stored.

With approval on, a waiting submission additionally stores its **body** and the
author's real id in `confession_pending` — the only place the bot holds a
confession's text itself, since `anon_audit_log` stores no content and recovers
it by joining the general `messages` table. The row is deleted on approve, on
reject, on erasure, and by the seven-day sweep; nothing about a rejected
confession is retained anywhere.

The moderator-facing audit trail is kept **separately**, as `anon_audit_log`
rows under the `confessions` feature slug (one per confession and per reply,
carrying the real author id, the message pointer and the root message id — but
never the body; see migration 145). The two lifetimes are deliberately
independent: seven days is the operational TTL that bounds thread identity and
reply routing, not a sensible retention policy for a moderation record. Audit
rows expire on the guild-wide anon-audit retention window (default 90 days,
`0` to keep forever), configured on the Anonymous Features panel.

Ephemeral identity replies pop the shared pools but never write an assignment row, by design — that's what makes them ephemeral. Launcher state lives in memory as per-guild locks; pool state lives in the database so identities survive bot restarts.
