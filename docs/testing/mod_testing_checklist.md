# Mod Testing Checklist

For testers with a **configured mod role** (Manage Messages/Manage Guild-level, not full Administrator). Assumes you've already run through — or are also covering — [user_testing_checklist.md](user_testing_checklist.md), since mods can do everything a regular member can too.

Companion lists: [user_testing_checklist.md](user_testing_checklist.md), [admin_testing_checklist.md](admin_testing_checklist.md).

---

## Jail, Tickets & Warnings

- [ ] **`/jail <user> [duration] [reason]`** — Jail a member with a duration like `24h`; confirm roles strip, `@Jailed` applies, a private jail channel is created, and the member is DM'd. Try jailing an already-jailed member or another mod and confirm the refusal.
- [ ] **`/unjail <user> [reason]`** — Release a jailed member; confirm original roles restore, a transcript posts to the log channel and is DM'd, and the jail channel deletes.
- [ ] **Jail User (context menu)** — Right-click a member, submit duration+reason in the modal; confirm the same jail flow.
- [ ] **`/ticket panel <channel>`** — Post the persistent Open Ticket button; confirm it survives a bot restart.
- [ ] **`/ticket close [reason]`** — Close an open ticket; confirm the channel locks read-only for the creator and the embed shows Reopen/Delete buttons.
- [ ] **`/ticket reopen`** — Reopen a closed ticket; confirm send permissions restore and the 24h auto-delete clock resets. Try on an already-open ticket and confirm the rejection.
- [ ] **`/ticket delete`** — Delete a closed ticket; confirm a transcript generates and posts, DMs the creator, then the channel deletes.
- [ ] **`/ticket claim`** — Claim a ticket; confirm you get coalesced DM alerts on new non-claimer activity. Have another mod try to reassign and confirm the confirmation prompt.
- [ ] **`/ticket escalate [reason]`** — Escalate a ticket; confirm admin roles gain access, the embed shows "Escalated", and they're pinged. Escalate twice and confirm the "already escalated" rejection.
- [ ] **`/pull <user>`** — Pull a bystander into a jail/ticket channel; confirm they gain access and appear in the transcript participant list.
- [ ] **`/remove <user>`** — Remove a previously pulled user; confirm access revokes. Try removing the primary jailed/ticket member and confirm the refusal.
- [ ] **`/warn <user> [reason]`** — Warn a member; confirm a DM with reason + running count, plus an audit embed. Hit the warning threshold (default 3) and confirm the admin-ping alert.
- [ ] **`/warnings <user>`** — Confirm it lists active and revoked warnings with dates, reasons, and issuing mod.
- [ ] **`/revokewarn <user> <warning_id>`** — Soft-delete a real warning id; confirm it moves to revoked history. Try a bogus id and confirm the "couldn't find" error.
- [ ] **`/modinfo <user>`** — Confirm one embed shows jail status/history, warnings, and ticket history.
- [ ] **Auto re-jail on rejoin** — Jail a member, have them leave and rejoin; confirm they're automatically re-stripped/re-jailed with a note in the jail channel.
- [ ] **Auto-unjail on expiry** — Jail with a short duration; confirm the background task auto-unjails within a minute of expiry.
- [ ] **Auto-delete closed ticket after 24h** — Close a ticket and confirm it auto-deletes with a transcript after 24h if never reopened.
- [ ] **Dashboard moderation endpoints** — Confirm jail/ticket/warning lists, stats, and transcript fetch render on the dashboard. Note the known gap: dashboard close/reopen/dismiss/escalate are DB-only and don't touch the actual Discord channel — verify this is still true, don't rely on it to actually manage a live ticket.
- [ ] **`/purge count:<n>`** — Delete recent messages; confirm the right count. Try `after:<HH:MM>` and confirm time-based deletion. Run as a non-mod and confirm the permission error.
- [ ] **`/rename target:<member> new_name:<text>`** — Rename a member's nickname; confirm audit-log attribution. Omit `new_name` and confirm it resets to username. Try on the server owner and confirm the refusal.
- [ ] **`/inactive mark`** — Mark a member inactive; confirm roles are snapshotted/stripped, `@Inactive` applied, and they're DM'd + moved. Try on an admin/mod target and confirm refusal.
- [ ] **`/inactive release`** — Release a marked member; confirm roles restore and `@Inactive` is removed.
- [ ] **DM request 24h expiry sweep** — Leave a DM connection request pending past 24 hours (or force via test data); confirm it flips to expired, DMs the requester, and logs the expiry.

## AI Moderation & Rules Watch

- [ ] **`/watch add user:<member>`** — Subscribe to a member; confirm confirmation text. Try on a bot and on yourself and confirm both refusals.
- [ ] **`/watch remove user:<member>`** — Unsubscribe; confirm the pair drops.
- [ ] **`/watch list`** — Confirm it lists every member you're watching (including departed members as bare IDs).
- [ ] **Passive watch DM relay** — As the watched member, post a message that reads as a rule violation; confirm you get a DM relay with content, attachments, and a rule-concern note.
- [ ] **`/rules-watch enable` / `disable`** — Toggle passive monitoring; confirm the start/stop confirmations.
- [ ] **`/rules-watch set-channel`** — Point the alert channel at a chosen channel; confirm it updates.
- [ ] **`/rules-watch digest`** — Run it after some digest-tier events exist; confirm a summary posts.
- [ ] **`/rules-watch stats`** — Confirm event counts, false-positive rate, and signal firing rates display.
- [ ] **`/rules-watch label <event_id> <verdict>`** — Label a real event; confirm it records. Try a bogus id and confirm the "not found" error.
- [ ] **`/rules-watch status`** — Confirm it reports whether monitoring is active and the alert channel.
- [ ] **Passive Rules Watch alert** — Post a message that trips the pre-filter; confirm an alert posts with Confirm/Dismiss buttons, and clicking one disables further action and writes a label.
- [ ] **Guild-wide message query (dashboard)** — Run a free-form archive query with filters; confirm results. With the LLM unconfigured, confirm the "not configured" message.
- [ ] **Rules Watch alert queue (dashboard)** — Confirm flagged events list with inline Confirm/Dismiss.
- [ ] **Rules Watch label stats (dashboard)** — Confirm label counts, false-positive rate, and per-tier/rule breakdown render.

## Economy

- [ ] **`/xp_give member:<member>`** — Grant XP; confirm a public confirmation and a ledger entry excluded from leaderboards. Try on a bot/self/in a DM and confirm the guard.
- [ ] **Approve/Deny a sign-off claim** — Approve a pending claim from its bank-channel card; confirm payout + DM. Deny another with a reason; confirm the reason DM and that the period stays re-claimable.
- [ ] **Community quest progress + settlement** — Advance a community quest's progress; confirm it flat-pays every recently-active member exactly once on completion.
- [ ] **Quest authoring (dashboard)** — Create a new daily/weekly/community/event quest with reward + completion mode; confirm it becomes claimable.
- [ ] **AI idea generator (dashboard)** — Click "Generate ideas" on the quest form; confirm suggestions render without persisting anything until you pick one.
- [ ] **Settle sign-off community quest (dashboard)** — Manually run Settle; confirm the auto weekly sweep never double-pays it.
- [ ] **`/qotd post <question>`** — Post a QOTD; confirm the banner renders.
- [ ] **`/bank grant @member amount reason`** — Grant currency with a reason; confirm the balance updates and the ledger is audit-tagged.
- [ ] **`/bank post-guide [channel]`** — Post the branded guide embed; re-run in the same channel and confirm it edits in place.
- [ ] **`/bank post-shop [channel]`** — Post the persistent shop panel; confirm any member can click Rent and get an ephemeral reply.
- [ ] **`/bank post-leaderboard [channel]`** — Post the leaderboard panel; confirm it auto-refreshes hourly.
- [ ] **Income Sources page (dashboard)** — Disable a trigger kind; confirm its quests stop firing immediately while staying in the library.
- [ ] **Rental cancel (dashboard)** — Cancel an active rental (confirm it runs to end-of-week with no refund); cancel a grace-period rental (confirm immediate cancellation).
- [ ] **Statistics page (dashboard)** — Confirm supply concentration, balance histogram, 7-day flow, and per-member income table render.
- [ ] **`/delete_user member:<user>`** — Erase another (including departed) member's data. Run as a non-mod and confirm the permission error.

## Games

- [ ] **`/games join` / `/games leave` (others)** — Add/remove another player as a Mod or Game-Host; confirm a non-elevated player attempting the same is rejected.
- [ ] **`/games config game-status`** — Run in a channel with an active game; confirm it reports live state.
- [ ] **`/games config game-end`** — Force-close an active game; confirm the "Force-Closed" notice.
- [ ] **`/games track watch <channel> <bot>`** — Watch a channel + external bot; confirm it starts banking messages there.
- [ ] **`/games track status` / `disable` / `enable`** — Confirm status reporting and that pausing/resuming doesn't lose banked data.
- [ ] **`/games track sample`** — Confirm it dumps recent bot messages as JSON.
- [ ] **Quickdraw / Hot Potato / Hot Potato Group / Chicken / Musical Chairs — `config`** — Update each game's timing/player-bound knobs; confirm the new settings apply to the next game.
- [ ] **Pressure Cooker — `/pressure config`** — View then update `cooldown_hours`/`sentence_hours`; confirm changes persist.
- [ ] **Guess — `/guess round <round_id>`** — Inspect an existing round; confirm status/submitter/answer/counts show.
- [ ] **Guess — `/guess delete` (other's round)** — Delete another member's round; confirm a non-mod/non-submitter is rejected doing the same.
- [ ] **Guess — `/guess prompt`** — Force an immediate repost of the sticky Submit/Help prompt.
- [ ] **Guess — dashboard audit log** — Confirm recent submit/delete/solve/guess-cap events list.
- [ ] **`/247 enabled:<bool>`** — Enable 24/7 with an autoplay playlist for your channel; confirm the bot stays connected when idle and autoplays once the queue empties.
- [ ] **`/247_status`** — Confirm it lists channels with 24/7 enabled.

## Content, Docs & Panels

- [ ] **Build & publish a role menu (Oracle)** — Create a menu (any style/mode, a few roles) and Publish; confirm the live preview matches what actually posts.
- [ ] **Update live message (Oracle)** — Edit a published menu and click Update; confirm the live post updates in place with no delete/repost.
- [ ] **Unpublish (Oracle)** — Unpublish a live menu; confirm the post remains as decor but stops applying roles.
- [ ] **Delete (Oracle)** — Delete a menu (with confirm step); confirm both the post and the record are gone.
- [ ] **Role menu mod-log integration** — Trigger a role grant/removal via a menu; confirm a compact mod-log line appears.
- [ ] **Starboard NSFW leak guard** — Star a message in an NSFW source channel while the starboard channel isn't NSFW; confirm the repost is suppressed.
- [ ] **Starboard stale-record recovery** — Manually delete a starboard repost, then add another qualifying reaction; confirm a fresh post is created.
- [ ] **`/docs post`** — Post a dashboard-authored doc into a channel; confirm it renders and stays tracked for sync.
- [ ] **`/docs sync`** — Edit a doc's markdown on the dashboard, then sync; confirm all placements re-render in place.
- [ ] **`/docs unpost`** — Confirm the doc's messages are deleted from that channel and the placement drops.
- [ ] **`/docs list`** — Confirm it lists the guild's docs and where each is posted.
- [ ] **Docs dashboard authoring** — Create a doc, upload an image, add a placement, toggle pin, save; confirm live re-render and pin/unpin behavior.
- [ ] **`/quality_leave add/remove/list`** — Mark a member on leave, clear it, and list the active roster; confirm scoring reflects leave status.
- [ ] **Message Review panel (dashboard)** — Filter past messages, issue a natural-language query; confirm results and filter chips populate correctly.
- [ ] **Message Review export** — Export a filtered result set; confirm a CSV downloads.
- [ ] **`/bump log` / `/bump status`** — Log a site (confirm cooldown resets + widget refreshes) and check status. Try an unknown site name and confirm the error.
- [ ] **Bump Tracker background ping** — Let a logged site's cooldown expire; confirm the configured role gets pinged once and the widget re-posts.
- [ ] **Chat Revive frequency/protection gates** — Trigger a revive, then try to force a second one within the rest period/daily budget/quiet hours; confirm suppression.
- [ ] **Chat Revive dashboard** — Adjust settings/channel dials, add a bank question, run "Check" for a channel, manually Fire a revive, view the scoreboard; confirm manual fire still respects ping scarcity and counts toward the daily budget.
- [ ] **Pen Pals — `/penpals pair <user1> <user2>`** — Force-pair two members bypassing pool/cooldown; confirm a private channel + intro + first question post.
- [ ] **Pen Pals — `/penpals round`** — Drain the pool; confirm eligible members pair (skipping anyone in the 30-day cooldown).
- [ ] **Pen Pals questions (dashboard)** — Manage the question bank; confirm new questions appear in future sessions.
- [ ] **Confessions config write (dashboard)** — As game host, edit destination channel or character cap; confirm it applies to the next submission.
- [ ] **Confessions launcher placement (dashboard)** — Post/move the launcher button to a chosen channel.
