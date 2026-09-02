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

The prompt and button name the *real* scope — a media scrub's button reads "Yes, delete my images & files" (or "their" when a mod acts on someone else). The prompt is also where retention is disclosed, on both commands: XP, activity, and profile stay exactly as they are, and the server keeps its own records of the messages for moderation. Since 2026-08-06 that line is honest about *what* is kept per guild: it reads the guild's `message_storage_level` and says "including the message text" when the guild retains content (`all`), or "as metadata (who, where, when — not the text)" at the default. The person is told that **before** the irreversible click rather than discovering it in the summary afterwards.

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

The purge implementation, `purge_user_data` (`src/bot_modules/services/privacy_service.py`), is deliberately **unwired** rather than deleted. It is the out-of-band path for a genuine legal erasure request (e.g. GDPR): an operator runs it manually against the database — the procedure lives in [gdpr_runbook.md](gdpr_runbook.md). It removes the user's rows from XP (including reaction awards), voice sessions, member activity, quality-score history, gender, member events, birthdays, bios (row + snapshotted answers/fields), voice-master profiles and trust lists (both sides), watch lists and invite edges (both sides), the interaction graph (both directions), wellness state and counters, usage telemetry (`usage_events` — see [[usage-telemetry-spec]]; nothing prunes that table on a schedule, so this run is the only thing that ever clears it), audit-event tables, and — via `economy_service.econ_purge_user` — all per-member economy and casino state (`econ_ledger` is deliberately preserved: it is the pseudonymous double-entry record, and deleting one side of transfers would break audit sums). With `keep_messages=False` it also drops the `messages` archive and its children (attachments, mentions, embeds, reactions, sentiment, and the per-user dedup table), chunked in batches of 500 ids so a heavy poster can never blow SQLite's bound-variable cap mid-erasure. Every per-table delete tolerates schema drift (guild deployments vary by age) — a failed table logs a warning and the rest of the purge proceeds; the caller owns the transaction, so a hard failure rolls the whole erasure back rather than leaving partial state.

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

## Subject access export

`export_user_data` (`src/bot_modules/services/privacy_service.py`) is the read
half of the erasure path — the answer to an access request (GDPR Art 15) or a
portability request (Art 20). Like the purge it is **unwired from any command**
and run by an operator: `scripts/export_user_data.py --guild <gid> --user <uid>`,
with `--summary` for a table/row-count view that discloses no content. The
procedure is in [gdpr_runbook.md](gdpr_runbook.md) §1.

**It finds tables by column discovery, not from a curated list.** Every table in
`sqlite_master` whose columns intersect `SUBJECT_ID_COLUMNS` (86 conventional
member-reference names — `user_id`, `author_id`, `reactor_id`, `winner_id`,
`user_a`/`user_b`, …) is queried for the subject, guild-scoped where the table
has a `guild_id`. A curated list is the thing that goes stale, and a stale
access export is an incomplete answer to a statutory request; discovery means a
new feature's table is covered the day it lands, provided it names its member
column conventionally.

**The export is deliberately a superset of the purge.** Categories the server
keeps under Art 17(3) — `econ_ledger`, sanction history, consent audit,
no-contact orders — are exported even though they are never deleted. Retention
is not a disclosure exemption. `test_export_covers_every_table_the_purge_deletes`
enforces the direction: any table the purge learns to delete must be reachable
by the export, and the test reads both table lists off the source rather than
restating them.

Two limits the export reports rather than hides:

- **Message children** (`message_attachments`, `_mentions`, `_embeds`,
  `_reactions`, `_sentiment`) carry no member column — they are the subject's
  data only by hanging off their message. Discovery cannot see them, so they
  are reached by joining through the author's message ids, chunked at 500 like
  the purge.
- **List-valued columns** (`risky_pending_questions.participant_user_ids` and
  five others) store member ids as JSON/CSV, which an equality match cannot
  find. `LIST_VALUED_MEMBER_COLUMNS` names them and the export emits a note
  telling the operator to grep them by hand.

Tables in `THIRD_PARTY_TABLES` — whispers, guesses, no-contact orders, the
interaction graph, moderation records — come back flagged in `review_required`.
Art 15(4) says an access request must not adversely affect others' rights, so
the counterparty decision is surfaced for a human rather than resolved by
redacting (which corrupts the record) or dropping (which hides the tension).

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
- ~~**No export.** Right-to-portability is intentionally deferred.~~ **Shipped
  2026-08-06** — see [Subject access export](#subject-access-export) below. It
  is an operator script, not a command: same reasoning as the purge.
- **No retry queue.** Failed deletions surface in the summary but don't reschedule. Re-running the command after fixing perms is the path.
- **No notification to the target.** `/delete_user @alice` does not DM Alice; the action is silent except for the actor's ephemeral progress.

## Configuration

Privacy has no per-guild configuration. The behavioral constants — 60-second confirm timeout, the 14-day cutoff between bulk and one-at-a-time delete, the throttle cadences — mirror Discord's own constraints and are not exposed.

The only per-call switches are the `mode` (which slice of Discord messages is targeted) and whether the channel walker tries to surface unjoined private archived threads (on for privacy, off for the [[events-spec]] backfill caller).

## Stored data

Privacy is a **pure deleter of Discord messages** — it owns no tables of its own, and the commands purge none. Deletion enumerates the target's messages by walking Discord itself (a live `channel.history` scan across every readable channel, as Phase 1 describes) — **not** by reading the local `messages` archive, so gaps in the archive don't limit what gets deleted.

The `messages` archive itself holds less than the name suggests: `message_storage_level` defaults to `"none"`, under which message content and attachments are dropped at ingest and only metadata derived at ingest time (author, channel, timestamps, media kind, sentiment) is kept. The "server keeps its own records" disclosure is about that metadata unless a guild has opted into `"all"`.

**Preserved by the commands**: everything server-side — the commands write no DB deletions at all. For the out-of-band `purge_user_data` run, the preserve list is implicit: a table is purged iff it's named in the purge (XP incl. reaction awards and the `xp_daily` rollup, activity, gender, birthdays, bios, wellness, interactions, watch/trust/invite pair tables, member events, audit events, per-member economy/casino state, and optionally the archive), and deliberately preserved otherwise — the `econ_ledger` double-entry record, [[dm-perms-spec]] consent and audit data, [[no_contact_spec]] protective records, [[pressure-cooker-spec]] game history, [[guess-spec]] / [[whisper-spec]] / [[jail-spec]] tables (whisper has its own `/whisper forget-me`), [[confessions-spec]] audit and submissions (author links self-destruct on a 7-day TTL), `reaction_log` and `voice_follow_log` (the attention report's evidence path — `docs/reviews/2026-08-05-health-analytics.md` G3), and `intake_cards`/`intake_card_steps` (greeter accountability; the volume is low). New per-user tables landing in other features must make an explicit decision — join the purge or document why they're kept, and add a row to `docs/data_register.md` either way.

**Revised by the 2026-09-02 GDPR review.** The purge additionally clears the starboard (both who was starred and who starred), duel and group-game records, role-menu picks, inactivity exemptions, the LegitLibs writing tables, the two paid-submission products that were missed when their siblings shipped (`econ_emoji_submissions`, `econ_qotd_submissions`), and eight small per-member stores. One category joined the **preserved** side with a named ground: `created_by`/`updated_by`/`set_by`/`hidden_by` on admin-authored configuration — a scheduled announcement, a dashboard doc, a role menu, a hidden channel — kept under Art 17(3)(e) as the record of who configured a server surface, extending the existing `mention_award_rules.created_by` precedent rather than leaving it a one-off. Five tables remain **open decisions** and are marked as such in the register: `quote_audit_log`, `inactive_members`, `promotion_review_cards`, `qa_tests`/`qa_verdicts` and `voice_master_blocked`.

The same review found the purge's schema-drift tolerance hiding real failures: eight tables named in a purge list lacked the columns their statement used, so every erasure failed on them silently. `tests/test_privacy_service.py::test_purge_runs_clean_against_the_migrated_schema` now fails on any swallowed schema error, and `scripts/privacy_coverage.py` reports, from a live database, every column whose values are member ids but whose name `SUBJECT_ID_COLUMNS` does not know.

In-memory only: the set of currently-running deletions (keyed by target user id), cleared in a finally-block whether the run succeeded, failed, or was cancelled.
