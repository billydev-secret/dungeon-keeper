# Dungeon Keeper — Test Environment & Dev Strategy

How the bot is iterated on safely. There are two environments — **prod** (the live Golden Meadow server) and **dev** (a structurally equivalent test guild). Everything in this doc exists to make it impossible to accidentally touch prod from a dev session, and to make pytest do as much of the verification as Discord allows.

## Environments

Two parallel installs, one configuration switch, no shared state.

| | prod | dev |
|---|---|---|
| Discord application | The real bot | A separate `[DEV]` bot — different user, different token |
| Guild | Golden Meadow | The test guild (structural clone of prod) |
| Database | The live DB | A disposable dev DB |
| Audit channel | Prod's audit log | Dev's audit log |
| Slash command sync | Global | Guild-scoped to the test guild — changes appear instantly |

A single environment flag picks which set of credentials, paths, and channel IDs to load. Example `.env` stanza:

```ini
BOT_ENV=dev
DISCORD_TOKEN_DEV=...
DISCORD_TOKEN_PROD=...
```

Each side keeps its own log file and its own startup banner so a glance at the terminal tells you which bot is running.

## Safety rails

Checks that run at startup, before the bot starts handling events. Any failure logs `CRITICAL` and exits — the bot does not register handlers until all rails pass.

- **Token identity** — fetch the bot's own user from Discord, compare against the expected bot ID for the active environment. If the prod token was set with `BOT_ENV=dev`, the mismatch fails the check.
- **DB path matches env** — dev path must contain `"dev"`; prod path must not. Catches a swapped path before any write happens.
- **Guild identity** — the bot must be in its configured home guild (`GUILD_ID_<ENV>`); if it isn't, it exits. Behaviour on *additional* guilds is environment-specific:
  - **dev** is single-guild: any guild other than the configured test guild is left immediately and the bot shuts down, so a misinvited dev bot can never act in the wrong server.
  - **prod (Dungeon Keeper) is multi-guild**: additional guilds are legitimate and are only logged, never left. The check still requires membership in the configured home guild. (The `beta_tools` / puppet bots are a separate, deliberately single-guild application — their `on_guild_join` leaves anything but the dev server; that is intended, not a bug.)
- **One-way refresh** — the dev-DB refresh script hard-codes source = prod and destination = dev. It refuses any override that would reverse them.
- **Startup banner** — on `on_ready`, the bot prints (and posts to the dev audit channel) the active env, the bot user, the guild, and the DB path. Prod's banner is colour-coded red in the terminal.

## ID remapping

Prod stores real Golden Meadow channel, category, and role IDs. Those IDs don't exist in the test guild, so the dev bot can't use them raw. The remapper resolves prod IDs to their dev equivalents by **name match**.

- **When it runs** — on dev startup, against the live test guild. Prod skips it entirely; lookups return the stored ID unchanged.
- **Inputs** — a snapshot of prod's channel/category/role names and IDs (regenerated against prod whenever its structure changes) and the live state of the dev guild.
- **What's remapped** — channels (by name + type + parent category), categories (by name), roles (by name). The dev bot's own user ID is treated as the remap target for any stored reference to the prod bot's user ID.
- **What isn't** — user IDs (a Discord account has the same ID in every guild it's in). Dynamic channels created at runtime (per-jail, per-ticket) are never remapped — they only ever exist on one side.
- **No match** — logged at warning level, lookup returns nothing. Features that depend on the missing ID degrade (skip the audit post, skip the role sync) rather than crash. Ambiguous matches (two dev channels with the same name) are treated as no-match and surfaced the same way.
- **Contract** — the test guild is a name-parity clone. When prod renames a channel or role, dev follows in the same change. Dev may have extra channels/roles that don't exist in prod; those are ignored.

On startup the bot posts a remap report to the dev audit channel summarising how many channels, categories, and roles matched and naming any that didn't.

## Fixtures

Refreshing dev from prod gives realistic message and config history but doesn't help with feature flows that reference guild-specific dynamic state (open tickets, active jails, scoring windows tied to specific channels). Fixtures are designed to fill that gap.

Scenarios envisioned:

- Active and expired jails, including one mid-appeal.
- Open, claimed, and closed tickets covering the rate-limit edge.
- Backdated message and reaction activity for scoring window tests, expressed in time relative to "now" so they survive a clock change.
- Simulated automod hits for linking tests.

A loader would write these into the dev DB after the remap step, tagging rows so a re-run can clear and reapply them idempotently.

**Status:** the fixture loader is not built. The dev-only flag that would trigger it is wired through the config loader but is a no-op today. Reach for this section as the design brief when revisiting fixtures.

## Test tiers

Four layers, ordered by how much of the bot each one covers. The goal is pytest catching roughly 85% of meaningful bugs; manual dev-guild testing is the last-mile UX check, not the primary safety net.

The split rests on extracting **pure logic out of every cog callback** — duration parsing, scoring math, ticket state transitions, remap matching. Callbacks become thin translators between a Discord interaction and a pure function. Pure functions are trivially testable; the wrappers are trivially reviewable.

- **Tier 1 — Pure logic.** Required for every cog. Unit tests against the extracted functions: parsers, math, state transitions, DB read/write helpers. Property-based tests on the bounded inputs (e.g., scoring weights produce a result in `[0, 100]` for any valid component).
- **Tier 2 — View and modal structure.** Required for every persistent component. Cheap structural assertions: each persistent view has a stable `custom_id` and no timeout, no row exceeds Discord's component-per-row limit, no view exceeds the per-view limit, modal inputs declare the right `max_length`/`required`/style. Catches a category of bug that otherwise only surfaces when the user clicks the broken button.
- **Tier 3 — Callbacks with fake interactions.** Required for mod-critical cogs (jail, tickets, automod link). A shared library of typed fake `Interaction`/`Guild`/`User`/`Channel`/`Role` objects lets callback code be invoked directly. Covers ephemeral vs public responses, conditional followups, error branches, permission gates — the glue between the Discord shell and the pure logic.
- **Tier 4 — Snapshot tests.** Optional. Every embed, DM template, and audit message gets a snapshot; intentional changes are reviewed in the PR diff. Catches accidental wording drift.

Time-sensitive tests use a clock-freezing helper so jail expiry, scoring windows, and rate limits are deterministic. DB-touching tests run against a real on-disk SQLite in a temp directory — the DB layer is never mocked.

## Manual checklist

What the four tiers can't reach, and what every PR touching a user-facing surface must verify in the dev guild before merging:

- Embed rendering on desktop **and** mobile clients (the layouts differ).
- Modal UX feel — popup latency, field tab order.
- Real Discord API shape — event payloads and any new fields after a `discord.py` upgrade.
- Rate-limit behaviour under real load.
- Gateway reconnect and resume.
- Permission cache freshness when roles change mid-operation.
- Integration with Discord's server-side AutoMod (the bot links to it, doesn't control it).

Every relevant PR carries a short manual-test checklist in the description covering the affected flow.

## Status

- **Implemented** — environment config loader, startup safety rails, banner, dev-only hot reload, the prod-snapshot export script, the remap table and helper, and Tiers 1–2 across the active cogs. The dev-only Beta Tools sidecar (puppets, ghosts, ambient sim, scenarios, historical seed) builds on top — see [[beta-tools-spec]].
- **Not committed** — the snapshot JSON itself. It must be regenerated against the live prod guild and placed in the repo before dev-side remapping can resolve anything; until then the remap helper logs warnings and returns nothing, which the code handles gracefully.
- **Not built** — the fixture loader. The flag exists; the loader does not.
- **In progress** — Tier 3 callback coverage. The shared fake-interaction library exists; the per-cog callback suites are landing cog by cog.
- **Open question** — whether the prod snapshot should auto-refresh on a schedule. Current call: no, manual re-export is cheap and guild structure rarely changes.
