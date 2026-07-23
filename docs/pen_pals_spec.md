# Pen Pals — Feature Spec

Members opt in to a pairing pool. Joining pairs immediately when an eligible member is already waiting; otherwise the joiner sits in the pool and is paired the moment the next eligible member joins. The background loop also sweeps each pool every 5 minutes, so anyone who was ineligible at join time is paired within minutes of becoming eligible. Each pair gets a private two-person text channel with a conversation-starter from the question bank posted into it, torn down after a configurable session length (default 24 hours). **A member is in at most one pen pal chat at a time**, and is only re-matched once they've had no pen pal for a configurable cooldown (default a month). The goal is low-stakes 1-on-1 connection inside the server. Session length, match cooldown, question-swap cap, close-warning window, and question-suppress window are all configured on the dashboard's Pen Pals → Pairing Mechanics panel.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/penpals join` | Slash | Everyone (server only) | Matched on the spot if someone eligible is waiting, otherwise queued until the next person joins |
| `/penpals leave` | Slash | Everyone (server only) | Leave the pool before being paired |
| `/penpals status` | Slash | Everyone (server only) | Ephemeral: current pairing state, time remaining, or pool position |
| `/penpals block` | Slash | Everyone (server only) | Ephemeral panel to manage your own "never match me with these members" list |
| `/penpals new-question` | Slash | Session members (active channel only) | Replace the current question with a fresh one from the bank (max 3 per session) |
| `/penpals end` | Slash | Everyone (active channel only) | Start a 15-second confirm to close your current pen pal early |
| `/penpals pair <user1> <user2>` | Slash | Manage Guild | Force-pair two members who are both waiting in the pool, bypassing queue order and cooldown |
| `/penpals round` | Slash | Manage Guild | Force a pool sweep now instead of waiting for the 5-minute tick |
| Pen Pals config | Web (dashboard) | Admin | Set category, opt-in role, question category, log + panel channels; manage never-match separations |
| Pen Pals questions | Web (dashboard) | Admin / Game Host | Question-bank manager (`game_type = 'pen_pals'`) plus a Prompts & AI studio for the AI-fallback prompt |

## Behavior

### Pool and pairing

`/penpals join` adds the invoker to the pool, then immediately looks for a partner. A candidate is eligible when they are **not already in an active session** and are past the re-match cooldown; among the eligible waiters (oldest signup first) the bot prefers one the invoker hasn't been paired with recently, falling back to the oldest. If a partner is found the channel opens right there and the invoker is told "🖊️ Matched! Say hi to @them in #channel." If nobody is eligible — an empty pool, everyone waiting is on cooldown or already chatting, or the invoker is themselves on cooldown — they're told "You're in the pool! The moment someone else joins, your private channel opens automatically." A member already in an active pen pal is blocked from joining at all until their session expires or they end it early; a pairing that fails (permissions, a lost race, a member who left) leaves the joiner in the pool rather than costing them their spot.

**One chat at a time** is enforced in three places: the join guard, the eligibility filter (so a stale pool row can never hand someone a second channel), and a final re-check inside the pairing transaction that aborts and deletes the freshly-created channel if a concurrent join won the race.

Rounds are the sweeper for whoever instant matching left behind (the odd one out, members who joined while on cooldown, failed pairs). A round pairs the *eligible* pool members (see **Match cooldown**) in FIFO order, leaving ineligible members in the pool; if an odd number is eligible, the last one stays in. The background tick runs a round for every enabled guild whose pool holds **two or more eligible members** — checked every 5 minutes, so a backlog clears on its own without any schedule. `/penpals round` forces the same sweep immediately and reports ephemerally: "Paired **N** pairs. **M** members still in the pool (waiting or on cooldown)." — the pool count includes members left behind by the cooldown, so it isn't the number who'll pair next.

### Channel lifecycle

**Creation.** The bot creates a text channel under the configured Pen Pals category. Name format: `penpals-<user1>-<user2>` (display names, lowercased, spaces replaced with `-`, truncated to 100 chars total). Permissions: only the two members and the bot can view and send. When the guild's question category is `all` (NSFW prompts included), the channel is created age-restricted so the channel gate matches the content that can appear in it.

The bot posts two messages in sequence and pins the first:

**Message 1 — intro embed (pinned)**

```
Title:  🖊️ Pen Pals
Fields: Matched with  |  @user1  @user2
        Session ends  |  <t:{unix_expiry}:F> (<t:{unix_expiry}:R>)
        Commands      |  /penpals new-question — swap the prompt (3 max)
                         /penpals end — leave this chat early
Footer: Admins can see this channel.
```

**Message 2 — first question**

```
{user1.mention} {user2.mention}
💬 Here's your first question:
> {question}
```

The expiry timestamp uses Discord's absolute + relative format so both users see it in their local time.

**Session countdown (default 24 hours, configurable).** A background task checks active sessions every 5 minutes. When `now ≥ expiry_at`, the channel is deleted. A warning is posted when the configured close-warning window remains (default 1 hour): "⏰ This pen pal channel closes in 1 hour." Deletion only removes the channel — no transcript is produced.

**Early close.** `/penpals end` in the active channel prompts the invoker for a 15-second confirm. Either member can initiate; the channel is deleted on confirmation. The other member receives a DM: "Your pen pal session in **{server}** was ended early."

**Partner leaves mid-session.** If a session member leaves — voluntarily, kicked, or banned — `on_member_remove` closes the session (`close_reason = 'member_left'`), deletes the channel, and returns the *surviving* partner to the pool for a fresh match. The departed member is dropped from the pool and never re-queued. The survivor gets a DM: "Your pen pal session in **{server}** ended early — your partner is no longer available. You've been put back in the Pen Pals pool for a new match." This does **not** run the expiry path, so `pen_pal_complete` (the quest hook) does not fire for an abandoned session.

**Channel deleted.** If a mod deletes an active pen pal channel, `on_guild_channel_delete` closes the session (`close_reason = 'channel_deleted'`) and returns **both** members to the pool with the same DM. Since a ban's channel deletion also fires this event, the close is claimed atomically — whichever handler runs first wins, and the duplicate is a no-op, so no one is re-queued twice.

**Channel already gone.** If the channel is missing when the background task goes to clean up (e.g. it was deleted while the bot was offline, so `on_guild_channel_delete` never fired), the session is closed with `close_reason = 'channel_missing'` and both members are returned to the pool — the same teardown as a manual delete.

### Questions

Questions are drawn from `games_question_bank` where `game_type = 'pen_pals'`, using the shared tags model: rows tagged `nsfw` are only served when the guild's question category is `all`. The pair's question history is tracked per session to avoid repeating within the 24-hour window; the draw itself is round-robin (least-recently-served row first, ties random) so the same small pool doesn't resurface a question across separate sessions until every row has been served once. If the bank is exhausted the bot falls back to AI generation using the same prompt path as the other bank-backed games (`prompt_config.json` → `games.pen_pals`, editable in the dashboard studio); the AI fallback always generates SFW — NSFW prompts come only from the curated bank.

**Automatic cadence.** The auto-question machinery fires every 24 hours after pairing:

```
{user1.mention} {user2.mention}
💬 A new question to keep things going:
> {question}
```

In practice a default-length (24-hour) session shows only the opening question: the first follow-up would land exactly as the channel closes, and the background task suppresses any auto-question if less than the configured question-suppress window remains (default 2 hours). Members who want a fresh prompt sooner use the manual swap.

**Manual swap.** `/penpals new-question` posts a question immediately out of cycle, visibly delineating the conversation:

```
{user1.mention} {user2.mention}
🔄 New question ({n} swap(s) remaining):
> {question}
```

A manual swap does not reset the 24-hour auto-cadence clock. After the configured swap cap is used (default 3) the command is blocked: "You've used all N question swaps for this session."

### Match cooldown

A member is only eligible for a new pairing once they've had no pen pal for a configurable cooldown (default a month, from the `started_at` of their most recent session — active or closed). It applies to both sides of a match and on both paths: instant matching checks the joiner *and* the candidate, and a round skips ineligible members and leaves them untouched in the pool. They become eligible automatically once the cooldown has passed. Set it to 0 to allow back-to-back chats. `/penpals pair <user1> <user2>` is an explicit admin override and ignores the cooldown — but not the one-chat-at-a-time rule, and not consent: both members must already be in the pool (`/penpals join`), the same population a round draws from. There is no bypass flag; if an override is ever wanted it belongs on the dashboard.

### No-repeat pairing

Both paths share `_pick_partner`: among the *eligible* candidates the bot checks the last **10 pairings** for the member within the guild and prefers anyone they haven't been paired with. Avoiding a repeat is a preference, not a gate — if the only eligible candidate is a past partner, the bot pairs them anyway rather than leaving both alone.

### Opt-in role (optional)

If an opt-in role is configured, `/penpals join` requires the invoker to hold that role. Members without it are told: "You need the **{role}** role to join Pen Pals." This lets the server gate participation (e.g., require Level 5 or a verified role).

### Never-match blocks

Two sources feed one exclusion list, and matching treats every entry as **symmetric** — a pairing is skipped whenever any entry connects the two members in either direction:

- **Member blocks (self-service).** `/penpals block` opens an ephemeral panel: a user-select adds people to your personal "never match me with them" list, and a select of your current blocks removes them. One member blocking is enough to prevent that pairing, and the other side is never told they were blocked. Blocking is directional intent but symmetric in effect; it does **not** end a chat you're already in (use `/penpals end` for that). Self- and bot-selections are ignored.
- **Admin separations.** The dashboard Pen Pals panel has a "Never-match separations" section where a mod pairs two members who must never be matched, regardless of either member's own list. Separations are normalized to one row per couple (order-independent) and are independent of member blocks — replacing the separation list never touches anyone's personal blocks, and unblocking a member never clears an admin separation.

Enforcement is layered so no path can slip a blocked pair through: instant matching filters the candidate, a round filters each candidate and leaves anyone with no un-blocked partner pooled for a later round, and a final check inside the pairing transaction (`_do_pair`) refuses — this last one also covers `/penpals pair`, which tells the admin "these two can't be paired — one has blocked the other, or they're on the separations list."

### Pool sweep

There is no round schedule to configure — the weekly auto-round (day-of-week + UTC hour) was removed when instant matching landed, since the 5-minute sweep always gets there first. The `auto_round_dow` / `auto_round_hour` columns remain in `pen_pals_config` but are unread; `last_auto_round_at` now records the last sweep. Pair confirmations go to the configured log channel (or nowhere if none is set).

## User-visible errors

| When | The user sees |
|---|---|
| Pen Pals category not configured | "Pen Pals isn't set up yet — ask an admin." |
| Already in an active session | "You already have an active pen pal. Use `/penpals status` to see it." |
| Already in the pool | "You're already in the pool. Use `/penpals status` to check your position." |
| Not in the pool and no active session | "You're not in the pool. Use `/penpals join` to sign up." |
| Opt-in role required but missing | "You need the **{role}** role to join Pen Pals." |
| `/penpals new-question` outside an active pen pal channel | "This command only works in an active pen pal channel." |
| All 3 question swaps used | "You've used all 3 question swaps for this session." |
| Bot lacks Manage Channels in the category | "I don't have permission to create channels here — ask an admin to fix the bot's permissions." |
| `/penpals pair` on a member who never joined the pool | "**{name}** hasn't opted in to Pen Pals — they need to run `/penpals join` first. Force-pairing skips the queue, not consent." |
| `/penpals pair` on a blocked/separated pair | "These two can't be paired — one has blocked the other, or they're on the Pen Pals separations list. Clear the block first if this is intended." |
| Early-close confirm timed out | "Close cancelled." |
| Invoker already has DMs closed for early-close DM to the other party | Silent — DM failure doesn't block channel deletion. |

## Non-goals

- **No anonymity.** Both members can see who they've been paired with from the moment the channel is created. The appeal is connecting with a known-community-member, not a blind pen pal.
- **No multi-person channels.** Pairs only — no trios or group rooms.
- **No transcript on close.** Channels are deleted without export. Members who want a record should copy it themselves before the timer expires.
- **No image or file restrictions.** The channel is a normal Discord text channel; members can send anything their Discord permissions allow.
- **No carry-over.** When a session ends, no message carries over to a follow-up session. Each pairing starts fresh.

## Configuration

Per-guild keys set via the dashboard:

- **Category** — the Discord category under which pen pal channels are created. Required.
- **Opt-in role** — if set, only members with this role can `/penpals join`. Optional.
- **Question category** — `sfw` (default) or `all` (includes NSFW questions). Optional.
- **Log channel** — where the bot posts pair confirmations. Optional.
- **Enabled** — per-guild on/off switch. Default off.
- **Never-match separations** — mod-defined pairs of members who must never be matched. Optional; independent of members' own `/penpals block` lists.

**Pairing Mechanics** (separate dashboard section):

- **Session length** — how long a matched channel stays open. Default 24 hours.
- **Re-match cooldown** — how long a member must go without a pen pal before they're eligible again. Default 30 days; 0 allows back-to-back chats.
- **Max question swaps** — how many times a pair can swap the conversation-starter per session. Default 3.
- **Close-warning window** — how much session time must remain to post the "closing soon" notice. Default 1 hour.
- **Question-suppress window** — skip posting a new auto-question if less than this much session time remains. Default 2 hours.

## Stored data

**`pen_pals_sessions`** — one row per active or closed pair: `session_id`, `guild_id`, `channel_id`, `user1_id`, `user2_id`, `started_at`, `expiry_at`, `next_question_at`, `question_swaps_used`, `closed_at`, `state` (`active` / `closed`), `close_reason` (`expired` / `early` / `admin` / `channel_missing`). `next_question_at` advances by 24 hours each time an auto question fires; the background task also uses it to decide whether to post.

**`pen_pals_pool`** — one row per queued member: `guild_id`, `user_id`, `joined_at`.

**`pen_pals_questions`** — which questions have been shown per session: `session_id`, `question_id`, `shown_at`. Used for no-repeat within a session and for logging what was asked.

**`pen_pals_blocks`** — never-match entries: `guild_id`, `user_id`, `blocked_user_id`, `source` (`member` / `admin`), `created_at`. Member rows are directional (blocker → blockee); admin rows are normalized to `(min_id, max_id)`, one per couple. The match exclusion (`_is_blocked_pair`) treats any row as symmetric across both directions and both sources.

Past pairings are queried from `pen_pals_sessions` for the no-repeat check. No separate history table is needed.

---

## Elevator pitch

Most servers have hundreds of members who never talk to each other beyond the same handful of regulars. Pen Pals quietly solves that. Members opt in and — if anyone else is waiting — land immediately in a private channel with someone they probably haven't spoken to one-on-one, a conversation starter already waiting, no awkward "so… hi." The 24-hour window creates just enough structure to make it feel like an event rather than a neglected DM thread. After the timer expires the channel disappears, so there's no pressure to maintain it forever. Matching is instant so nobody sits waiting for a scheduled round, while the re-match cooldown keeps it from becoming a chore — a slow, low-stakes way for the whole community to actually meet.
