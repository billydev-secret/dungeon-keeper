# Feature map (Reference)

Every feature area Dungeon Keeper ships, grouped the way the dashboard groups
them. This is the complete list; [README.md](../README.md) is the short pitch,
and the two reference surfaces that stay current automatically are `/help`
inside Discord and the illustrated manual in the dashboard's own Help panel
(`src/web_server/static/manual.html`).

Configuration lives on the web dashboard rather than in Discord: members and
mods get slash commands and button panels, admins get a dashboard of over 130
pages. The bot registers about 160 slash commands and 6 right-click menus.

## Moderation & safety

- **Jail** — Send a member to a private intake channel with their roles stripped and automatically restored on release. Pull witnesses in or out; every action lands in `/modinfo` history and the audit log.
- **Inactive hold** — The softer sibling of jail for idle members: roles are snapshotted and stripped, everyone held shares one channel, and a persistent panel there lets them open a ticket to be reactivated. Members enter by hand or by sweep.
- **Tickets** — Panel-button private support channels that members or mods open. Claim, escalate, close, reopen, and auto-generate a transcript, all from buttons that survive restarts.
- **Warnings** — Document infractions, review them, and undo them, with configurable escalation to admins at a threshold. `/modinfo` rolls jail history, warnings, and tickets into one profile.
- **No-contact list** — Discord's block stops someone messaging you; it does not stop them reaching you *through* a bot. Adding someone to your no-contact list closes that gap in both directions across every surface that can carry a message between two people — whispers, AMA questions, confession replies, Guess Who, Pen Pals matching, voice rooms, DM requests, Risky Rolls, and pay/gift notifications. Much of it is invisible by design: the dice simply never pair you, and the blocked party can't tell the refusal from an ordinary outcome.
- **AI Moderation** — On-demand AI review of a user, a channel, or a free-form question, backed by a guard model that learns your community's consent norms from confirmed and dismissed flags.
- **Rules Watch** — A passive, recall-leaning monitor that pre-screens public chat with cheap heuristics, then weighs suspicious messages against context signals. Flags route to a human-reviewed queue.
- **Spoiler guard** — Unflagged images in spoiler-required channels are removed with a friendly, self-deleting reminder. Bypass roles keep trusted members exempt.
- **Policies** — Collaborative rule proposals with an open / vote / close / list flow, so policy changes get decided in the open.
- **Purge, cleanup & privacy** — Bulk-delete messages by count and/or cutoff time, plus scheduled cleanup: a server-wide sweep that retires everything past a set age outside protected channels, and per-channel schedules on their own age and interval. Pinned messages are never touched. Members erase all of their own data with `/delete_me`; mods fully purge a user with `/delete_user`.

## XP & analytics

- **XP & leveling** — Earn XP from text, replies, voice participation, and reactions on your image posts, with anti-grind multipliers. Level milestones grant roles and trigger announcements.
- **Leaderboards** — Rank top earners by source and time window and see exactly where you stand. The dashboard adds time-to-level histograms and per-source breakdowns.
- **Web analytics dashboard** — 27 cached, read-only report panels (`DASHBOARD_ENABLED=1`; loopback-only origin, published via a Cloudflare tunnel). Caches pre-warm hourly and refresh every 15 minutes, so big servers load instantly.
  - Covers engagement & retention, activity patterns, community structure, growth & onboarding, anomalies & at-risk members, and quality & demographics — DAU/MAU stickiness, cohort retention curves, 7×24 message heatmaps, churn-risk scores, participation Gini, chilling-effect detection, and more.
  - **Connection graph** — a drag-and-zoom map of who interacts with whom, sized by activity and colored by the friend group the math found, with a replay that plays the network back week by week as people arrive, drift between groups and leave.
  - **Ping response** — when the server pings a role, does anybody turn up? Turnout is counted as distinct people who posted or reacted inside the response window, with the game's actual roster alongside it when the bot's ping launched one.
  - **One-sided attention** — a moderator-review report surfacing pairs where one person keeps directing attention at another who isn't returning it.
- **Reports** — Member, role, and engagement reports live in the dashboard, and an approved leave-of-absence list keeps those members from being flagged inactive.

## Voice & music

- **Voice Control** — Join a hub channel to instantly spawn your own voice room, then lock, hide, rename, limit, invite, kick, transfer, or claim it. Profiles persist trust lists, blocks, and knock-to-join.
- **Music** — YouTube and Spotify playback via Lavalink with a persistent now-playing card and queue. Mod-only 24/7 mode parks the bot in a channel and auto-queues from a playlist when idle.
- **Music Playlist** — One watched channel feeds a rolling Spotify playlist: links are parsed and confidence-matched, with a dashboard review queue for anything uncertain.

## Party games

A 17-game social suite sharing session windows, anonymous audit logging,
per-guild enable/disable, channel allowlists, and an AI question-bank fallback.
Spicier prompts appear only in channels an admin has marked age-restricted in
Discord itself.

Truth or Dare (anonymous, classic, and banner variants), Would You Rather,
Never Have I Ever, Most Likely To, Marry / Fornicate / Kiss, Two Truths & a Lie,
Spin the Compliment, Hot Takes, Story Builder, Anonymous AMA, Fantasies &
Dealbreakers, Name Your Price, Mt. Rushmore Draft, Clapback, and LegitLibs.

## Head-to-head & group games

High-stakes games with server-authoritative hidden state, per-pair cooldowns,
audit logging, and 24-hour auto-reverting nickname stakes (or custom cosmetic
stakes).

Pressure Cooker (1v1 gauge duel), Quickdraw (hidden-timer reflex duel), Hot
Potato (duel or group free-for-all), Chicken (bail before the crash), and
Musical Chairs (3+ players).

- **Meadow Mahjong** — Card-driven American-style mahjong at 2 or 4 seats, with
  the full Charleston, claim windows, joker redemption, and coin stakes held in
  escrow. Hands are matched against an original seasonal Meadow Card managed
  from the dashboard. `meadow_mahjong_spec.md`.
- **Survivor** — A season-long NFL pick'em survival pool. Pick one team to win
  each week, straight up, and never the same team twice; lose and you're out,
  and the last one standing takes the coin pot. Picks stay secret until the
  weekly reveal, so table talk is legal and lying is encouraged. The channel's
  pinned panel keeps itself at the bottom with the standings and the living and
  eliminated named. `plans/survivor.md`.

## Economy & perk shop

- **Coins & wallet** — Earn server currency from daily logins, chatting, voice, games, reactions, and QOTD answers, all recorded in a full ledger with a per-member wallet view.
- **Quests & daily boards** — A personal daily/weekly/monthly quest board draws each member their own slice of the guild's quest pool, alongside tiered community weeklies with a live tracker.
- **Perk Shop & rentals** — Rent custom role colors, gradients, holographic colors, role icons, emoji slots, voice styling, gifts, and QOTD sponsorship. A showroom shows every color as a picture with its weekly price rather than asking you to imagine a hex code. Servers can also sell whatever they like alongside the built-ins — a shoutout, a custom emoji, a favour — as one-off or weekly items, routed either to an instant role grant or to the mod team's list. Rentals auto-bill weekly; cancel for a pro-rated refund.
- **Sinks & stakes** — Coin wagers on duel and group games, paid quest rerolls, raffles, auctions, community bounties, and tiered community goals keep the currency circulating. `economy_spec.md` is the deep doc.
- **Pools** — Once a day the bot opens one prediction market in its own channel on a metric it hasn't run recently: will the server do more than the line today? Back Over or Under; the winning side splits the whole pool in proportion to stake, so the payout moves every time somebody bets and the panel shows the implied odds live.
- **The Golden Meadow Casino** — Button-driven house gambling in one admin-configured channel: coinflip, slots, blackjack, roulette, a six-critter derby, baccarat, sic bo, war, and keno. RTP-tested paytables, a progressive jackpot, and dashboard-set wager caps. `casino_spec.md`.
- **Mention Awards** — For the games the bot doesn't host: an announcement matching a rule's conditions pays whoever it @-mentions, so a member-run rotation can still settle up in server currency.

## Engagement & content

- **Whisper** — Send an anonymous message to an opted-in member, who gets three guesses to name you. Share publicly, reply back, or reveal yourself once guessed.
- **Pen Pals** — Scheduled rounds pair pool members (never re-matched within a month) into private channels seeded with a conversation-starter question, torn down after ~24 hours.
- **Confessions** — Post an anonymous confession to a channel or forum thread with anonymous-reply buttons, using either a stable per-thread identity or a fresh one. Mirrors to a mod-only log, with an optional approval queue.
- **Starboard** — Messages crossing a reaction threshold repost to a dedicated board. Self-stars don't count, and an NSFW guard keeps age-gated content out of SFW channels.
- **Quote** — Right-click any message to render it as a styled quote card over the author's avatar, with theme and font pickers.
- **Auto-react** — Automatically drop chosen emoji on images and embeds in configured channels, so visual content gets the engagement it deserves.
- **Needle (auto-thread)** — Automatically spawn a thread from each new message in designated channels, with custom thread names, welcome messages, and status-reaction tracking.
- **Photo Challenge** — Challenge cards post to a dedicated channel on their own schedule, and posting a photo pays a once-daily participation award plus a quest bonus.
- **Chat Revive ("Ember")** — A dashboard-managed lull watcher that drops a conversation-starter question when a watched channel goes quiet — rhythm-aware, budgeted, with an opt-in ping button.
- **Greeting Watch** — When a member's "good morning" in a watched channel goes unanswered, the bot quietly DMs them a hello so nobody greets an empty room.
- **Event Echo** — A small note in main chat whenever something worth joining is happening elsewhere, with a link that drops you on it: a game opening for players, an auction or betting round about to close, a fresh week of quests. It never pings — the note is for people already reading — and rate limits keep it rare.
- **Feature rotation** — Instead of every activity channel sitting open all the time, a pool of them takes turns: one room is open today and the rest are tucked away until theirs comes round, with a note in main chat saying which. Rooms can arrive with their game already running, and the game is closed out and paid properly when the day ends. Anything reachable by slash command keeps working while its room is out of sight.
- **QA Tracker** — Behavior-changing updates post QA cards with Pass / Fail / Blocked buttons; volunteer testers earn economy coins per verdict, with admin oversight on the dashboard.
- **Bios** — Members build rich, multi-field profiles through an interactive wizard, and finished bios live as persistent cards in a dedicated channel.
- **Emoji Stealer** — Right-click a message or paste an image URL to upload it as a custom emoji to one of your servers.
- **Bump Tracker** — Track cooldowns for listing sites like DISBOARD and get pinged the moment each is ready to bump again, with a live status widget.
- **Risky Rolls** — Everyone rolls 1–100; the highest unique roll asks a question and the lowest answers. Special rolls unlock variants.
- **Guess** — Consenting members submit an NSFW image that the bot auto-crops with face-excluding detection, and the community guesses the submitter. Leaderboards track accuracy.

## Onboarding & community

- **Role grants** — Hand out community roles through a per-role permission allowlist (greeters can grant Denizen, mods can grant NSFW or Veteran) without handing anyone Manage Roles.
- **Role menus** — Self-assign roles via persistent button or dropdown menus, built, previewed, and published entirely from the dashboard's Oracle builder.
- **Announcements** — Dashboard-queued one-shot channel posts: embed + ping line, live preview, guild-local scheduling, sent history, and up to five self-assign role buttons.
- **Docs** — Author rules pages, guides, and FAQs on the dashboard and post them into channels as bot-maintained messages; editing re-renders every posted copy in place.
- **Welcome / leave** — Configurable join and leave messages, edited and previewed live from the dashboard.
- **Booster role buttons** — Persistent click-to-claim buttons for booster perks that survive restarts. Set them up once and they keep working.
- **Birthday** — Members record a birthday and the bot posts a daily celebration from a customizable template. The dashboard previews the next 90 days.
- **DM permissions** — An opt-in consent system: members pick Open / Ask / Closed, requests route through panel and DM buttons, and either side can revoke with mutual notification.
- **Intake cards** — Per-newcomer welcome-checklist cards in greeter chat that auto-tick as the procedure happens. Dashboard-configured, no commands.
- **Server todo & chore board** — A shared task list mods manage from a live channel board with Add and Complete buttons, or from the dashboard. Above the tasks sit today's recurring chores, kept on screen once ticked so the board answers "did we do it today?" — each with who did it, when, and how many days running. A chore can sign itself off when the bot sees the work happen (a QOTD posted, a mod-run game started). Quest sign-offs and paid member requests queue on the same board.
- **Watch list** — Quietly forward a member's public posts to your DMs: a light touch for keeping an eye on a situation without a heavy moderation footprint.

## Wellness

- **Wellness Guardian** — A self-managed boundary tool: opt in, set message and voice caps, schedule blackout windows, and pair with an accountability partner. Limits apply gentle friction, never lockouts.

## Setup & utilities

- **`/help`** — A contextual command reference that only shows the sections your permissions unlock.
- **`/info`** — A member's own card: their stats, perks and settings, with buttons that re-enter each feature's own consent flow rather than granting anything outright.
- **`/ask` — Billy-bot** — An AI helper answering "how do I use X" in plain language, grounded in the server guide. Admins also get setup-gap review and settings changes proposed behind permission-checked Apply buttons.
- **Hidden channels** — Hide a channel from everyone and later restore it exactly as it was, permission overwrites and placement intact.
- **`/invite` / `/support`** — Quick links to invite the bot and reach the support server.
- **Owner tools** — Hot-reload a cog, and run the one-time Spotify auth flow.

First-time configuration is done on the dashboard, not in Discord — see
[DEPLOYMENT.md](DEPLOYMENT.md) §6.

## Background services

- DB backup loop, voice-XP loop, sentiment-score backfill, message archive,
  cleanup sweeps, the midnight feature-rotation roll, health-metrics batch
  (15 min), and reports cache warmer (hourly) keep analytics fresh and data
  durable.
