# Wellness Guardian — Feature Spec

A self-managed boundary tool. Members opt in, pick their own enforcement level, set message caps, and schedule blackout windows. When someone hits a limit, the bot adds friction (per-user slow mode) rather than locking them out. **This is not therapy** — a one-time disclaimer surfaces during setup.

> **Document status (2026-07-15):** This doc was previously "Aspirational" and described ~22 `/wellness` slash commands as if they were live. In reality the surface is split by *how you reach a feature*, not by which feature: the **enforcement engine, background loops, and the web dashboard CRUD are built and wired**, but **only two slash commands exist** (`/wellness away on`/`away off` collapsed into the single `/wellness away set` 2026-07-28, dropping the count from three to two). The sections below reflect that. Everything not confirmed in code lives under [Not Yet Built / Roadmap](#not-yet-built--roadmap), preserved for design intent. *(The provisioning gap this note originally flagged was closed 2026-08-29 — see Activation, next.)*

---

## Activation (gap closed 2026-08-29)

The feature is gated on `wellness_config.role_id` (member opt-in refuses without it) and `channel_id` (the pinned active list and milestone posts refuse without it). From 2026-07-30's honesty pass until 2026-08-29 **nothing in src/ could write either key** — the whole feature was dormant by construction for any guild whose row wasn't seeded by hand (relaunch decision 5, `docs/reviews/2026-08-28-wellness-readiness.md`; relaunch plan Stage D). Now:

- The admin panel (**Config → Members → Wellness**, route id `config-wellness`) shows an **Activate Wellness card** whenever either key is unset *or its stored role/channel no longer resolves*: a role picker with a "create a Wellness Guardian role for me" default, plus a text-channel picker. Once both resolve, the panel shows the current role/channel with Change buttons instead.
- Two setter routes back it, both `require_manage_server`: `POST /api/wellness/admin/provision/role` (`{role_id}` for an existing role — @everyone and managed roles refused — or `{auto_create: true}`, which goes through `core/role_provision.ensure_feature_role`, so a role named exactly "Wellness Guardian" is **adopted**, never twinned) and `POST /api/wellness/admin/provision/channel` (`{channel_id}`, must be a text channel). `GET /provision` serves the card's state.
- `/wellness setup` still refuses without `role_id`; its error copy (and the deleted-role one) now names the Activate card's location.
- The background scheduler additionally writes `active_list_message_id`, as before.

Provisioning writes ids only — it never creates channels/categories and never touches channel permission overwrites; whether the wellness role reveals a channel is (and remains) the admin's own Discord setup.

---

## Current Behavior

Everything in this section is confirmed present and wired in `src/`. Configuration for members happens almost entirely through the **web dashboard**, not slash commands.

### Slash commands (all that exist)

The `/wellness` group (`wellness_cog.py`) registers exactly two commands:

| Command | Permission | Behavior |
|---|---|---|
| `/wellness setup` | Everyone (server only) | Opens an ephemeral 2-step wizard: (1) disclaimer + timezone select, (2) enforcement level (Gentle / Slow mode / Gradual — the never-enforced "Cooldown" level was retired 2026-07-30; stored legacy rows keep their old behavior). On completion, writes the member's opt-in row and assigns the Wellness Guardian role. **Aborts early** if the guild has no `role_id` configured (see activation gap). **A re-run preserves settings it does not ask about** (fixed 2026-08-22): the wizard collects timezone and enforcement only, and `opt_in_user` used to write the module-default `notifications_pref` on every call — so a member who chose "ephemeral only" on the dashboard and later re-ran the wizard to change timezone had DMs silently switched back on. `opt_in_user` now treats an omitted `enforcement_level` / `notifications_pref` as "leave it alone", matching how `public_commitment` and the away fields were already handled. |
| `/wellness away set state:<on\|off>` | Opted-in members | Turns the away auto-reply on or off. Optional `message` arg (≤ 500 chars, on only — passing it with `off` is refused rather than silently dropped); if omitted when turning on, the stored message is kept, falling back to a default. Turning on replies with an ephemeral preview embed. Replaced the separate `/wellness away on` and `/wellness away off` commands 2026-07-28. |

The `away` subgroup is nested under `wellness`. The cog's dead `_SettingsView` stub (never wired to any command) was deleted 2026-08-28 along with the partners system, below. A proposal to build a shared ephemeral settings menu behind a channel panel (the bank paradigm) lives in `docs/plans/wellness-discord-panel.md` (2026-07-30) — not built.

### Enforcement engine (live)

`wellness_on_message()` (`wellness_enforcement.py`) is called unconditionally from the message handler (`events_cog.py`, `on_message`) for every non-bot guild message. For opted-in, non-paused members it runs this decision tree:

1. **Away-mention interception** — if the message @-mentions any opted-in member who has away mode on, the bot posts an in-channel auto-reply embed (rate-limited per channel). Fires regardless of whether the *author* is opted in; never deletes the message.
2. **Slow-mode pre-check** — if the author has active per-user slow mode and is posting inside their rate interval, the bot deletes the message and DMs them the held content plus a countdown. **Per-user global**, not per-channel — switching channels doesn't defeat it. If the bot lacks Manage Messages, or the user's DMs are closed, the message is **not** deleted (no silent destruction).
3. **Cap evaluation + escalation** — increments per-cap counters; on overage within a window, escalates: 1st → **nudge**, 2nd → **cooldown** (bot interactions paused ~5 min), 3rd+ → **friction** (arm slow mode). The action is capped by the member's enforcement level (`gentle`→nudge max, `slow_mode`/`gradual`→friction; legacy `cooldown` rows→cooldown max). Caps support `global` / `channel` / `category` scope, `hourly` / `daily` / `weekly` windows, an `exclude_exempt` flag, and optional per-hour/per-day `bucket_limits`. (`voice` scope is rejected as "coming soon".)
4. **Blackout enforcement** — during an active blackout window the member's enforcement level applies to all messages; `gradual` escalates per-day within the blackout.
5. **Streak violation** — any overage or blackout-triggered enforcement marks the day as a slip for streak accounting.

Notifications are delivered per the member's `notifications_pref` (`ephemeral` — actually a self-deleting channel reply — / `dm` / `both`). Since 2026-07-30 both the web select and the (dormant) Discord settings view label these honestly: "In-channel reply (visible to the room ~30s)" / "DM only (private)" / "In-channel + DM"; the enforcement selects show the wizard's friendly labels instead of raw enums.

### Daily login digest (economy DM)

The two digest parts have **individual opt-ins** and each member receives
exactly the parts they opted into:

- **Economy part** — requires the opt-in economy game role (existing
  `require_game_role` gate).
- **Wellness part** — `login_digest_value()`: active, non-paused wellness
  members whose `notifications_pref` includes DMs ("dm"/"both" — the same
  dial that governs enforcement notices). Renders badge, clean-day streak,
  next milestone, dashboard link (link line omitted when no public URL).

Both opted → one combined DM (wellness field above the quest sections).
Economy-only → the classic digest. **Wellness content only ever rides this
economy morning message** — a member without the economy game role gets no
daily wellness DM (decided 2026-07-30; the weekly report remains their DM
touchpoint). The digest's public bank-channel fallback carries a scrubbed
economy-only embed (`notify_member(fallback_embed=…)`) so wellness state is
never posted publicly.

### Background loops (all registered in `cog_load`)

- All member-facing wellness DMs (blackout entry, nudge, cooldown, slow-mode notices, weekly report) go out as **branded embeds** via `send_branded_dm` — the guild's accent, with its name and icon in the footer. A boundary tool that nudges you without saying which server it speaks for is not much use; the in-channel copies stay plain text, since their guild is already obvious.
- **`wellness_tick_loop`** (every 60s): posts blackout entry DMs on transition, lifts expired slow mode, auto-resumes paused members whose pause expired, credits a clean-day streak once per day in each member's timezone, and runs nightly GC (old counter rows + sweep opted-out members past the 30-day retention).
- **`wellness_active_list_loop`** (hourly): rebuilds the pinned "💚 Active in Commitment" embed in the configured channel (names + streak days for members who opted into public commitment) and posts milestone-badge celebration messages. Badges: 🌱 join, 🌟 7d, 🔥 30d, 💪 100d, 👑 365d. **Public commitment defaults OFF at opt-in** (as of 2026-07-30): joining the program never places a member on the list or in celebrations — that requires the explicit dashboard toggle, and a re-opt-in preserves the earlier choice.
- **`wellness_weekly_report_loop`** (every 5 min, gated to Sunday ≥ 09:00 local, once per ISO week): DMs each member a weekly summary embed (streak, personal best, clean days out of *tracked* days — days since opt-in that have occurred, so a flawless partial week reads 100% — and compliance %) with an AI-generated encouragement line (falls back to canned text with no API key).

### Web dashboard — member panel (`/api/wellness`, mounted in `server.py`)

Full CRUD, authenticated as the logged-in member. The Wellness nav section is gated on `wellness_opted_in` from `/api/me` (the member's actual opt-in row — not a role-name match; changed 2026-07-30), with the usual `manage_server`/admin bypass:

| Endpoint(s) | Feature |
|---|---|
| `GET /me`, `GET /history`, `GET /activity-histogram` | Profile, streak/history, activity histogram (average *messages* per hour/day from message-shaped `xp_events` — renamed from `/xp-histogram` 2026-07-30 when it stopped averaging XP amounts, which had seeded caps several times too tight) |
| `GET/POST/PUT/DELETE /caps` | Create, edit, remove message caps (scope, window, limit, exclude-exempt, optional bucket limits) |
| `GET /blackouts`, `POST /blackouts`, `PUT /blackouts/{id}/toggle`, `DELETE /blackouts/{id}` | Blackout windows, including the four preset **templates** (Night Owl 23:00–07:00 daily, Work Hours 09:00–17:00 weekdays, School Hours 08:00–15:00 weekdays, Weekend Detox all-day Sat–Sun) |
| `GET/POST /away` | Away message text + toggle (mirrors the two slash commands) |
| `POST /settings` | Enforcement level, notifications pref, public-commitment toggle, timezone, daily reset hour, slow-mode rate |
| `POST /pause`, `POST /resume` | Pause / resume the member's own tracking |
| `POST /optout` | Leave the program — tracking deactivated, slow mode lifted, wellness role removed (best-effort); settings retained 30 days, then swept |

### Web dashboard — admin panel (`/api/wellness/admin`, requires `manage_server`)

| Endpoint(s) | Feature |
|---|---|
| `GET /dashboard` | Active-member count, exempt channels, server config summary |
| `GET/POST /defaults` | Server default enforcement level |
| `GET /users`, `POST /users/{id}/pause`, `POST /users/{id}/resume` | List opted-in members; admin pause/resume a member |
| `GET/POST /exempt`, `DELETE /exempt/{id}` | Manage the exempt-channel list |

The admin panel provisions the wellness role + channel via the Activate card (see Activation; `GET /provision`, `POST /provision/role`, `POST /provision/channel`) but does not let admins create caps/blackouts on a member's behalf.

### Data model (confirmed)

Per-guild + per-member tables back all of the above: member settings (timezone, enforcement, notifications pref, slow-mode rate, public-commitment, daily reset hour), caps + per-window counters + overage counters, blackouts + active-marker state, the away message, streak state (current + personal-best + last-violation-date + clean-day history), milestone-badge celebration state, weekly-report cache, per-user slow-mode state, and per-guild config (role id, channel id, active-list message id, default enforcement, exempt channels). Schema lives in `wellness_service.py` (`init_wellness_tables`). `opt_out_user()` is surfaced via `POST /api/wellness/optout` and the Overview panel's **Leave the Program** action (added 2026-07-30).

---

## Not Yet Built / Roadmap

Everything below was in the original design spec. Some of the *behavior* here already runs (see Current Behavior — caps/blackouts/streaks/away are real via the dashboard + engine; partners was real too until its 2026-08-28 retirement, below); what is **not** built is the member-facing **slash-command surface** these tables describe, the **provisioning** step, and the explicitly-deferred v2 items. The tables, message copy, and templates are preserved verbatim so the design intent isn't lost.

### Provisioning (`/wellness-admin setup`)

**Built 2026-08-29** — as the dashboard Activate Wellness card, not the slash command this section originally envisioned (see Activation at the top of this doc). What remains unbuilt from the original design is the *category/channel creation*: the card stores the id of an **existing** text channel, whereas the original design envisioned the dashboard creating a whole wellness category:

| Channel | Purpose |
|---|---|
| `#wellness-lounge` | Open discussion. Crisis-resource link in the channel topic. Auto-flagged as exempt |
| `#active-in-commitment` | Bot posts the participation list and milestone celebrations (read-only) |
| `#find-a-partner` | Accountability partner matchmaking |

Today only a single configured `channel_id` is used (for the active-in-commitment embed); the multi-channel category and the lounge/find-a-partner channels are unbuilt.

### Member slash-command surface (not built as commands)

The original doc presented this full `/wellness` command table. **Only `/wellness setup` and `/wellness away set` actually exist** (see Current Behavior). The rest are unbuilt as slash commands; most have a dashboard equivalent, with exceptions noted after the table.

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/wellness setup` | Slash | Everyone | Quick-start: timezone + enforcement level. Assigns the Wellness Guardian role |
| `/wellness cap add` | Slash | Wellness role | Create a cap (scope: global / channel / category / voice; window: hourly / daily / weekly; limit; exclude-exempt toggle) |
| `/wellness cap list` | Slash | Wellness role | Show all caps with current counts |
| `/wellness cap edit` | Slash | Wellness role | Edit a cap's limit |
| `/wellness cap remove` | Slash | Wellness role | Delete a cap |
| `/wellness blackout add` | Slash | Wellness role | Create a blackout (name, start, end, days) |
| `/wellness blackout template` | Slash | Wellness role | Apply a preset (Night Owl, Work Hours, School Hours, Weekend Detox) |
| `/wellness blackout list` | Slash | Wellness role | Show all blackouts |
| `/wellness blackout toggle` | Slash | Wellness role | Enable / disable a blackout |
| `/wellness blackout remove` | Slash | Wellness role | Delete a blackout |
| `/wellness away set` | Slash | Wellness role | **Built** — turns away mode on or off, with an optional message |
| `/wellness away preview` | Slash | Wellness role | Preview the away message |
| `/wellness score` | Slash | Wellness role | Streak, personal best, milestone badge, qualitative summary |
| `/wellness partner request @user` | Slash | Wellness role | Send a partner request (DM with Accept / Decline) |
| `/wellness partner list` | Slash | Wellness role | Show all partners with milestone badges |
| `/wellness partner dissolve` | Slash | Wellness role | End a partnership |
| `/wellness settings` | Slash | Wellness role | Change enforcement, notification mode, public-commitment toggle, timezone, slow-mode rate |
| `/wellness pause` | Slash | Wellness role | Pause tracking + lift slow mode. Optional duration |
| `/wellness resume` | Slash | Wellness role | Resume tracking |
| `/wellness optout` | Slash | Wellness role | Remove role, deactivate tracking, lift slow mode. Settings kept 30 days |

**Dashboard-equivalent coverage of the above:** caps (add/list/edit/remove), blackouts (add/template/list/toggle/remove), away (on/off/set), settings, pause, resume, and optout (the Overview panel's **Leave the Program** button, `POST /api/wellness/optout` — surfaced 2026-07-30) all exist as dashboard endpoints today. The Overview panel's hero card also shows the streak, personal best, and badge that `/wellness score` would have reported, though with no on-demand qualitative summary line. **No equivalent anywhere** for `/wellness away preview`.

### Admin surface

The original design placed all admin functionality in the **web Wellness panel** — no `/wellness-admin` slash command group. The dashboard was to expose: provisioning the wellness category, server-side defaults (enforcement, caps, blackout template, crisis-resource URL), per-user management (caps, blackouts, settings), the exempt-channel multi-select, and a server-wide stats tile.

> *A short historical mapping from the retired `/wellness-admin X` commands to their dashboard equivalents lived here while admins migrated. It's now retained only in git history.*

*Built today:* defaults (enforcement), per-user pause/resume, exempt-channel management, stats tile. *(Crisis-resource URL support was removed 2026-07-30 — the setup disclaimer no longer references a crisis resource; the orphaned `crisis_resource_url` column itself was then dropped by migration 189, 2026-08-28.)* *Not built:* provisioning, and admin-side per-user cap/blackout/settings editing.

### Onboarding (`/wellness setup`) — original 3-step design

> The live wizard is 2 steps (disclaimer+timezone, then enforcement). The original design specced three:

1. **Disclaimer + timezone** — one-time disclaimer ("this is not therapy"), then a select pre-populated from the user's Discord locale.
2. **Enforcement level** — Gentle reminders / Cooldown breaks / Slow mode / Gradual (start at reminders, escalate per overage). All levels preserve the ability to post — nothing ever locks the user out.
3. **Done** — confirms the role assignment and links to follow-up commands.

### Day-to-day enforcement — original message copy

> The enforcement *engine* is live (see Current Behavior); this is the original message-copy design, retained for reference.

**Nudge (gentle reminder)** — fires when the user hits 80% of a cap and again on first overage. Suppressed if already nudged within the last 5 minutes.

> 💛 Heads up — you're at 80 of your 100 daily messages. No rush, just keeping you in the loop.

> 💛 You've hit your daily cap of 100 messages. Resets at 7:00 AM. You're doing great — tomorrow's a new day!

**Cooldown** — bot commands pause for 5 minutes.

> ☕ Time for a 5-minute breather. Bot commands are paused until 3:47 PM. Stretch, hydrate, look out a window.

**Friction (per-user slow mode)** — the bot tracks the user's last message timestamp per channel. If they post inside their slow-mode interval (default 1 message per 2 minutes, configurable), the bot deletes the message and DMs them with the deleted content plus a countdown.

> 🐢 Slow mode is active — your message was held. You can post again in **1:47**.
>
> Your message: *"hey does anyone want to play tonight"*

Slow mode lifts when the cap window resets or the blackout ends.

**Escalation** — within a single cap window: first overage → nudge, second → cooldown, third+ → friction. Resets each window.

**Blackout entry** — during a blackout the user's enforcement level applies to all interactions.

> 🌙 Your **Night Owl** blackout just started. Slow mode is active until **7:00 AM**.

### Away message (manual) — original design

Decoupled from enforcement. The user toggles it on/off like a status. When another member @-mentions or replies to the away user:

> 💚 **Ben says:** "Gone fishing 🎣 — back in the morning!"

Rate-limited to once per channel per 30 minutes. Default text (if enabled without a custom message): "💚 Hey! **{user}** is currently away." Footer line: *"This is an automated wellness boundary message."*

### Streaks — decay model

A streak day is earned each calendar day (user's timezone) with no cap or blackout overages. **Streaks never reset to zero.** An overage decays the streak by 10%, rounded up, minimum 1 day. Personal best (longest streak) is tracked separately and never decays.

> 🌱 Your streak dipped from **140** to **126 days** — you're still on a 126-day journey. One day doesn't erase what you've built.

> 🔥 New personal best — **150 days!** That's something to be proud of.

### Active in Commitment + milestones

A participation list posted in `#active-in-commitment` — names + milestone badges only. No numbers, no ranking, no streak counts.

| Badge | Earned at |
|---|---|
| 🌱 | Joined |
| 🌟 | 7 days |
| 🔥 | 30 days |
| 💪 | 100 days |
| 👑 | 365 days |

Milestone upgrades are celebrated in the channel for opted-in members.

> The live implementation posts a list that *includes* the streak-day count (`current_days`), a deviation from the "badges only, no numbers" design below.

### Partners

> **Retired 2026-08-28.** The partners system shipped (dashboard request →
> DM Accept/Decline → dissolution) but was never used once, an accepted
> partnership had no downstream effect, and the request path predated the
> no-contact rule. Migration 189 dropped the (empty) table and the code was
> removed; the design below is preserved for reference only.

`/wellness partner request @user` DMs the target with Accept / Decline buttons. Unlimited partners per user. `/wellness partner list` shows everyone's milestone badges. Either side can dissolve via `/wellness partner dissolve` — dissolving preserves both users' streaks. If a partner leaves the guild, the partnership auto-dissolves and the other user is notified.

### Weekly summary

Every Sunday at 9:00 AM (user's local timezone):

> 🌿 **Your Week in Review** *(Apr 6–12)*
>
> **Activity:** 487 messages, 3.2 hours voice *(down 15% from last week)*
> **Cap compliance:** 94% — stayed within limits in 17 of 18 windows
> **Streak:** 126 days 🔥 *(personal best: 140)*
>
> *"Consistent effort compounds. You're building something real."*

The closing AI line is warm, brief, and never references specific channels or content.

### Blackout templates

| Template | Days | Start | End |
|---|---|---|---|
| Night Owl | Every day | 23:00 | 07:00 |
| Work Hours | Weekdays | 09:00 | 17:00 |
| School Hours | Weekdays | 08:00 | 15:00 |
| Weekend Detox | Sat–Sun | 00:00 | 23:59 |

A user can apply a template and customize it, or build a fully custom recurring schedule with per-day granularity. *(These four templates are live via the dashboard blackouts endpoint.)*

### Permissions (original)

- **User-side**: most `/wellness` commands require the Wellness Guardian role (assigned by `/wellness setup`). Anyone can run `/wellness setup`.
- **Web**: admin only.
- **Bot-side**: **Manage Messages** in any channel where friction (per-user slow mode) is active — without it, the deleted-message + DM path can't enforce. **Manage Roles** for assigning / removing the Wellness Guardian role. **Manage Channels** for provisioning the wellness category from the dashboard.

### User-visible errors (original)

| When | The user sees |
|---|---|
| Friction deletes a message | DM: "🐢 Slow mode is active — your message was held. You can post again in **m:ss**. Your message: *…*" |
| Blackout entry | DM: "🌙 Your **{name}** blackout just started. Slow mode is active until **{end}**." |
| Approaching cap (80%) | Per configured notification mode (DM / ephemeral / both): "💛 Heads up — you're at N of your M daily messages…" |
| At cap | Per configured notification mode: "💛 You've hit your daily cap of N messages. Resets at {time}." |
| Cooldown active | Per configured notification mode: "☕ Time for a 5-minute breather. Bot commands are paused until {time}…" |
| Partner request received | DM with Accept / Decline buttons: "💚 **{user}** wants to be your accountability partner!" |
| Streak decays after overage | "🌱 Your streak dipped from **X** to **Y** days — you're still on a Y-day journey." |
| New personal best | "🔥 New personal best — **N days**!" |

### Non-goals

- **No hard lockouts.** Every enforcement level preserves the ability to post.
- **No public streak numbers.** The Active in Commitment list shows badges only, no counts or rankings. *(Note: the live list currently shows day counts — a known deviation.)*
- **No medical / clinical framing.** Disclaimer is one-time at setup; no repeated warnings.
- **No per-message scoring or surveillance dashboards.** Caps measure volume only.
- **No NSFW / link / sentiment analysis** from this feature. Content checks live in [[post-monitoring-spec]] and (separately) the wellness AI keyword pipeline.
- **No admin-imposed enforcement on a non-consenting member.** Every member configures their own level. Admins set server defaults that apply only to opted-in members.

### Deferred to v2

- Weighted scoring system (session distribution, time-of-day health)
- Channel weight modes (equal / nsfw-heavier / separate / custom)
- Session summary micro-notifications
- `/wellness insights` baseline retrospective
- Admin per-user lock / override with transparency DMs
- Behavioral pattern detection (escalating sessions, late-night displacement)

### Configuration (original design reference)

#### Per member
- Timezone
- Enforcement level (gentle / cooldown / slow / gradual)
- Notification mode (ephemeral / DM / both)
- Slow-mode rate (default 1 message / 2 minutes)
- Public-commitment opt-in
- Caps (scope, window, limit, exclude-exempt)
- Blackouts (days, start, end, optional template)
- Away message text and toggle

#### Per guild (dashboard)
- Wellness category + channel provisioning *(not built)*
- Server-side defaults (enforcement, caps, blackout template, crisis-resource URL) *(only enforcement built; crisis URL removed 2026-07-30)*
- Exempt-channel multi-select
- Per-user overrides *(not built)*

#### Tone

Wellness messages avoid words like "violation," "blocked," "warning," "failed," "exceeded," "punishment," "tracked." Instead: "overage," "slowed down," "heads up," "dipped," "hit your cap," "boundary," "keeping count." Streak dips are always framed partially ("dipped from X to Y," not "lost X days").

### Stored data (original design reference)

Per-guild + per-user tables for: member settings (timezone, enforcement, notification mode, slow-mode rate, public-commitment opt-in), caps, blackouts, the away message, streak state (current + personal-best + last-day-counted), partnerships, milestone-badge history, and weekly-summary cache.

Server-wide config tables for: server defaults, the wellness category + channel ids, the crisis-resource URL, and the exempt-channel list.

On `/wellness optout`: role removed, tracking deactivated, slow mode lifted; settings retained 30 days then purged. *(The dashboard exit shipped 2026-07-30 — `POST /optout` + Overview panel. The slash-command form remains roadmap.)*
