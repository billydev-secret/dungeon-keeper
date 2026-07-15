# Admin Testing Checklist

For testers with **Administrator** (or bot-owner access for the handful of owner-gated tools). This is the smallest-audience list — mostly configuration, dashboard, and irreversible/destructive actions. Assumes admins can also do everything on [user_testing_checklist.md](user_testing_checklist.md) and [mod_testing_checklist.md](mod_testing_checklist.md).

---

## Onboarding & Setup

- [ ] **`/setup`** — Run the onboarding wizard as an admin; confirm it DMs the 6-step flow (mod roles, admin roles, jail category, ticket category, log channel, transcript channel), falling back to an in-channel wizard if your DMs are closed. Confirm skipping a step leaves existing config untouched and re-running is safe.
- [ ] **`/config set/get/list <key> <value>`** — Change a setting (e.g. `warning_threshold`) via `set`, confirm via `get`, and confirm `list` shows all current keys.
- [ ] **`/hidden hide`** — Hide a channel; confirm `@everyone` loses View Channel and it moves to a "Hidden Channels" category. Try hiding an already-hidden channel and confirm the rejection.
- [ ] **`/hidden restore`** — Restore a hidden channel; confirm original overwrites/position/category return. Try on a never-hidden channel and confirm the error.
- [ ] **`/hidden list`** — Confirm it shows each hidden channel and who hid it (or "none currently hidden").
- [ ] **`/inactive panel`** — Confirm the `@Inactive` role is created/wired and the info + ticket panel posts.
- [ ] **`/inactive sweep`** — Dry-run first (confirm it lists candidates without acting), then `apply:true` (confirm eligible idle members are actually marked).
- [ ] **`/inactive config`** — Set threshold/auto/cap; confirm values save and echo back.
- [ ] **Automatic inactive-sweep loop** — With `auto:true` and a channel configured, confirm the 6-hourly loop marks idle members automatically.
- [ ] **Role Grant dashboard config** — Create/update a grant role (role_id, allowlist, announce/log channels, message template); confirm it's usable by `/grant` afterward.
- [ ] **Needle dashboard config** — Set a channel's title style, slowmode, delete behavior, reply type, status/default reactions, and the guild-wide settings; confirm they persist and apply.
- [ ] **Auto React rule config** — Create/replace a rule for a channel and remove one; confirm upsert replaces the emoji list wholesale and a disabled rule stops firing.
- [ ] **Bump Tracker dashboard config** — Set channel, role, sites, per-site cooldown/detector; confirm the feature stays inactive until a channel is set.
- [ ] **Voice transcription dashboard config** — Enable, pick a model, set a channel allowlist; confirm settings persist and affect the listener.
- [ ] **Voice transcription model download widget** — Trigger a model download; confirm it caches, and re-running when already cached no-ops.

## Bot-Owner-Only Tools

- [ ] **`/reload_cog extension:<ext>`** — Reload a valid extension; confirm the resync/unchanged reply. Try an unknown extension and confirm the error. Run as a non-owner and confirm "Bot owner only."
- [ ] **`/spotify_authorize`** — Confirm an ephemeral link to the dashboard's Spotify OAuth flow.
- [ ] **Command sync gate** — Restart (or `/reload_cog`) with no command changes and confirm no resync call happens; change a command and confirm it does resync.
- [ ] **Bot Identity panel (dashboard)** — Set a per-server nickname and avatar; confirm live application, and that clearing the nickname reverts to the global name.

## Moderation Config

- [ ] **`/ai review user:<member> days:[1-30]`** — Confirm an ephemeral violation/pattern analysis. Run as non-admin and confirm the permission error.
- [ ] **`/ai scan count:[10-200]`** — Run in a text channel/thread; confirm it analyzes recent messages. Run in a voice/category/forum channel and confirm the "text channels only" error.
- [ ] **`/ai channel question:<text> minutes:[1-1440]`** — Ask a free-form question against a channel's recent window; confirm an ephemeral answer.
- [ ] **`/ai query user:<member> question:<text> days:[1-30]`** — Ask about a specific member; confirm the scoped answer. Run against a member with zero archived rows and confirm the "no messages found" message.
- [ ] **AI config (dashboard)** — Set a per-command model/prompt override; confirm it applies on the next `/ai` call. Clear it and confirm fallback to default.
- [ ] **AI prompt test (dashboard)** — Run the current prompt+model against arbitrary input; confirm a live response.
- [ ] **Model status/reload (dashboard)** — Inspect the loaded model, then reload; confirm the phase transitions idle→downloading→loading→ready.
- [ ] **`/risky reset_state`** — Wipe all active/pending Risky Roll state in a channel; confirm subsequent Roll/Close attempts see "no open round." Run as non-admin and confirm the permission error.
- [ ] **Risky Roll panel (dashboard)** — Configure the ping role and min-game-time floor; confirm they apply to the next round. Submit a negative min-game-seconds and confirm the 400 error.
- [ ] **DM Permissions config (dashboard)** — Set the request channel and audit channel; confirm they persist.
- [ ] **DM Permissions post panel (dashboard)** — Force-repost the request panel into a chosen channel.
- [ ] **DM Permissions audit log (dashboard)** — Confirm every state transition (requested/accepted/denied/expired/revoked) lists with working filters.
- [ ] **Confessions block list (dashboard)** — Add a user; confirm their next `/confess` attempt is refused.
- [ ] **Confessions config (dashboard, read)** — Confirm destination/log channel, cooldown, cap, panic, replies, and per-day limit are viewable.
- [ ] **Confessions audit log (dashboard)** — Confirm recent confessions list with archived bodies.
- [ ] **Whisper audit log (dashboard)** — Confirm every whisper lists with state and report counts.
- [ ] **Auto-delete: set a rule (dashboard)** — Create a rule with `max_age`/`sweep_interval`; confirm it activates and the next sweep deletes eligible messages.
- [ ] **Auto-delete: media-only toggle** — Enable it on an existing rule; confirm the queue rebuilds to track only attachment-bearing messages.
- [ ] **Auto-delete: bulk vs single-message pacing** — With messages both under and over 13 days old due, confirm under-13-day ones bulk-delete and older ones delete one at a time.
- [ ] **Auto-delete: startup catch-up + pinned skip** — Pin a message in a rule channel, restart the bot; confirm the startup pass skips the pin but processes everything else past `max_age`.
- [ ] **Auto-delete: remove a rule** — Confirm the rule and all tracked messages for that channel are discarded.
- [ ] **Auto-delete: missing Manage Messages mid-sweep** — Revoke the bot's Manage Messages in a rule channel mid-sweep; confirm it stops cleanly, the rule stays active, and it retries silently next tick.
- [ ] **Post monitoring: bot missing Manage Messages** — Revoke Manage Messages in a spoiler-required channel, post a non-spoilered image; confirm it survives with no user-facing message.

## Economy & XP Config

- [ ] **Economy Settings (dashboard)** — Set `enabled` and `bank_channel_id`; confirm the economy stays off until both are set.
- [ ] **Economy Metrics tile (dashboard)** — Confirm median/p90 income and week-over-week net-mint arrow (or "rollup pending" pre-first-rollup).
- [ ] **XP config panel (dashboard)** — Edit a coefficient (e.g. per-word XP); confirm it changes future awards, not past ones.

## Games Config

- [ ] **`/games dev fill` / `/games dev answer`** — As a developer, populate a lobby with fake players and submit fake Clapback answers; confirm this stays a dev-only surface, not exposed to normal hosts.
- [ ] **Games channel allowlist (dashboard)** — Remove a channel from the allowlist; confirm `/games play` there is refused.
- [ ] **Per-guild per-game enable/disable (dashboard)** — Disable one game; confirm `/games play <slug>` reports it's disabled.
- [ ] **Games audit channel (dashboard)** — Configure one, then submit an anonymous entry (FFA/Hot Takes/AMA); confirm it mirrors there with the original author visible.
- [ ] **Game-Host / editor role (dashboard)** — Assign it to a non-mod member; confirm they can add/remove players via `/games join|leave`.
- [ ] **LegitLibs per-channel tier cap (dashboard)** — Set `max_tier` to 1 for a channel, then request `tier:4` there; confirm silent downgrade with an ephemeral warning.
- [ ] **Server-owner rename edge case (duels)** — Have the server owner lose a nickname-mode game (Pressure Cooker or the PvP suite); confirm the rename is skipped and the owner is asked to self-apply it, while the game result still records.

## Voice, Social & Content Config

- [ ] **`/voice-admin post-panel`** — With a configured control channel, post (or repost) the persistent control panel.
- [ ] **Voice Master how-to guide post (dashboard)** — Post the member-facing how-it-works embed into a channel.
- [ ] **Voice Master web config** — Configure Hub, target category, control channel, and spectator gate role; confirm a subsequent Hub join reflects the new settings.
- [ ] **Role menu — elevated-role override** — Try adding a dangerous role (e.g. Manage Server) to a menu; confirm it's hidden unless the override is checked, and that using the override is logged loudly.
- [ ] **Role menu — graceful degradation** — Delete a role referenced by a published menu; confirm members get a polite failure message and mods get exactly one alert, with the panel flagging the affected menu.
- [ ] **Starboard web config** — Configure channel/threshold/emoji/enabled/exclusion list; confirm an unparseable emoji returns the 400 error.
- [ ] **Custom quote border upload (dashboard)** — Upload a custom frame; confirm one with no usable center opening is rejected, and a valid one drives its own layout on the next card.
- [ ] **Bios field/template editor (dashboard)** — Add/edit/soft-retire a field and reorder via drag; confirm the template version updates and exactly one `is_headline` field is enforced.
- [ ] **Bios question editor (dashboard)** — Add/edit/soft-retire an icebreaker question.
- [ ] **Bios config editor (dashboard)** — Set bios channel, wizard category, questions-per-bio, embed color, timeout, archive grace.
- [ ] **Birthday panel (dashboard)** — Set announcement channel and message template; confirm the upcoming-90-days preview reflects stored birthdays. Save an empty template and confirm the 400 error.
- [ ] **Pen Pals config (dashboard)** — Set category, opt-in role, question category, auto-round schedule.

## Reporting & Analytics

- [ ] **Dashboard report tiles** — Load each report area (Activity, Membership health, Greeter performance, XP, Interaction graph, Invite effectiveness, Quality score, Chilling effect); confirm role-listing/inactivity tiles 503 gracefully while the bot is offline.
- [ ] **Report cache clear** — Confirm it drops cached payloads for the active guild and the next load recomputes.
- [ ] **Incident detection — velocity spike** — Generate a message-rate burst; confirm a velocity incident fires (max once per 5 min) at the right severity.
- [ ] **Incident detection — join raid** — Simulate 3+ new (<7-day-old) accounts joining within 2 minutes; confirm a critical join-raid incident records.
- [ ] **Invite attribution** — Have a member join through a specific invite; confirm the inviter is recorded, and a re-join after leaving doesn't overwrite the original inviter.

## Wellness Guardian (Admin Side)

- [ ] **Seed `wellness_config` prerequisite** — Manually seed a `wellness_config` row (direct DB write) with valid `role_id`/`channel_id` for the test guild — nothing else provisions this. Confirm `/wellness setup` no longer aborts once seeded, and note this in your handoff to the user/mod testers since they're blocked on it.
- [ ] **Admin dashboard — overview** — Confirm active-member count, exempt channels, and config summary display.
- [ ] **Admin dashboard — defaults** — Update the server default enforcement level and crisis-resource URL; confirm they persist and apply to new opt-ins.
- [ ] **Admin dashboard — user pause/resume** — List opted-in members, then pause/resume one; confirm admin can override any member's tracking.
- [ ] **Admin dashboard — exempt channels** — Add/remove a channel; confirm exempt channels are excluded from cap enforcement.

## Beta / Dev Tools (test-guild only)

- [ ] **`/beta help`** — Confirm the command overview lists all `/beta` subcommands.
- [ ] **`/beta health`** — Confirm it reports puppet/sim/DB/profile status and recent errors.
- [ ] **`/beta sim start/stop/pause/status`** — Toggle the ambient synthetic-activity loop; confirm `status` reports rate + recent posts, and re-running `start` while running says "already running."
- [ ] **`/beta sim rate <multiplier>`** — Confirm posting cadence visibly changes.
- [ ] **`/beta scenario list/describe/run/history`** — Run a named scenario (e.g. `jail-full-cycle`) end-to-end; confirm it logs to the scenario-log channel and appears in history. Try an unknown name and confirm the error.
- [ ] **`/beta puppets list/reload/reconnect/impersonate`** — List the roster, reload personas, reconnect one, and impersonate a puppet posting text in a target channel.
- [ ] **`/beta ghosts list/reload`** — Confirm the webhook + DB ghost roster displays and reloads.
- [ ] **`/beta seed run/status/cleanup`** — Run it (confirm ~90 days of backdated activity populate); re-run without `--force` (confirm "already seeded"); run `status` and `cleanup` (confirm seed rows are removed while sim rows survive).
- [ ] **`/beta sim cleanup` / `/beta cleanup [--dry-run]`** — Confirm `sim cleanup` only removes `beta_sim`-tagged rows; confirm `--dry-run` previews a count without deleting, and without it all `beta_*` rows are removed.
- [ ] **`/beta profile reload` / `/beta markov reload`** — Confirm both reload without error.
- [ ] **`/beta nuke`** — Confirm it drops and refreshes the dev DB from prod. Run as non-admin and confirm "Admin only." **Only run this against the test/beta environment, never prod.**

## Passive System Checks

- [ ] **On-ready backfill** — Restart the bot after messages were posted while it was offline; confirm backfill catches up only newer messages per channel with no duplicates.
