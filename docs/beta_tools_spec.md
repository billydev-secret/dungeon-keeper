# Beta Tools — Feature Spec

## Intent

The dev guild is empty by default. An empty server is a bad place to test against: leaderboards show nothing, dashboards have no signal, edge cases never fire, and most user-experience decisions can't be evaluated until real members start using the bot in prod. That's too late.

Beta Tools fills the dev guild with believable activity so the bot can be exercised the way it'll be used. Two halves:

- **The sidecar** — a separate process from the main bot that drives three puppet bot accounts, a fleet of webhook "ghosts," and a roster of ~50 DB-only personas. Crashes on one side can't take the other down. Puppets behave as real members (post, react, join voice, get jailed, opened tickets on, watched, etc.); webhook ghosts add unbounded scrollback volume; DB-only ghosts pre-populate 90 days of backdated activity so the bot looks lived-in on day one.
- **The sim** — an ambient chat loop that drives the puppets to post Markov-generated messages on a prod-matched cadence, plus a library of scenarios mods can fire by hand (`/beta scenario run jail-full-cycle`, `ticket-claim`, `starboard-hit`, etc.) for the flows ambient activity won't reliably hit.

The whole thing is **physically incapable of running in prod** — five independent safety rails refuse to start outside `BOT_ENV=dev`. Every row the sidecar writes carries a `source` tag so cleanup is always reversible.

Companion infrastructure to [[dungeon-keeper-test-env-spec]] — same dev guild, same dev database, additive on top.

## Commands

All `/beta` commands are guild-scoped to the test guild and gated to **Mod** (or **Admin** for destructive ones). Registered by the DK Tools sidecar, not the main bot.

| Command | Permission | Purpose |
|---|---|---|
| `/beta help` | Mod | Overview embed of every `/beta` command |
| `/beta health` | Mod | Status of puppets, ambient sim, DB, profile, recent errors |
| `/beta sim start` / `stop` / `pause` / `status` | Mod | Toggle the ambient chat loop; status reports rate + recent posts |
| `/beta sim rate <multiplier>` | Mod | Override ambient cadence (0.0×–10.0×) at runtime |
| `/beta scenario list` / `describe <name>` / `run <name> [args]` / `history` | Mod | Run a single end-to-end scenario; history shows who fired what when |
| `/beta puppets list` / `reload` / `reconnect <key>` / `impersonate <key> <channel> <text>` | Mod | Inspect the puppet roster; reload personas; force one puppet to post |
| `/beta ghosts list` / `reload` | Mod | Paged listing + reload the webhook + DB ghost roster |
| `/beta seed run [--force] [--with-content]` / `status` / `cleanup` | Mod | One-shot historical seeding of 90 days of backdated activity |
| `/beta sim cleanup` / `/beta cleanup [--dry-run]` | Mod | Remove sim rows / all beta-tagged rows |
| `/beta profile reload` / `/beta markov reload` | Mod | Re-read the prod traffic profile JSON / Markov chain corpus |
| `/beta nuke` | Admin | Drop the dev DB and refresh from prod |

Every command logs invocation (user, args, timestamp, outcome) to the beta-tools audit channel and to a beta command log table.

## Behaviour

### The three identity classes

The sidecar uses three different kinds of fake "users" depending on what the scenario needs:

- **Puppets (3 real bot members)** — real Discord bot accounts (alice, bob, clara) that the sidecar drives via the gateway. They post messages, react, join voice, can be jailed, ticketed, role-granted, watched, and appear in autocompletes. Trigger every code path that needs an actual `Member` object. Cap consequences: three reactors max per message (starboard threshold for beta = 3), three voice users max.
- **Webhook ghosts (~50 personas, unbounded volume)** — Discord webhooks posting with rotating name + avatar. Pure message generators. They show up as scrollback chatter but **don't appear in the member list, can't react, can't join voice, can't be jailed**. Anything that requires a real Member silently skips them. The sidecar side-writes XP / scoring / interaction data directly so leaderboards and reports stay populated.
- **DB-only ghosts (same ~50 personas, historical seed)** — the same roster used by the webhook fleet, also pre-populated as backdated DB rows so leaderboards have 90 days of depth on day one. Synthetic IDs in a reserved high range (≥ 9 × 10¹⁸) so they never collide with real Discord IDs.

### Ambient sim

A background loop in the sidecar drives the three puppets to post realistic chat in the test guild. Cadence: roughly four posts per minute on base interval (15 s ± 20 % jitter); after each post a 30-second burst window kicks in where the next interval drops to 5 s. Multiplier env var or `/beta sim rate` lets a mod tester dial it up to 10× or down to a near-pause.

Each puppet has a persona file (display name, avatar, activity weight, channel affinities, message length bias). On each tick the loop picks a puppet by activity-weight, picks a channel by that puppet's affinity, generates a Markov-chain message in the puppet's length bias, and posts it. Channel affinities reference channel names; missing channels get skipped with one WARNING log (not spammed). The loop survives any single bad tick — it sleeps 30 s on an unhandled exception and continues.

### Scenarios

Deliberate end-to-end triggers, one file per cog or feature in the sidecar. About 40 scenarios across roughly 25 files cover full lifecycles (jail-full-cycle, jail-appeal-approved/-denied, ticket-open / -claim / -dual-claim-escalation / -close-then-delete, automod-each-rule, starboard-hit, watch-escalation / -decay, confession-post / -reply, welcome-on-join, birthday-today, wellness-checkin-due, voice-master-create-temp-channel, xp-level-up, interaction-graph-edge, invite-join-attribution, music-queue-add, ai-mod-evaluate, plus more).

Each scenario logs step-by-step to a scenario-log channel so testers can follow along. Scenarios that depend on time mutate the specific DB rows they touch (birthday rows, last-checkin timestamps, last-active timestamps) rather than fudging the global clock. Every scenario implements its own `cleanup()` that reverses what it did; the broader `/beta cleanup` family wipes by tag.

### Seed

`/beta seed run` is a one-shot operation that pre-populates the dev DB so the bot looks lived-in on day one. Idempotent — re-running skips already-seeded rows tagged `source='beta_seed'`. Coverage: 90 days of backdated message-counts per channel per day, joined-at dates spread over the past year, opt-in/opt-out flags split realistically, past jails (some long-released, some recent), past tickets (mostly closed), watch signals at varying levels, starboard hits, birthdays scattered across the calendar, wellness check-in cadence, and XP / scoring history recomputed by the live scoring code against the seeded activity. With `--with-content`, also writes ~10–50k Markov-generated messages into the message archive at backdated timestamps.

### Source tagging and cleanup

Every row the sidecar writes carries a `source` tag (`beta_seed`, `beta_sim`, `beta_scenario_<name>`). Cleanup commands operate on those tags only:

- `/beta seed cleanup` removes seed rows; live sim activity survives.
- `/beta sim cleanup` removes sim rows; seed survives.
- `/beta cleanup` removes everything tagged `beta_*`; a `--dry-run` flag previews the deletion count first.
- `/beta nuke` is the nuclear option — drops the dev DB entirely and refreshes from prod. Admin-only.

### Safety rails

Five independent layers make accidental prod activation effectively impossible:

1. **Process refuses to start** outside `BOT_ENV=dev`. Fails loud and exits before any handler registers.
2. **Tools bot leaves non-test guilds** on startup. If it's been mis-invited somewhere, it removes itself.
3. **Puppets validate themselves** — each puppet checks its own bot user ID against the expected list before sending anything.
4. **DB writes are gated** — every side-write goes through a wrapper that double-checks the env flag and the DB path.
5. **Source tagging** is mandatory on every sim-or-seed write so cleanup is always reversible.

## Permissions

- All `/beta` commands except `/beta nuke`: **Mod** role in the test guild.
- `/beta nuke`: **Admin** role.
- The sidecar bot itself needs **Send Messages**, **Manage Webhooks** (for the ghost fleet), **Read Message History**, and **Move Members** (for voice scenarios) in the test guild. It does **not** need Manage Server.
- Puppet bots need only the standard message + voice gateway intents.

## User-visible errors

The "user" here is a mod tester running `/beta` commands in the test guild.

| When | The tester sees |
|---|---|
| `/beta` command run outside the test guild | "Beta tools are only available in the test guild." |
| `/beta sim start` while already running | "Ambient sim is already running." |
| `/beta sim stop` while not running | "Ambient sim is not running." |
| Ambient sim hits a channel with missing send perms | Logged WARNING; channel is skipped; tester sees nothing |
| Discord rate-limits the sim | Logged WARNING; loop sleeps 10 s extra and continues |
| Scenario name not in the registry | "Unknown scenario `{name}`. Use /beta scenario list." |
| Scenario raises during run | Status line in the scenario-log channel with the exception class + message |
| `/beta seed run` without `--force` while seed already present | "Already seeded (run with --force to re-seed)." |
| `/beta nuke` invoked by a non-admin | "Admin only." |

## Non-goals

- **Time-travel / global clock fudging.** Scenarios mutate specific DB rows instead.
- **Audio playback by puppets.** They connect their voice state but transmit nothing — music + TTS are exercised by mods directly.
- **Full member-list parity with prod.** 3 puppets + ~50 DB-only ghosts + real mod testers. No hundreds-of-avatars illusion.
- **Performance / load testing.** Cadence is calibrated to look real, not to stress the system.
- **Replacing the main test-env story** in [[dungeon-keeper-test-env-spec]]. This is additive.
- **Running in prod.** Five independent safety layers reject any attempt.

## Configuration

| Env var | Purpose |
|---|---|
| `BOT_ENV=dev` | Required. The sidecar refuses to start if missing or set to anything else. |
| `BETA_AMBIENT_AUTOSTART` | If set, the ambient sim's `start()` is called automatically when the sidecar finishes initialising. Otherwise a mod runs `/beta sim start`. |
| `BETA_AMBIENT_RATE_MULTIPLIER` | Scales the base interval (default 1.0). Setting it to 2.0 doubles the posting rate. Overridable at runtime via `/beta sim rate`. |
| `BETA_LLM_BLEND` | Reserved for a future phase that blends LLM-generated text with Markov output. No-op today. |

Two fixture files define the rosters:

- **Puppet personas** — three entries (alice, bob, clara) with display name, avatar URL, activity weight, channel-affinity weights, voice tendency, and a short/medium/long message-length bias.
- **DB + webhook ghost personas** — ~50 entries with synthetic ID, display name, avatar URL, activity weight, channel affinities, joined-at-days-ago, and message-length bias. Same file feeds both the live webhook fleet and the historical seed.

A separate **traffic profile JSON** (regenerated against prod periodically) captures per-channel hourly message rates so the ambient sim matches prod's natural cadence. Reload with `/beta profile reload`.

The **Markov corpus** (`fixtures/markov_chain.json`) is committed to the repo. It contains only word-transition data — no user IDs, no message IDs, no author data. Regenerate from a real DB with the build-markov script.

## Stored data

Beta tools writes into the same dev database as the main bot. Every row carries a `source` tag so cleanup is targeted:

- `source='beta_seed'` for one-shot historical seed rows (messages, XP, scoring, member metadata, past jails / tickets / starboard hits / etc.).
- `source='beta_sim'` for live ambient-sim activity and the side-written XP / scoring / interaction rows the webhook fleet generates.
- `source='beta_scenario_<name>'` for scenario-fired rows.

A `beta_command_log` table records every `/beta` invocation (user, args, timestamp, outcome). Webhooks themselves (one per chatter-enabled channel, named `dk-tools-ghost`) are managed Discord-side via the Manage Webhooks permission; they're created idempotently and aren't tracked in the database.

There is no separate beta-tools schema — the sidecar reuses the main bot's tables. **Nothing the sidecar writes ever exists in the prod database** because the sidecar can't start outside `BOT_ENV=dev` and the safety rails verify the DB path before each write.
