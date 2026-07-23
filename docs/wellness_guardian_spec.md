# Wellness Guardian — Feature Spec

A self-managed boundary tool. Members opt in, pick their own enforcement level, set message caps, schedule blackout windows, and pair up with accountability partners. When someone hits a limit, the bot adds friction (per-user slow mode) rather than locking them out. **This is not therapy** — a one-time disclaimer surfaces during setup.

> **Document status (2026-07-15):** This doc was previously "Aspirational" and described ~22 `/wellness` slash commands as if they were live. In reality the surface is split by *how you reach a feature*, not by which feature: the **enforcement engine, background loops, and the web dashboard CRUD are built and wired**, but **only three slash commands exist** and there is **no supported path to provision a guild**. The sections below reflect that. Everything not confirmed in code lives under [Not Yet Built / Roadmap](#not-yet-built--roadmap), preserved for design intent.

---

## ⚠️ Activation gap (read first)

At the code level, **there is no supported way to turn Wellness Guardian on for a guild.** The machinery is real and running, but it is gated on a config row that nothing writes:

- `/wellness setup` refuses to run unless `wellness_config.role_id` is set, and points the user at the web dashboard.
- **`/wellness-admin setup` does not exist** — there is no `/wellness-admin` slash command group anywhere. The member-facing "not set up" error strings (`wellness_cog.py:228,249,496`) now all point at the web dashboard rather than naming a phantom command.
- The dashboard admin router (`/api/wellness/admin`) has **no create-role / create-category / provisioning endpoint**. Its only writer of config sets `default_enforcement` and `crisis_resource_url` — never `role_id` or `channel_id`.
- The only other writer of the config row is the background scheduler, which sets `active_list_message_id` only.

Net effect: unless a `wellness_config` row is seeded out-of-band (e.g. a manual DB edit), no member can opt in, so no enforcement, dashboard data, or loops have any subject to act on. **Provisioning is the single genuinely-missing piece** — see [Roadmap](#provisioning-admin-setup).

---

## Current Behavior

Everything in this section is confirmed present and wired in `src/`. Configuration for members happens almost entirely through the **web dashboard**, not slash commands.

### Slash commands (all that exist)

The `/wellness` group (`wellness_cog.py`) registers exactly three commands:

| Command | Permission | Behavior |
|---|---|---|
| `/wellness setup` | Everyone (server only) | Opens an ephemeral 2-step wizard: (1) disclaimer + timezone select, (2) enforcement level (Gentle / Cooldown / Slow mode / Gradual). On completion, writes the member's opt-in row and assigns the Wellness Guardian role. **Aborts early** if the guild has no `role_id` configured (see activation gap). |
| `/wellness away on` | Opted-in members | Enables the away auto-reply. Optional `message` arg (≤ 500 chars); if omitted, a default message is used. Replies with an ephemeral preview embed. |
| `/wellness away off` | Opted-in members | Disables the away auto-reply. |

The `away` subgroup is nested under `wellness`. Note: a `_SettingsView` class exists in the cog but is **not wired to any command** — a dead stub from the abandoned slash surface.

### Enforcement engine (live)

`wellness_on_message()` (`wellness_enforcement.py`) is called unconditionally from the message handler (`events_cog.py:557`) for every non-bot guild message. For opted-in, non-paused members it runs this decision tree:

1. **Away-mention interception** — if the message @-mentions any opted-in member who has away mode on, the bot posts an in-channel auto-reply embed (rate-limited per channel). Fires regardless of whether the *author* is opted in; never deletes the message.
2. **Slow-mode pre-check** — if the author has active per-user slow mode and is posting inside their rate interval, the bot deletes the message and DMs them the held content plus a countdown. **Per-user global**, not per-channel — switching channels doesn't defeat it. If the bot lacks Manage Messages, or the user's DMs are closed, the message is **not** deleted (no silent destruction).
3. **Cap evaluation + escalation** — increments per-cap counters; on overage within a window, escalates: 1st → **nudge**, 2nd → **cooldown** (bot interactions paused ~5 min), 3rd+ → **friction** (arm slow mode). The action is capped by the member's enforcement level (`gentle`→nudge max, `cooldown`→cooldown max, `slow_mode`/`gradual`→friction). Caps support `global` / `channel` / `category` scope, `hourly` / `daily` / `weekly` windows, an `exclude_exempt` flag, and optional per-hour/per-day `bucket_limits`. (`voice` scope is rejected as "coming soon".)
4. **Blackout enforcement** — during an active blackout window the member's enforcement level applies to all messages; `gradual` escalates per-day within the blackout.
5. **Streak violation** — any overage or blackout-triggered enforcement marks the day as a slip for streak accounting.

Notifications are delivered per the member's `notifications_pref` (`ephemeral` — actually a self-deleting channel reply — / `dm` / `both`).

### Background loops (all registered in `cog_load`)

- **`wellness_tick_loop`** (every 60s): posts blackout entry DMs on transition, lifts expired slow mode, auto-resumes paused members whose pause expired, credits a clean-day streak once per day in each member's timezone, and runs nightly GC (old counter rows + sweep opted-out members past the 30-day retention).
- **`wellness_active_list_loop`** (hourly): rebuilds the pinned "💚 Active in Commitment" embed in the configured channel (names + streak days for members who opted into public commitment) and posts milestone-badge celebration messages. Badges: 🌱 join, 🌟 7d, 🔥 30d, 💪 100d, 👑 365d.
- **`wellness_weekly_report_loop`** (every 5 min, gated to Sunday ≥ 09:00 local, once per ISO week): DMs each member a weekly summary embed (streak, personal best, clean-days/7, compliance %) with an AI-generated encouragement line (falls back to canned text with no API key).

### Web dashboard — member panel (`/api/wellness`, mounted in `server.py`)

Full CRUD, authenticated as the logged-in member:

| Endpoint(s) | Feature |
|---|---|
| `GET /me`, `GET /history`, `GET /xp-histogram` | Profile, streak/history, activity histogram |
| `GET/POST/PUT/DELETE /caps` | Create, edit, remove message caps (scope, window, limit, exclude-exempt, optional bucket limits) |
| `GET /blackouts`, `POST /blackouts`, `PUT /blackouts/{id}/toggle`, `DELETE /blackouts/{id}` | Blackout windows, including the four preset **templates** (Night Owl 23:00–07:00 daily, Work Hours 09:00–17:00 weekdays, School Hours 08:00–15:00 weekdays, Weekend Detox all-day Sat–Sun) |
| `GET/POST /away` | Away message text + toggle (mirrors the two slash commands) |
| `GET /partners`, `POST /partners/request`, `DELETE /partners/{id}` | Accountability partners — request (DMs the target with Accept/Decline), list, dissolve |
| `POST /settings` | Enforcement level, notifications pref, public-commitment toggle, timezone, daily reset hour, slow-mode rate |
| `POST /pause`, `POST /resume` | Pause / resume the member's own tracking |

### Web dashboard — admin panel (`/api/wellness/admin`, requires `manage_server`)

| Endpoint(s) | Feature |
|---|---|
| `GET /dashboard` | Active-member count, exempt channels, server config summary |
| `GET/POST /defaults` | Server default enforcement level + crisis-resource URL |
| `GET /users`, `POST /users/{id}/pause`, `POST /users/{id}/resume` | List opted-in members; admin pause/resume a member |
| `GET/POST /exempt`, `DELETE /exempt/{id}` | Manage the exempt-channel list |

The admin panel does **not** provision the wellness role/category (see activation gap) and does not let admins create caps/blackouts on a member's behalf.

### Data model (confirmed)

Per-guild + per-member tables back all of the above: member settings (timezone, enforcement, notifications pref, slow-mode rate, public-commitment, daily reset hour), caps + per-window counters + overage counters, blackouts + active-marker state, the away message, streak state (current + personal-best + last-violation-date + clean-day history), partnerships, milestone-badge celebration state, weekly-report cache, per-user slow-mode state, and per-guild config (role id, channel id, active-list message id, default enforcement, crisis URL, exempt channels). Schema lives in `wellness_service.py` (`init_wellness_tables`). `opt_out_user()` exists as a function but is **not** surfaced by any command or endpoint.

---

## Not Yet Built / Roadmap

Everything below was in the original design spec. Some of the *behavior* here already runs (see Current Behavior — caps/blackouts/partners/streaks/away are real via the dashboard + engine); what is **not** built is the member-facing **slash-command surface** these tables describe, the **provisioning** step, and the explicitly-deferred v2 items. The tables, message copy, and templates are preserved verbatim so the design intent isn't lost.

### Provisioning (`/wellness-admin setup`)

The most important missing piece. Nothing creates the Wellness Guardian role or the wellness category/channels, and nothing writes `wellness_config.role_id` / `channel_id`. The original design assumed an admin provisioning step (a `/wellness-admin setup` command, since "retired" in favor of the dashboard) — but the dashboard replacement covers defaults/exempt/user-management and **never replaced provisioning**. Until a provisioning path exists (slash command or a dashboard "Set up wellness" button that creates the role + category and stores their ids), the whole feature is dormant.

The original design also envisioned the wellness category being provisioned from the dashboard:

| Channel | Purpose |
|---|---|
| `#wellness-lounge` | Open discussion. Crisis-resource link in the channel topic. Auto-flagged as exempt |
| `#active-in-commitment` | Bot posts the participation list and milestone celebrations (read-only) |
| `#find-a-partner` | Accountability partner matchmaking |

Today only a single configured `channel_id` is used (for the active-in-commitment embed); the multi-channel category and the lounge/find-a-partner channels are unbuilt.

### Member slash-command surface (not built as commands)

The original doc presented this full `/wellness` command table. **Only `/wellness setup`, `/wellness away on`, and `/wellness away off` actually exist** (see Current Behavior). The rest are unbuilt as slash commands; most have a dashboard equivalent, with exceptions noted after the table.

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
| `/wellness away on` | Slash | Wellness role | Enable the away message. Optional custom text (≤500 chars). Variables: `{user}`, `{streak_days}` |
| `/wellness away off` | Slash | Wellness role | Disable the away message |
| `/wellness away set` | Slash | Wellness role | Update away message without toggling |
| `/wellness away preview` | Slash | Wellness role | Preview the away message |
| `/wellness score` | Slash | Wellness role | Streak, personal best, milestone badge, qualitative summary |
| `/wellness partner request @user` | Slash | Wellness role | Send a partner request (DM with Accept / Decline) |
| `/wellness partner list` | Slash | Wellness role | Show all partners with milestone badges |
| `/wellness partner dissolve` | Slash | Wellness role | End a partnership |
| `/wellness settings` | Slash | Wellness role | Change enforcement, notification mode, public-commitment toggle, timezone, slow-mode rate |
| `/wellness pause` | Slash | Wellness role | Pause tracking + lift slow mode. Optional duration |
| `/wellness resume` | Slash | Wellness role | Resume tracking |
| `/wellness optout` | Slash | Wellness role | Remove role, deactivate tracking, lift slow mode. Settings kept 30 days |

**Dashboard-equivalent coverage of the above:** caps (add/list/edit/remove), blackouts (add/template/list/toggle/remove), away (on/off/set), partners (request/list/dissolve), settings, pause, resume all exist as dashboard endpoints today. **No equivalent anywhere** for `/wellness away preview`, `/wellness score`, and `/wellness optout` (the `opt_out_user()` backend function exists but is unsurfaced).

### Admin surface

The original design placed all admin functionality in the **web Wellness panel** — no `/wellness-admin` slash command group. The dashboard was to expose: provisioning the wellness category, server-side defaults (enforcement, caps, blackout template, crisis-resource URL), per-user management (caps, blackouts, settings), the exempt-channel multi-select, and a server-wide stats tile.

> *A short historical mapping from the retired `/wellness-admin X` commands to their dashboard equivalents lived here while admins migrated. It's now retained only in git history.*

*Built today:* defaults (enforcement + crisis URL), per-user pause/resume, exempt-channel management, stats tile. *Not built:* provisioning, and admin-side per-user cap/blackout/settings editing.

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
- Server-side defaults (enforcement, caps, blackout template, crisis-resource URL) *(only enforcement + crisis URL built)*
- Exempt-channel multi-select
- Per-user overrides *(not built)*

#### Tone

Wellness messages avoid words like "violation," "blocked," "warning," "failed," "exceeded," "punishment," "tracked." Instead: "overage," "slowed down," "heads up," "dipped," "hit your cap," "boundary," "keeping count." Streak dips are always framed partially ("dipped from X to Y," not "lost X days").

### Stored data (original design reference)

Per-guild + per-user tables for: member settings (timezone, enforcement, notification mode, slow-mode rate, public-commitment opt-in), caps, blackouts, the away message, streak state (current + personal-best + last-day-counted), partnerships, milestone-badge history, and weekly-summary cache.

Server-wide config tables for: server defaults, the wellness category + channel ids, the crisis-resource URL, and the exempt-channel list.

On `/wellness optout`: role removed, tracking deactivated, slow mode lifted; settings retained 30 days then purged. *(The `opt_out_user()` backend exists; no command or endpoint invokes it.)*
