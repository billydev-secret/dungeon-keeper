# Pen Pals — Feature Spec

Members opt in to a pairing pool. On a schedule (the weekly auto-round) or when an admin runs a round, the bot pairs the waiting members into private two-person text channels, posts a conversation-starter from the question bank into each, and tears them down 24 hours later. A member is only re-matched once they've had no pen pal for a month. The goal is low-stakes 1-on-1 connection inside the server.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/penpals join` | Slash | Everyone (server only) | Enter the pairing pool; you're matched on the next round |
| `/penpals leave` | Slash | Everyone (server only) | Leave the pool before being paired |
| `/penpals status` | Slash | Everyone (server only) | Ephemeral: current pairing state, time remaining, or pool position |
| `/penpals new-question` | Slash | Session members (active channel only) | Replace the current question with a fresh one from the bank (max 3 per session) |
| `/penpals end` | Slash | Everyone (active channel only) | Start a 15-second confirm to close your current pen pal early |
| `/penpals pair <user1> <user2>` | Slash | Manage Guild | Force-pair two specific members, bypassing the pool |
| `/penpals round` | Slash | Manage Guild | Drain the current pool — pair everyone waiting, leave the odd one out in the pool |
| Pen Pals config | Web (dashboard) | Admin | Set category, opt-in role, question category, auto-round schedule |
| Pen Pals questions | Web (dashboard) | Admin / Game Host | Question-bank manager (`game_type = 'pen_pals'`) plus a Prompts & AI studio for the AI-fallback prompt |

## Behavior

### Pool and pairing

`/penpals join` adds the invoker to the pool and nothing more — joining never pairs on the spot. The invoker is told: "You're in the pool! You'll get a private channel the next time matches are drawn." A member already in an active pen pal is blocked from joining again until their session expires or they end it early.

Pairing happens only when a round runs — either the weekly auto-round or `/penpals round`. A round pairs the *eligible* pool members (see **Match cooldown**) in FIFO order, skipping anyone matched within the last month and leaving them in the pool. If an odd number is eligible, the last one stays in for the next round. After pairing, a confirmation is posted ephemerally to the `/penpals round` invoker: "Paired **N** pairs. **M** members still in the pool (waiting or on cooldown)." — the pool count includes members left behind by the cooldown, so it isn't the number who'll pair next round. `/penpals round` and the auto-round share the same logic.

### Channel lifecycle

**Creation.** The bot creates a text channel under the configured Pen Pals category. Name format: `penpals-<user1>-<user2>` (display names, lowercased, spaces replaced with `-`, truncated to 100 chars total). Permissions: only the two members and the bot can view and send. When the guild's question category is `all` (NSFW prompts included), the channel is created age-restricted so the channel gate matches the content that can appear in it.

The bot posts two messages in sequence and pins the first:

**Message 1 — intro embed (pinned)**

```
Title:  🖊️ Pen Pals
Fields: Matched with  |  @user1  @user2
        Session ends  |  <t:{unix_expiry}:F> (<t:{unix_expiry}:R>)
Footer: Admins can see this channel.
        Use /penpals new-question to swap the prompt (3 times max).
```

**Message 2 — first question**

```
{user1.mention} {user2.mention}
💬 Here's your first question:
> {question}
```

The expiry timestamp uses Discord's absolute + relative format so both users see it in their local time.

**24-hour countdown.** A background task checks active sessions every 5 minutes. When `now ≥ expiry_at`, the channel is deleted. A warning is posted 1 hour before deletion: "⏰ This pen pal channel closes in 1 hour." Deletion only removes the channel — no transcript is produced.

**Early close.** `/penpals end` in the active channel prompts the invoker for a 15-second confirm. Either member can initiate; the channel is deleted on confirmation. The other member receives a DM: "Your pen pal session in **{server}** was ended early."

**Channel already gone.** If the channel is missing when the bot goes to clean up (deleted by an admin, etc.), the session row is marked `closed` and the cleanup moves on silently.

### Questions

Questions are drawn from `games_question_bank` where `game_type = 'pen_pals'`, using the shared tags model: rows tagged `nsfw` are only served when the guild's question category is `all`. The pair's question history is tracked per session to avoid repeating within the 24-hour window. If the bank is exhausted the bot falls back to AI generation using the same prompt path as the other bank-backed games (`prompt_config.json` → `games.pen_pals`, editable in the dashboard studio); the AI fallback always generates SFW — NSFW prompts come only from the curated bank.

**Automatic cadence.** The auto-question machinery fires every 24 hours after pairing:

```
{user1.mention} {user2.mention}
💬 A new question to keep things going:
> {question}
```

In practice a 24-hour session shows only the opening question: the first follow-up would land exactly as the channel closes, and the background task suppresses any auto-question with fewer than 2 hours left. Members who want a fresh prompt sooner use the manual swap.

**Manual swap.** `/penpals new-question` posts a question immediately out of cycle, visibly delineating the conversation:

```
{user1.mention} {user2.mention}
🔄 New question ({n} swap(s) remaining):
> {question}
```

A manual swap does not reset the 12-hour auto-cadence clock. After all 3 swaps are used the command is blocked: "You've used all 3 question swaps for this session."

### Match cooldown

A member is only eligible for a new pairing once they've had **no pen pal for a month** (`_MATCH_COOLDOWN_SECS`, 30 days from the `started_at` of their most recent session — active or closed). Members still inside the cooldown are skipped by the round and left untouched in the pool; they become eligible automatically once a month has passed. Because joining never pairs on the spot, the round is the only pairing path, so the cooldown can't be bypassed. `/penpals pair <user1> <user2>` is an explicit admin override and ignores the cooldown.

### No-repeat pairing

Among the *eligible* members in a round, when picking partners the bot checks the last **10 pairings** for each user within the guild and avoids re-pairing them if any other eligible partner exists. If only two eligible members remain and they were previously paired, the bot pairs them anyway.

### Opt-in role (optional)

If an opt-in role is configured, `/penpals join` requires the invoker to hold that role. Members without it are told: "You need the **{role}** role to join Pen Pals." This lets the server gate participation (e.g., require Level 5 or a verified role).

### Auto-round schedule

Admins can configure a weekly auto-round (day-of-week + time). When it fires, the bot drains the pool exactly like `/penpals round` — pairing eligible members and skipping anyone matched within the last month — and posts a summary to a configured log channel (or silently if none is set).

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
- **Log channel** — where the bot posts auto-round summaries and pair confirmations. Optional.
- **Auto-round schedule** — day-of-week + UTC time for automatic pool draining. Optional; disabled by default.
- **Enabled** — per-guild on/off switch. Default off.

## Stored data

**`pen_pals_sessions`** — one row per active or closed pair: `session_id`, `guild_id`, `channel_id`, `user1_id`, `user2_id`, `started_at`, `expiry_at`, `next_question_at`, `question_swaps_used`, `closed_at`, `state` (`active` / `closed`), `close_reason` (`expired` / `early` / `admin` / `channel_missing`). `next_question_at` advances by 24 hours each time an auto question fires; the background task also uses it to decide whether to post.

**`pen_pals_pool`** — one row per queued member: `guild_id`, `user_id`, `joined_at`.

**`pen_pals_questions`** — which questions have been shown per session: `session_id`, `question_id`, `shown_at`. Used for no-repeat within a session and for logging what was asked.

Past pairings are queried from `pen_pals_sessions` for the no-repeat check. No separate history table is needed.

---

## Elevator pitch

Most servers have hundreds of members who never talk to each other beyond the same handful of regulars. Pen Pals quietly solves that. Members opt in, get matched with someone they probably haven't spoken to one-on-one, and land in a private channel with a conversation starter already waiting — no awkward "so… hi." The 24-hour window creates just enough structure to make it feel like an event rather than a neglected DM thread. After the timer expires the channel disappears, so there's no pressure to maintain it forever. Run a weekly round — and with the month-long re-match cooldown, nobody gets paired so often it becomes a chore — and you're giving the whole community a slow, low-stakes way to actually meet.
