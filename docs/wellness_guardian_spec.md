# Wellness Guardian — Feature Spec

A self-managed boundary tool. Members opt in, pick their own enforcement level, set message / voice caps, schedule blackout windows, and pair up with accountability partners. When someone hits a limit, the bot adds friction (per-user slow mode) rather than locking them out. A role-gated wellness category gives participants a private support space. **This is not therapy** — a one-time disclaimer surfaces during setup and crisis-resource links live in the wellness lounge channel topic.

## Commands

### Member commands (`/wellness`)

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

### Admin surface

All admin functionality lives in the **web Wellness panel** — there is no `/wellness-admin` slash command group. The dashboard exposes: provisioning the wellness category, server-side defaults (enforcement, caps, blackout template, crisis-resource URL), per-user management (caps, blackouts, settings), the exempt-channel multi-select, and a server-wide stats tile.

> *A short historical mapping from the retired `/wellness-admin X` commands to their dashboard equivalents lived here while admins migrated. It's now retained only in git history.*

## Behaviour

### Onboarding (`/wellness setup`)

Three steps inside one ephemeral flow:
1. **Disclaimer + timezone** — one-time disclaimer ("this is not therapy"), then a select pre-populated from the user's Discord locale.
2. **Enforcement level** — Gentle reminders / Cooldown breaks / Slow mode / Gradual (start at reminders, escalate per overage). All levels preserve the ability to post — nothing ever locks the user out.
3. **Done** — confirms the role assignment and links to follow-up commands.

### Wellness category and channels

Provisioned once per guild from the dashboard. Invisible to anyone without the Wellness Guardian role:

| Channel | Purpose |
|---|---|
| `#wellness-lounge` | Open discussion. Crisis-resource link in the channel topic. Auto-flagged as exempt |
| `#active-in-commitment` | Bot posts the participation list and milestone celebrations (read-only) |
| `#find-a-partner` | Accountability partner matchmaking |

Running `/wellness optout` removes the role and the category vanishes for that user. Settings retained 30 days.

### Day-to-day enforcement

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

### Away message (manual)

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

A user can apply a template and customize it, or build a fully custom recurring schedule with per-day granularity.

## Permissions

- **User-side**: most `/wellness` commands require the Wellness Guardian role (assigned by `/wellness setup`). Anyone can run `/wellness setup`.
- **Web**: admin only.
- **Bot-side**: **Manage Messages** in any channel where friction (per-user slow mode) is active — without it, the deleted-message + DM path can't enforce. **Manage Roles** for assigning / removing the Wellness Guardian role. **Manage Channels** for provisioning the wellness category from the dashboard.

## User-visible errors

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

## Non-goals

- **No hard lockouts.** Every enforcement level preserves the ability to post.
- **No public streak numbers.** The Active in Commitment list shows badges only, no counts or rankings.
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
- Behavioural pattern detection (escalating sessions, late-night displacement)

## Configuration

### Per member
- Timezone
- Enforcement level (gentle / cooldown / slow / gradual)
- Notification mode (ephemeral / DM / both)
- Slow-mode rate (default 1 message / 2 minutes)
- Public-commitment opt-in
- Caps (scope, window, limit, exclude-exempt)
- Blackouts (days, start, end, optional template)
- Away message text and toggle

### Per guild (dashboard)
- Wellness category + channel provisioning
- Server-side defaults (enforcement, caps, blackout template, crisis-resource URL)
- Exempt-channel multi-select
- Per-user overrides (the same settings available to the user themselves)

### Tone

Wellness messages avoid words like "violation," "blocked," "warning," "failed," "exceeded," "punishment," "tracked." Instead: "overage," "slowed down," "heads up," "dipped," "hit your cap," "boundary," "keeping count." Streak dips are always framed partially ("dipped from X to Y," not "lost X days").

## Stored data

Per-guild + per-user tables for: member settings (timezone, enforcement, notification mode, slow-mode rate, public-commitment opt-in), caps, blackouts, the away message, streak state (current + personal-best + last-day-counted), partnerships, milestone-badge history, and weekly-summary cache.

Server-wide config tables for: server defaults, the wellness category + channel ids, the crisis-resource URL, and the exempt-channel list.

On `/wellness optout`: role removed, tracking deactivated, slow mode lifted; settings retained 30 days then purged.
