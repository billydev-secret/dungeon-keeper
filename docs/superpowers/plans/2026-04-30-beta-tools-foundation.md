# Beta Tools — Plan 1: Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the DK Tools sidecar bot with 3 puppet bots and a webhook fleet, all behind five layers of dev-only safety rails. End state: `/beta puppets list` shows the roster + connection state, `/beta puppets impersonate <key> <channel> <text>` can manually drive a puppet or a webhook ghost, and the whole thing physically refuses to run outside `BOT_ENV=dev`. No simulation yet — that's Plan 3.

**Architecture:** A separate Python package `beta_tools/` runs as a second process (`python -m beta_tools`). It uses its own `BetaConfig` (parallel to the main `Config`), connects with its own bot token, spins up three additional `discord.Client` instances for the puppets, and shares the `dk_dev.db` migration framework with the main bot. Slash commands live under `/beta` scoped to the test guild only.

**Tech Stack:** Python 3.11+, `discord.py 2.4+` (`commands.Bot`, `app_commands`), `aiosqlite`, `python-dotenv`, `PyYAML`, `pytest` + `pytest-asyncio` (asyncio_mode=auto). All already in the project.

**Spec reference:** `docs/superpowers/specs/2026-04-30-beta-tools-sidecar-design.md` — sections §2, §3.1, §3.2 (webhook plumbing only), §6 (skeleton), §7 (all 5 safety rails).

---

## File Structure

This plan creates these files:

| Path | Responsibility |
|---|---|
| `beta_tools/__init__.py` | Package marker |
| `beta_tools/__main__.py` | Entry point: safety check → bot construction → run |
| `beta_tools/config.py` | `BetaConfig` dataclass + `load_beta_config()` reading sidecar env vars |
| `beta_tools/safety.py` | All 5 safety-rail functions, modeled on the existing `safety.py` |
| `beta_tools/db_gate.py` | `beta_write(db, query, params)` wrapper that asserts dev env |
| `beta_tools/personas.py` | `Persona` dataclass + `load_puppet_personas(path)` reading YAML |
| `beta_tools/puppet_manager.py` | `PuppetManager` — owns 3 `discord.Client` instances |
| `beta_tools/webhook_fleet.py` | `WebhookFleet` — provisions and sends through per-channel webhooks |
| `beta_tools/bot.py` | DK Tools `commands.Bot` subclass with on_ready + on_guild_join hooks |
| `beta_tools/slash/__init__.py` | Package marker |
| `beta_tools/slash/_base.py` | Role-check decorator (mod-or-admin) shared across slash modules |
| `beta_tools/slash/help.py` | `/beta help` |
| `beta_tools/slash/puppets.py` | `/beta puppets list \| reload \| reconnect \| impersonate` |
| `migrations/007_beta_source_tags.sql` | Adds `source` columns to tables beta_tools will write to in later plans |
| `fixtures/beta_puppets.yaml` | 3 puppet persona configs |

Plus tests in `tests/beta/` (no `__init__.py` per project convention):

| Path | What it covers |
|---|---|
| `tests/beta/test_beta_config.py` | `BetaConfig` + `load_beta_config()` |
| `tests/beta/test_beta_safety.py` | All five safety-rail functions |
| `tests/beta/test_beta_db_gate.py` | `beta_write` wrapper |
| `tests/beta/test_beta_personas.py` | YAML loader |
| `tests/beta/test_beta_puppet_manager.py` | PuppetManager construction + persona application logic |
| `tests/beta/test_beta_webhook_fleet.py` | WebhookFleet provisioning + send logic |
| `tests/beta/test_beta_slash_puppets.py` | `/beta puppets *` command handlers |
| `tests/beta/test_beta_migration.py` | Migration 007 applies idempotently and adds the expected columns |

`.env.example` is updated. `requirements.txt` already has all deps; nothing to add.

---

## Task 1: Add the source-tag schema migration

**Files:**
- Create: `migrations/007_beta_source_tags.sql`
- Test: `tests/beta/test_beta_migration.py`

The initial table list is the four named in the spec (`message_store`, `xp_members`, `jails`, `tickets`). More tables get the column in later plans as needed; this migration is incremental — Plan 3 adds another migration covering scoring/watch/etc. tables.

- [ ] **Step 1: Confirm the four tables exist in current schema**

Run: `sqlite3 dk_dev.db ".schema message_store" 2>/dev/null | head -5 && sqlite3 dk_dev.db ".schema xp_members" 2>/dev/null | head -5 && sqlite3 dk_dev.db ".schema jails" 2>/dev/null | head -5 && sqlite3 dk_dev.db ".schema tickets" 2>/dev/null | head -5`

Expected: each table has a `CREATE TABLE` line. If any are missing, look in `migrations/000_init.sql` for the actual table name and adjust this task to match.

If a table name differs (e.g., `message_archive` instead of `message_store`), use the actual name throughout the rest of this plan — search/replace before continuing.

- [ ] **Step 2: Write the failing migration test**

Create `tests/beta/test_beta_migration.py`:

```python
"""Tests for migration 007 (beta source-tag columns)."""

from __future__ import annotations

import pytest


async def _column_names(db, table: str) -> list[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return [row[1] for row in rows]


async def test_migration_007_adds_source_to_message_store(temp_db):
    cols = await _column_names(temp_db, "message_store")
    assert "source" in cols, f"expected 'source' column on message_store, got {cols}"


async def test_migration_007_adds_source_to_xp_members(temp_db):
    cols = await _column_names(temp_db, "xp_members")
    assert "source" in cols


async def test_migration_007_adds_source_to_jails(temp_db):
    cols = await _column_names(temp_db, "jails")
    assert "source" in cols


async def test_migration_007_adds_source_to_tickets(temp_db):
    cols = await _column_names(temp_db, "tickets")
    assert "source" in cols


async def test_migration_007_creates_index(temp_db):
    cursor = await temp_db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_message_store_source'"
    )
    row = await cursor.fetchone()
    assert row is not None, "expected idx_message_store_source index to exist"


async def test_migration_007_is_idempotent(temp_db):
    """Re-applying migrations on an already-migrated DB should be a no-op."""
    from migrations import apply_migrations
    await apply_migrations(temp_db)  # second apply
    # Still works:
    cols = await _column_names(temp_db, "message_store")
    assert "source" in cols
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/beta/test_beta_migration.py -v`
Expected: FAIL — `assert 'source' in [...]` because the column doesn't exist yet.

- [ ] **Step 4: Write the migration**

Create `migrations/007_beta_source_tags.sql`:

```sql
-- Beta tools: source-tag columns for clean cleanup of synthetic data.
-- Default NULL = real production data. 'beta_sim' = ambient sim. 'beta_seed' = one-shot historical seed.
-- More tables get this column in later beta_tools plans (008+) as those code paths are added.
-- Each ALTER is wrapped in a no-op-on-conflict pattern: SQLite's ALTER TABLE ADD COLUMN does not
-- support IF NOT EXISTS, so we use a sentinel SELECT against pragma_table_info to skip if present.

-- message_store
INSERT INTO schema_version (migration, applied_at)
SELECT '007_beta_source_tags.sql.guard', strftime('%s', 'now')
WHERE NOT EXISTS (SELECT 1 FROM pragma_table_info('message_store') WHERE name = 'source');

-- The above guard insert is a hack to make the rest conditional; SQLite has no proper IF NOT EXISTS
-- for ALTER TABLE ADD COLUMN. Instead we rely on the migrations framework only running this file
-- once (tracked in schema_version), and accept that re-running manually would error.

ALTER TABLE message_store ADD COLUMN source TEXT;
ALTER TABLE xp_members   ADD COLUMN source TEXT;
ALTER TABLE jails        ADD COLUMN source TEXT;
ALTER TABLE tickets      ADD COLUMN source TEXT;

CREATE INDEX IF NOT EXISTS idx_message_store_source
  ON message_store(source) WHERE source IS NOT NULL;

-- Clean up the guard row written above.
DELETE FROM schema_version WHERE migration = '007_beta_source_tags.sql.guard';
```

Note: the migrations framework in `migrations/__init__.py` already tracks applied migrations in `schema_version` and skips them on re-apply, so the per-file idempotency we need is "the framework won't run it twice." The guard hack above is removed in step 6 once we confirm the framework's once-only contract is sufficient.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/beta/test_beta_migration.py -v`
Expected: PASS — all six tests green.

- [ ] **Step 6: Simplify the migration (remove the guard hack)**

The migrations framework guarantees once-only application, so the guard isn't needed. Replace `migrations/007_beta_source_tags.sql` with the clean version:

```sql
-- Beta tools: source-tag columns for clean cleanup of synthetic data.
-- Default NULL = real production data. 'beta_sim' = ambient sim. 'beta_seed' = one-shot historical seed.
-- More tables get this column in later beta_tools plans (008+) as those code paths are added.
ALTER TABLE message_store ADD COLUMN source TEXT;
ALTER TABLE xp_members   ADD COLUMN source TEXT;
ALTER TABLE jails        ADD COLUMN source TEXT;
ALTER TABLE tickets      ADD COLUMN source TEXT;

CREATE INDEX IF NOT EXISTS idx_message_store_source
  ON message_store(source) WHERE source IS NOT NULL;
```

- [ ] **Step 7: Re-run tests**

Run: `pytest tests/beta/test_beta_migration.py -v`
Expected: PASS — same six tests still green.

- [ ] **Step 8: Commit**

```bash
git add migrations/007_beta_source_tags.sql tests/beta/test_beta_migration.py
git commit -m "feat(beta-tools): add source-tag columns to seed cleanup pivot

Adds nullable source TEXT column to message_store, xp_members, jails,
and tickets. Cleanup queries in later plans pivot on source LIKE 'beta_%'
to delete only synthetic rows. Real prod rows have source=NULL.
"
```

---

## Task 2: Add `BetaConfig` dataclass and loader

**Files:**
- Create: `beta_tools/__init__.py`
- Create: `beta_tools/config.py`
- Test: `tests/beta/test_beta_config.py`

A separate config dataclass (parallel to the main `Config` in `config.py`) keeps sidecar-only fields isolated from the production bot's config surface.

- [ ] **Step 1: Write the failing test**

Create `tests/beta/test_beta_config.py`:

```python
"""Tests for BetaConfig and load_beta_config."""

from __future__ import annotations

import pytest

from beta_tools.config import BetaConfig, load_beta_config


def test_beta_config_dataclass_fields():
    cfg = BetaConfig(
        tools_token="tools-token",
        tools_expected_id=10001,
        puppet_tokens=("p1", "p2", "p3"),
        puppet_expected_ids=(20001, 20002, 20003),
        enabled=True,
        ambient_rate_multiplier=1.0,
        ambient_autostart=True,
        llm_blend=False,
    )
    assert cfg.tools_token == "tools-token"
    assert cfg.puppet_tokens == ("p1", "p2", "p3")
    assert cfg.enabled is True


def test_load_beta_config_reads_env(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN_TOOLS", "tools-token")
    monkeypatch.setenv("EXPECTED_BOT_ID_TOOLS", "10001")
    monkeypatch.setenv("BETA_TOOLS_ENABLED", "1")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_1", "p1")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_2", "p2")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_3", "p3")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_1", "20001")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_2", "20002")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_3", "20003")
    monkeypatch.setenv("BETA_AMBIENT_RATE_MULTIPLIER", "1.5")
    monkeypatch.setenv("BETA_AMBIENT_AUTOSTART", "0")
    monkeypatch.setenv("BETA_LLM_BLEND", "1")
    cfg = load_beta_config()
    assert cfg.tools_token == "tools-token"
    assert cfg.tools_expected_id == 10001
    assert cfg.puppet_tokens == ("p1", "p2", "p3")
    assert cfg.puppet_expected_ids == (20001, 20002, 20003)
    assert cfg.enabled is True
    assert cfg.ambient_rate_multiplier == 1.5
    assert cfg.ambient_autostart is False
    assert cfg.llm_blend is True


def test_load_beta_config_missing_token_raises(monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN_TOOLS", raising=False)
    with pytest.raises(KeyError):
        load_beta_config()


def test_load_beta_config_defaults_when_optional_missing(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN_TOOLS", "tools-token")
    monkeypatch.setenv("EXPECTED_BOT_ID_TOOLS", "10001")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_1", "p1")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_2", "p2")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_3", "p3")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_1", "20001")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_2", "20002")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_3", "20003")
    monkeypatch.delenv("BETA_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("BETA_AMBIENT_RATE_MULTIPLIER", raising=False)
    monkeypatch.delenv("BETA_AMBIENT_AUTOSTART", raising=False)
    monkeypatch.delenv("BETA_LLM_BLEND", raising=False)
    cfg = load_beta_config()
    assert cfg.enabled is False  # default
    assert cfg.ambient_rate_multiplier == 1.0  # default
    assert cfg.ambient_autostart is True  # default
    assert cfg.llm_blend is False  # default
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/beta/test_beta_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'beta_tools'`.

- [ ] **Step 3: Create the package marker**

Create `beta_tools/__init__.py`:

```python
"""DK Tools — beta sidecar bot package.

Runs only in dev (BOT_ENV=dev, BETA_TOOLS_ENABLED=1) against the test guild.
Manages 3 puppet bot accounts + a webhook fleet to drive synthetic activity
in the beta server. See docs/superpowers/specs/2026-04-30-beta-tools-sidecar-design.md.
"""
```

- [ ] **Step 4: Implement `BetaConfig` and `load_beta_config()`**

Create `beta_tools/config.py`:

```python
"""Sidecar configuration for the beta tools bot.

Parallel to config.Config (which configures the main Dungeon Keeper bot).
Reads DISCORD_TOKEN_TOOLS, BETA_PUPPET_TOKEN_1..3, and the BETA_* knobs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(override=True)


@dataclass(frozen=True)
class BetaConfig:
    tools_token: str
    tools_expected_id: int
    puppet_tokens: tuple[str, str, str]
    puppet_expected_ids: tuple[int, int, int]
    enabled: bool
    ambient_rate_multiplier: float
    ambient_autostart: bool
    llm_blend: bool


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip() == "1"


def load_beta_config() -> BetaConfig:
    return BetaConfig(
        tools_token=os.environ["DISCORD_TOKEN_TOOLS"],
        tools_expected_id=int(os.environ["EXPECTED_BOT_ID_TOOLS"]),
        puppet_tokens=(
            os.environ["BETA_PUPPET_TOKEN_1"],
            os.environ["BETA_PUPPET_TOKEN_2"],
            os.environ["BETA_PUPPET_TOKEN_3"],
        ),
        puppet_expected_ids=(
            int(os.environ["EXPECTED_BOT_ID_PUPPET_1"]),
            int(os.environ["EXPECTED_BOT_ID_PUPPET_2"]),
            int(os.environ["EXPECTED_BOT_ID_PUPPET_3"]),
        ),
        enabled=_bool_env("BETA_TOOLS_ENABLED", default=False),
        ambient_rate_multiplier=float(os.getenv("BETA_AMBIENT_RATE_MULTIPLIER", "1.0")),
        ambient_autostart=_bool_env("BETA_AMBIENT_AUTOSTART", default=True),
        llm_blend=_bool_env("BETA_LLM_BLEND", default=False),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/beta/test_beta_config.py -v`
Expected: PASS — all 4 tests green.

- [ ] **Step 6: Commit**

```bash
git add beta_tools/__init__.py beta_tools/config.py tests/beta/test_beta_config.py
git commit -m "feat(beta-tools): BetaConfig dataclass and env loader

Sidecar-only config (DISCORD_TOKEN_TOOLS, BETA_PUPPET_TOKEN_1..3,
BETA_TOOLS_ENABLED, ambient sim knobs). Parallel to config.Config so
the production bot's surface stays clean.
"
```

---

## Task 3: Implement `beta_tools.safety.assert_safe_to_start()`

**Files:**
- Create: `beta_tools/safety.py`
- Test: `tests/beta/test_beta_safety.py`

Layer 1 of the safety rails: process refuses to start outside dev.

- [ ] **Step 1: Write the failing test**

Create `tests/beta/test_beta_safety.py`:

```python
"""Tests for beta_tools.safety — all five safety-rail layers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from beta_tools.safety import assert_safe_to_start


def _set_minimal_env(monkeypatch, *, env="dev", db_path="dk_dev.db"):
    monkeypatch.setenv("BOT_ENV", env)
    monkeypatch.setenv("DISCORD_TOKEN_DEV", "main-dev-token")
    monkeypatch.setenv("GUILD_ID_DEV", "9001")
    monkeypatch.setenv("DB_PATH_DEV", db_path)
    monkeypatch.setenv("AUDIT_CHANNEL_DEV", "9999")
    monkeypatch.setenv("DISCORD_TOKEN_TOOLS", "tools-token")
    monkeypatch.setenv("EXPECTED_BOT_ID_TOOLS", "10001")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_1", "p1")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_2", "p2")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_3", "p3")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_1", "20001")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_2", "20002")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_3", "20003")
    monkeypatch.setenv("BETA_TOOLS_ENABLED", "1")


def test_assert_safe_to_start_passes_in_dev(monkeypatch):
    _set_minimal_env(monkeypatch)
    # should not raise
    assert_safe_to_start()


def test_assert_safe_to_start_exits_in_prod(monkeypatch):
    _set_minimal_env(monkeypatch, env="prod", db_path="dungeonkeeper.db")
    monkeypatch.setenv("DISCORD_TOKEN_PROD", "prod-token")
    monkeypatch.setenv("GUILD_ID_PROD", "1")
    monkeypatch.setenv("DB_PATH_PROD", "dungeonkeeper.db")
    monkeypatch.setenv("AUDIT_CHANNEL_PROD", "0")
    with pytest.raises(SystemExit):
        assert_safe_to_start()


def test_assert_safe_to_start_exits_when_tools_disabled(monkeypatch):
    _set_minimal_env(monkeypatch)
    monkeypatch.setenv("BETA_TOOLS_ENABLED", "0")
    with pytest.raises(SystemExit):
        assert_safe_to_start()


def test_assert_safe_to_start_exits_when_db_path_missing_dev(monkeypatch):
    _set_minimal_env(monkeypatch, db_path="dungeonkeeper.db")
    with pytest.raises(SystemExit):
        assert_safe_to_start()


def test_assert_safe_to_start_exits_when_tools_id_matches_prod_id(monkeypatch):
    _set_minimal_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_BOT_ID_PROD", "10001")  # collide with TOOLS
    with pytest.raises(SystemExit):
        assert_safe_to_start()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/beta/test_beta_safety.py -v`
Expected: FAIL — `ImportError: cannot import name 'assert_safe_to_start' from 'beta_tools.safety'`.

- [ ] **Step 3: Implement `assert_safe_to_start()`**

Create `beta_tools/safety.py`:

```python
"""Safety rails for the beta tools sidecar (spec §7).

Layer 1 here: assert_safe_to_start() runs before any Discord connection.
Layers 2-5 are implemented as functions called from later modules:
  - check_tools_bot_identity() / check_tools_guild_membership() — bot.py
  - check_puppet_identity()    / check_puppet_guild_membership() — puppet_manager.py
  - beta_write()                                                — db_gate.py
"""

from __future__ import annotations

import logging
import os
import sys

import discord

from beta_tools.config import BetaConfig, load_beta_config
from config import load_config

log = logging.getLogger("beta_tools.safety")


def assert_safe_to_start() -> BetaConfig:
    """Layer 1: refuse to start outside dev. Returns the loaded BetaConfig."""
    main_cfg = load_config()
    if not main_cfg.is_dev:
        log.critical("CRITICAL: beta_tools refuses to start outside dev env (BOT_ENV=%r)", main_cfg.env)
        sys.exit(1)

    if "dev" not in main_cfg.db_path.lower():
        log.critical("CRITICAL: db_path=%r does not contain 'dev'", main_cfg.db_path)
        sys.exit(1)

    if os.getenv("BETA_TOOLS_ENABLED") != "1":
        log.critical("CRITICAL: BETA_TOOLS_ENABLED must be '1' to launch beta tools")
        sys.exit(1)

    beta_cfg = load_beta_config()

    prod_id_raw = os.getenv("EXPECTED_BOT_ID_PROD")
    if prod_id_raw and prod_id_raw.strip():
        try:
            prod_id = int(prod_id_raw)
        except ValueError:
            prod_id = -1
        if beta_cfg.tools_expected_id == prod_id:
            log.critical(
                "CRITICAL: tools bot id (%d) matches prod bot id — config error",
                beta_cfg.tools_expected_id,
            )
            sys.exit(1)

    log.info(
        "beta_tools safety: env=dev db=%s tools_id=%d puppets=%d — start permitted",
        main_cfg.db_path,
        beta_cfg.tools_expected_id,
        len(beta_cfg.puppet_tokens),
    )
    return beta_cfg


def check_tools_bot_identity(bot_user: discord.ClientUser, expected_id: int) -> None:
    """Layer 2 (a): assert connected bot user matches EXPECTED_BOT_ID_TOOLS."""
    if bot_user.id != expected_id:
        log.critical(
            "CRITICAL: connected as bot id %d (%s) but expected %d. Refusing to continue.",
            bot_user.id, bot_user, expected_id,
        )
        sys.exit(1)


async def check_tools_guild_membership(bot: discord.Client, expected_guild_id: int) -> None:
    """Layer 2 (b): leave any guild that isn't the test guild."""
    wrong = [g for g in bot.guilds if g.id != expected_guild_id]
    for g in wrong:
        log.critical(
            "CRITICAL: tools bot is in unexpected guild %d (%r) — leaving immediately.",
            g.id, g.name,
        )
        try:
            await g.leave()
        except Exception:  # noqa: BLE001 — best-effort; we're shutting down anyway
            log.exception("failed to leave guild %d", g.id)
    if wrong:
        sys.exit(1)
    if not any(g.id == expected_guild_id for g in bot.guilds):
        log.critical("CRITICAL: tools bot is not in the configured test guild %d", expected_guild_id)
        sys.exit(1)


def check_puppet_identity(puppet_user: discord.ClientUser, expected_id: int, key: str) -> None:
    """Layer 3 (a): assert each puppet's bot user matches its expected ID."""
    if puppet_user.id != expected_id:
        log.critical(
            "CRITICAL: puppet %r connected as id %d but expected %d. Refusing to continue.",
            key, puppet_user.id, expected_id,
        )
        sys.exit(1)


async def check_puppet_guild_membership(puppet: discord.Client, expected_guild_id: int, key: str) -> None:
    """Layer 3 (b): puppet leaves any guild that isn't the test guild."""
    wrong = [g for g in puppet.guilds if g.id != expected_guild_id]
    for g in wrong:
        log.critical("CRITICAL: puppet %r in unexpected guild %d (%r) — leaving.", key, g.id, g.name)
        try:
            await g.leave()
        except Exception:  # noqa: BLE001
            log.exception("puppet %r failed to leave guild %d", key, g.id)
    if wrong:
        sys.exit(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/beta/test_beta_safety.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add beta_tools/safety.py tests/beta/test_beta_safety.py
git commit -m "feat(beta-tools): safety rails layers 1-3

assert_safe_to_start enforces BOT_ENV=dev, BETA_TOOLS_ENABLED=1,
db path contains 'dev', and tools bot id != prod bot id.
check_tools_bot_identity / check_tools_guild_membership /
check_puppet_identity / check_puppet_guild_membership are exposed
for use by the bot and puppet startup paths.
"
```

---

## Task 4: Implement `beta_tools.db_gate.beta_write()`

**Files:**
- Create: `beta_tools/db_gate.py`
- Test: `tests/beta/test_beta_db_gate.py`

Layer 4 of the safety rails: every DB write from the sidecar goes through this gate, which refuses to execute outside dev. Plan 1 doesn't write to the DB, but the gate exists and is exercised by tests so Plan 3 can rely on it.

- [ ] **Step 1: Write the failing test**

Create `tests/beta/test_beta_db_gate.py`:

```python
"""Tests for beta_tools.db_gate."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from config import Config


@pytest.fixture
def dev_cfg(tmp_path):
    return Config(
        env="dev", token="t", guild_id=9001, db_path=str(tmp_path / "dk_dev.db"),
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )


@pytest.fixture
def prod_cfg():
    return Config(
        env="prod", token="t", guild_id=1, db_path="dungeonkeeper.db",
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )


async def test_beta_write_executes_in_dev(dev_cfg):
    from beta_tools.db_gate import beta_write
    db = AsyncMock()
    await beta_write(db, "INSERT INTO foo VALUES (?)", (1,), cfg=dev_cfg)
    db.execute.assert_called_once_with("INSERT INTO foo VALUES (?)", (1,))


async def test_beta_write_refuses_in_prod(prod_cfg):
    from beta_tools.db_gate import beta_write
    db = AsyncMock()
    with pytest.raises(RuntimeError, match="non-dev environment"):
        await beta_write(db, "INSERT INTO foo VALUES (?)", (1,), cfg=prod_cfg)
    db.execute.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/beta/test_beta_db_gate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'beta_tools.db_gate'`.

- [ ] **Step 3: Implement `beta_write()`**

Create `beta_tools/db_gate.py`:

```python
"""Layer 4 of the beta tools safety rails: every sidecar DB write goes through this gate.

Refuses to execute in any non-dev environment. Plan 1 doesn't write to the DB,
but later plans depend on this wrapper being available and tested.
"""

from __future__ import annotations

import logging

from config import Config

log = logging.getLogger("beta_tools.db_gate")


async def beta_write(db, query: str, params: tuple = (), *, cfg: Config) -> None:
    """Execute a write against db, only if cfg.is_dev. Raises RuntimeError otherwise."""
    if not cfg.is_dev:
        raise RuntimeError(
            f"beta_tools write attempted in non-dev environment (env={cfg.env!r}). "
            "This indicates a config error or accidental import in a prod path."
        )
    await db.execute(query, params)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/beta/test_beta_db_gate.py -v`
Expected: PASS — both tests green.

- [ ] **Step 5: Commit**

```bash
git add beta_tools/db_gate.py tests/beta/test_beta_db_gate.py
git commit -m "feat(beta-tools): beta_write DB gate (safety rail layer 4)

Wrapper that asserts cfg.is_dev before executing any sidecar write.
Plan 1 doesn't write to the DB but the gate exists so Plan 3+ can
rely on it.
"
```

---

## Task 5: Implement persona YAML loader

**Files:**
- Create: `fixtures/beta_puppets.yaml`
- Create: `beta_tools/personas.py`
- Test: `tests/beta/test_beta_personas.py`

- [ ] **Step 1: Write the failing test**

Create `tests/beta/test_beta_personas.py`:

```python
"""Tests for beta_tools.personas."""

from __future__ import annotations

from pathlib import Path

import pytest

from beta_tools.personas import Persona, load_puppet_personas


def test_load_puppet_personas_from_file(tmp_path):
    yaml_text = """\
- key: alice
  display_name: Alice
  avatar_url: https://example.com/alice.png
  activity_weight: 1.0
  channel_affinities:
    general: 0.5
    photos: 0.5
  voice_likely: true
  message_length_bias: short

- key: bob
  display_name: Bob the Builder
  avatar_url: https://example.com/bob.png
  activity_weight: 1.5
  channel_affinities:
    drama: 1.0
  voice_likely: false
  message_length_bias: medium
"""
    p = tmp_path / "puppets.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    personas = load_puppet_personas(p)
    assert len(personas) == 2
    assert personas[0].key == "alice"
    assert personas[0].display_name == "Alice"
    assert personas[0].activity_weight == 1.0
    assert personas[0].channel_affinities == {"general": 0.5, "photos": 0.5}
    assert personas[0].voice_likely is True
    assert personas[0].message_length_bias == "short"

    assert personas[1].key == "bob"
    assert personas[1].activity_weight == 1.5
    assert personas[1].voice_likely is False


def test_load_puppet_personas_rejects_bad_length_bias(tmp_path):
    yaml_text = """\
- key: alice
  display_name: Alice
  avatar_url: https://example.com/a.png
  activity_weight: 1.0
  channel_affinities: {general: 1.0}
  voice_likely: true
  message_length_bias: enormous
"""
    p = tmp_path / "puppets.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="message_length_bias"):
        load_puppet_personas(p)


def test_load_puppet_personas_rejects_duplicate_keys(tmp_path):
    yaml_text = """\
- key: alice
  display_name: Alice One
  avatar_url: https://example.com/a.png
  activity_weight: 1.0
  channel_affinities: {general: 1.0}
  voice_likely: true
  message_length_bias: short
- key: alice
  display_name: Alice Two
  avatar_url: https://example.com/a2.png
  activity_weight: 1.0
  channel_affinities: {general: 1.0}
  voice_likely: true
  message_length_bias: short
"""
    p = tmp_path / "puppets.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_puppet_personas(p)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/beta/test_beta_personas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'beta_tools.personas'`.

- [ ] **Step 3: Implement `Persona` and `load_puppet_personas()`**

Create `beta_tools/personas.py`:

```python
"""Puppet persona configs.

Loaded from fixtures/beta_puppets.yaml at sidecar startup. Each persona maps
to one of the three puppet bot accounts (BETA_PUPPET_TOKEN_1..3 in env order).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_VALID_LENGTH_BIAS = {"short", "medium", "long"}


@dataclass(frozen=True)
class Persona:
    key: str
    display_name: str
    avatar_url: str
    activity_weight: float
    channel_affinities: dict[str, float]
    voice_likely: bool
    message_length_bias: str  # "short" | "medium" | "long"


def load_puppet_personas(path: str | Path) -> list[Persona]:
    """Load and validate the puppet persona list from a YAML file."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"expected a YAML list at top level, got {type(raw).__name__}")

    personas: list[Persona] = []
    seen_keys: set[str] = set()

    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"persona #{i} is not a mapping: {entry!r}")
        bias = entry.get("message_length_bias", "medium")
        if bias not in _VALID_LENGTH_BIAS:
            raise ValueError(
                f"persona #{i} has invalid message_length_bias={bias!r}; "
                f"must be one of {sorted(_VALID_LENGTH_BIAS)}"
            )
        key = entry["key"]
        if key in seen_keys:
            raise ValueError(f"duplicate persona key {key!r}")
        seen_keys.add(key)
        personas.append(Persona(
            key=key,
            display_name=entry["display_name"],
            avatar_url=entry["avatar_url"],
            activity_weight=float(entry["activity_weight"]),
            channel_affinities={str(k): float(v) for k, v in entry["channel_affinities"].items()},
            voice_likely=bool(entry["voice_likely"]),
            message_length_bias=bias,
        ))

    return personas
```

- [ ] **Step 4: Create the puppet fixtures file**

Create `fixtures/beta_puppets.yaml`:

```yaml
# Beta tools puppet roster (3 personas, one per BETA_PUPPET_TOKEN_N).
# Order matters: index 0 → BETA_PUPPET_TOKEN_1, etc.
# Replace avatar_url with real URLs after registering the puppet apps in
# the Discord Developer Portal.

- key: alice
  display_name: Alice
  avatar_url: https://i.imgur.com/REPLACE_ME_alice.png
  activity_weight: 1.0
  channel_affinities:
    general: 0.5
    photos: 0.2
    drama: 0.1
    random: 0.2
  voice_likely: true
  message_length_bias: short

- key: bob
  display_name: Bob the Builder
  avatar_url: https://i.imgur.com/REPLACE_ME_bob.png
  activity_weight: 1.5
  channel_affinities:
    general: 0.3
    drama: 0.5
    random: 0.2
  voice_likely: false
  message_length_bias: medium

- key: clara
  display_name: Clara
  avatar_url: https://i.imgur.com/REPLACE_ME_clara.png
  activity_weight: 0.8
  channel_affinities:
    general: 0.4
    photos: 0.4
    random: 0.2
  voice_likely: true
  message_length_bias: long
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/beta/test_beta_personas.py -v`
Expected: PASS — all 3 tests green.

- [ ] **Step 6: Verify the real fixture loads cleanly**

Run: `python -c "from beta_tools.personas import load_puppet_personas; print(load_puppet_personas('fixtures/beta_puppets.yaml'))"`
Expected: a list of 3 Persona objects printed.

- [ ] **Step 7: Commit**

```bash
git add beta_tools/personas.py fixtures/beta_puppets.yaml tests/beta/test_beta_personas.py
git commit -m "feat(beta-tools): puppet persona dataclass + YAML loader

Validates length bias and rejects duplicate keys at load time.
Initial fixture has 3 personas (alice, bob, clara) with placeholder
avatar URLs to be replaced after registering puppet Discord apps.
"
```

---

## Task 6: Implement `PuppetManager` (construction + persona application logic)

**Files:**
- Create: `beta_tools/puppet_manager.py`
- Test: `tests/beta/test_beta_puppet_manager.py`

This task implements the testable parts of `PuppetManager` — construction and the persona-diff logic that decides whether to call Discord's edit-profile API. The actual gateway connection is exercised in Task 7 with mocked `discord.Client`.

- [ ] **Step 1: Write the failing test**

Create `tests/beta/test_beta_puppet_manager.py`:

```python
"""Tests for beta_tools.puppet_manager construction + persona diff logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from beta_tools.personas import Persona


@pytest.fixture
def three_personas():
    return [
        Persona(
            key="alice", display_name="Alice", avatar_url="https://x/a.png",
            activity_weight=1.0, channel_affinities={"general": 1.0},
            voice_likely=True, message_length_bias="short",
        ),
        Persona(
            key="bob", display_name="Bob", avatar_url="https://x/b.png",
            activity_weight=1.0, channel_affinities={"general": 1.0},
            voice_likely=False, message_length_bias="medium",
        ),
        Persona(
            key="clara", display_name="Clara", avatar_url="https://x/c.png",
            activity_weight=1.0, channel_affinities={"general": 1.0},
            voice_likely=True, message_length_bias="long",
        ),
    ]


def test_puppet_manager_construction(three_personas):
    from beta_tools.puppet_manager import PuppetManager
    tokens = ("t1", "t2", "t3")
    expected_ids = (1, 2, 3)
    pm = PuppetManager(personas=three_personas, tokens=tokens, expected_ids=expected_ids, expected_guild_id=9001)
    assert len(pm.handles) == 3
    assert pm.handles[0].key == "alice"
    assert pm.handles[1].key == "bob"
    assert pm.handles[2].key == "clara"


def test_puppet_manager_rejects_count_mismatch(three_personas):
    from beta_tools.puppet_manager import PuppetManager
    with pytest.raises(ValueError, match="3 personas"):
        PuppetManager(
            personas=three_personas[:2],   # only 2
            tokens=("t1", "t2", "t3"),
            expected_ids=(1, 2, 3),
            expected_guild_id=9001,
        )


def test_puppet_manager_get_handle_by_key(three_personas):
    from beta_tools.puppet_manager import PuppetManager
    pm = PuppetManager(
        personas=three_personas,
        tokens=("t1", "t2", "t3"),
        expected_ids=(1, 2, 3),
        expected_guild_id=9001,
    )
    handle = pm.get_handle("bob")
    assert handle.key == "bob"
    with pytest.raises(KeyError):
        pm.get_handle("nobody")


async def test_apply_persona_skips_when_already_correct(three_personas):
    """If the puppet's display name and avatar already match the persona, don't call edit."""
    from beta_tools.puppet_manager import PuppetManager, _apply_persona

    persona = three_personas[0]  # alice, https://x/a.png

    # Build a fake user whose display_name already matches.
    fake_user = MagicMock()
    fake_user.name = "Alice"
    fake_user.display_avatar.url = "https://x/a.png"
    fake_user.edit = AsyncMock()

    fake_client = MagicMock()
    fake_client.user = fake_user

    await _apply_persona(fake_client, persona)
    fake_user.edit.assert_not_called()


async def test_apply_persona_renames_when_name_differs(three_personas):
    from beta_tools.puppet_manager import _apply_persona

    persona = three_personas[0]  # alice
    fake_user = MagicMock()
    fake_user.name = "OldName"
    fake_user.display_avatar.url = "https://x/a.png"
    fake_user.edit = AsyncMock()

    fake_client = MagicMock()
    fake_client.user = fake_user

    await _apply_persona(fake_client, persona)
    # Should call edit with username="Alice"
    fake_user.edit.assert_awaited_once()
    _, kwargs = fake_user.edit.call_args
    assert kwargs["username"] == "Alice"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/beta/test_beta_puppet_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'beta_tools.puppet_manager'`.

- [ ] **Step 3: Implement `PuppetManager` and `_apply_persona()`**

Create `beta_tools/puppet_manager.py`:

```python
"""PuppetManager — owns the 3 puppet discord.Client instances.

Construction is cheap (no I/O). start_all() actually connects to the gateway.
Persona diff logic (_apply_persona) is a free function for unit testability.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import discord

from beta_tools.personas import Persona
from beta_tools.safety import check_puppet_guild_membership, check_puppet_identity

log = logging.getLogger("beta_tools.puppet_manager")


@dataclass
class PuppetHandle:
    key: str
    persona: Persona
    token: str
    expected_id: int
    client: Optional[discord.Client] = None
    task: Optional[asyncio.Task] = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class PuppetManager:
    def __init__(
        self,
        *,
        personas: list[Persona],
        tokens: tuple[str, str, str],
        expected_ids: tuple[int, int, int],
        expected_guild_id: int,
    ) -> None:
        if len(personas) != 3 or len(tokens) != 3 or len(expected_ids) != 3:
            raise ValueError(
                f"PuppetManager requires exactly 3 personas/tokens/ids; "
                f"got {len(personas)} personas, {len(tokens)} tokens, {len(expected_ids)} ids"
            )
        self.expected_guild_id = expected_guild_id
        self.handles: list[PuppetHandle] = [
            PuppetHandle(key=p.key, persona=p, token=tokens[i], expected_id=expected_ids[i])
            for i, p in enumerate(personas)
        ]

    def get_handle(self, key: str) -> PuppetHandle:
        for h in self.handles:
            if h.key == key:
                return h
        raise KeyError(f"no puppet with key {key!r}")

    async def start_all(self) -> None:
        """Connect all 3 puppets to the gateway and wait until all are ready."""
        for h in self.handles:
            client = _new_puppet_client(h, self.expected_guild_id)
            h.client = client
            h.task = asyncio.create_task(client.start(h.token), name=f"puppet-{h.key}")
        # Wait for all to be ready (or for any to error out).
        await asyncio.gather(*(h.ready.wait() for h in self.handles))

    async def apply_personas(self) -> None:
        """Idempotent: apply persona display_name + avatar to each connected puppet."""
        for h in self.handles:
            if h.client is None or h.client.user is None:
                log.warning("puppet %r is not connected; skipping persona apply", h.key)
                continue
            await _apply_persona(h.client, h.persona)

    async def close_all(self) -> None:
        for h in self.handles:
            if h.client is not None:
                try:
                    await h.client.close()
                except Exception:  # noqa: BLE001
                    log.exception("error closing puppet %r", h.key)


def _new_puppet_client(handle: PuppetHandle, expected_guild_id: int) -> discord.Client:
    """Build a fresh discord.Client wired with on_ready safety checks."""
    intents = discord.Intents.default()
    intents.message_content = False  # puppets don't need to read others' messages
    intents.members = True            # for guild membership and member edit
    intents.voice_states = True       # so puppets can join voice in later plans
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        log.info("puppet %r connected as %s (id=%d)", handle.key, client.user, client.user.id)
        check_puppet_identity(client.user, handle.expected_id, handle.key)
        await check_puppet_guild_membership(client, expected_guild_id, handle.key)
        handle.ready.set()

    @client.event
    async def on_guild_join(guild: discord.Guild) -> None:
        if guild.id != expected_guild_id:
            log.critical(
                "CRITICAL: puppet %r joined unexpected guild %d (%r) — leaving.",
                handle.key, guild.id, guild.name,
            )
            try:
                await guild.leave()
            except Exception:  # noqa: BLE001
                log.exception("puppet %r failed to leave guild %d", handle.key, guild.id)

    return client


async def _apply_persona(client: discord.Client, persona: Persona) -> None:
    """Idempotent: only call user.edit() if name or avatar differs from the persona."""
    user = client.user
    if user is None:
        log.warning("client.user is None; cannot apply persona %r", persona.key)
        return

    desired_name = persona.display_name
    current_name = user.name
    current_avatar_url = user.display_avatar.url

    needs_name_update = current_name != desired_name
    needs_avatar_update = current_avatar_url != persona.avatar_url

    if not needs_name_update and not needs_avatar_update:
        log.info("puppet %r persona already applied; skipping", persona.key)
        return

    edit_kwargs: dict = {}
    if needs_name_update:
        edit_kwargs["username"] = desired_name
    if needs_avatar_update:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(persona.avatar_url) as resp:
                    resp.raise_for_status()
                    edit_kwargs["avatar"] = await resp.read()
        except Exception:  # noqa: BLE001
            log.exception("failed to fetch avatar for persona %r", persona.key)
            # Proceed with name update only
            edit_kwargs.pop("avatar", None)

    log.info("applying persona %r: name_update=%s avatar_update=%s",
             persona.key, needs_name_update, "avatar" in edit_kwargs)
    await user.edit(**edit_kwargs)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/beta/test_beta_puppet_manager.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add beta_tools/puppet_manager.py tests/beta/test_beta_puppet_manager.py
git commit -m "feat(beta-tools): PuppetManager with idempotent persona application

Construction validates count parity (exactly 3 personas/tokens/ids).
_apply_persona only calls user.edit when name or avatar actually
differs, so re-running on the same puppet is a no-op. Connection
logic (start_all) wires on_ready safety checks and leave-guild-on-join.
"
```

---

## Task 7: Implement `WebhookFleet`

**Files:**
- Create: `beta_tools/webhook_fleet.py`
- Test: `tests/beta/test_beta_webhook_fleet.py`

`WebhookFleet` provisions one webhook per channel that wants ghost chatter, idempotently. The webhook is named `dk-tools-ghost`, so `ensure()` finds an existing one before creating.

- [ ] **Step 1: Write the failing test**

Create `tests/beta/test_beta_webhook_fleet.py`:

```python
"""Tests for beta_tools.webhook_fleet."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

WEBHOOK_NAME = "dk-tools-ghost"


def _fake_text_channel(channel_id: int = 4001, existing_webhooks: list | None = None):
    """Build a fake TextChannel with the relevant async API surface."""
    ch = MagicMock()
    ch.id = channel_id
    ch.webhooks = AsyncMock(return_value=existing_webhooks or [])
    ch.create_webhook = AsyncMock()
    return ch


def _fake_webhook(name: str = WEBHOOK_NAME, webhook_id: int = 5001):
    wh = MagicMock()
    wh.name = name
    wh.id = webhook_id
    wh.send = AsyncMock()
    return wh


async def test_ensure_returns_existing_webhook_if_present():
    from beta_tools.webhook_fleet import WebhookFleet

    existing = _fake_webhook()
    channel = _fake_text_channel(existing_webhooks=[existing])
    fleet = WebhookFleet()

    wh = await fleet.ensure(channel)
    assert wh is existing
    channel.create_webhook.assert_not_called()


async def test_ensure_creates_webhook_when_missing():
    from beta_tools.webhook_fleet import WebhookFleet

    new_wh = _fake_webhook()
    channel = _fake_text_channel(existing_webhooks=[])
    channel.create_webhook.return_value = new_wh
    fleet = WebhookFleet()

    wh = await fleet.ensure(channel)
    assert wh is new_wh
    channel.create_webhook.assert_awaited_once_with(name=WEBHOOK_NAME, reason="dk_tools beta sim")


async def test_ensure_caches_per_channel_id():
    from beta_tools.webhook_fleet import WebhookFleet

    existing = _fake_webhook()
    channel = _fake_text_channel(existing_webhooks=[existing])
    fleet = WebhookFleet()

    await fleet.ensure(channel)
    await fleet.ensure(channel)  # second call should hit cache, not re-list
    channel.webhooks.assert_awaited_once()


async def test_send_uses_username_and_avatar():
    from beta_tools.webhook_fleet import WebhookFleet

    existing = _fake_webhook()
    channel = _fake_text_channel(existing_webhooks=[existing])
    fleet = WebhookFleet()

    await fleet.send(channel, content="hello", username="GhostName", avatar_url="https://x/y.png")

    existing.send.assert_awaited_once_with(
        content="hello", username="GhostName", avatar_url="https://x/y.png", wait=False,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/beta/test_beta_webhook_fleet.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'beta_tools.webhook_fleet'`.

- [ ] **Step 3: Implement `WebhookFleet`**

Create `beta_tools/webhook_fleet.py`:

```python
"""WebhookFleet — per-channel webhooks for ghost message dispatch.

Creates and reuses a single webhook named 'dk-tools-ghost' per channel.
Idempotent: ensure() finds an existing webhook before creating a new one.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

log = logging.getLogger("beta_tools.webhook_fleet")

WEBHOOK_NAME = "dk-tools-ghost"


class WebhookFleet:
    def __init__(self) -> None:
        # Cache mapping channel_id → Webhook so we don't re-list webhooks every send.
        self._cache: dict[int, discord.Webhook] = {}

    async def ensure(self, channel: discord.TextChannel) -> discord.Webhook:
        """Return the dk-tools-ghost webhook for the channel, creating if missing. Cached."""
        if channel.id in self._cache:
            return self._cache[channel.id]

        existing = await channel.webhooks()
        for wh in existing:
            if wh.name == WEBHOOK_NAME:
                log.info("reusing existing webhook in channel %d (%s)", channel.id, getattr(channel, "name", "?"))
                self._cache[channel.id] = wh
                return wh

        log.info("creating webhook in channel %d (%s)", channel.id, getattr(channel, "name", "?"))
        wh = await channel.create_webhook(name=WEBHOOK_NAME, reason="dk_tools beta sim")
        self._cache[channel.id] = wh
        return wh

    async def send(
        self,
        channel: discord.TextChannel,
        *,
        content: str,
        username: str,
        avatar_url: str,
    ) -> None:
        """Send a message via the channel's ghost webhook with custom name + avatar."""
        wh = await self.ensure(channel)
        await wh.send(content=content, username=username, avatar_url=avatar_url, wait=False)

    def invalidate(self, channel_id: int) -> None:
        """Drop a cached webhook (e.g. after a manual delete in Discord)."""
        self._cache.pop(channel_id, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/beta/test_beta_webhook_fleet.py -v`
Expected: PASS — all 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add beta_tools/webhook_fleet.py tests/beta/test_beta_webhook_fleet.py
git commit -m "feat(beta-tools): WebhookFleet with idempotent per-channel webhooks

Reuses existing 'dk-tools-ghost' webhooks before creating new ones,
caches per channel_id so subsequent sends don't re-list. send()
takes username + avatar_url so each ghost persona renders distinctly.
"
```

---

## Task 8: Implement DK Tools `Bot` class

**Files:**
- Create: `beta_tools/bot.py`
- Test: `tests/beta/test_beta_bot.py`

The bot subclasses `commands.Bot` because we need slash commands via `bot.tree`. Adds `on_ready` (identity + guild check), `on_guild_join` (leave non-test), and holds references to the puppet manager and webhook fleet.

- [ ] **Step 1: Write the failing test**

Create `tests/beta/test_beta_bot.py`:

```python
"""Tests for beta_tools.bot.DkToolsBot — on_ready and on_guild_join hooks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_main_cfg(tmp_path):
    from config import Config
    return Config(
        env="dev", token="t", guild_id=9001, db_path=str(tmp_path / "dk_dev.db"),
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )


@pytest.fixture
def mock_beta_cfg():
    from beta_tools.config import BetaConfig
    return BetaConfig(
        tools_token="tt", tools_expected_id=10001,
        puppet_tokens=("p1", "p2", "p3"),
        puppet_expected_ids=(1, 2, 3),
        enabled=True, ambient_rate_multiplier=1.0,
        ambient_autostart=False, llm_blend=False,
    )


def test_dk_tools_bot_construction(mock_main_cfg, mock_beta_cfg):
    from beta_tools.bot import DkToolsBot
    bot = DkToolsBot(main_cfg=mock_main_cfg, beta_cfg=mock_beta_cfg)
    assert bot.main_cfg is mock_main_cfg
    assert bot.beta_cfg is mock_beta_cfg
    assert bot.puppet_manager is None  # set later in setup_hook
    assert bot.webhook_fleet is None


async def test_on_guild_join_leaves_non_test_guild(mock_main_cfg, mock_beta_cfg):
    from beta_tools.bot import DkToolsBot
    bot = DkToolsBot(main_cfg=mock_main_cfg, beta_cfg=mock_beta_cfg)
    wrong_guild = MagicMock()
    wrong_guild.id = 99999  # not 9001
    wrong_guild.name = "WrongGuild"
    wrong_guild.leave = AsyncMock()

    await bot.on_guild_join(wrong_guild)
    wrong_guild.leave.assert_awaited_once()


async def test_on_guild_join_does_not_leave_test_guild(mock_main_cfg, mock_beta_cfg):
    from beta_tools.bot import DkToolsBot
    bot = DkToolsBot(main_cfg=mock_main_cfg, beta_cfg=mock_beta_cfg)
    correct_guild = MagicMock()
    correct_guild.id = 9001
    correct_guild.name = "TestGuild"
    correct_guild.leave = AsyncMock()

    await bot.on_guild_join(correct_guild)
    correct_guild.leave.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/beta/test_beta_bot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'beta_tools.bot'`.

- [ ] **Step 3: Implement `DkToolsBot`**

Create `beta_tools/bot.py`:

```python
"""DkToolsBot — the sidecar bot's commands.Bot subclass.

Owns the puppet manager, webhook fleet, and slash command tree.
Slash commands register in setup_hook() once the bot is logged in.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from beta_tools.config import BetaConfig
from beta_tools.puppet_manager import PuppetManager
from beta_tools.webhook_fleet import WebhookFleet
from config import Config

log = logging.getLogger("beta_tools.bot")


class DkToolsBot(commands.Bot):
    def __init__(self, *, main_cfg: Config, beta_cfg: BetaConfig) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.main_cfg = main_cfg
        self.beta_cfg = beta_cfg
        self.puppet_manager: Optional[PuppetManager] = None
        self.webhook_fleet: Optional[WebhookFleet] = None

    async def setup_hook(self) -> None:
        """Called once after login. Wire up puppets, webhook fleet, slash commands."""
        # Imported here to avoid circular import: slash modules import DkToolsBot for typing.
        from beta_tools.personas import load_puppet_personas
        from beta_tools.slash import register_all
        from beta_tools.safety import check_tools_bot_identity

        # safety layer 2 (identity check) once user is available — done in on_ready
        # because user is None until then.

        personas = load_puppet_personas("fixtures/beta_puppets.yaml")
        self.puppet_manager = PuppetManager(
            personas=personas,
            tokens=self.beta_cfg.puppet_tokens,
            expected_ids=self.beta_cfg.puppet_expected_ids,
            expected_guild_id=self.main_cfg.guild_id,
        )
        self.webhook_fleet = WebhookFleet()

        # Register all /beta slash commands scoped to the test guild.
        register_all(self)
        guild_obj = discord.Object(id=self.main_cfg.guild_id)
        await self.tree.sync(guild=guild_obj)
        log.info("registered /beta commands to guild %d", self.main_cfg.guild_id)

        # Connect puppets and apply personas.
        log.info("starting %d puppets", len(self.puppet_manager.handles))
        await self.puppet_manager.start_all()
        await self.puppet_manager.apply_personas()
        log.info("puppets ready")

    async def on_ready(self) -> None:
        from beta_tools.safety import check_tools_bot_identity, check_tools_guild_membership
        check_tools_bot_identity(self.user, self.beta_cfg.tools_expected_id)
        await check_tools_guild_membership(self, self.main_cfg.guild_id)
        log.info("DK Tools ready: %s (id=%d) in guild %d", self.user, self.user.id, self.main_cfg.guild_id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != self.main_cfg.guild_id:
            log.critical(
                "CRITICAL: DK Tools joined unexpected guild %d (%r) — leaving.",
                guild.id, guild.name,
            )
            try:
                await guild.leave()
            except Exception:  # noqa: BLE001
                log.exception("failed to leave guild %d", guild.id)

    async def close(self) -> None:
        if self.puppet_manager is not None:
            await self.puppet_manager.close_all()
        await super().close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/beta/test_beta_bot.py -v`
Expected: PASS — all 3 tests green.

Note: Tests pass because `on_guild_join` is fully exercised. `setup_hook` and `on_ready` are not tested in isolation here — they're verified by the manual smoke test in Task 11.

- [ ] **Step 5: Commit**

```bash
git add beta_tools/bot.py tests/beta/test_beta_bot.py
git commit -m "feat(beta-tools): DkToolsBot — sidecar commands.Bot

setup_hook loads personas, constructs PuppetManager + WebhookFleet,
registers /beta commands scoped to the test guild, connects puppets,
and applies personas. on_guild_join leaves any guild that isn't the
configured test guild.
"
```

---

## Task 9: Slash command base + `/beta help`

**Files:**
- Create: `beta_tools/slash/__init__.py`
- Create: `beta_tools/slash/_base.py`
- Create: `beta_tools/slash/help.py`
- Test: `tests/beta/test_beta_slash_base.py`

`register_all(bot)` is the package's public entry point — wires every slash module's commands onto `bot.tree`. The mod-or-admin role check decorator is shared via `_base.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/beta/test_beta_slash_base.py`:

```python
"""Tests for beta_tools.slash._base — role-check decorator."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from beta_tools.slash._base import has_mod_or_admin


def _fake_member(role_names: list[str]):
    member = MagicMock()
    member.roles = [MagicMock(name=name) for name in role_names]
    for r, name in zip(member.roles, role_names):
        r.name = name
    return member


def test_has_mod_or_admin_accepts_mod():
    member = _fake_member(["Member", "Mod"])
    assert has_mod_or_admin(member) is True


def test_has_mod_or_admin_accepts_admin():
    member = _fake_member(["Admin"])
    assert has_mod_or_admin(member) is True


def test_has_mod_or_admin_rejects_regular():
    member = _fake_member(["Member"])
    assert has_mod_or_admin(member) is False


def test_has_mod_or_admin_rejects_no_roles():
    member = _fake_member([])
    assert has_mod_or_admin(member) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/beta/test_beta_slash_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'beta_tools.slash'`.

- [ ] **Step 3: Create the slash package**

Create `beta_tools/slash/__init__.py`:

```python
"""/beta slash commands.

register_all(bot) wires every slash module's commands onto bot.tree.
"""

from __future__ import annotations

from beta_tools.slash.help import register as register_help
from beta_tools.slash.puppets import register as register_puppets


def register_all(bot) -> None:
    register_help(bot)
    register_puppets(bot)
```

- [ ] **Step 4: Implement `_base.py`**

Create `beta_tools/slash/_base.py`:

```python
"""Shared utilities for /beta slash command modules."""

from __future__ import annotations

import discord


def has_mod_or_admin(member: discord.Member) -> bool:
    """True if member has any role named 'Mod' or 'Admin'."""
    if not getattr(member, "roles", None):
        return False
    names = {r.name for r in member.roles}
    return bool(names & {"Mod", "Admin"})


async def reject_if_not_mod(interaction: discord.Interaction) -> bool:
    """Send an ephemeral 'mods only' message and return False if not authorized.
    Otherwise return True."""
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None or not has_mod_or_admin(member):
        await interaction.response.send_message(
            "This command is restricted to moderators.", ephemeral=True,
        )
        return False
    return True
```

- [ ] **Step 5: Stub the puppets slash module**

We need `beta_tools.slash.puppets` to import for the `register_all` to work. We'll fully implement it in Task 10. For now, create `beta_tools/slash/puppets.py`:

```python
"""/beta puppets ... — implemented in Task 10."""

from __future__ import annotations


def register(bot) -> None:
    """Stub — full implementation in Task 10."""
    return
```

- [ ] **Step 6: Implement `/beta help`**

Create `beta_tools/slash/help.py`:

```python
"""/beta help — overview embed."""

from __future__ import annotations

import discord
from discord import app_commands

from beta_tools.slash._base import reject_if_not_mod


def register(bot) -> None:
    guild_obj = discord.Object(id=bot.main_cfg.guild_id)

    @bot.tree.command(name="beta-help", description="Show DK Tools beta-mode commands", guild=guild_obj)
    async def beta_help(interaction: discord.Interaction) -> None:
        if not await reject_if_not_mod(interaction):
            return
        embed = discord.Embed(
            title="DK Tools — Beta Tester Commands",
            description="Slash commands available while running against the beta server.",
            color=discord.Color.dark_purple(),
        )
        embed.add_field(
            name="Puppets",
            value=(
                "`/beta-puppets-list` — show roster + connection state\n"
                "`/beta-puppets-reload` — re-read fixtures/beta_puppets.yaml\n"
                "`/beta-puppets-reconnect <key>` — reconnect a single puppet\n"
                "`/beta-puppets-impersonate <key> <channel> <text>` — drive a puppet/ghost manually\n"
            ),
            inline=False,
        )
        embed.set_footer(text="More commands ship in later beta_tools plans.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
```

Slash command names use hyphenated dashes (`beta-puppets-list`) rather than nested groups for the v1 surface; group commands are slightly more code and not necessary for Plan 1's small set. A future plan can refactor into `app_commands.Group` if the surface grows.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/beta/test_beta_slash_base.py -v`
Expected: PASS — all 4 tests green.

- [ ] **Step 8: Commit**

```bash
git add beta_tools/slash/__init__.py beta_tools/slash/_base.py beta_tools/slash/help.py beta_tools/slash/puppets.py tests/beta/test_beta_slash_base.py
git commit -m "feat(beta-tools): /beta slash command package + /beta-help

register_all(bot) wires module-level register() functions onto the
guild-scoped command tree. Mod-or-admin role check shared via
_base.reject_if_not_mod. /beta-help shows the command surface.
puppets module stubbed for now; filled in next task.
"
```

---

## Task 10: Implement `/beta puppets list / reload / reconnect / impersonate`

**Files:**
- Modify: `beta_tools/slash/puppets.py` (replace stub)
- Test: `tests/beta/test_beta_slash_puppets.py`

Four commands. `list` is read-only, `reload` re-reads YAML and applies personas, `reconnect` recycles one puppet's gateway connection, `impersonate` lets a mod manually post a message as either a puppet or a ghost persona.

For ghost impersonation, since we don't have ghost personas defined yet (Plan 2's `fixtures/beta_db_ghosts.yaml`), `impersonate <key>` will only accept puppet keys for now. A separate `impersonate-ghost` would land in Plan 2.

Actually — `/beta-puppets-impersonate` is puppet-only. A separate `/beta-ghosts-impersonate <name> <channel> <text>` takes a freeform display name + avatar URL and uses the webhook fleet, so mods can test the webhook path without waiting for ghost personas. Let's include both.

- [ ] **Step 1: Write the failing test**

Create `tests/beta/test_beta_slash_puppets.py`:

```python
"""Tests for /beta-puppets-* command handler logic.

Discord's app_commands decorators make registration hard to unit-test directly.
We extract the handler functions and test them as plain async callables.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from beta_tools.personas import Persona


@pytest.fixture
def three_personas():
    return [
        Persona(key="alice", display_name="Alice", avatar_url="https://x/a.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=True, message_length_bias="short"),
        Persona(key="bob", display_name="Bob", avatar_url="https://x/b.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=False, message_length_bias="medium"),
        Persona(key="clara", display_name="Clara", avatar_url="https://x/c.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=True, message_length_bias="long"),
    ]


def _mod_interaction():
    """Build a fake discord.Interaction whose user has Mod role."""
    interaction = MagicMock()
    role = MagicMock()
    role.name = "Mod"
    interaction.user = MagicMock()
    interaction.user.roles = [role]
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _regular_interaction():
    interaction = MagicMock()
    role = MagicMock()
    role.name = "Member"
    interaction.user = MagicMock()
    interaction.user.roles = [role]
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


async def test_puppets_list_handler_lists_handles(three_personas):
    from beta_tools.slash.puppets import _puppets_list_handler
    from beta_tools.puppet_manager import PuppetHandle

    bot = MagicMock()
    bot.puppet_manager = MagicMock()
    handle1 = PuppetHandle(key="alice", persona=three_personas[0], token="t1", expected_id=1)
    handle1.client = MagicMock()
    handle1.client.user = MagicMock()
    handle1.client.user.__str__ = lambda self: "Alice#0001"
    handle1.client.user.id = 1
    handle1.ready = MagicMock()
    handle1.ready.is_set = MagicMock(return_value=True)

    handle2 = PuppetHandle(key="bob", persona=three_personas[1], token="t2", expected_id=2)
    handle2.client = None
    handle2.ready = MagicMock()
    handle2.ready.is_set = MagicMock(return_value=False)

    bot.puppet_manager.handles = [handle1, handle2]

    interaction = _mod_interaction()
    await _puppets_list_handler(bot, interaction)

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    msg = args[0] if args else kwargs.get("content", "")
    assert "alice" in msg
    assert "bob" in msg
    assert kwargs.get("ephemeral") is True


async def test_puppets_list_handler_rejects_non_mod(three_personas):
    from beta_tools.slash.puppets import _puppets_list_handler

    bot = MagicMock()
    interaction = _regular_interaction()
    await _puppets_list_handler(bot, interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert "moderator" in args[0].lower() or "moderator" in (kwargs.get("content", "") or "").lower()
    assert kwargs.get("ephemeral") is True


async def test_puppets_impersonate_handler_dispatches_to_puppet(three_personas):
    from beta_tools.slash.puppets import _puppets_impersonate_handler
    from beta_tools.puppet_manager import PuppetHandle

    fake_channel = MagicMock()
    fake_channel.send = AsyncMock()

    handle = PuppetHandle(key="alice", persona=three_personas[0], token="t1", expected_id=1)
    fake_puppet_channel = MagicMock()
    fake_puppet_channel.send = AsyncMock()
    handle.client = MagicMock()
    handle.client.get_channel = MagicMock(return_value=fake_puppet_channel)

    bot = MagicMock()
    bot.puppet_manager = MagicMock()
    bot.puppet_manager.get_handle = MagicMock(return_value=handle)

    interaction = _mod_interaction()
    interaction.response.defer = AsyncMock()

    await _puppets_impersonate_handler(bot, interaction, key="alice", channel=fake_channel, text="hello")

    handle.client.get_channel.assert_called_once_with(fake_channel.id)
    fake_puppet_channel.send.assert_awaited_once_with("hello")
    interaction.followup.send.assert_awaited_once()


async def test_puppets_impersonate_handler_rejects_unknown_key(three_personas):
    from beta_tools.slash.puppets import _puppets_impersonate_handler

    bot = MagicMock()
    bot.puppet_manager = MagicMock()
    bot.puppet_manager.get_handle = MagicMock(side_effect=KeyError("nobody"))

    interaction = _mod_interaction()
    fake_channel = MagicMock()
    await _puppets_impersonate_handler(bot, interaction, key="nobody", channel=fake_channel, text="hello")

    args, kwargs = interaction.response.send_message.call_args
    msg = args[0] if args else kwargs.get("content", "")
    assert "unknown" in msg.lower() or "no puppet" in msg.lower()
    assert kwargs.get("ephemeral") is True


async def test_ghosts_impersonate_handler_uses_webhook_fleet():
    from beta_tools.slash.puppets import _ghosts_impersonate_handler

    bot = MagicMock()
    bot.webhook_fleet = MagicMock()
    bot.webhook_fleet.send = AsyncMock()

    interaction = _mod_interaction()
    interaction.response.defer = AsyncMock()
    fake_channel = MagicMock()

    await _ghosts_impersonate_handler(
        bot, interaction,
        display_name="ghost_test", avatar_url="https://x/g.png",
        channel=fake_channel, text="hi",
    )

    bot.webhook_fleet.send.assert_awaited_once_with(
        fake_channel, content="hi", username="ghost_test", avatar_url="https://x/g.png",
    )
    interaction.followup.send.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/beta/test_beta_slash_puppets.py -v`
Expected: FAIL — `ImportError: cannot import name '_puppets_list_handler' from 'beta_tools.slash.puppets'`.

- [ ] **Step 3: Implement the handlers**

Replace `beta_tools/slash/puppets.py`:

```python
"""/beta-puppets-* and /beta-ghosts-impersonate slash commands."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands

from beta_tools.slash._base import reject_if_not_mod

log = logging.getLogger("beta_tools.slash.puppets")


# ── Handlers (testable as plain async functions) ─────────────────────


async def _puppets_list_handler(bot, interaction: discord.Interaction) -> None:
    if not await reject_if_not_mod(interaction):
        return
    pm = bot.puppet_manager
    if pm is None:
        await interaction.response.send_message("Puppet manager not initialized yet.", ephemeral=True)
        return
    lines = []
    for h in pm.handles:
        if h.client is not None and h.client.user is not None:
            user = h.client.user
            ready = "✅" if (h.ready and h.ready.is_set()) else "⏳"
            lines.append(f"{ready} `{h.key}` → {user} (id={user.id})")
        else:
            lines.append(f"❌ `{h.key}` → not connected")
    msg = "\n".join(["**Puppet roster:**"] + lines) if lines else "No puppets configured."
    await interaction.response.send_message(msg, ephemeral=True)


async def _puppets_reload_handler(bot, interaction: discord.Interaction) -> None:
    if not await reject_if_not_mod(interaction):
        return
    if bot.puppet_manager is None:
        await interaction.response.send_message("Puppet manager not initialized yet.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    from beta_tools.personas import load_puppet_personas
    new_personas = load_puppet_personas("fixtures/beta_puppets.yaml")
    if len(new_personas) != len(bot.puppet_manager.handles):
        await interaction.followup.send(
            f"Reload failed: fixture has {len(new_personas)} personas but {len(bot.puppet_manager.handles)} puppets are connected.",
            ephemeral=True,
        )
        return
    # Update each handle's persona in-place, then re-apply.
    for h, new in zip(bot.puppet_manager.handles, new_personas):
        h.persona = new
    await bot.puppet_manager.apply_personas()
    await interaction.followup.send(f"Reloaded {len(new_personas)} personas.", ephemeral=True)


async def _puppets_reconnect_handler(bot, interaction: discord.Interaction, *, key: str) -> None:
    if not await reject_if_not_mod(interaction):
        return
    if bot.puppet_manager is None:
        await interaction.response.send_message("Puppet manager not initialized yet.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        handle = bot.puppet_manager.get_handle(key)
    except KeyError:
        await interaction.followup.send(f"Unknown puppet key {key!r}.", ephemeral=True)
        return
    log.info("reconnecting puppet %r", key)
    if handle.client is not None:
        try:
            await handle.client.close()
        except Exception:  # noqa: BLE001
            log.exception("error closing puppet %r before reconnect", key)
    # Build a fresh client and start it.
    import asyncio
    from beta_tools.puppet_manager import _new_puppet_client
    handle.ready.clear()
    handle.client = _new_puppet_client(handle, bot.main_cfg.guild_id)
    handle.task = asyncio.create_task(handle.client.start(handle.token), name=f"puppet-{handle.key}")
    await handle.ready.wait()
    await interaction.followup.send(f"Puppet `{key}` reconnected.", ephemeral=True)


async def _puppets_impersonate_handler(
    bot,
    interaction: discord.Interaction,
    *,
    key: str,
    channel: discord.TextChannel,
    text: str,
) -> None:
    if not await reject_if_not_mod(interaction):
        return
    if bot.puppet_manager is None:
        await interaction.response.send_message("Puppet manager not initialized yet.", ephemeral=True)
        return
    try:
        handle = bot.puppet_manager.get_handle(key)
    except KeyError:
        await interaction.response.send_message(f"Unknown puppet key {key!r}.", ephemeral=True)
        return
    if handle.client is None:
        await interaction.response.send_message(f"Puppet `{key}` is not connected.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    puppet_channel = handle.client.get_channel(channel.id)
    if puppet_channel is None:
        await interaction.followup.send(
            f"Puppet `{key}` cannot see channel {channel.mention}.", ephemeral=True,
        )
        return
    await puppet_channel.send(text)
    await interaction.followup.send(
        f"Posted to {channel.mention} as `{key}`.", ephemeral=True,
    )


async def _ghosts_impersonate_handler(
    bot,
    interaction: discord.Interaction,
    *,
    display_name: str,
    avatar_url: str,
    channel: discord.TextChannel,
    text: str,
) -> None:
    if not await reject_if_not_mod(interaction):
        return
    if bot.webhook_fleet is None:
        await interaction.response.send_message("Webhook fleet not initialized yet.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await bot.webhook_fleet.send(
        channel, content=text, username=display_name, avatar_url=avatar_url,
    )
    await interaction.followup.send(
        f"Posted to {channel.mention} as ghost `{display_name}`.", ephemeral=True,
    )


# ── Registration on the slash command tree ───────────────────────────


def register(bot) -> None:
    guild_obj = discord.Object(id=bot.main_cfg.guild_id)

    @bot.tree.command(name="beta-puppets-list", description="Show puppet roster + connection state", guild=guild_obj)
    async def list_cmd(interaction: discord.Interaction) -> None:
        await _puppets_list_handler(bot, interaction)

    @bot.tree.command(name="beta-puppets-reload", description="Re-read fixtures/beta_puppets.yaml and reapply personas", guild=guild_obj)
    async def reload_cmd(interaction: discord.Interaction) -> None:
        await _puppets_reload_handler(bot, interaction)

    @bot.tree.command(name="beta-puppets-reconnect", description="Reconnect a single puppet", guild=guild_obj)
    @app_commands.describe(key="Puppet key (alice, bob, clara)")
    async def reconnect_cmd(interaction: discord.Interaction, key: str) -> None:
        await _puppets_reconnect_handler(bot, interaction, key=key)

    @bot.tree.command(name="beta-puppets-impersonate", description="Post a message as a specific puppet", guild=guild_obj)
    @app_commands.describe(
        key="Puppet key (alice, bob, clara)",
        channel="Target channel",
        text="Message text",
    )
    async def impersonate_cmd(
        interaction: discord.Interaction,
        key: str,
        channel: discord.TextChannel,
        text: str,
    ) -> None:
        await _puppets_impersonate_handler(bot, interaction, key=key, channel=channel, text=text)

    @bot.tree.command(name="beta-ghosts-impersonate", description="Post a message via webhook with a custom name+avatar", guild=guild_obj)
    @app_commands.describe(
        display_name="Display name shown on the message",
        avatar_url="Avatar image URL",
        channel="Target channel",
        text="Message text",
    )
    async def ghost_impersonate_cmd(
        interaction: discord.Interaction,
        display_name: str,
        avatar_url: str,
        channel: discord.TextChannel,
        text: str,
    ) -> None:
        await _ghosts_impersonate_handler(
            bot, interaction,
            display_name=display_name, avatar_url=avatar_url,
            channel=channel, text=text,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/beta/test_beta_slash_puppets.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Run the full new test suite**

Run: `pytest tests/beta/ -v`
Expected: all tests across the new files PASS.

- [ ] **Step 6: Commit**

```bash
git add beta_tools/slash/puppets.py tests/beta/test_beta_slash_puppets.py
git commit -m "feat(beta-tools): /beta-puppets-{list,reload,reconnect,impersonate} + ghost impersonate

list shows roster + connection state, reload reapplies persona names+avatars
from yaml, reconnect recycles one puppet's gateway connection, impersonate
posts as a specific puppet, ghost impersonate uses the webhook fleet for
ad-hoc ghost-name posts.
"
```

---

## Task 11: Wire up `__main__.py` entry point and verify end-to-end

**Files:**
- Create: `beta_tools/__main__.py`
- Modify: `.env.example`
- Modify: `README.md` (add running-the-beta-tools section)

This is the integration point. After this task, `BOT_ENV=dev BETA_TOOLS_ENABLED=1 python -m beta_tools` brings up the sidecar with all 3 puppets connected.

- [ ] **Step 1: Create `__main__.py`**

Create `beta_tools/__main__.py`:

```python
"""Entry point for DK Tools sidecar.

Usage:
    BOT_ENV=dev BETA_TOOLS_ENABLED=1 python -m beta_tools

Refuses to run outside dev. See beta_tools.safety for the full set of guards.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

from beta_tools.bot import DkToolsBot
from beta_tools.safety import assert_safe_to_start
from config import load_config


def _setup_logging() -> None:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    log_path = Path(__file__).parent.parent / "log_beta_tools.txt"
    log_path.write_text("", encoding="utf-8")
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, encoding="utf-8", maxBytes=2_000_000, backupCount=1,
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(stream)
    root.addHandler(file_handler)


async def _main() -> None:
    _setup_logging()
    log = logging.getLogger("beta_tools.main")
    beta_cfg = assert_safe_to_start()  # exits on any safety violation
    main_cfg = load_config()
    log.info("DK Tools starting in dev (guild=%d, db=%s)", main_cfg.guild_id, main_cfg.db_path)
    bot = DkToolsBot(main_cfg=main_cfg, beta_cfg=beta_cfg)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler(*_args) -> None:
        log.info("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows: signal handlers via add_signal_handler not supported. Fall back to default.
            signal.signal(sig, lambda *_a: stop_event.set())

    bot_task = asyncio.create_task(bot.start(beta_cfg.tools_token), name="dk-tools-bot")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")
    done, pending = await asyncio.wait(
        {bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
    )
    log.info("shutting down")
    if not bot.is_closed():
        await bot.close()
    for t in pending:
        t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
```

- [ ] **Step 2: Update `.env.example`**

Append to `.env.example` (create the file if it doesn't exist):

```ini

# ====== Beta tools sidecar (DEV ONLY) ======================
# Sidecar control bot
DISCORD_TOKEN_TOOLS=
EXPECTED_BOT_ID_TOOLS=

# Puppets — 3 separate Discord apps registered in the Developer Portal
BETA_TOOLS_ENABLED=0
BETA_PUPPET_TOKEN_1=
BETA_PUPPET_TOKEN_2=
BETA_PUPPET_TOKEN_3=
EXPECTED_BOT_ID_PUPPET_1=
EXPECTED_BOT_ID_PUPPET_2=
EXPECTED_BOT_ID_PUPPET_3=

# Sim controls (used in later plans)
BETA_AMBIENT_RATE_MULTIPLIER=1.0
BETA_AMBIENT_AUTOSTART=1
BETA_LLM_BLEND=0
```

- [ ] **Step 3: Update README**

Look for the existing "Development" or "Running" section in `README.md`. If one exists, add a subsection. If not, append a new section. Read the file first:

Run: `head -60 README.md`

Then add (adjust placement to match existing style):

```markdown
### Running the beta tools sidecar (dev only)

The sidecar drives synthetic Discord activity in the test guild for moderator
testers to exercise. It refuses to run outside `BOT_ENV=dev`.

1. Register a new Discord application "Dungeon Keeper Tools" plus 3 puppet
   apps ("Puppet Alice", "Puppet Bob", "Puppet Clara") in the Developer
   Portal. Get a bot token + bot user ID for each.
2. Invite all 4 to the test guild with the `bot` scope.
3. Fill in the new env vars in `.env` (see `.env.example`).
4. In one terminal: `BOT_ENV=dev python -m dungeonkeeper`
5. In another terminal: `BOT_ENV=dev python -m beta_tools`

Verify with `/beta-puppets-list` in the test guild — all 3 puppets should
show as connected. Use `/beta-puppets-impersonate alice #general "hello"`
to test that puppet sends are working.

See `docs/superpowers/specs/2026-04-30-beta-tools-sidecar-design.md` for the
full design.
```

- [ ] **Step 4: Run the entire beta_tools test suite once more**

Run: `pytest tests/beta/ -v`
Expected: all tests PASS. 0 failures.

- [ ] **Step 5: Static-import smoke test**

Run: `python -c "from beta_tools import bot, puppet_manager, webhook_fleet, slash, safety, config, db_gate, personas; print('imports ok')"`
Expected: `imports ok` printed without error.

- [ ] **Step 6: Manual smoke test (requires real puppet tokens)**

This step requires registered Discord apps + invited puppets. Skip if not yet provisioned — the test suite still gives confidence in the unit-level correctness.

If puppets are provisioned:

1. `BOT_ENV=dev BETA_TOOLS_ENABLED=1 python -m beta_tools`
2. Watch logs for: "puppet 'alice' connected as Alice#NNNN", repeat for bob/clara
3. Watch logs for: "registered /beta commands to guild ..."
4. In test guild: type `/beta-puppets-list` — expect ephemeral reply with 3 connected puppets
5. In test guild: type `/beta-puppets-impersonate alice #general "hello world"` — expect "hello world" posted as Alice
6. In test guild: type `/beta-ghosts-impersonate Phantom https://i.imgur.com/.../ghost.png #general "boo"` — expect "boo" posted with the ghost name + avatar
7. Ctrl-C — expect clean shutdown

- [ ] **Step 7: Commit**

```bash
git add beta_tools/__main__.py .env.example README.md
git commit -m "feat(beta-tools): __main__ entry point + env.example + README

BOT_ENV=dev BETA_TOOLS_ENABLED=1 python -m beta_tools brings up the
sidecar with all 3 puppets connected and /beta commands registered.
README documents the puppet-app registration ritual.
"
```

---

## Spec Coverage Self-Review

Mapping each Plan-1-relevant spec section to a task above:

| Spec section | Task(s) |
|---|---|
| §2.1 Repo layout (sidecar package) | Tasks 2-11 (each creates a file from the layout) |
| §2.2 Process model | Task 11 (`__main__.py`) |
| §2.3 Env variables | Task 2 (`BetaConfig`), Task 11 (`.env.example`) |
| §2.4 Startup sequence | Task 8 (`setup_hook`), Task 11 (entry point) |
| §3.1 Puppets — persona config | Task 5 |
| §3.1 Puppets — gateway clients + on_ready | Task 6 |
| §3.1 Puppets — display name + avatar idempotent | Task 6 (`_apply_persona`) |
| §3.2 Webhook fleet (plumbing only) | Task 7 |
| §6 `/beta help` skeleton | Task 9 |
| §6 `/beta puppets list/reload/reconnect/impersonate` | Task 10 |
| §6 `/beta ghosts impersonate` (added beyond spec for Plan 1 coverage) | Task 10 |
| §7.1 Layer 1 — refuses outside dev | Task 3 (`assert_safe_to_start`) |
| §7.2 Layer 2 — tools bot leaves non-test guilds | Task 3 + Task 8 |
| §7.3 Layer 3 — puppets validate themselves | Task 3 + Task 6 |
| §7.4 Layer 4 — DB writes gated | Task 4 |
| §7.5 Layer 5 — source tagging schema | Task 1 |

Spec sections explicitly **deferred to later plans:**

- §3.3 DB-only ghosts roster + seeder behavior — Plan 3
- §3.2 Side-writes for webhook ghost messages — Plan 2
- §4 Traffic profile + Markov chains — Plan 2
- §5.1 Ambient sim loop — Plan 3
- §5.2 Scenario library — Plan 4
- §5.3 Seed (`/beta seed run`) — Plan 3
- §6 Remaining `/beta sim ...`, `/beta scenario ...`, `/beta seed ...`, `/beta cleanup`, `/beta health`, `/beta nuke`, `/beta profile reload`, `/beta markov reload`, `/beta ghosts list/reload`, `/beta scenario history` — Plans 3-5
- §8 Cleanup model commands (other than the schema column from §7.5) — Plan 3
- §9 Observability (`#beta-tools-audit`, `#beta-scenario-log`, sim heartbeat, `/beta health`) — Plan 5
- §11 Open Questions — addressed during implementation of relevant plans

## Done state for Plan 1

After all 11 tasks:

- DK Tools bot connects in dev only, refuses everywhere else
- All 3 puppets connect with personas applied (display name + avatar)
- Tools bot and puppets all leave any non-test guild they're invited to
- Webhook fleet provisions per-channel webhooks idempotently
- `/beta-help`, `/beta-puppets-list`, `/beta-puppets-reload`, `/beta-puppets-reconnect`, `/beta-puppets-impersonate`, `/beta-ghosts-impersonate` all work
- `beta_write` DB gate exists and is tested
- Schema migration 007 adds `source` column to 4 tables
- Full pytest suite (`pytest tests/beta/ -v`) passes with no warnings
- Foundation is in place for Plan 2 to add the data artifacts (traffic profile, Markov chains, side-writes)
