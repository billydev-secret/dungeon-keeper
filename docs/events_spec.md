# Events — Feature Spec

The events module is the bot ' s central Discord gateway router. It owns no slash commands; instead it listens to every gateway event the bot cares about and dispatches the work to feature services. Most of what runs here belongs to other specs — this document is the dispatch overview that keeps cross-links honest.

Three concerns live entirely here and have no dedicated spec elsewhere: the **permanent message archive** populated on every non-DM message, **sentiment scoring** that runs alongside the archive, and the **on-ready backfill** that catches up the archive after the bot was offline.

## Commands

No slash commands. The surface is gateway listeners. The behavioral map for each listener is in Behavior; cross-links point at the spec that owns the dispatched work.

The bot needs **View Channels** + **Read Message History** in every text channel it should listen to. Welcome / leave / greeter posts require **Send Messages** + **Embed Links** in their configured channels; missing perms are best-effort warnings DMed to the guild owner.

## Behavior

### On ready

When the gateway becomes ready, the bot upserts every visible member into the historical-names archive (marking them as currently in the guild) and every named channel into the channel archive. It refreshes the invite-uses cache for [[reporting-spec]]. It then spawns the message backfill as a background task. Re-entry on a later `on_ready` is a no-op while the previous backfill is still running.

### Message backfill

Per channel the bot can read, the backfill pulls only messages newer than the most recent already-archived message for that channel (oldest-first so insertion order matches Discord order), batching writes every 200 messages. Each message goes through sentiment scoring and is mirrored to the archive idempotently — a re-run never duplicates a row. If the channel has an auto-delete rule active, backfilled messages are also tracked so the sweep doesn ' t miss anything posted while the bot was offline. Channels the bot can ' t read are skipped silently; other per-channel errors flush the partial batch and continue.

### New messages

Three paths, picked from `(guild presence, author bot flag, message type)`:

- **DM** — early return; DMs are never stored or scored.
- **Bot author** — archive only (store + sentiment + auto-delete tracking + velocity tracker). Spoiler enforcement, wellness, XP, and interaction graph are all skipped.
- **System / join / boost / pin message** — archived with the system-rendered text so the audit captures the line, but no XP, no wellness, no interactions.
- **Default or reply by a member** — the full pipeline runs: spoiler enforcement ([[post-monitoring-spec]]), wellness check ([[wellness-guardian-spec]]), sentiment + archive write + last-activity update + interaction-graph record + velocity-tracker tick ([[reporting-spec]]), then message XP award and level progression ([[xp-spec]]).

Spoiler and wellness short-circuit — if either deletes or handles the message, the rest of the pipeline doesn ' t run.

### Reactions

On reaction add (guild only), the bot awards image-reaction XP to the message author when the message has a qualifying image, with up to 30 seconds of retries on transient Discord 5xx errors. Then in one database transaction it bumps the per-message reaction count and, if the reactor isn ' t the author, records the giver-receiver pair for [[reporting-spec]]. On reaction remove, the per-message count decrements; there is no XP refund.

### Message deletes

Discord deletes only clear auto-delete tracking ([[auto-delete-spec]]). The local message archive is a **permanent record** — historical content, sentiment, mod review, and XP audit all depend on rows surviving Discord-side deletes. Bulk deletes behave the same way.

### Member updates

Every role grant or removal the bot sees is appended to a role-event audit log (owned by [[xp-spec]]). This is broader than XP — all role changes land here.

### Joins

A joining member runs through: jail rejoin enforcement ([[jail-spec]]), an upsert into the historical-names archive marking them present, a join row in the membership event log, the join-raid check ([[reporting-spec]]), and invite attribution (cache diff + record). Finally the welcome embed posts to the configured welcome channel and a greeter ping is sent to the greeter chat channel. Missing perms on these posts produce a one-time DM to the guild owner.

### Leaves

A leaving member is marked as no longer in the guild on the historical-names archive, a leave row is recorded in the membership event log, and a leave embed posts to the configured leave channel.

### Slash-command errors

A registered tree-wide error handler converts `CommandNotFound` into an ephemeral "That command is out of date on this server. Please try again in a moment." and every other unhandled command error into ephemeral "Command failed. Please try again." (skipped if the interaction was already responded to). Every command invocation and failure is logged separately for observability.

## Permissions

This module has no user-facing permission gates — it reacts to gateway events. Bot-side it needs:

- **View Channels** + **Read Message History** in every text channel that should be archived or XP-eligible.
- **Send Messages** + **Embed Links** in the welcome / leave / greeter channels (best-effort).
- The spoiler enforcer needs **Manage Messages** to actually delete; missing perm produces a warn-log and the message survives — owned by [[post-monitoring-spec]].

## User-visible errors

| When | The user sees |
|---|---|
| Slash command not found (e.g. command tree updated) | "That command is out of date on this server. Please try again in a moment." |
| Slash command raised an unhandled error | "Command failed. Please try again." |

All other gateway-listener failures are silent to users by design — backfill skips a channel the bot can ' t read, invite detection degrades to "no inviter", level-up posts that get rejected by Discord log without re-raising, and so on. The only owner-visible failure is a DM warning when welcome / leave / greeter posts can ' t be sent due to missing channel permissions.

## Non-goals

- **No deletion of archive rows.** Discord deletes don ' t propagate to the local archive. Mod review and historical sentiment depend on this.
- **No edit tracking.** Edits are not listened to; the archive row reflects original content only.
- **No DM archival.** Any DM-context event short-circuits.
- **No re-pull of already-archived messages on backfill.** The backfill resumes from the newest already-archived message per channel.
- **No per-guild listener toggle.** Every listener fires for every guild; per-guild behavior is selected by the consumer service.

## Configuration

The module owns no configuration keys — every key it reads is owned by another spec. It consumes:

- Spoiler-required channels and the bypass-role list (passed through to spoiler enforcement; see [[post-monitoring-spec]]).
- The recorded-bots list (bots whose messages still get mention tracking).
- The XP-excluded channel list and XP settings (passed through to message and image-reaction XP; see [[xp-spec]]).
- The welcome channel, welcome message, welcome ping role, and greeter chat channel (used by the join handler).
- The leave channel and leave message (used by the leave handler).
- Level-5 role and the level-up / level-5 log channels (passed through to level progression).

## Stored data

This module is the **sole writer** to the message archive (and its sentiment mirror, the per-message reaction counts, per-message mention rows, the historical-names archive, the channel-names archive, and the join/leave event log). It is also the dispatch point that triggers writes owned by other specs: XP events and role-event audit ([[xp-spec]]), interaction graph and invite attribution and incident detection ([[reporting-spec]]), auto-delete tracking ([[auto-delete-spec]]).

The only in-memory state owned here is a single handle on the running backfill task. The archive is permanent: nothing in this module ever deletes rows, and any future archival policy would need to also clean up the sentiment mirror, the reaction counts, and the per-message mention rows together.
