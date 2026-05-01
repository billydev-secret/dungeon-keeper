# Beta Tools Sidecar — Design Spec

**Date:** 2026-04-30
**Status:** Draft for implementation
**Target:** Dungeon Keeper bot, dev environment
**Companion to:** `DUNGEON_KEEPER_TEST_ENV_SPEC.md` (v1.2) — extends the dev-environment story with synthetic activity for beta moderator testers

---

## 1. Goals & Non-Goals

### Goals

- Stand up a beta copy of the prod server that **feels and runs like a real server** for moderator testers.
- Produce continuous, realistic-looking traffic (messages, reactions, voice, joins/leaves) at prod-mirrored cadences and volumes.
- Pre-populate `dk_dev.db` with believable historical state so leaderboards, scoring, reports, and dashboards look populated from day one.
- Provide deliberate scenario triggers that exercise every feature path of every cog, including flows that ambient activity can't reliably hit (jail full cycle, ticket lifecycle, starboard hits, birthday firing, etc.).
- Make the whole thing physically incapable of running in prod via independent layers of safety rails.

### Non-Goals

- Time-travel / global clock fudging. Per-scenario time mutations only (e.g., scenario sets a puppet's birthday row to today, not "advance bot's clock 30 days").
- Audio playback or voice transmission by puppets. Puppets connect their voice gateway state but transmit no audio. Music and TTS audio playback is the main bot's job and is exercised by mods directly.
- Full member-list parity with prod. The beta guild has 3 puppets + ~50 DB-only ghosts + real mod testers. Member list won't have hundreds of avatars; that's a known limitation.
- Performance / load testing. Sim cadence is calibrated to look real, not stress the system.
- Replacing the existing test-env spec. This builds on top.

---

## 2. Architecture Overview

Two independent processes share the dev environment.

```
DK[DEV]                                     DK Tools                           3 Puppet bots
(main bot, all 30 cogs)                     (sidecar, beta-only)               (gateway-only clients
       │                                          │                              spun up by DK Tools)
       │ — connects to dk_dev.db ─────────────────│ — connects to dk_dev.db
       │                                          │
       │ — sees Discord events from puppets       │ — drives puppets via API
       │   (on_message, reactions, etc.)          │ — sends webhook ghost chatter
       │                                          │ — registers /beta commands
       │                                          │ — runs ambient sim loop
       │                                          │ — runs scenarios
```

**DK[DEV]** is the existing main bot in dev mode. Untouched by this spec — no new code paths, no `if cfg.is_dev` branching beyond what already exists. It receives real Discord events from puppets and webhook authors and processes them through its normal handlers; it has no awareness that those events are synthetic.

**DK Tools** is a new sidecar bot with its own Discord application + token. It runs only in dev, only when `BETA_TOOLS_ENABLED=1`, only against the test guild. It manages 3 puppet client connections, the webhook fleet, the Markov generator, the ambient sim loop, the scenario runner, and the slash command surface.

**Puppets** are 3 separate Discord bot accounts (each its own app + token) that DK Tools spins up as in-process `discord.Client` instances. They idle on the gateway and act on instructions from DK Tools (post a message, react, join voice, leave voice). From Discord's perspective and from DK[DEV]'s perspective, they're real members.

### 2.1 Repository Layout

The sidecar lives in the same repo as a sibling top-level package. Shared infrastructure (config, migrations, ID remap) is reused; sim-specific code is isolated.

```
dungeonkeeper/
├── dungeonkeeper.py                # main bot entry (existing, unchanged)
├── config.py                       # shared (existing)
├── migrations/                     # shared (existing)
│   └── 00X_beta_source_tags.sql    # NEW — adds source columns to affected tables
├── id_remap.py                     # shared (existing)
├── cogs/                           # untouched by this work
├── beta_tools/                     # NEW — sidecar package
│   ├── __main__.py                 # python -m beta_tools entry
│   ├── bot.py                      # Bot class for DK Tools
│   ├── puppet_manager.py           # spins up + manages 3 puppet clients
│   ├── webhook_fleet.py            # per-channel webhook handles for ghost chatter
│   ├── markov.py                   # per-channel order-3 word-level chains (JSON-backed)
│   ├── traffic_profile.py          # loads + samples from prod_traffic_profile.json
│   ├── ambient_sim.py              # background event-scheduling loop
│   ├── seeder.py                   # one-shot DB-ghost historical seed
│   ├── safety.py                   # extra guards layered on existing safety.py
│   ├── side_writes.py              # XP/scoring/message_store side-writes for ghost messages
│   ├── slash/                      # /beta slash commands
│   │   ├── sim.py
│   │   ├── scenarios.py
│   │   ├── puppets.py
│   │   ├── ghosts.py
│   │   ├── seed.py
│   │   ├── cleanup.py
│   │   ├── profile.py
│   │   └── health.py
│   └── scenarios/                  # one file per cog being scenario'd
│       ├── _base.py                # Scenario base class + registry
│       ├── jail.py
│       ├── tickets.py
│       ├── automod.py
│       ├── starboard.py
│       ├── watch.py
│       ├── confessions.py
│       ├── welcome.py
│       ├── birthday.py
│       ├── wellness.py
│       ├── voice_master.py
│       ├── xp.py
│       ├── inactivity_prune.py
│       ├── gender.py
│       ├── denizen.py
│       ├── drama.py
│       ├── interaction.py
│       ├── invite.py
│       ├── dm_perms.py
│       ├── tts.py
│       ├── music.py
│       ├── ai_mod.py
│       ├── foolsday.py
│       ├── booster.py
│       └── support.py
├── scripts/
│   ├── build_traffic_profile.py    # NEW — runs against prod, emits prod_traffic_profile.json
│   └── train_markov.py             # NEW — runs against prod, emits beta_tools/markov_chains/*.json
├── prod_traffic_profile.json       # NEW — committed, regenerated occasionally
└── fixtures/
    ├── beta_puppets.yaml           # NEW — 3 persona configs
    └── beta_db_ghosts.yaml         # NEW — ~50 DB-only ghost roster (also drawn for webhook chatter)
```

`beta_tools/markov_chains/` is gitignored (chain JSON files can be large). A `markov_metadata.json` with sha256 checksums is committed for verification of trained outputs.

### 2.2 Process Model

In dev:

```
Terminal 1: BOT_ENV=dev python -m dungeonkeeper
Terminal 2: BOT_ENV=dev python -m beta_tools
```

Or a `make dev-with-beta` target that runs both. DK can run without DK Tools just fine. DK Tools refuses to start without `BOT_ENV=dev` and `BETA_TOOLS_ENABLED=1`.

### 2.3 Environment Variables (dev `.env` additions)

```ini
# Sidecar control bot
DISCORD_TOKEN_TOOLS=...
EXPECTED_BOT_ID_TOOLS=...

# Puppets (3 separate Discord apps)
BETA_TOOLS_ENABLED=1
BETA_PUPPET_TOKEN_1=...
BETA_PUPPET_TOKEN_2=...
BETA_PUPPET_TOKEN_3=...
EXPECTED_BOT_ID_PUPPET_1=...
EXPECTED_BOT_ID_PUPPET_2=...
EXPECTED_BOT_ID_PUPPET_3=...

# Sim controls
BETA_AMBIENT_RATE_MULTIPLIER=1.0    # 0.0 = paused, 1.0 = prod-rate, 10.0 = fast-forward
BETA_AMBIENT_AUTOSTART=1            # 1 = start sim loop on boot; 0 = require /beta sim start
BETA_LLM_BLEND=0                    # off in v1; reserved for future LLM-message blending
```

### 2.4 Startup Sequence (DK Tools)

1. Load config, run `beta_tools.safety.assert_safe_to_start()` (Section 7.1, Layer 1).
2. Connect DK Tools' own gateway, run safety Layer 2 on `on_ready`.
3. Spin up the 3 puppet clients (`asyncio.create_task(puppet.start(token))` × 3), await each `on_ready`. Run safety Layer 3 per puppet.
4. For each persona on first run, set the puppet's display name and avatar (idempotent — skip if already correct).
5. Provision channel webhooks via `WebhookFleet.ensure()` (idempotent — reuses existing `dk-tools-ghost` webhooks).
6. Load `prod_traffic_profile.json` and Markov chains.
7. Register `/beta` slash commands scoped to the test guild.
8. If `BETA_AMBIENT_AUTOSTART=1`, kick off `AmbientSim.run()`.
9. Post a startup banner to `#beta-tools-audit`.

---

## 3. Identity Model

Three classes of fake "users," each with different powers and limitations. The sim picks the right class per event.

### 3.1 Puppets (3 real bot members)

Real Discord bot accounts. Real `Member` objects in the guild. Use puppets for anything that needs a genuine member identity.

**Persona config — `fixtures/beta_puppets.yaml`:**

```yaml
- key: alice
  display_name: Alice
  avatar_url: https://i.imgur.com/...
  activity_weight: 1.0          # relative posting frequency
  channel_affinities: { general: 0.5, photos: 0.2, drama: 0.1, random: 0.2 }
  voice_likely: true
  message_length_bias: short    # short | medium | long

- key: bob
  display_name: Bob the Builder
  avatar_url: ...
  activity_weight: 1.5
  channel_affinities: { general: 0.3, drama: 0.5, random: 0.2 }
  voice_likely: false
  message_length_bias: medium

- key: clara
  display_name: Clara
  avatar_url: ...
  activity_weight: 0.8
  channel_affinities: { general: 0.4, photos: 0.4, random: 0.2 }
  voice_likely: true
  message_length_bias: long
```

**Puppets do:**
- Post messages (real `on_message` event in DK[DEV])
- React to messages (real `on_reaction_add` — needed for starboard, drama, interaction graph)
- Join/leave voice channels (real voice state — needed for voice XP, voice master)
- Be jailed, ticketed, role-granted, watched, automod-flagged
- Be `/jail @PuppetAlice` autocomplete targets
- Trigger welcome/goodbye flows by leaving and rejoining the guild
- Set their own birthday, wellness check-in cadence, gender preference, etc.

**Cap consequences:**
- Reactions max 3 fake reactors per message. Starboard threshold for beta = 3 (or mods top up). Documented constraint, not a bug.
- 3 voice users — fine for voice XP, voice master, voice presence sims.

### 3.2 Webhook Ghosts (unbounded message volume)

Discord webhooks posting with rotating name + avatar combos. Pure message generators.

`WebhookFleet` creates one webhook per channel that needs ghost chatter (idempotent, named `dk-tools-ghost`). When the sim wants ghost X to post in channel Y, it sends via that channel's webhook with `username=X.display_name, avatar_url=X.avatar_url`. The roster of ~50 ghost personas is defined in `fixtures/beta_db_ghosts.yaml` (same file used by the historical seeder — same identity in two roles).

**Webhook ghosts do:**
- Post messages with custom name + avatar (visible scrollback chatter; real `on_message` fires in DK)

**Webhook ghosts do NOT:**
- Appear in the member list
- React, join voice, get jailed, open tickets, or anything else requiring a `Member`
- Show up in `/jail @user` autocomplete

**Side-write reconciliation.** Webhook authors have a webhook ID, not a member ID. DK[DEV]'s services that do `guild.get_member(message.author.id)` will get `None` and silently skip XP/scoring/etc. for ghost messages. We accept that and side-write the data the sim cares about populating.

After the sim sends a webhook message, it directly writes:
- `message_store` — synthetic row tagged `source='beta_sim'` with the ghost's synthetic ID as author
- XP/scoring tables — bumps the ghost's row directly
- `interaction_graph` — if the message is a reply, records the edge

This keeps leaderboards, charts, scoring, and reports accurate for ghost activity without modifying any production service. ~4–5 services have side-writes; the rest stay untouched.

### 3.3 DB-only Ghosts (the historical seed)

The roster of ~50 fake "members" who never post live. They exist only as backdated rows so leaderboards have depth and scoring has 90 days of history on day one.

**Identity:** `fixtures/beta_db_ghosts.yaml` — 50 entries with synthetic Discord IDs in a reserved high range (e.g., `9_000_000_000_000_000_000+`) so they can never collide with real IDs. The same 50 personas are also the pool the webhook fleet draws from when it picks a ghost author for live chatter.

```yaml
- id: 9000000000000000001
  display_name: ghost_aria
  avatar_url: https://i.imgur.com/...
  activity_weight: 0.4
  channel_affinities: { general: 0.6, photos: 0.4 }
  joined_at_days_ago: 142
  message_length_bias: short

- id: 9000000000000000002
  display_name: ghost_marcus
  ...
```

### 3.4 Service Interaction Summary

| Event | Source | Hits real DK code path? | DB side-write? |
|------|--------|------|--------|
| Puppet posts message | Puppet | Yes — full `on_message` chain | No (DK handles) |
| Ghost posts message via webhook | WebhookFleet | Partial — services that need `Member` skip | Yes — sim writes XP/scoring/store rows |
| Puppet reacts | Puppet | Yes | No |
| Puppet joins voice | Puppet | Yes — voice XP, voice master | No |
| Puppet jailed | Puppet | Yes — full jail flow | No |
| Ghost "jailed" (scenario) | DB-only | No — DB row inserted directly | Yes — full jail row + linked records |
| Historical seed | DB-only | No | Yes — bulk insert |

Mental model: **puppets are real, webhook ghosts are real-on-the-wire-but-not-members, DB ghosts are imaginary-but-tracked.**

---

## 4. Traffic Profile & Content Generator

Two artifacts produced from prod once and reused by the sim forever after.

### 4.1 `prod_traffic_profile.json`

Built by `scripts/build_traffic_profile.py`, run against the prod DB read-only (or a backup). Output committed to repo. No PII, no message content — only aggregate numbers.

```json
{
  "exported_at": "2026-04-30T14:00:00Z",
  "window": "last_90d",
  "channels": {
    "general": {
      "share_of_total_messages": 0.38,
      "hourly_rate_curve": [0.2, 0.1, 0.1, "...", 1.4, 1.6, 1.5],
      "dow_multiplier": [1.0, 1.0, 1.0, 1.0, 1.1, 1.4, 1.3],
      "message_length_pmf": {"1-10": 0.45, "11-30": 0.30, "31-100": 0.18, "101-300": 0.06, "301+": 0.01},
      "reactions_per_message_pmf": {"0": 0.78, "1": 0.14, "2-3": 0.06, "4+": 0.02},
      "reply_rate": 0.22,
      "burst_intensity": 0.35
    },
    "photos": "..."
  },
  "voice": {
    "session_length_minutes_pmf": {"0-5": 0.35, "5-30": 0.30, "30-120": 0.25, "120+": 0.10},
    "channel_share": {"voice-1": 0.4, "voice-2": 0.3},
    "hourly_rate_curve": ["..."]
  },
  "members": {
    "active_user_distribution": "lognormal",
    "active_user_params": {"mu": 2.1, "sigma": 1.4},
    "join_rate_per_day": 0.8,
    "leave_rate_per_day": 0.3
  },
  "global_messages_per_minute_baseline": 4.2
}
```

The sim multiplies `global_messages_per_minute_baseline` by `BETA_AMBIENT_RATE_MULTIPLIER` to get its target rate, then distributes events across channels weighted by `share_of_total_messages × hour_curve(now) × dow_multiplier(now)`. Inter-arrival times are sampled from a mixture of exponential (Poisson baseline) and clustered bursts when `burst_intensity > 0` to capture conversational burstiness.

**Channel name mapping:** profile keys are channel *names*, not IDs. The sim joins to live dev channel IDs via name lookup (consistent with `prod_snapshot.json` from the test-env spec). Channels in prod but not dev are skipped silently.

### 4.2 Markov Chains

Built by `scripts/train_markov.py`. **Per-channel order-3 word-level chains.** Output as plain JSON to `beta_tools/markov_chains/<channel_name>.json` (gitignored). Checksums committed in `markov_metadata.json` for verification.

JSON serialization is deliberate — no pickle. Loading a chain is a regular `json.load()`, no risk of code execution from a malformed file. Chain JSON shape:

```json
{
  "channel": "general",
  "order": 3,
  "training_window_days": 180,
  "vocab_size": 12483,
  "sample_count": 84210,
  "transitions": {
    "thequickbrown": {"fox": 12, "dog": 3, "...": "..."},
    "...": "..."
  },
  "starts": [["the", "quick", "brown"], ["..."]]
}
```

State keys are tuples of the last 3 words joined by `` (a control character that won't appear in real text). Values are next-word frequency counts. `starts` is a list of valid state-tuples to seed generation. Files are typically a few MB per active channel, smaller for quiet channels.

**Pipeline:**

1. **Pull messages** from prod's `message_store` for the last N days (default 180), per channel.
2. **Strip technical artifacts that won't make sense in beta:**
   - Discord mentions: `<@\d+>`, `<@!\d+>`, `<#\d+>`, `<@&\d+>`
   - Custom emoji refs whose IDs point to prod-only emojis: `<:[\w_]+:\d+>` (replace with `:emoji_name:` text)
   - URLs (replace with literal `[link]` so chains can still produce sentences referencing links without resolving to real URLs)
3. **Train order-3 word-level chains** per channel. State = last 3 words → distribution over next word.
4. **Defensive build-time check:** generate 10k samples per chain, fail the build if any sample matches a "bot-secret-shaped" regex (`r"[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"` — Discord token shape — and a few similar guards). Cheap, prevents accidental token leakage if a stray prod log line landed in `message_store`.
5. **Output** chain JSON files + `markov_metadata.json` (vocab size, sample count, training window, sha256 per chain).

**Privacy stance:** the user explicitly opted to use real prod messages without privacy-opt-out filtering, on the basis that mod testers in the beta server are insiders who already have visibility into prod content. No additional PII scrubbing beyond the defensive token-shape check.

**Per-channel chains** preserve channel texture — `#photos`, `#drama`, and `#general` messages all sound different. A global chain would homogenize them.

**Generator API:**

```python
class MarkovGenerator:
    def generate(self, channel_name: str, length_target_words: int) -> str: ...
```

Length is sampled from `message_length_pmf` for the channel; the Markov walk runs until target ± noise.

**Channel cold-start:** if a channel has fewer than 100 prod messages, it gets a fallback chain (the global "all channels" chain) so the sim never silently fails on small channels.

**LLM blend (off by default):** `BETA_LLM_BLEND=1` would route ~20% of generations through Haiku with a context-aware prompt. Not implemented in v1 — design contains the hook but `BETA_LLM_BLEND` defaults false. Avoids API cost and avoids "real beta needs an Anthropic key" entanglement.

### 4.3 Refresh Cadence

Both scripts are run manually, monthly or after major server-activity shifts. Both are safe against live prod (read-only). Output is regenerated and committed via PR.

---

## 5. Ambient Sim, Scenarios, and Seed

Three modes that combine into "feels like a real server."

### 5.1 Ambient Sim — the always-on background

A single `AmbientSim.run()` task in DK Tools' event loop. Tick-driven, ±5s jitter tolerated.

```python
async def run(self):
    while self.running:
        target_rate = self.profile.global_rate_now() * cfg.rate_multiplier
        # = baseline_rate × hour_curve(now) × dow_multiplier(now) × user_multiplier
        if target_rate <= 0:
            await sleep(60); continue

        next_event = self.scheduler.next()
        await sleep(next_event.delay_seconds)

        if next_event.type == "message":
            await self._dispatch_message(next_event)
        elif next_event.type == "reaction":
            await self._dispatch_reaction(next_event)
        elif next_event.type == "voice_session_start":
            await self._dispatch_voice_join(next_event)
        elif next_event.type == "voice_session_end":
            await self._dispatch_voice_leave(next_event)
        elif next_event.type == "member_join":
            await self._dispatch_ghost_join(next_event)
        elif next_event.type == "member_leave":
            await self._dispatch_ghost_leave(next_event)
```

**Event mix per tick window** (drawn from profile percentages):
- ~85% messages
- ~10% reactions (puppets only)
- ~3% voice events (puppets only)
- ~2% join/leave (DB-only ghosts)

**Author selection per message event:**

1. Pick channel weighted by `share_of_total_messages × hour_curve(now)` for that channel.
2. Pick author class — **30% puppet, 70% ghost** (puppets visible enough to feel active, ghosts carry the volume). User noted this can be fine-tuned later via config.
3. If puppet: pick weighted by `activity_weight × channel_affinity[channel]`. Puppet client posts via `channel.send(content)`. Real `on_message` fires in DK[DEV].
4. If ghost: pick weighted by `activity_weight × channel_affinity[channel]`. WebhookFleet sends with `username=ghost.display_name, avatar_url=ghost.avatar_url`. Sim then runs side-writes (Section 3.2).

**Reactions** (puppet only): pick a recent message in a random channel, pick 1–3 puppets to react, pick emoji from a small curated set + the guild's top-N custom emoji.

**Voice sessions** (puppet only): pick a puppet not currently in voice, pick a voice channel weighted by `voice.channel_share`, sample session length from `session_length_minutes_pmf`, schedule the leave event for that puppet at `now + length`.

**Ghost joins/leaves:** writes/removes a row in the member-tracking table tagged `source='beta_sim'`, fires the welcome/goodbye cog hook via direct service call, runs member-quality re-score on leave. Doesn't touch Discord — these ghosts never were members.

**Controls:**
- `/beta sim start` / `stop` / `pause` — pause is soft (loop runs with `target_rate=0`)
- `/beta sim rate <multiplier>` — runtime override of `BETA_AMBIENT_RATE_MULTIPLIER`
- `/beta sim status` — embed showing current rate, events fired in last 5/15/60 min, per-channel rate, error count, next scheduled event

### 5.2 Scenario Library — deliberate end-to-end triggers

One file per cog/feature in `beta_tools/scenarios/`. Each scenario:

- Inherits from `Scenario` base class with a registered name, optional args, an `async run(ctx)` method, and an `async cleanup(ctx)` method
- Is registered with `ScenarioRunner` at import time
- Surfaces via `/beta scenario run <name> [args]`, with autocomplete
- Logs every step to `#beta-scenario-log` so mods can follow along

**The v1 scenario list:**

| File | Scenarios |
|---|---|
| `jail.py` | `jail-full-cycle`, `jail-vote-quorum`, `jail-vote-below-quorum`, `jail-rejoin-while-jailed`, `jail-appeal-approved`, `jail-appeal-denied`, `jail-appeal-rate-limited`, `jail-expiry-tick` |
| `tickets.py` | `ticket-open`, `ticket-claim`, `ticket-dual-claim-escalation`, `ticket-close-then-delete`, `ticket-rate-limit`, `ticket-panel-button` |
| `automod.py` | `automod-each-rule` (parameterized over rule type) |
| `starboard.py` | `starboard-hit`, `starboard-just-below-threshold` |
| `watch.py` | `watch-escalation`, `watch-decay` |
| `confessions.py` | `confession-post`, `confession-reply` |
| `welcome.py` | `welcome-on-join`, `welcome-with-invite-attribution` |
| `birthday.py` | `birthday-today`, `birthday-tomorrow-reminder` |
| `wellness.py` | `wellness-checkin-due`, `wellness-partner-pairing`, `wellness-enforcement-tick` |
| `voice_master.py` | `voice-master-create-temp-channel`, `voice-master-cleanup-empty` |
| `xp.py` | `xp-level-up`, `xp-prestige-tier-bump` |
| `inactivity_prune.py` | `inactivity-prune-due` |
| `gender.py` | `gender-set-pref` |
| `denizen.py` | `denizen-event` |
| `drama.py` | `drama-incident` |
| `interaction.py` | `interaction-graph-edge`, `interaction-graph-cluster` |
| `invite.py` | `invite-join-attribution` |
| `dm_perms.py` | `dm-perms-toggle` |
| `tts.py` | `tts-play` |
| `music.py` | `music-queue-add`, `music-skip` |
| `ai_mod.py` | `ai-mod-evaluate` |
| `foolsday.py` | `foolsday-trigger-now` |
| `booster.py` | `booster-grant`, `booster-role-create` |
| `support.py` | `support-flow` |

Approximately 40 scenarios across ~25 files. Each is small (~30–80 lines). The list grows organically when new cogs are added; PR template includes a "did you add a scenario for new features?" checkbox.

**Per-scenario time fudges** (no global clock): scenarios that depend on time mutate the relevant DB rows directly. Examples:

- `birthday-today` writes today's date to a puppet's birthday row, then calls the birthday cog's tick handler.
- `wellness-checkin-due` sets the puppet's last-checkin timestamp far enough in the past for the next tick to fire.
- `inactivity-prune-due` sets the target's last-active timestamp past the prune threshold.

Each scenario localizes its time-fudging to the specific rows it touches.

**Cleanup:** every scenario's `cleanup()` reverses what it did. Most are idempotent and tagged `source='beta_sim'` so the coarser `/beta cleanup` can also reset state.

### 5.3 Seed — historical population

`/beta seed run` is a one-shot operation that runs `seeder.py`. Idempotent (skips already-seeded rows by checking `source='beta_seed'`).

For the 50-ghost roster + 3 puppets it inserts:

- **Backdated message activity** — 90 days of message-counts-per-channel-per-day, sampled from each persona's profile.
- **Backdated message content** (optional, `--with-content`) — uses the Markov generator to fill `message_store` with synthetic messages at backdated timestamps. ~10–50k rows; slower seed pass.
- **XP and scoring history** — recompute over the backdated activity using the existing scoring code paths so data is internally consistent.
- **Member metadata** — joined-at dates spread over the past year for the ghost roster, opt-in/opt-out flags split realistically.
- **Mixed historical events** — handful of past jails (some long-released, some recent), past tickets (mostly closed), watch signals at varying levels, starboard hits, birthdays scattered across the calendar, wellness check-in cadence.
- **Zero-touch scoring**: after seeding raw activity, run the scoring service against the seeded data so leaderboards reflect computed scores.

**Commands:**
- `/beta seed run [--force] [--with-content]` — `--force` re-seeds from scratch (calls cleanup first)
- `/beta seed status` — what's currently seeded: ghost count, puppet count, oldest seeded message date, current leaderboard top 10 (smoke test)
- `/beta seed cleanup` — wipes seed-only rows, preserves live sim activity

---

## 6. Control Surface

All slash commands registered to the test guild only by DK Tools. Gated behind `@Mod` or `@Admin` role checks.

```
/beta help                                         — overview embed

/beta sim status                                   — current rate, recent events, errors
/beta sim start
/beta sim stop
/beta sim pause                                    — soft pause (loop runs, rate=0)
/beta sim rate <multiplier>                        — runtime rate override (0.0..10.0)

/beta scenario list                                — list registered scenarios
/beta scenario describe <name>                     — show what a scenario does + its args
/beta scenario run <name> [args...]                — execute one scenario
/beta scenario history [limit]                     — last N runs (who, when, outcome)

/beta puppets list                                 — show roster + connection state
/beta puppets reload                               — re-read fixtures/beta_puppets.yaml
/beta puppets reconnect <key>                      — kick + reconnect a single puppet
/beta puppets impersonate <key> <channel> <text>   — manually drive a puppet (mod-or-admin)

/beta ghosts list                                  — paged list of webhook+DB ghosts
/beta ghosts reload                                — re-read fixtures/beta_db_ghosts.yaml

/beta seed run [--force] [--with-content]
/beta seed status
/beta seed cleanup                                 — wipe rows tagged source='beta_seed' only

/beta sim cleanup                                  — wipe rows tagged source='beta_sim' only
/beta cleanup                                      — wipe ALL rows tagged source LIKE 'beta_%'
/beta cleanup --dry-run                            — preview deletions

/beta profile reload                               — re-read prod_traffic_profile.json
/beta markov reload                                — reload markov chains

/beta health                                       — full health embed (puppets, sim, db, profile, errors)

/beta nuke                                         — admin-only: drop dk_dev.db + refresh from prod
```

Every command logs invocation (user, args, timestamp, outcome) to `#beta-tools-audit` and to a DB table `beta_command_log`.

---

## 7. Safety Rails

Five independent layers. Each one alone would prevent disaster; all five together make accidental prod activation effectively impossible.

### 7.1 Process Refuses to Start in Prod

```python
# beta_tools/safety.py — runs before any other code in beta_tools/__main__.py
def assert_safe_to_start():
    cfg = load_config()
    if not cfg.is_dev:
        sys.exit("CRITICAL: beta_tools refuses to start outside dev env (BOT_ENV != 'dev')")
    if os.getenv("BETA_TOOLS_ENABLED") != "1":
        sys.exit("CRITICAL: BETA_TOOLS_ENABLED must be '1' to launch beta tools")
    if "dev" not in cfg.db_path:
        sys.exit(f"CRITICAL: db_path={cfg.db_path!r} does not contain 'dev'")
    expected = int(os.environ["EXPECTED_BOT_ID_TOOLS"])
    if expected == int(os.environ.get("EXPECTED_BOT_ID_PROD", "-1")):
        sys.exit("CRITICAL: tools bot id matches prod bot id — config error")
```

### 7.2 Tools Bot Leaves Non-Test Guilds

On `on_guild_join` and `on_ready`: if the guild ID does not match `cfg.guild_id`, leave immediately, log `CRITICAL`, DM the bot owner.

### 7.3 Puppets Validate Themselves

Each puppet:
- Validates its app ID matches the corresponding `EXPECTED_BOT_ID_PUPPET_N` on `on_ready`
- Leaves any guild that isn't the test guild
- Exits if `BETA_TOOLS_ENABLED != "1"` (re-checked on connect)

### 7.4 DB Writes Are Gated

A wrapper `async def beta_write(db, query, params)` checks `cfg.is_dev` before executing. Any write from beta_tools goes through it. In a non-dev env (which can't happen but is checked anyway), it raises `RuntimeError` and refuses.

### 7.5 Source Tagging for Everything

Every row written by any beta_tools code path is tagged `source='beta_sim'` (ambient) or `source='beta_seed'` (one-shot seed). Real prod rows have `source IS NULL`. Cleanup queries always require `source LIKE 'beta_%'` — structurally incapable of touching real data.

A boot-time check counts beta-tagged rows in the active DB. If beta-tagged rows are ever found in prod's DB (e.g., a backup got crossed), it logs a loud warning to the audit channel.

**Schema migration `migrations/00X_beta_source_tags.sql`:**

```sql
-- Idempotent ALTER TABLE adds for affected tables. Default NULL = real data.
-- Final list of tables to be confirmed during a schema audit at implementation time.
ALTER TABLE message_store ADD COLUMN source TEXT;
ALTER TABLE xp_members ADD COLUMN source TEXT;
ALTER TABLE jails ADD COLUMN source TEXT;
ALTER TABLE tickets ADD COLUMN source TEXT;
-- ... per affected table

CREATE INDEX IF NOT EXISTS idx_message_store_source
  ON message_store(source) WHERE source IS NOT NULL;
```

The migration runs in both dev and prod for schema parity. In prod the column is always NULL — harmless. In dev the column is the cleanup pivot.

---

## 8. Cleanup Model

Three levels, increasing severity:

- **`/beta seed cleanup`** — deletes only `source='beta_seed'` rows. Historical population goes; live sim activity persists.
- **`/beta sim cleanup`** — deletes only `source='beta_sim'` rows. Ambient sim's accumulated activity goes; historical seed persists.
- **`/beta cleanup`** — deletes all `source LIKE 'beta_%'` rows. Full reset back to "real data only." Use before showing the beta to a new mod tester.

All three:
- Are idempotent
- Run in a transaction (full rollback on error)
- Print a per-table delete summary in their response embed
- Have `--dry-run` mode
- Log to `#beta-tools-audit`

**`/beta nuke`** (separate, admin-only): drops `dk_dev.db` and re-runs `scripts/refresh_dev_db.py` from prod. Last-resort total reset. Wired through DK Tools but only the bot owner ID can invoke.

---

## 9. Observability

- **`#beta-tools-audit`** — channel created on first use, hidden from `@everyone`. Logs all `/beta` invocations, scenario starts/ends, ambient sim start/stop, errors, and source-tag boot checks.
- **`#beta-scenario-log`** — step-by-step play-by-play of running scenarios. Scenarios post their own progress here so mods watching the server can follow what happened.
- **Sim heartbeat** — every 10 minutes the sim posts a one-line status to the audit channel: rate, events fired, errors. Catches silent failure.
- **`/beta health`** — embed showing puppet connection state, last sim event, last scenario run, DB connection state, profile + Markov load timestamps, recent error log tail.

---

## 10. Out of Scope

Explicitly not built in v1:

- **Time travel / global clock fudging.** Per-scenario time mutations only.
- **Audio playback / voice transmission by puppets.** Puppets connect voice gateway state but transmit no audio.
- **More than 3 puppets.** Hard-coded ceiling — the env loader looks for exactly `BETA_PUPPET_TOKEN_1..3`.
- **LLM-blended message content.** Hook reserved (`BETA_LLM_BLEND`), implementation deferred.
- **Service-layer awareness of webhook authors.** DK[DEV] services treat webhook authors as `None` members and silently skip; sim side-writes cover what dashboards need.
- **Auto-refresh of `prod_traffic_profile.json` and Markov chains.** Manual regeneration only.
- **Member-list parity with prod.** 3 puppets + ~50 DB-only ghosts is the cap; mods will see a smaller member list than prod.
- **Performance / load testing.** Cadence is calibrated for realism, not stress.

---

## 11. Open Questions

- **Final list of tables that get the `source` column.** Requires a schema audit at implementation time — every table beta_tools writes to needs the column. Initial list: `message_store`, `xp_members`, `jails`, `tickets`. Likely additions during audit: scoring component tables, watch signals, starboard entries, interaction graph, member tracking, audit log mirror tables.
- **Where exactly to draw the line on "side-write what a service would have written."** Some services run complex pipelines (member quality scoring, AI moderation evaluation). Tier services into "side-write the result" vs "let it skip and accept partial coverage." Decide per-service during implementation.
- **Welcome/goodbye for ghost joins/leaves.** Triggering the cog via direct service call is straightforward; whether the welcome message lands in `#welcome` (visible to mods) is a behavior decision — leaving it on by default for visible realism.
- **Markov chain regeneration cost.** Training time on 180 days × ~30 channels of prod data is unknown. If it takes >10 min, we may want progress reporting in `train_markov.py`.

---

## 12. Implementation Order

Recommended build order:

1. **Schema migration** (`migrations/00X_beta_source_tags.sql`) — the source-tag columns. Foundation for everything else.
2. **DK Tools skeleton** — `beta_tools/__main__.py`, `bot.py`, `safety.py`. Bot connects to test guild, runs all five safety rails, idles. Slash commands stub-only.
3. **Puppet manager** — load tokens, spin up clients, `on_ready` validation, persona name/avatar setup. `/beta puppets list` works.
4. **Webhook fleet** — provision per-channel webhooks, `WebhookFleet.send()` API. `/beta puppets impersonate` works as a test of the webhook + puppet plumbing.
5. **Traffic profile builder** — `scripts/build_traffic_profile.py` against prod. Commit `prod_traffic_profile.json`.
6. **Markov trainer** — `scripts/train_markov.py` against prod. Defensive token-shape check. Commit `markov_metadata.json`.
7. **Side-writes module** — `beta_tools/side_writes.py`. Functions for each affected service's tables.
8. **Ambient sim — messages only** — minimal viable loop, just messages (no reactions/voice/joins). Verify sim runs against profile + Markov, side-writes work, leaderboards populate.
9. **Ambient sim — reactions** — puppet reactions to recent messages.
10. **Ambient sim — voice sessions** — puppet voice join/leave.
11. **Ambient sim — ghost joins/leaves** — DB-only member churn.
12. **Seeder** — `/beta seed run`, including the optional `--with-content` Markov-driven backfill. Verify scoring + leaderboards reflect seeded data.
13. **Cleanup commands** — `/beta cleanup`, `/beta sim cleanup`, `/beta seed cleanup`, dry-run support.
14. **Scenario base + first 5 scenarios** — `jail-full-cycle`, `ticket-open`, `starboard-hit`, `birthday-today`, `automod-each-rule`. Verify the scenario pattern works end-to-end.
15. **Remaining scenarios** — fill out the rest of the v1 scenario list incrementally.
16. **Observability** — `#beta-tools-audit`, `#beta-scenario-log` channels, heartbeat, `/beta health`.
17. **Documentation** — `README.md` section on "Running the beta," runbook for adding new scenarios, refresh cadence guide.

---

## 13. Files Added

```
beta_tools/                          # entire new package
scripts/build_traffic_profile.py     # new
scripts/train_markov.py              # new
prod_traffic_profile.json            # new, committed
fixtures/beta_puppets.yaml           # new
fixtures/beta_db_ghosts.yaml         # new
migrations/00X_beta_source_tags.sql  # new
```

Existing files **not modified by this spec:**

- All cogs (`cogs/`)
- All services (`services/`)
- `dungeonkeeper.py`
- `config.py` (only `.env.example` gains new vars)

`safety.py` may receive a small addition to assert that `BETA_TOOLS_ENABLED` is not set when `cfg.is_prod`, as a paranoid extra check from the main bot side. Otherwise, untouched.
