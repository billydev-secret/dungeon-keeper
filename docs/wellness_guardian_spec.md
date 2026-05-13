# Wellness Guardian — UX & Command Specification

**Dungeon Keeper Discord Bot**
**The Golden Meadow — April 2026**

---

## 1. What This Is

Wellness Guardian lets community members set their own healthy boundaries — message caps, scheduled blackout windows, and accountability partnerships — all opt-in and self-managed. When someone hits their limit, the bot adds friction (slow mode) rather than locking them out — research shows friction-based interventions produce 57% usage reduction with nearly double the sustained adoption of hard lockouts.

A role-gated wellness category gives participants a private space to support each other.

**This is not therapy.** One-time disclaimer during setup. Crisis resources linked in the wellness lounge channel topic.

### Design Principles

- **Consent-first:** Users opt in and choose their own enforcement level
- **Friction over force:** Slow things down rather than lock things out — backed by intervention research showing graduated friction achieves 62% sustained use vs. 36% for hard blocks
- **No shame:** Language is always supportive. Streaks decay rather than reset — one bad day costs 10%, not everything
- **Invisible to non-participants:** Everything is hidden behind a role
- **Progressive disclosure:** Quick start gets you running in 30 seconds. Deeper config lives in the web panel
- **Harm reduction, not abstinence:** The tool helps moderate, not eliminate. Research shows abstinence-framed tools can worsen outcomes

---

## 2. Onboarding

### `/wellness setup`

Two decisions. That's it.

**Step 1 — Disclaimer + Timezone**

> 👋 **Welcome to Wellness Guardian**
>
> This tool helps you set healthy boundaries with Discord — it's not a substitute for professional support. If you're ever struggling, reach out to a trusted person or a crisis resource.
>
> 🕐 **What's your timezone?**

Select menu, pre-populated from Discord locale when possible. For ambiguous locales (en-US), shows US region options.

**Step 2 — Enforcement Level**

> 🛡️ **How firm should your boundaries be?**
>
> - **Gentle reminders** — I'll send you a heads-up, but won't stop you
> - **Cooldown breaks** — I'll suggest a 5-minute breather when you go over
> - **Slow mode** — I'll add a per-user slow mode so you can still post, just at a slower pace
> - **Gradual** — Start with reminders, then breaks, then slow mode if needed

All levels preserve the user's ability to post — nothing ever locks them out.

**Step 3 — Done**

> ✅ **You're all set!**
>
> Your Wellness Guardian role has been assigned — check out the new 🌿 Wellness channels in your channel list.
>
> **Next steps:**
> - `/wellness cap` — Set your first message or voice limit
> - `/wellness blackout` — Schedule offline hours
> - `/wellness partner` — Find an accountability buddy
> - `/wellness away` — Set a custom away message anytime
> - **Web config panel** — Fine-tune everything visually at [link]

---

## 3. Wellness Category & Channels

Created once by an admin via `/wellness-admin setup`. Invisible to everyone without the Wellness Guardian role.

| Channel | Type | Purpose |
|---------|------|---------|
| `#wellness-lounge` | Text | Open discussion — tips, encouragement, checking in. Auto-flagged as exempt. Crisis resource link in channel topic. |
| `#active-in-commitment` | Text (read-only) | Bot posts the Active in Commitment list and milestone celebrations. |
| `#find-a-partner` | Text | Accountability partner matchmaking. |

When someone runs `/wellness optout`, the role is removed and the category vanishes. Settings preserved 30 days.

---

## 4. Day-to-Day Experience

### 4.1 Nudge (Gentle Reminder)

**Approaching cap (80%):**

> 💛 Heads up — you're at 80 of your 100 daily messages. No rush, just keeping you in the loop.

**At cap:**

> 💛 You've hit your daily cap of 100 messages. Resets at 7:00 AM. You're doing great — tomorrow's a new day!

Suppressed if already nudged within the last 5 minutes.

### 4.2 Cooldown

> ☕ Time for a 5-minute breather. Bot commands are paused until 3:47 PM. Stretch, hydrate, look out a window.

### 4.3 Friction (Per-User Slow Mode)

When friction activates, the bot enforces a per-user slow mode by monitoring message frequency. If the user posts faster than their configured rate, the bot deletes the message and DMs them with the content so nothing is lost.

**How it works:**

1. Bot tracks each user's last message timestamp per channel
2. If a message arrives before the slow-mode interval has elapsed, the bot deletes it immediately
3. Bot DMs the user with their deleted message content and a countdown to when they can post again
4. Once the interval passes, the next message goes through normally

**DM on deleted message:**

> 🐢 Slow mode is active — your message was held. You can post again in **1:47**.
>
> Your message: *"hey does anyone want to play tonight"*
>
> *Adjust your settings anytime with `/wellness settings` or the web panel.*

The slow mode rate is configurable per user (default: 1 message per 2 minutes). Slow mode is lifted when the cap window resets or blackout ends. Requires **Manage Messages** bot permission.

### 4.4 Escalating

Per cap window: first overage → nudge, second → cooldown, third+ → friction. Resets each window.

### 4.5 Blackout Entry

During a blackout, the user's enforcement level applies to ALL interactions:

> 🌙 Your **Night Owl** blackout just started. Slow mode is active until **7:00 AM**.
>
> Sleep well! 💚

### 4.6 Away Message (Manual Toggle)

The away message is a standalone feature, not tied to enforcement. Users toggle it on/off like a status:

**Setting it:**

> `/wellness away on` — "Gone fishing 🎣 — back in the morning!"

**What others see** when they @mention or reply to the user:

> 💚 **Ben says:** "Gone fishing 🎣 — back in the morning!"

**Behavior:**
- Manually toggled on/off by the user — independent of caps, blackouts, or enforcement
- Rate limited: once per channel per 30 minutes
- Custom message up to 500 characters, with template variables: `{user}`, `{streak_days}`
- Default (if enabled without a custom message): "💚 Hey! **{user}** is currently away."
- Footer: *"This is an automated wellness boundary message."*
- `/wellness away off` clears the away status immediately

---

## 5. Streaks — Decay Model

### How It Works

A streak day is earned for each calendar day (user's timezone) with no cap or blackout violations. **Streaks never reset to zero.** Instead, violations decay the streak by 10% (rounded up, minimum 1 day).

**Examples:**
- 140-day streak, one violation → loses 14 days → now 126 days
- 20-day streak, one violation → loses 2 days → now 18 days
- 5-day streak, one violation → loses 1 day → now 4 days

Personal best (longest streak) is tracked separately and never changes.

### Messaging

**On violation:**

> 🌱 Your streak dipped from **140** to **126 days** — you're still on a 126-day journey. One day doesn't erase what you've built.

**On personal best:**

> 🔥 New personal best — **150 days!** That's something to be proud of.

### Why Decay Instead of Reset

Research on the Abstinence Violation Effect shows that binary resets (all-or-nothing streaks) cause disproportionate psychological damage — people who lose a long streak often abandon the activity entirely. Decay preserves the sense of progress while still acknowledging the slip. The "never miss twice" principle from habit-formation research shows this approach maintains habits 37% longer than hard resets.

---

## 6. Active in Commitment & Milestones

### Active in Commitment

An opt-in list posted in `#active-in-commitment`. No numbers, no ranking, no streak counts — just names and milestone badges.

> 🌿 **Active in Commitment**
>
> 🏅 **Ben** — 🔥
> 🏅 **Alex** — 🌟
> 🏅 **Jordan** — 💪
> 🏅 **Sam** — 🌱

This is a participation list, not a performance board. It says "these people are showing up for themselves."

### Milestone Badges

| Badge | Earned At | Meaning |
|-------|----------|---------|
| 🌱 | Joined | Getting started |
| 🌟 | 7 days | First week |
| 🔥 | 30 days | One month strong |
| 💪 | 100 days | Triple digits |
| 👑 | 365 days | One full year |

Milestone celebrations are posted in `#active-in-commitment` when earned. Only for opted-in users. No specific numbers — just the badge upgrade.

> 🌟 **Alex** just earned their first-week badge! Welcome to the journey.

---

## 7. Accountability Partners

### Request Flow

`/wellness partner request @user` sends a DM with buttons:

> 💚 **Ben wants to be your accountability partner!**
>
> You'll share milestone updates and cheer each other on.
>
> **[Accept]** **[Decline]**

One-tap interaction. No IDs needed.

### Partnership

- Unlimited partners per user
- `/wellness partner list` shows all partners with their milestone badges
- Either user can dissolve anytime via `/wellness partner dissolve`
- Dissolving preserves both users' streaks
- If a partner leaves the guild, partnership auto-dissolves and the other user is notified

---

## 8. Weekly Summary Reports

Every Sunday at 9:00 AM (user's local timezone):

> 🌿 **Your Week in Review** *(Apr 6–12)*
>
> **Activity:** 487 messages, 3.2 hours voice *(down 15% from last week)*
> **Cap compliance:** 94% — stayed within limits in 17 of 18 windows
> **Streak:** 126 days 🔥 *(personal best: 140)*
>
> *"Consistent effort compounds. You're building something real."*

The AI-generated encouragement line is warm, brief, and never references specific channels or content.

---

## 9. Blackout Templates

| Template | Days | Start | End | Description |
|----------|------|-------|-----|-------------|
| Night Owl | Every day | 23:00 | 07:00 | Sleep boundary |
| Work Hours | Weekdays | 09:00 | 17:00 | Focus during work |
| School Hours | Weekdays | 08:00 | 15:00 | Focus during school |
| Weekend Detox | Sat–Sun | 00:00 | 23:59 | Full weekend disconnect |

Users can apply a template and customize it, or build fully custom recurring schedules with per-day granularity.

---

## 10. Slash Commands — Complete Reference

### User Commands (`/wellness`)

| Command | Description |
|---------|-------------|
| `/wellness setup` | Quick-start onboarding (timezone + enforcement). Assigns role. |
| `/wellness cap add` | Create a cap. Options: scope (global/channel/category/voice), channel, window (hourly/daily/weekly), limit, exclude_exempt (boolean) |
| `/wellness cap list` | Show all caps with current counts |
| `/wellness cap edit` | Edit a cap's limit. Autocomplete by cap label. |
| `/wellness cap remove` | Delete a cap. Autocomplete by cap label. |
| `/wellness blackout add` | Create a blackout. Options: name, start_time, end_time, days |
| `/wellness blackout template` | Apply a preset (night_owl/work_hours/school_hours/weekend_detox) |
| `/wellness blackout list` | Show all blackouts |
| `/wellness blackout toggle` | Enable/disable a blackout. Autocomplete by name. |
| `/wellness blackout remove` | Delete a blackout. Autocomplete by name. |
| `/wellness away on` | Enable away message. Optional: message (500 chars). Variables: `{user}`, `{streak_days}` |
| `/wellness away off` | Disable away message |
| `/wellness away set` | Update away message text without toggling |
| `/wellness away preview` | Preview your away message |
| `/wellness score` | View streak, personal best, milestone badge, and qualitative summary |
| `/wellness partner request` | Send a partner request (DM with Accept/Decline buttons) |
| `/wellness partner list` | Show all partners with milestone badges |
| `/wellness partner dissolve` | End a partnership. Autocomplete by partner name. |
| `/wellness settings` | Change enforcement, notifications (ephemeral/dm/both), public commitment toggle, timezone, slow-mode rate |
| `/wellness pause` | Pause tracking + lift slow mode. Optional duration (e.g. `24h`, `3d`). |
| `/wellness resume` | Resume tracking after a pause |
| `/wellness optout` | Remove role, deactivate tracking, lift slow mode. Settings kept 30 days. |

### Admin Commands (`/wellness-admin`)

All require **Manage Server** permission.

| Command | Description |
|---------|-------------|
| `/wellness-admin setup` | Create wellness role + category + 3 channels. Options: role_name, category_name |
| `/wellness-admin defaults` | Set server defaults: enforcement, hourly/daily/weekly caps, blackout template, crisis_resource_url |
| `/wellness-admin cap add @user` | Create a cap on behalf of a user |
| `/wellness-admin cap edit @user` | Edit a user's cap |
| `/wellness-admin cap remove @user` | Remove a user's cap |
| `/wellness-admin blackout add @user` | Create a blackout on behalf of a user |
| `/wellness-admin blackout remove @user` | Remove a user's blackout |
| `/wellness-admin settings @user` | Change a user's enforcement, notifications, etc. |
| `/wellness-admin exempt add` | Flag a channel as exempt. Options: channel, label |
| `/wellness-admin exempt remove` | Remove exemption from a channel |
| `/wellness-admin exempt list` | Show all exempt channels |
| `/wellness-admin dashboard` | Post server-wide wellness stats embed |

---

## 11. Web Config Panel

Full settings experience at the existing Dungeon Keeper web config tool.

### User-Facing ("My Wellness")

Authenticated via Discord OAuth.

- **Dashboard** — Current streak, personal best, milestone badge, active caps with progress bars, active blackouts on a visual timeline
- **Caps & Limits** — Add/edit/remove caps with sliders. Toggle exclude-exempt per cap. Slow-mode rate configuration.
- **Blackout Schedule** — Visual 24-hour timeline editor with drag handles. Day-of-week checkboxes. Template buttons.
- **Away Message** — On/off toggle, custom message editor with live preview, template variable buttons, character counter
- **Partners** — Current partners, request management, dissolve controls
- **History** — Scrollable weekly report archive, streak trend over time

### Admin Panel

- **Server Defaults** — Default enforcement, caps, blackout template, crisis resource URL
- **User Management** — Searchable user list. Click to expand — view/edit settings on behalf of user
- **Exempt Channels** — Channel list with exempt toggle and label editor
- **Dashboard** — Active users, average streak, top milestone distribution, users currently in blackout or slow mode

---

## 12. Language & Tone

### Word Choices

| Instead of... | Use... |
|---------------|--------|
| violation | overage, busy day |
| restricted / blocked | slowed down, taking it easy |
| warning | heads up |
| failed / reset | dipped, still on the journey |
| exceeded | hit your cap, went over |
| punishment | boundary, limit |
| tracked / monitored | keeping count, staying mindful |

### Emoji Guide

| Emoji | Meaning |
|-------|---------|
| 💚 | General wellness / away message / positive |
| 💛 | Nudge / heads-up |
| 🌙 | Blackout / sleep / downtime |
| ☕ | Cooldown break |
| 🐢 | Friction / slow mode |
| 🌿 | Weekly report / Active in Commitment |
| 🔥 | 30-day milestone badge |
| 💪 | 100-day milestone badge |
| 👑 | 365-day milestone badge |
| 🌟 | 7-day milestone badge |
| 🌱 | New member / streak dip / fresh energy |

### Tone Rules

- Never guilt-trip. An overage is information, not a moral failing.
- Never compare users to each other. The Active in Commitment list shows badges, not numbers.
- Always frame streak dips as partial: "dipped from X to Y" not "lost X days."
- Keep messages short. If it wouldn't fit in a text message, it's too long.
- The bot's personality: **supportive friend who respects your autonomy** — not coach, not therapist, not authority figure.
- Never frame the tool as recovery or treatment. It's boundary-setting, not intervention.

---

## 13. Research-Informed Design Notes

These design choices are grounded in specific research findings:

| Design Choice | Research Basis |
|---------------|---------------|
| Friction (slow mode) over hard lockouts | InteractOut study: 62% sustained use for friction vs. 36% for lockouts. PNAS "one sec" study: 57% reduction from simple friction pauses. |
| Decay streaks (10% loss) over binary reset | Silverman & Barasch (2023): broken streaks demotivate independent of actual behavior. Abstinence Violation Effect: binary resets trigger abandonment cascades. Lally et al.: missing a single day doesn't derail habit formation. |
| No streak numbers on public display | Health wearable studies: leaderboards demotivate lower-performing users while only helping already-active ones. |
| Harm reduction framing, not abstinence | Prause & Binnie (2024): abstinence-framed communities (NoFap) associated with worse mental health outcomes. ICD-11 CSBD criteria focus on loss of control, not frequency. |
| Unlimited accountability partners | Cochrane Review of AA: social network change is the primary mechanism. NUGU study: groups of 3-6 showed significantly greater reduction. More connections = more support. |
| Away message as manual toggle | Decoupled from enforcement so users maintain full agency. Research shows autonomy-preserving tools produce lasting change vs. compliance-only tools. |
| One-time disclaimer, not repeated | Avoid over-medicalization. Tool should feel like a utility, not clinical intervention. |
| AI encouragement in weekly reports | Meta-analysis: personalized feedback enhances engagement. Kept brief (1-2 sentences) to avoid gamification exhaustion. |

---

## 14. Deferred to v2

- Weighted scoring system (session distribution, time-of-day health)
- Channel weight modes (equal / nsfw_heavier / separate / custom)
- Session summary micro-notifications
- `/wellness insights` baseline retrospective
- Admin per-user lock/override system with transparency DMs
- Behavioral pattern detection (escalating sessions, late-night displacement)
