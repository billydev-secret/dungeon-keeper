# Dungeon Keeper — Test Environment & Dev Strategy Spec

**Version:** 1.2
**Status:** Draft for implementation
**Target:** Dungeon Keeper moderation bot (Python 3.10+, discord.py 2.4+, aiosqlite)
**Scope:** Full spec including environment separation, data strategy, seed fixtures, dev workflow, safety rails, and tiered automated testing.

**Changes in 1.2:** §9 fully rewritten around a four-tier pytest strategy (pure logic, static component trees, fake-interaction callbacks, snapshot tests). Dropped dpytest dependency due to maintenance status; replaced with typed fakes + real SQLite + freezegun + hypothesis + syrupy. Coverage targets raised significantly — realistic ceiling is ~85% of meaningful bugs catchable in CI. Added §9.11 explicit list of what remains manual.

**Changes in 1.1:** §4.3 rewritten — ID remapping is now auto-detected by name via a prod snapshot, replacing the hand-edited YAML. §3 updated to require name parity between prod and test guilds.

---

## 1. Goals & Non-Goals

### Goals
- Safely iterate on jail, ticket, scoring, and automod logic without risk to the live Golden Meadow server.
- Run a second bot instance against a dedicated test guild with realistic data.
- Make the dev/prod split explicit and hard to cross accidentally.
- Enable fast slash command iteration (guild-scoped sync in dev).
- Support reproducible bug repros via fixtures and fake history.
- Establish a baseline of automated tests for cog logic that can run in CI.

### Non-Goals
- Mirroring production 100% — the test guild is a simplified but structurally equivalent version of Golden Meadow.
- Load testing. This environment is for correctness, not performance.
- Staging between dev and prod. v1 is a two-environment model; a third "staging" tier can come later if needed.

---

## 2. Environment Model

### 2.1 Environments

| Env | Discord App | Guild | DB | Log Channel | Slash Sync |
|-----|-------------|-------|----|----|------------|
| `prod` | Dungeon Keeper | Golden Meadow (TGM) | `dk.db` | prod audit channel | global |
| `dev` | Dungeon Keeper [DEV] | Test guild (existing) | `dk_dev.db` | dev audit channel | guild-scoped |

### 2.2 Configuration (`.env`)

```ini
# Environment selector
BOT_ENV=dev                       # "dev" or "prod"

# Prod
DISCORD_TOKEN_PROD=...
GUILD_ID_PROD=...
DB_PATH_PROD=dk.db
AUDIT_CHANNEL_PROD=...

# Dev
DISCORD_TOKEN_DEV=...
GUILD_ID_DEV=...
DB_PATH_DEV=dk_dev.db
AUDIT_CHANNEL_DEV=...

# Dev-only flags
RESET_DEV_DB=0                    # 1 = refresh dev DB from prod on startup
SEED_DEV_FIXTURES=0               # 1 = load fixture data on startup
```

### 2.3 Config Loader

Central `config.py` module. All cogs read config from here, never directly from `os.environ`.

```python
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Config:
    env: str
    token: str
    guild_id: int
    db_path: str
    audit_channel_id: int
    reset_dev_db: bool
    seed_dev_fixtures: bool

    @property
    def is_dev(self) -> bool: return self.env == "dev"

    @property
    def is_prod(self) -> bool: return self.env == "prod"

def load_config() -> Config:
    env = os.getenv("BOT_ENV", "dev").lower()
    if env not in ("dev", "prod"):
        raise ValueError(f"BOT_ENV must be 'dev' or 'prod', got {env!r}")
    suffix = env.upper()
    return Config(
        env=env,
        token=os.environ[f"DISCORD_TOKEN_{suffix}"],
        guild_id=int(os.environ[f"GUILD_ID_{suffix}"]),
        db_path=os.environ[f"DB_PATH_{suffix}"],
        audit_channel_id=int(os.environ[f"AUDIT_CHANNEL_{suffix}"]),
        reset_dev_db=os.getenv("RESET_DEV_DB", "0") == "1" and env == "dev",
        seed_dev_fixtures=os.getenv("SEED_DEV_FIXTURES", "0") == "1" and env == "dev",
    )
```

### 2.4 Slash Command Sync

- **Dev:** sync to test guild only (`bot.tree.sync(guild=discord.Object(id=cfg.guild_id))`) — changes appear instantly.
- **Prod:** global sync, only when schema has actually changed (track a local hash of the command tree; skip sync if unchanged).

### 2.5 Logging

- Separate log files: `logs/dk_prod.log`, `logs/dk_dev.log`.
- Startup banner logs env, bot user, guild ID, DB path at INFO level. See §8.5.

---

## 3. Test Guild Structure

The test guild already exists and is set up as a **structural clone of Golden Meadow**: all channels, categories, and roles share the same names as prod. This name parity is what enables auto-detected ID remapping (§4.3) — rename-in-lockstep is the contract.

### 3.1 Name Parity Contract

- Every channel, category, and role used by the bot in prod must exist in the test guild with **the exact same name** (case-sensitive).
- When something is renamed in prod, dev follows within the same PR/refresh cycle.
- When something is added in prod, dev mirrors it before the next refresh.
- Dev may have **extra** channels/roles (e.g., `#bot-spam`, `@Testers`) that don't exist in prod — these are ignored by the remapper.
- Dev must not have duplicate names among channels/roles that the bot references (see §4.3 on ambiguity handling).

### 3.2 Required Channels & Categories (both guilds)

| Category | Channel | Purpose |
|----------|---------|---------|
| `— INFO —` | `#welcome`, `#rules` | Static, rarely touched |
| `— GENERAL —` | `#general`, `#random`, `#photos` | Active chat for scoring tests |
| `— MOD —` | `#mod-log`, `#mod-chat`, `#audit-log` | Audit/log channels |
| `— JAIL —` (hidden from @everyone) | (dynamic) | Per-user jail channels created here |
| `— TICKETS —` (hidden from @everyone) | (dynamic) | Ticket channels; `#ticket-panel` holds button panel |

### 3.3 Required Roles (both guilds)

| Role | Purpose | Notes |
|------|---------|-------|
| `@Admin` | Full bot control | Your account only |
| `@Mod` | Moderator commands | Real + at least one alt |
| `@Member` | Standard member | All real/alt accounts |
| `@Jailed` | Strip-to-jail target role | No send/view on normal channels |
| `@Bot` | The bot itself | Manage Roles, Manage Channels, etc. — separate role per environment with a different bot ID |

### 3.4 Provisioning Script (optional)

Ship `scripts/provision_test_guild.py` that, given admin token + test guild ID + a prod snapshot (§4.3), creates any missing categories/channels/roles idempotently to match prod's name structure. Useful for rebuilding the test guild from scratch or onboarding a second developer later.

---

## 4. Database Strategy

### 4.1 Principles

1. **Dev DB is disposable.** Any dev run may wipe and rebuild it.
2. **Prod → dev is one-way.** No code path ever writes prod.
3. **Copy uses SQLite backup API**, not raw file copy (handles WAL/SHM correctly even if prod bot is live).
4. **Guild-specific IDs are remapped, never used raw.**

### 4.2 Refresh Workflow

On startup, if `RESET_DEV_DB=1`:

1. If `dk_dev.db` exists, rename to `dk_dev.db.bak-<timestamp>` (keep last 3, delete older).
2. Copy prod via backup API:
   ```python
   import sqlite3
   with sqlite3.connect(cfg_prod_path) as src, sqlite3.connect(cfg.db_path) as dst:
       src.backup(dst)
   ```
3. Run ID remapping (§4.3).
4. Run migrations if schema version differs.
5. Optionally load fixtures (§5) if `SEED_DEV_FIXTURES=1`.

Refresh script lives at `scripts/refresh_dev_db.py` and is also runnable standalone.

### 4.3 ID Remapping (Auto-Detected by Name)

Prod references real Golden Meadow IDs (channels, roles, members) that don't exist in the test guild. Because the test guild is a name-parity clone (§3.1), we build the remap table automatically by matching names — no hand-edited YAML.

#### 4.3.1 Approach

1. **Prod snapshot** — a JSON file `prod_snapshot.json` captures every channel, category, and role in prod with `{id, name, type, parent_name}`. Exported by the prod bot (or a one-off script).
2. **Dev startup scan** — on dev startup, the bot walks the test guild's live channels, categories, and roles.
3. **Name-matching** — join the two by `name` (and `type`/`parent_name` for disambiguation). Write results into the `id_remap` table.
4. **Graceful degradation** — any prod entity with no name match in dev logs a warning and returns `None` at lookup time. Features that depend on the unmatched entity degrade rather than crash.
5. **Users & bots** — not remapped by name. Users map to themselves (same Discord account = same ID across guilds). The dev bot's own user ID is a special case: store `EXPECTED_BOT_ID_PROD` and treat any stored reference to the prod bot's user ID as remapping to the dev bot's user ID automatically.

#### 4.3.2 Schema

```sql
CREATE TABLE IF NOT EXISTS id_remap (
    kind TEXT NOT NULL,           -- 'channel' | 'category' | 'role' | 'bot_user'
    prod_id INTEGER NOT NULL,
    dev_id INTEGER,               -- NULL if unmatched; lookup returns None
    name TEXT NOT NULL,           -- name at time of mapping, for diagnostics
    parent_name TEXT,             -- category name for channels; NULL otherwise
    matched_at TEXT NOT NULL,     -- ISO timestamp
    PRIMARY KEY (kind, prod_id)
);
```

Rebuilt from scratch on every dev startup — it's a cache, not a source of truth.

#### 4.3.3 Prod Snapshot Format

`prod_snapshot.json` (written by `scripts/export_prod_snapshot.py`, run against prod):

```json
{
  "exported_at": "2026-04-23T14:30:00Z",
  "guild_id": 123456789012345678,
  "guild_name": "Golden Meadow",
  "bot_user_id": 100000000000000001,
  "categories": [
    {"id": 200000000000000001, "name": "— GENERAL —"}
  ],
  "channels": [
    {"id": 300000000000000001, "name": "general", "type": "text", "parent_id": 200000000000000001, "parent_name": "— GENERAL —"}
  ],
  "roles": [
    {"id": 400000000000000001, "name": "Mod", "position": 10}
  ]
}
```

Regenerated whenever prod structure changes meaningfully. Committed to the repo (it contains no secrets, just IDs + names).

#### 4.3.4 Matching Algorithm

```python
def match_channel(prod: dict, dev_channels: list[dict]) -> int | None:
    # Exact match on (name, type, parent_name)
    candidates = [
        c for c in dev_channels
        if c["name"] == prod["name"]
        and c["type"] == prod["type"]
        and c.get("parent_name") == prod.get("parent_name")
    ]
    if len(candidates) == 1:
        return candidates[0]["id"]
    if len(candidates) > 1:
        log.warning(
            "ambiguous channel match for prod=%r (%d candidates in dev)",
            prod["name"], len(candidates),
        )
        return None
    # Fallback: name + type only (parent may have been reorganized)
    fallback = [c for c in dev_channels if c["name"] == prod["name"] and c["type"] == prod["type"]]
    if len(fallback) == 1:
        log.info("loose channel match for prod=%r (parent differs)", prod["name"])
        return fallback[0]["id"]
    return None
```

Roles match on `name` alone (no parent concept). Categories match on `name` + `type='category'`.

#### 4.3.5 Remap Build on Startup

```python
async def build_remap(db, dev_guild: discord.Guild, snapshot_path: str) -> RemapStats:
    snapshot = json.loads(Path(snapshot_path).read_text())
    dev_channels = [channel_to_dict(c) for c in dev_guild.channels if not c.category or True]
    dev_roles = [{"id": r.id, "name": r.name} for r in dev_guild.roles]

    stats = RemapStats()
    await db.execute("DELETE FROM id_remap")  # rebuild fresh

    for prod_ch in snapshot["channels"]:
        dev_id = match_channel(prod_ch, dev_channels)
        await db.execute(
            "INSERT INTO id_remap (kind, prod_id, dev_id, name, parent_name, matched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("channel", prod_ch["id"], dev_id, prod_ch["name"], prod_ch.get("parent_name"), now_iso()),
        )
        stats.count("channel", matched=dev_id is not None)

    # ... same for categories, roles ...

    # Bot user: always remap prod bot ID -> dev bot ID
    await db.execute(
        "INSERT INTO id_remap (kind, prod_id, dev_id, name, parent_name, matched_at) "
        "VALUES ('bot_user', ?, ?, 'bot', NULL, ?)",
        (snapshot["bot_user_id"], dev_guild.me.id, now_iso()),
    )

    await db.commit()
    return stats
```

#### 4.3.6 Lookup Helper

The same helper as in v1.0 — cogs never branch on env, always call `resolve_id`:

```python
async def resolve_id(db, kind: str, stored_id: int, cfg: Config) -> int | None:
    if cfg.is_prod:
        return stored_id
    row = await db.fetchone(
        "SELECT dev_id FROM id_remap WHERE kind=? AND prod_id=?",
        (kind, stored_id),
    )
    if row is None or row["dev_id"] is None:
        log.warning("no dev remap for %s id=%s", kind, stored_id)
        return None
    return row["dev_id"]
```

#### 4.3.7 Startup Report

After `build_remap`, log a summary and post to dev audit channel:

```
ID remap built from prod_snapshot.json (exported 2026-04-23T14:30:00Z)
  channels:   12/12 matched
  categories:  5/5  matched
  roles:       8/9  matched  ⚠ 1 unmatched: "LegacyRole"
  bot_user:   1/1  mapped
```

Unmatched items are logged with `WARNING` level, not errors. The bot continues to run; any feature that tries to resolve the unmatched ID will get `None` and should handle that gracefully (e.g., skip the audit log post, skip the role sync).

#### 4.3.8 When to Re-Snapshot

Re-run `scripts/export_prod_snapshot.py` whenever:
- A channel, category, or role is renamed, created, or deleted in prod.
- The prod bot is replaced with a new Discord application (new `bot_user_id`).
- You notice unexpected unmatched-ID warnings in dev after a refresh.

The script is safe to run against prod at any time — it only reads guild structure, writes no data.

#### 4.3.9 Edge Cases

- **Ambiguous names in dev** (two channels named `general`): logged as warning, returns `None`. Fix by renaming the duplicate in the test guild.
- **Missing in dev** (prod has `#archive`, dev doesn't): returns `None`, feature degrades. Don't auto-create — silent creation hides structural drift.
- **Extra in dev** (dev has `#bot-spam`, prod doesn't): harmless, ignored by the matcher.
- **Renamed out of lockstep** (prod renamed, snapshot not refreshed): match fails, warning logged. Fix by re-exporting the snapshot.
- **User IDs**: not remapped (same account across guilds). Stored alt account IDs work on both servers because you're in both with the same Discord identity.
- **Dynamic channels** (jail channels, ticket channels created per-incident): these don't exist in the snapshot. They're created fresh in dev via normal bot flow, with their own dev IDs from the start. No remapping needed.

### 4.4 Safety Rails (DB-specific)

- `load_config()` asserts DB path contains `"dev"` iff `env == "dev"` and does not iff `env == "prod"`.
- `scripts/refresh_dev_db.py` refuses to run if destination path does not contain `"dev"`.
- No script in the repo accepts a destination DB path without `"dev"` in it outside of explicit prod deploy scripts.

---

## 5. Seed Data / Fixtures

Copying prod doesn't help for features where records reference guild-specific entities (jails, tickets, scoring history tied to channels). Fixtures fill that gap.

### 5.1 Layout

```
fixtures/
├── dev_users.yaml           # Fake user definitions (see §6 for real alts)
├── jail_scenarios.yaml      # Pre-built jail rows for testing UI/flows
├── ticket_scenarios.yaml    # Open/closed/claimed tickets
├── scoring_history.yaml     # Backdated message activity for scoring tests
└── automod_hits.yaml        # Simulated automod events for linking tests
```

### 5.2 Loader

`scripts/load_fixtures.py` reads YAML, writes rows directly to `dk_dev.db`, using dev guild IDs (from `id_remap`). Idempotent: clears fixture-owned rows before inserting (mark with `source='fixture'` column on relevant tables).

### 5.3 Scenario Examples

**Jail scenarios** (covers edge cases the UI needs to handle):

```yaml
- name: active_standard_jail
  user: alt_account_1
  sentence_minutes: 60
  reason: "Testing standard jail flow"
  channel: auto                  # will be created by bot
  appeals: []

- name: expired_awaiting_release
  user: alt_account_2
  sentence_minutes: 1
  started_at: -5m                # relative to now
  reason: "Tests release handler"

- name: appeal_pending
  user: alt_account_1
  sentence_minutes: 1440
  appeals:
    - submitted_at: -1h
      body: "I was testing, please let me out"
```

**Scoring history** uses relative dates so tests work regardless of clock:

```yaml
- user: alt_account_1
  messages:
    - channel: general
      count: 45
      window: "-7d..now"
    - channel: photos
      count: 12
      window: "-30d..-7d"
  reactions_given:
    - count: 80
      window: "-14d..now"
```

### 5.4 Time-Travel Helpers

For the 90-day scoring window, fixtures need to insert rows at arbitrary past timestamps. Provide `scripts/backdate_activity.py --user <id> --channel <id> --days-ago 85 --count 10` for one-off testing of edge-of-window behavior.

---

## 6. Test Accounts

### 6.1 Recommendation

Maintain **2–3 alt Discord accounts** in the test guild:

- `alt_jailbird` — exercises jail target flow, receives DMs, files appeals.
- `alt_ticket_user` — opens tickets, interacts with claim/close flow.
- `alt_mod` (optional) — carries `@Mod` role so you can test two-mod escalation without handing off control.

### 6.2 ToS Note

Discord's ToS permits alts for legitimate purposes (bot testing qualifies). Keep the alts' purpose clear in the account's "About me" if asked. Never use alts in the prod guild.

### 6.3 Practical Setup

- Use browser profiles (Firefox Multi-Account Containers or Chrome profiles) rather than logging in/out.
- Keep alt credentials in a password manager, not in `.env`.
- Alts never receive bot admin permissions.

---

## 7. Development Workflow

### 7.1 Branch → Test → Merge Loop

1. `git checkout -b feature/xyz` off `main`.
2. Run dev bot locally (`BOT_ENV=dev python -m dungeon_keeper`).
3. Iterate in test guild; use cog hot-reload (`/reload <cog>` admin command, dev-only).
4. When stable, open PR. CI runs tests (§9).
5. Merge to `main`.
6. Deploy prod bot from `main` via systemd/Docker restart on the host.

### 7.2 Schema Migrations

- Use a lightweight versioned migration system: `migrations/000_init.sql`, `001_add_jail_appeals.sql`, etc.
- `schema_version` table tracks applied migrations.
- On startup, apply any pending migrations in order. Dev and prod use the same migration files.
- **After refreshing dev from prod, migrations still re-run idempotently** (each migration checks `schema_version` before applying). This handles the case where prod is at v7, a new v8 migration lands, and dev needs to apply it on top of the refreshed copy.

### 7.3 Hot Reload (Dev Only)

```python
@bot.tree.command(name="reload", guild=discord.Object(id=cfg.guild_id))
async def reload_cog(interaction, cog: str):
    if not cfg.is_dev:
        return await interaction.response.send_message("Dev only.", ephemeral=True)
    await bot.reload_extension(f"cogs.{cog}")
    await interaction.response.send_message(f"Reloaded `{cog}`", ephemeral=True)
```

Only registered when `cfg.is_dev`. Prod does clean restarts.

---

## 8. Safety Rails

All of these are startup assertions in `bot.py` before `bot.start()`.

### 8.1 Token/Env Match

Fetch bot user via Discord API on ready, compare ID against an expected-ID env var per environment:

```ini
EXPECTED_BOT_ID_PROD=...
EXPECTED_BOT_ID_DEV=...
```

If mismatch, log `CRITICAL` and exit before registering any handlers.

### 8.2 DB Path vs Env

- `env == "dev"` requires `"dev"` in DB filename.
- `env == "prod"` requires `"dev"` NOT in DB filename.

### 8.3 Guild ID vs Env

On `on_ready`, if the bot is in a guild ID that doesn't match `cfg.guild_id`, log `CRITICAL`, leave the guild, and shut down. This prevents the prod bot from ever operating against the test guild if someone accidentally invites it.

### 8.4 One-Way Refresh Enforcement

`scripts/refresh_dev_db.py` hard-codes source=prod path, dest=dev path; refuses any CLI override that would swap them.

### 8.5 Startup Banner

```
════════════════════════════════════════════════════
  DUNGEON KEEPER   env=DEV   bot=DK[DEV]#1234
  guild=987654321098765432 (Test Guild)
  db=dk_dev.db   audit=#audit-log
  seed_fixtures=True   reset_db=False
════════════════════════════════════════════════════
```

Color-coded in terminal (red background for prod). Logged at INFO. Posted to audit channel on first `on_ready`.

---

## 9. Automated Testing Strategy

### 9.1 Philosophy

Discord offers no native testing features beyond "run a second bot against a second guild." Everything automated lives in our repo. The goal is to pytest as much as possible — realistically **85%+ of meaningful coverage** — and treat manual testing in the dev guild as the final UX smoke check, not the primary safety net.

The strategy rests on two principles:

1. **Pure logic lives outside the Discord shell.** Every cog callback is a thin wrapper around a pure function that takes IDs and primitives and returns a result. The wrapper's job is translating between `discord.Interaction` and the pure function. Pure functions are trivially testable; wrappers are trivially reviewable.
2. **dpytest is not relied upon.** As of early 2026, dpytest has had no PyPI release in over 12 months and is self-described as alpha with partial API coverage. We use pytest + typed fake objects + real SQLite instead. This keeps the test suite independent of a library that may stop working on a discord.py upgrade.

### 9.2 Toolchain

| Tool | Purpose |
|------|---------|
| `pytest` | Test runner |
| `pytest-asyncio` (`asyncio_mode = "auto"`) | Async test support without decorators on every test |
| `aiosqlite` against `tmp_path` or `:memory:` | Real DB in tests (never mock the DB layer) |
| `freezegun` | Deterministic time for expiry/window/rate-limit tests |
| `hypothesis` | Property-based testing for scoring math and parsers |
| `syrupy` | Snapshot testing for embeds and structured outputs |
| `pytest-cov` | Coverage reporting |
| `ruff`, `mypy` | Lint and type checks in CI |

Deliberately **not** used: `dpytest` (maintenance risk), mocked DB layers (hides bugs), `pytest-mock` as primary approach (prefer plain `unittest.mock` in small typed fakes).

### 9.3 Test Tiers

Four tiers, ordered by how much of the bot each covers. Every cog must have coverage at Tiers 1–3; Tier 4 is optional.

#### Tier 1 — Pure Logic (required, target 95%+ coverage)

Everything extracted from callbacks: jail duration parsing, scoring math, ticket state transitions, ID remap matchers, evasion detection, automod rule evaluation, DB read/write helpers.

```python
# dungeon_keeper/scoring.py — pure logic
def compute_score_from_components(
    engagement: float, consistency: float, resonance: float, activity: float
) -> float:
    return (
        0.40 * engagement
        + 0.25 * consistency
        + 0.20 * resonance
        + 0.15 * activity
    )

# tests/unit/test_scoring.py
from hypothesis import given, strategies as st

@given(
    e=st.floats(0, 100), c=st.floats(0, 100),
    r=st.floats(0, 100), a=st.floats(0, 100),
)
def test_score_bounded(e, c, r, a):
    result = compute_score_from_components(e, c, r, a)
    assert 0 <= result <= 100

def test_score_weights_sum_to_one():
    # all components = 100 → result = 100
    assert compute_score_from_components(100, 100, 100, 100) == 100

def test_engagement_dominates():
    high_e = compute_score_from_components(100, 0, 0, 0)
    high_a = compute_score_from_components(0, 0, 0, 100)
    assert high_e > high_a  # 40% vs 15%
```

#### Tier 2 — Static Component Trees (required, target 100% of Views/Modals)

Every `discord.ui.View` and `discord.ui.Modal` subclass gets structural assertions: custom_id presence, Discord limits respected, persistent views properly configured, TextInput constraints set correctly. These are cheap, fast, and catch a category of bug that otherwise only surfaces at runtime.

```python
# tests/components/test_ticket_views.py
import discord
from dungeon_keeper.cogs.tickets import TicketPanelView, TicketActionsView, JailAppealModal

def test_ticket_panel_persistent():
    view = TicketPanelView()
    assert view.timeout is None
    for child in view.children:
        assert child.custom_id is not None
        assert "{" not in child.custom_id  # no f-string interpolation in persistent IDs

def test_ticket_panel_within_discord_limits():
    view = TicketPanelView()
    assert len(view.children) <= 25
    rows = {}
    for child in view.children:
        rows.setdefault(child.row or 0, []).append(child)
    for row_items in rows.values():
        assert len(row_items) <= 5

def test_ticket_close_button_config():
    view = TicketActionsView(ticket_id=42)
    close = next(c for c in view.children if c.custom_id == "ticket:close")
    assert close.style == discord.ButtonStyle.danger
    assert close.label == "Close Ticket"

def test_appeal_modal_fields():
    modal = JailAppealModal(jail_id=1)
    body = next(c for c in modal.children if c.custom_id == "appeal:body")
    assert body.required is True
    assert body.max_length == 2000
    assert body.style == discord.TextStyle.paragraph
```

#### Tier 3 — Callback Branches with Fake Interactions (required for mod-critical cogs, target 80%+)

Callbacks themselves are tested by building a fake `discord.Interaction` and invoking the callback directly. This covers ephemeral vs public responses, conditional followups, error branches, and interaction-level permission checks — the glue layer between the shell and the pure logic.

Fake factories live in `tests/fakes.py` (see §9.4) and are shared across all callback tests.

```python
# tests/cogs/test_ticket_callbacks.py
async def test_ticket_modal_submit_success(fake_interaction, temp_db):
    modal = TicketModal(db=temp_db, category_id=111)
    modal.reason._value = "Need help with role sync"
    await modal.on_submit(fake_interaction)

    fake_interaction.response.send_message.assert_called_once()
    args, kwargs = fake_interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "Ticket opened" in args[0]

    # DB state
    row = await temp_db.fetchone("SELECT * FROM tickets WHERE opened_by=?", (fake_interaction.user.id,))
    assert row["status"] == "open"

async def test_ticket_modal_submit_rate_limited(fake_interaction, temp_db):
    await seed_recent_ticket(temp_db, user_id=fake_interaction.user.id, minutes_ago=2)
    modal = TicketModal(db=temp_db, category_id=111)
    modal.reason._value = "Another ticket"
    await modal.on_submit(fake_interaction)

    args, kwargs = fake_interaction.response.send_message.call_args
    assert "wait" in args[0].lower()
    assert kwargs.get("ephemeral") is True
```

#### Tier 4 — Snapshot Tests for User-Facing Content (optional but cheap)

Every embed, DM template, and audit log message gets a snapshot. Catches accidental wording drift.

```python
# tests/snapshots/test_jail_embeds.py
def test_jail_notification_embed(snapshot):
    embed = build_jail_notification(
        user_id=1001,
        reason="spam testing",
        duration_minutes=60,
        appeal_channel_id=2001,
    )
    assert embed.to_dict() == snapshot
```

First run writes the snapshot; subsequent runs fail if the output changes. Intentional changes updated with `pytest --snapshot-update`. Diffs are reviewed in PRs.

### 9.4 Shared Fakes (`tests/fakes.py`)

One module of typed fake objects, imported wherever needed. Never rebuilt ad-hoc in individual tests.

```python
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock
import discord

@dataclass
class FakeRole:
    id: int
    name: str = "Role"
    position: int = 0

@dataclass
class FakeUser:
    id: int = 1001
    name: str = "alt_jailbird"
    bot: bool = False
    roles: list = field(default_factory=list)

@dataclass
class FakeChannel:
    id: int
    name: str = "general"
    type: str = "text"
    parent_id: int | None = None

@dataclass
class FakeGuild:
    id: int = 9001
    name: str = "Test Guild"
    members: dict = field(default_factory=dict)
    channels: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)

    def get_member(self, uid): return self.members.get(uid)
    def get_channel(self, cid): return self.channels.get(cid)
    def get_role(self, rid): return self.roles.get(rid)

def fake_interaction(*, user=None, guild=None, **overrides) -> MagicMock:
    i = MagicMock(spec=discord.Interaction)
    i.user = user or FakeUser()
    i.guild = guild or FakeGuild()
    i.response = MagicMock()
    i.response.send_message = AsyncMock()
    i.response.send_modal = AsyncMock()
    i.response.defer = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.followup = MagicMock()
    i.followup.send = AsyncMock()
    i.edit_original_response = AsyncMock()
    for k, v in overrides.items():
        setattr(i, k, v)
    return i
```

### 9.5 Shared Fixtures (`tests/conftest.py`)

```python
import pytest
import pytest_asyncio
import aiosqlite
from pathlib import Path
from dungeon_keeper.migrations import apply_migrations
from dungeon_keeper.config import Config
from tests.fakes import fake_interaction as _fake_interaction, FakeGuild, FakeUser

@pytest_asyncio.fixture
async def temp_db(tmp_path):
    path = tmp_path / "test.db"
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await apply_migrations(db)
    yield db
    await db.close()

@pytest.fixture
def test_config(tmp_path):
    return Config(
        env="dev",
        token="fake",
        guild_id=9001,
        db_path=str(tmp_path / "test.db"),
        audit_channel_id=9999,
        reset_dev_db=False,
        seed_dev_fixtures=False,
    )

@pytest.fixture
def fake_interaction():
    return _fake_interaction()

@pytest.fixture
def guild_with_mods():
    g = FakeGuild()
    g.roles[5001] = FakeRole(id=5001, name="Mod")
    g.roles[5002] = FakeRole(id=5002, name="Jailed")
    return g
```

### 9.6 Directory Layout

```
tests/
├── conftest.py                     # shared fixtures (§9.5)
├── fakes.py                        # typed fake objects (§9.4)
├── unit/                           # Tier 1
│   ├── test_scoring.py
│   ├── test_id_remap.py
│   ├── test_jail_logic.py
│   ├── test_ticket_logic.py
│   ├── test_automod_rules.py
│   └── test_config.py
├── components/                     # Tier 2
│   ├── test_ticket_views.py
│   ├── test_jail_views.py
│   └── test_appeal_modal.py
├── cogs/                           # Tier 3
│   ├── test_jail_callbacks.py
│   ├── test_ticket_callbacks.py
│   └── test_automod_link.py
├── snapshots/                      # Tier 4
│   ├── test_jail_embeds.py
│   ├── test_ticket_embeds.py
│   └── __snapshots__/              # auto-managed by syrupy
├── migrations/
│   └── test_migrations.py
└── integration/                    # DB-level, no Discord
    ├── test_jail_lifecycle.py      # jail → expiry → release (with freezegun)
    └── test_ticket_lifecycle.py    # open → claim → close → delete
```

### 9.7 Time-Dependent Tests

Everything time-sensitive uses `freezegun`. Non-negotiable — wall-clock tests are flaky.

```python
from freezegun import freeze_time

@freeze_time("2026-04-23 12:00:00")
async def test_score_excludes_day_91(temp_db):
    await insert_message(temp_db, user_id=1, sent_at="2026-01-22 12:00:00")
    score = await compute_posting_activity(temp_db, user_id=1, window_days=90)
    assert score == 0

@freeze_time("2026-04-23 12:00:00") as frozen:
    async def test_jail_lifecycle_expiry(temp_db, guild_with_mods):
        await jail_user(temp_db, user_id=1001, duration_minutes=60, reason="test")
        frozen.move_to("2026-04-23 13:00:01")
        released = await process_expired_jails(temp_db, guild_with_mods)
        assert len(released) == 1
```

### 9.8 Sample Test Cases by Feature

**Scoring:**
- Weights sum to 1.0 (property test).
- Result bounded 0–100 for any valid component inputs (property test).
- 90-day window excludes day 91 exactly (freezegun).
- Leave-of-absence pause: score frozen during paused window.
- Tenure buffer: members <14 days cannot drop below removal threshold.
- Anti-gaming: rapid-fire reactions from single user capped in engagement score.

**Jail:**
- `jail_user` creates row with correct expiry (freezegun + duration).
- Role snapshot captures all non-@everyone roles at jail time.
- `process_expired_jails` returns restore plans for expired rows only.
- Rejoin-while-jailed detection: user leaves jail state persisted → rejoin re-applies `@Jailed`.
- Vote-jail threshold: below quorum no action, at quorum jails.
- Appeal rate limit: second appeal within 24h rejected.

**Tickets:**
- Ticket creation sets `status='open'`, `opened_by`, `channel_id`.
- Claim creates DM-subscription row, emits audit event.
- Dual-claim: second claimer triggers escalation flag.
- Two-step close → delete: close sets `status='closed'`, delete archives transcript then removes row.
- Rate limit: same user opening 2nd ticket <5min gets rejected.

**ID remap (§4.3):**
- `match_channel` returns dev_id on exact (name, type, parent_name) match.
- `match_channel` logs warning + returns `None` on ambiguous dev-side names.
- `match_channel` falls back to (name, type) when parent differs.
- `build_remap` populates expected row counts from fixture snapshot.
- `resolve_id` short-circuits in prod mode.
- Bot user special-case: prod bot ID → dev bot live user ID.

**Components (Tier 2):**
- Every persistent View has `timeout=None` and static `custom_id`s.
- No row exceeds 5 components; no View exceeds 25.
- All button labels ≤ 80 chars, all modal titles ≤ 45 chars.
- All TextInputs have correct `max_length`, `required`, `style`.

**Snapshots (Tier 4):**
- Jail notification DM embed.
- Jail public announcement embed.
- Ticket opened confirmation.
- Ticket closed transcript header.
- Appeal approved / appeal denied DMs.
- All audit log messages (one snapshot per event type).

### 9.9 Coverage Targets

| Module | Target | Required? |
|--------|--------|-----------|
| `dungeon_keeper/scoring.py` | 95% | Yes |
| `dungeon_keeper/jail/logic.py` | 95% | Yes |
| `dungeon_keeper/tickets/logic.py` | 95% | Yes |
| `dungeon_keeper/id_remap.py` | 95% | Yes |
| `dungeon_keeper/config.py` | 100% | Yes |
| Views/Modals (Tier 2) | 100% | Yes |
| Callbacks (Tier 3) — jail, tickets | 80% | Yes |
| Callbacks (Tier 3) — other cogs | 60% | No |
| Snapshots (Tier 4) | opportunistic | No |

CI fails the build if required coverage drops below target.

### 9.10 CI Pipeline

`.github/workflows/test.yml`:

```yaml
name: tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: ruff check .
      - run: mypy dungeon_keeper
      - run: pytest --cov=dungeon_keeper --cov-report=term-missing --cov-fail-under=80
      - name: Snapshot drift check
        run: pytest --snapshot-warn-unused tests/snapshots/
```

### 9.11 What Is NOT Automated

Explicit list of what still requires manual verification in the dev guild:

- Visual rendering of embeds on desktop + mobile.
- Actual Discord API shape (event payloads, new fields). Mitigation: pin `discord.py`, smoke-test after every upgrade.
- Modal UX feel (pop-up latency, field tab order).
- Rate limit behavior under real load.
- Gateway reconnection and resume.
- Permission cache staleness when roles change mid-operation.
- Integration with real Discord AutoMod rules (AutoMod is server-side, we link to it but don't control it).

Every PR that touches a user-facing surface includes a **Manual Test Checklist** in the description:

```markdown
## Manual verification (dev guild)
- [ ] Triggered affected command/flow with real alt account
- [ ] Embed rendered correctly on desktop client
- [ ] Embed rendered correctly on mobile client
- [ ] Audit log entry posted with correct content
- [ ] DM delivered and formatted correctly (if applicable)
```

### 9.12 Test Performance

Target: full suite runs in **under 10 seconds** on a laptop. Hypothesis property tests may add 2–3 seconds when they explore deeply. Keep it fast so nobody skips local runs.

- Real SQLite against `tmp_path` is fast (~1ms per test with migrations cached).
- No network calls in tests, ever. If a test needs to hit Discord, it's a manual test, not a pytest.
- Snapshot tests are the cheapest kind — string/dict comparison.

---

## 10. File/Module Additions

New files this spec introduces:

```
dungeon_keeper/
├── config.py                        # §2.3
├── id_remap.py                      # §4.3 (build_remap, match_channel, resolve_id)
├── safety.py                        # §8 startup assertions
├── scoring.py                       # pure scoring logic (§9.3 Tier 1)
├── jail/
│   ├── logic.py                     # pure jail logic (§9.3 Tier 1)
│   └── views.py                     # Views and Modals (§9.3 Tier 2)
└── tickets/
    ├── logic.py                     # pure ticket logic (§9.3 Tier 1)
    └── views.py                     # Views and Modals (§9.3 Tier 2)

scripts/
├── refresh_dev_db.py                # §4.2
├── export_prod_snapshot.py          # §4.3.3 (run against prod)
├── load_fixtures.py                 # §5.2
├── backdate_activity.py             # §5.4
└── provision_test_guild.py          # §3.4 (optional, consumes prod_snapshot.json)

tests/
├── conftest.py                      # §9.5
├── fakes.py                         # §9.4
├── unit/                            # Tier 1 — pure logic
├── components/                      # Tier 2 — static View/Modal assertions
├── cogs/                            # Tier 3 — fake-interaction callback tests
├── snapshots/                       # Tier 4 — embed/DM snapshots
├── integration/                     # DB-level, no Discord
└── migrations/

prod_snapshot.json                   # §4.3.3 (committed, regenerated on structure changes)
fixtures/                            # §5
migrations/                          # §7.2
.github/workflows/test.yml           # §9.10
requirements-dev.txt                 # §9.2 toolchain
```

Additions to `.env.example`: all vars from §2.2 plus `EXPECTED_BOT_ID_*` from §8.1.

`requirements-dev.txt` contents:

```
pytest>=8.0
pytest-asyncio>=0.23
pytest-cov>=4.1
freezegun>=1.4
hypothesis>=6.100
syrupy>=4.6
ruff>=0.4
mypy>=1.10
```

---

## 11. Implementation Order

Recommended build order for Claude Code handoff:

1. `config.py` + safety assertions (§2, §8) — foundational, everything else depends on env awareness.
2. Migration framework + initial migration (§7.2) — needed before any DB work.
3. `refresh_dev_db.py` using SQLite backup API (§4.2) — no remapping yet.
4. `export_prod_snapshot.py` (§4.3.3) — run against prod once, commit the resulting `prod_snapshot.json`.
5. `id_remap.py` — schema, `build_remap`, `match_channel`, `resolve_id` helper (§4.3). Wire into bot startup.
6. **Architectural split: extract pure logic from existing cogs** into `jail/logic.py`, `tickets/logic.py`, `scoring.py` (§9.3 Tier 1). Callbacks become thin wrappers. This is a prerequisite for meaningful automated testing — do it before writing tests.
7. Thread `resolve_id` through existing cogs wherever stored IDs are looked up.
8. `tests/` skeleton: `conftest.py`, `fakes.py`, Tier 1 unit tests for config/remap/scoring/jail logic/ticket logic (§9.4–9.6).
9. Tier 2 component tests for every existing View and Modal (§9.3).
10. Tier 3 callback tests for jail and ticket cogs (§9.3).
11. CI workflow (§9.10).
12. Fixture loader + 2–3 starter scenarios (§5) — enough to exercise jail/ticket happy paths in integration tests.
13. Tier 4 snapshot tests (§9.3) — opportunistic, add as embeds stabilize.
14. `provision_test_guild.py` (§3.4) — last, optional.

---

## 12. Open Questions

- **Should the snapshot auto-refresh from the prod bot on a schedule** (e.g., nightly, writing to `prod_snapshot.json` and committing via a bot-authored PR)? Convenient, but adds moving parts. Recommend **no** for v1 — manual re-export is fine given how rarely guild structure actually changes.
- **Snapshot versioning**: do we need to keep historical snapshots for reproducing past bugs? Probably not — the `matched_at` timestamp in `id_remap` plus git history of `prod_snapshot.json` gives enough audit trail.
- Do alts need to appear in prod scoring history fixtures, or only in dev-native fixtures? Recommend **dev-native only** — keeps prod data clean.
- Is there a case for a third "local-solo" env that doesn't touch Discord at all (pure unit tests)? Covered implicitly by `tests/unit/` and `tests/integration/` which use no gateway at all — no separate env needed.
- **Should we add a nightly job that runs the dev bot headlessly against a scripted scenario?** A cron that launches the dev bot, posts messages via a test harness account, asserts expected responses. Higher fidelity than pytest but much more fragile. Recommend **no** for v1 — manual dev-guild testing covers this adequately for a single-developer project.
