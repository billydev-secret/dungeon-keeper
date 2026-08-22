# dungeon-keeper

Discord moderation, community, voice, and analytics bot.

Configuration lives on a web dashboard rather than in Discord: members and mods
get slash commands and button panels, admins get 60+ config pages.

### Moderation & safety
- **Jail** — Send a member to a private intake channel with their roles stripped and automatically restored on release. Pull witnesses in or out; every action lands in `/modinfo` history and the audit log.
- **Tickets** — Panel-button private support channels that members or mods open. Claim, escalate, close, reopen, and auto-generate a transcript, all from buttons that survive restarts.
- **Warnings** — Document infractions, review them, and undo them, with configurable escalation to admins at a threshold. `/modinfo` rolls jail history, warnings, and tickets into one profile.
- **AI Moderation** — On-demand AI review of a user, a channel, or a free-form question, backed by a guard model that learns your community's consent norms from confirmed and dismissed flags.
- **Rules Watch** — A passive, recall-leaning monitor that pre-screens public chat with cheap heuristics, then weighs suspicious messages against context signals. Flags route to a human-reviewed queue.
- **Spoiler guard** — Unflagged images in spoiler-required channels are removed with a friendly, self-deleting reminder. Bypass roles keep trusted members exempt.
- **Policies** — Collaborative rule proposals with an open / vote / close / list flow, so policy changes get decided in the open.
- **Purge & privacy** — Bulk-delete messages by count and/or cutoff time. Members erase all of their own data with `/delete_me`; mods fully purge a user with `/delete_user`.

### XP & analytics
- **XP & leveling** — Earn XP from text, replies, voice participation, and reactions on your image posts, with anti-grind multipliers. Level milestones grant roles and trigger announcements.
- **Leaderboards** — Rank top earners by source and time window and see exactly where you stand. The dashboard adds time-to-level histograms and per-source breakdowns.
- **Web analytics dashboard** — 25+ cached, read-only panels (`DASHBOARD_ENABLED=1`; loopback-only origin, published via a Cloudflare tunnel). Caches pre-warm hourly and refresh every 15 minutes, so big servers load instantly.
  - Covers engagement & retention, activity patterns, community structure, growth & onboarding, anomalies & at-risk members, and quality & demographics — DAU/MAU stickiness, cohort retention curves, 7×24 message heatmaps, force-directed interaction graphs, churn-risk scores, participation Gini, chilling-effect detection, and more.
- **Reports** — Member, role, and engagement reports live in the dashboard, and an approved leave-of-absence list keeps those members from being flagged inactive.

### Voice & music
- **Voice Control** — Join a hub channel to instantly spawn your own voice room, then lock, hide, rename, limit, invite, kick, transfer, or claim it. Profiles persist trust lists, blocks, and knock-to-join.
- **Music** — YouTube and Spotify playback via Lavalink with a persistent now-playing card and queue. Mod-only 24/7 mode parks the bot in a channel and auto-queues from a playlist when idle.

### Party games

A 17-game social suite sharing session windows, anonymous audit logging,
per-guild enable/disable, channel allowlists, and an AI question-bank fallback.
Spicier prompts appear only in channels an admin has marked age-restricted in
Discord itself.

Truth or Dare (anonymous, classic, and banner variants), Would You Rather,
Never Have I Ever, Most Likely To, Marry / Fornicate / Kiss, Two Truths & a Lie,
Spin the Compliment, Hot Takes, Story Builder, Anonymous AMA, Fantasies &
Dealbreakers, Name Your Price, Mt. Rushmore Draft, Clapback, and LegitLibs.

### Head-to-head & group games

High-stakes games with server-authoritative hidden state, per-pair cooldowns,
audit logging, and 24-hour auto-reverting nickname stakes (or custom cosmetic
stakes).

Pressure Cooker (1v1 gauge duel), Quickdraw (hidden-timer reflex duel), Hot
Potato (duel or group free-for-all), Chicken (bail before the crash), and
Musical Chairs (3+ players).

- **Meadow Mahjong** — Card-driven American-style mahjong at 2 or 4 seats, with
  the full Charleston, claim windows, joker redemption, and coin stakes held in
  escrow. Hands are matched against an original seasonal Meadow Card managed
  from the dashboard. `docs/meadow_mahjong_spec.md`.

### Economy & perk shop
- **Coins & wallet** — Earn server currency from daily logins, chatting, voice, games, reactions, and QOTD answers, all recorded in a full ledger with a per-member wallet view.
- **Quests & daily boards** — A personal daily/weekly/monthly quest board draws each member their own slice of the guild's quest pool, alongside tiered community weeklies with a live tracker.
- **Perk Shop & rentals** — Rent custom role colors, gradients, holographic colors, role icons, emoji slots, voice styling, gifts, and QOTD sponsorship. Rentals auto-bill weekly; cancel for a pro-rated refund.
- **Sinks & stakes** — Coin wagers on duel and group games, paid quest rerolls, raffles, auctions, and community bounties keep the currency circulating. `docs/economy_spec.md` is the deep doc.
- **The Golden Meadow Casino** — Button-driven house gambling in one admin-configured channel: coinflip, slots, blackjack, roulette, a six-critter derby, baccarat, sic bo, war, and keno. RTP-tested paytables, a progressive jackpot, and dashboard-set wager caps. `docs/casino_spec.md`.

### Engagement & content
- **Whisper** — Send an anonymous message to an opted-in member, who gets three guesses to name you. Share publicly, reply back, or reveal yourself once guessed.
- **Pen Pals** — Scheduled rounds pair pool members (never re-matched within a month) into private channels seeded with a conversation-starter question, torn down after ~24 hours.
- **Confessions** — Post an anonymous confession to a channel or forum thread with anonymous-reply buttons, using either a stable per-thread identity or a fresh one. Mirrors to a mod-only log.
- **Starboard** — Messages crossing a reaction threshold repost to a dedicated board. Self-stars don't count, and an NSFW guard keeps age-gated content out of SFW channels.
- **Quote** — Right-click any message to render it as a styled quote card over the author's avatar, with theme and font pickers.
- **Auto-react** — Automatically drop chosen emoji on images and embeds in configured channels, so visual content gets the engagement it deserves.
- **Needle (auto-thread)** — Automatically spawn a thread from each new message in designated channels, with custom thread names, welcome messages, and status-reaction tracking.
- **Photo Challenge** — Challenge cards post to a dedicated channel on their own schedule, and posting a photo pays a once-daily participation award plus a quest bonus.
- **Chat Revive ("Ember")** — A dashboard-managed lull watcher that drops a conversation-starter question when a watched channel goes quiet — rhythm-aware, budgeted, with an opt-in ping button.
- **Greeting Watch** — When a member's "good morning" in a watched channel goes unanswered, the bot quietly DMs them a hello so nobody greets an empty room.
- **QA Tracker** — Behavior-changing updates post QA cards with Pass / Fail / Blocked buttons; volunteer testers earn economy coins per verdict, with admin oversight on the dashboard.
- **Bios** — Members build rich, multi-field profiles through an interactive wizard, and finished bios live as persistent cards in a dedicated channel.
- **Emoji Stealer** — Right-click a message or paste an image URL to upload it as a custom emoji to one of your servers.
- **Bump Tracker** — Track cooldowns for listing sites like DISBOARD and get pinged the moment each is ready to bump again, with a live status widget.
- **Risky Rolls** — Everyone rolls 1–100; the highest unique roll asks a question and the lowest answers. Special rolls unlock variants.
- **Guess** — Consenting members submit an NSFW image that the bot auto-crops with face-excluding detection, and the community guesses the submitter. Leaderboards track accuracy.

### Onboarding & community
- **Role grants** — Hand out community roles through a per-role permission allowlist (greeters can grant Denizen, mods can grant NSFW or Veteran) without handing anyone Manage Roles.
- **Role menus** — Self-assign roles via persistent button or dropdown menus, built, previewed, and published entirely from the dashboard's Oracle builder.
- **Announcements** — Dashboard-queued one-shot channel posts: embed + ping line, live preview, guild-local scheduling, sent history, and up to five self-assign role buttons.
- **Docs** — Author rules pages, guides, and FAQs on the dashboard and post them into channels as bot-maintained messages; editing re-renders every posted copy in place.
- **Welcome / leave** — Configurable join and leave messages, edited and previewed live from the dashboard.
- **Booster role buttons** — Persistent click-to-claim buttons for booster perks that survive restarts. Set them up once and they keep working.
- **Birthday** — Members record a birthday and the bot posts a daily celebration from a customizable template. The dashboard previews the next 90 days.
- **DM permissions** — An opt-in consent system: members pick Open / Ask / Closed, requests route through panel and DM buttons, and either side can revoke with mutual notification.
- **Intake cards** — Per-newcomer welcome-checklist cards in greeter chat that auto-tick as the procedure happens. Dashboard-configured, no commands.
- **Server todo** — A shared task list mods manage from Discord, from a live channel board with Add and Complete buttons, or from the dashboard. Recurring entries drop reminders on a schedule.
- **Watch list** — Quietly forward a member's public posts to your DMs: a light touch for keeping an eye on a situation without a heavy moderation footprint.

### Wellness
- **Wellness Guardian** — A self-managed boundary tool: opt in, set message and voice caps, schedule blackout windows, and pair with an accountability partner. Limits apply gentle friction, never lockouts.

### Setup & utilities
- **`/setup`** — Two-phase first-time setup: provision every bot channel and category, then walk a wizard for mod/admin roles, jail/ticket categories, and log channels.
- **`/help`** — A contextual command reference that only shows the sections your permissions unlock.
- **`/ask` — Billy-bot** — An AI helper answering "how do I use X" in plain language, grounded in the server guide. Admins also get setup-gap review and settings changes proposed behind permission-checked Apply buttons.
- **`/invite` / `/support`** — Quick links to invite the bot and reach the support server.
- **Owner tools** — Hot-reload a cog, and run the one-time Spotify auth flow.

### Background services
- DB backup loop, voice-XP loop, sentiment-score backfill, message archive, health-metrics batch (15 min), and reports cache warmer (hourly) keep analytics fresh and data durable.

## Quick Start

```bash
python -m venv .venv
.\.venv\Scripts\activate        # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -e .
cp .env.example .env            # fill in DISCORD_TOKEN
python -m dungeonkeeper
```

For full setup instructions — bot permissions, guild configuration, the
optional music stack (Lavalink + Spotify), and production deployment — see
[DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Environment

Required:
- `DISCORD_TOKEN` — bot token from the [Discord Developer Portal](https://discord.com/developers/applications)

Optional:
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` / `LAVALINK_PASSWORD` — music cog
- `DASHBOARD_ENABLED=1`, `DASHBOARD_HOST`, `DASHBOARD_PORT` — web dashboard

## Configuration

Runtime config is stored in `dungeonkeeper.db` (`config` and `config_ids`
tables). Nearly everything is configured through the web dashboard once the bot
is running; the keys below are the bootstrap set.

| Key | Description |
|-----|-------------|
| `debug` | `1` = guild-scoped command sync (dev), `0` = global sync (production) |
| `guild_id` | Target guild ID (required in debug mode) |
| `mod_channel_id` | Channel for moderation notifications |
| `xp_level_5_role_id` | Role granted at XP level 5 |
| `xp_level_5_log_channel_id` | Channel for level-5 milestone announcements |
| `xp_level_up_log_channel_id` | Channel for all level-up announcements |
| `greeter_role_id` | Greeter role (also pinged by intake cards) |

`config_ids` buckets: `spoiler_required_channels`, `bypass_role_ids` (exempt
from spoiler guard), `xp_grant_allowed_user_ids`, and
`xp_excluded_channel_ids`.

Role grants are no longer configured through flat keys — they live in
`grant_roles` rows managed from the dashboard, and legacy keys are migrated on
startup (see `docs/role_grant_spec.md`).

## Commands

Dungeon Keeper registers around 90 slash commands. `/help` gives a
permission-aware reference inside Discord, and the dashboard's Help panel
carries the full illustrated manual — both stay current automatically, so this
list is only a starting point.

| Command | What it does |
|---------|--------------|
| `/setup` | Provision channels, then walk the config wizard |
| `/help` | Contextual command reference |
| `/ask` | Billy-bot, the AI how-do-I helper |
| `/games play <game>` | Start a party game in an allowed channel |
| `/bank wallet` / `/bank shop` | Balance and ledger; browse and rent perks |
| `/voice access <state>` | One dial for who gets into your voice room |
| `/play <query>` | Play a YouTube/Spotify URL or search |
| `/jail` / `/warn` / `/modinfo` | Core moderation actions and history |
| `/purge [count] [after]` | Bulk-delete messages |
| `/delete_me` | Erase all of your own data |

## Development

Run the full gate (ruff + pyright + the whole pytest suite, xdist-parallel):

```bash
python scripts/gate.py
```

Useful variants:

```bash
python scripts/gate.py --quick    # ruff + pyright only (plus scoped browser
                                  # panel checks when dashboard assets changed)
python scripts/gate.py --scoped   # ruff + pyright + just the tests mapped to
                                  # your staged diff
```

The pre-commit hook runs `python scripts/gate.py --scoped` automatically on
every commit; touching broadly-shared files (core/, models/, migrations/, deps)
falls back to the full suite. CI runs the full suite + coverage on every push.

A dev-only sidecar drives synthetic Discord activity in the test guild for
moderator testers; it refuses to run outside `BOT_ENV=dev`. Setup and design are
in [docs/beta_tools_spec.md](docs/beta_tools_spec.md).
