# Pen Pals — Feature Spec

Members opt in to a pairing pool. When two or more are waiting, the bot creates a private two-person text channel for each matched pair, posts a conversation-starter from the question bank, and tears it down 72 hours later. The goal is low-stakes 1-on-1 connection inside the server.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/penpals join` | Slash | Everyone (server only) | Enter the pairing pool; if a partner is already waiting, pair immediately |
| `/penpals leave` | Slash | Everyone (server only) | Leave the pool before being paired |
| `/penpals status` | Slash | Everyone (server only) | Ephemeral: current pairing state, time remaining, or pool position |
| `/penpals new-question` | Slash | Session members (active channel only) | Replace the current question with a fresh one from the bank (max 3 per session) |
| `/penpals end` | Slash | Everyone (active channel only) | Start a 15-second confirm to close your current pen pal early |
| `/penpals pair <user1> <user2>` | Slash | Manage Guild | Force-pair two specific members, bypassing the pool |
| `/penpals round` | Slash | Manage Guild | Drain the current pool — pair everyone waiting, leave the odd one out in the pool |
| Pen Pals config | Web (dashboard) | Admin | Set category, opt-in role, question category, auto-round schedule |
| Pen Pals questions | Web (dashboard) | Admin / Game Host | Question-bank manager (`game_type = 'pen_pals'`) plus a Prompts & AI studio for the AI-fallback prompt |

## Behaviour

### Pool and pairing

`/penpals join` adds the invoker to the pool. If the pool now has ≥ 2 members, pairing fires immediately: the two longest-waiting members are matched, a private channel is created for them, and the pool shrinks by two. A member already in an active pen pal is blocked from joining again until their session expires or they end it early.

`/penpals round` pairs everyone in the pool in FIFO order. If the pool has an odd count, the last member stays in for the next round. After pairing, a confirmation is posted ephemerally to the invoker: "Paired N members into N/2 channels. 1 member still waiting." The `/penpals round` and auto-round schedule share the same logic.

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

**72-hour countdown.** A background task checks active sessions every 5 minutes. When `now ≥ expiry_at`, the channel is deleted. A warning is posted 1 hour before deletion: "⏰ This pen pal channel closes in 1 hour." Deletion only removes the channel — no transcript is produced.

**Early close.** `/penpals end` in the active channel prompts the invoker for a 15-second confirm. Either member can initiate; the channel is deleted on confirmation. The other member receives a DM: "Your pen pal session in **{server}** was ended early."

**Channel already gone.** If the channel is missing when the bot goes to clean up (deleted by an admin, etc.), the session row is marked `closed` and the cleanup moves on silently.

### Questions

Questions are drawn from `games_question_bank` where `game_type = 'pen_pals'`, using the shared tags model: rows tagged `nsfw` are only served when the guild's question category is `all`. The pair's question history is tracked per session to avoid repeating within the 72-hour window. If the bank is exhausted the bot falls back to AI generation using the same prompt path as the other bank-backed games (`prompt_config.json` → `games.pen_pals`, editable in the dashboard studio); the AI fallback always generates SFW — NSFW prompts come only from the curated bank.

**Automatic cadence.** Every 24 hours after pairing, the bot posts a new question and pings both members:

```
{user1.mention} {user2.mention}
💬 A new question to keep things going:
> {question}
```

This produces up to 3 question posts over the 72-hour session (at 0 h, 24 h, 48 h). The 1-hour-before-close warning from the background task suppresses the 48 h post if fewer than 2 hours remain at that point.

**Manual swap.** `/penpals new-question` posts a question immediately out of cycle, visibly delineating the conversation:

```
{user1.mention} {user2.mention}
🔄 New question ({n} swap(s) remaining):
> {question}
```

A manual swap does not reset the 12-hour auto-cadence clock. After all 3 swaps are used the command is blocked: "You've used all 3 question swaps for this session."

### No-repeat pairing

When picking partners, the bot checks the last **10 pairings** for each user within the guild and avoids re-pairing them if any other valid partner exists. If the pool has only two members and they were recently paired, the bot pairs them anyway and notes the repeat in the confirmation message.

### Opt-in role (optional)

If an opt-in role is configured, `/penpals join` requires the invoker to hold that role. Members without it are told: "You need the **{role}** role to join Pen Pals." This lets the server gate participation (e.g., require Level 5 or a verified role).

### Auto-round schedule

Admins can configure a weekly auto-round (day-of-week + time). When it fires, the bot drains the pool exactly like `/penpals round` and posts a summary to a configured log channel (or silently if none is set).

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

Most servers have hundreds of members who never talk to each other beyond the same handful of regulars. Pen Pals quietly solves that. Members opt in, get matched with someone they probably haven't spoken to one-on-one, and land in a private channel with a conversation starter already waiting — no awkward "so… hi." The 72-hour window creates just enough structure to make it feel like an event rather than a neglected DM thread. After the timer expires the channel disappears, so there's no pressure to maintain it forever. Run a weekly round and you're giving the whole community a slow, low-stakes way to actually meet.
