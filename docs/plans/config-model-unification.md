# Config-model unification — guild_config becomes the single source of truth

Review finding S2-config3 / #12. Status: **planned (2026-07-02).**

## 1. Context & goal

Guild config is currently modeled three ways in
`src/bot_modules/core/app_context.py`:

1. `RuntimeConfig` TypedDict + `load_runtime_config()` — boot-time loader
   feeding `Bot` construction and the AppContext flat fields;
2. ~22 mutable guild-scoped flat fields on `AppContext` (home-guild values,
   kept "fresh" by web-route mutations and three `reload_*` methods);
3. the frozen, cached, per-guild `GuildConfig` via `ctx.guild_config(gid)`.

Nearly every runtime reader already uses (3). This refactor makes
**`guild_config` the single source of truth for all guild-scoped reads** and
deletes (2) plus most of (1). `AppContext` keeps only process-global state:
`bot`, `log`, `db_path`, `guild_id`, `debug`, `xp_pair_states`,
`watched_users`, `_guild_config_cache`, and its helper methods (all of which
already read via `guild_config` — verified: `is_mod`, `is_admin`,
`can_grant_any_role`, `can_use_grant_role`, `can_use_xp_grant`).

**Runtime behavior must be unchanged for the home guild** (the only configured
guild in prod). Two reader switches additionally *fix* latent multi-guild bugs
where a non-home guild silently got home-guild values (the reports cache
warmer's tz and the greeter-response handler's channel/role ids); both now
read per-guild the same way the corresponding request-path code already does.
The web-save flow keeps its DB-write + `ctx.invalidate_guild_config(gid)`
shape everywhere.

Verified inventory of remaining flat-field readers (2026-07-02):

- `src/bot_modules/cogs/events_cog.py:305-315` — startup INFO/DEBUG log lines
  only (`spoiler_required_channels`, `xp_settings.role_grant_level`,
  `level_5_role_id`, `level_up_log_channel_id`, `level_5_log_channel_id`,
  `xp_excluded_channel_ids`).
- `src/dungeonkeeper/__main__.py:496` — `getattr(ctx, "tz_offset_hours", 0.0)`
  in the reports batch-warm loop (per-guild loop; `gid` in scope;
  `get_tz_offset_hours(conn, gid)` is what every request-path caller uses).
- `src/web_server/routes/reports.py:337-344` — `greeter_response` handler
  reads `greeter_chat_channel_id`, `welcome_channel_id`,
  `join_leave_log_channel_id`, `leave_channel_id`, `greeter_role_id` via
  `getattr(ctx, ..., 0)`.
- `src/web_server/routes/reports.py:482` — `getattr(ctx,
  "recorded_bot_user_ids", set())` in the activity handler (found during plan
  verification; must move in Stage 1 or the Stage 2 field removal silently
  turns it into a constant empty set — the `getattr` default would mask it).

All other flat-field mutations live in the web config-save routes
(`src/web_server/routes/config.py`), every one of which already calls
`ctx.invalidate_guild_config(guild_id)` — so deleting the mutations loses
nothing once no reader remains.

`GuildConfig` already has an equivalent for everything except:

- `greeter_role_id` — **missing; added in Stage 1** (DB key is
  `greeter_role_id`, verified: written by the welcome route
  `config.py:1018`, read by `load_runtime_config` and `db_utils.py:236`).
- `tz_offset_hours` — **deliberately not added.** Timezone is read fresh from
  the DB by every existing caller (`get_tz_offset_hours(conn, gid)` —
  reports, jail, mod, birthday, scheduled games); the one flat reader switches
  to that helper and the field dies.

Type nuance: flat id-set fields are `set[int]`; `GuildConfig`'s are
`frozenset[int]`. All switched readers only iterate/membership-test, and
`set == frozenset` compares by elements, so no call-site changes beyond the
attribute path are needed.

**Coordination note:** a parallel workstream is editing the `games_*` cogs,
`game_manager`, `ai_client`, and the `[tool.pyright]` section of
`pyproject.toml`. This plan touches **none** of those files. Do not edit them,
and do not be alarmed by unrelated working-tree changes in `git status`.

Toolchain gates (same as the shipped typed-bot refactor): `py_compile`, ruff
clean, pyright **0 errors**, and the full pytest suite (~4-5 min):

```bash
BOT_ENV=dev DISCORD_TOKEN_DEV=fake-token GUILD_ID_DEV=9001 DB_PATH_DEV=dk_dev.db AUDIT_CHANNEL_DEV=0 .venv/bin/python -m pytest -q
```

(That env prefix applies to every `pytest` invocation below.)

## 2. The design

### 2.1 Stage 1 (additive) — GuildConfig gains greeter_role_id; stragglers switch to guild_config

#### Edit A — `src/bot_modules/core/app_context.py`: GuildConfig field

In the `GuildConfig` dataclass body, change

```python
    unverified_role_id: int
    greeter_chat_channel_id: int
```

to

```python
    unverified_role_id: int
    greeter_role_id: int
    greeter_chat_channel_id: int
```

In `GuildConfig.load`, change

```python
            unverified_role_id=_int("unverified_role_id"),
            greeter_chat_channel_id=_int("greeter_chat_channel_id"),
```

to

```python
            unverified_role_id=_int("unverified_role_id"),
            greeter_role_id=_int("greeter_role_id"),
            greeter_chat_channel_id=_int("greeter_chat_channel_id"),
```

(`_int` resolves through `get_config_value` with the same home-guild legacy
fallback as every other field — exactly how `greeter_chat_channel_id` loads.)

#### Edit B — `tests/test_app_context.py`: direct GuildConfig construction

`test_guild_config_member_is_mod_matches_mod_or_admin_role` constructs
`GuildConfig(...)` with the full keyword list; a new required field breaks it.
Change

```python
        unverified_role_id=0,
        greeter_chat_channel_id=0,
```

to

```python
        unverified_role_id=0,
        greeter_role_id=0,
        greeter_chat_channel_id=0,
```

Also give the new field load coverage. In
`test_guild_config_load_reads_guild_specific_values`, add one `_db_set` line
after the existing `mod_channel_id` seed and one assertion after the existing
`mod_channel_id` assertion:

```python
        _db_set(conn, "greeter_role_id", "444", guild_id=42)
```

```python
    assert cfg.greeter_role_id == 444
```

(`tests/test_events.py`'s `_StubGuildConfig` is a hand-rolled stub, not the
real dataclass — no change needed. `confessions_service.py` has its own
unrelated local `GuildConfig` class — do not touch.)

#### Edit C — `src/web_server/routes/reports.py`: greeter_response handler

Replace (lines ~336-344)

```python
    greeter_channel_id = (
        getattr(ctx, "greeter_chat_channel_id", 0)
        or getattr(ctx, "welcome_channel_id", 0)
    )
    log_channel_id = (
        getattr(ctx, "join_leave_log_channel_id", 0)
        or getattr(ctx, "leave_channel_id", 0)
    )
    greeter_role_id = getattr(ctx, "greeter_role_id", 0)
```

with

```python
    cfg = ctx.guild_config(guild_id)
    greeter_channel_id = cfg.greeter_chat_channel_id or cfg.welcome_channel_id
    log_channel_id = cfg.join_leave_log_channel_id or cfg.leave_channel_id
    greeter_role_id = cfg.greeter_role_id
```

(`guild_id` is already in scope from `get_active_guild_id(request)` two lines
up. For the home guild the values are identical; for another guild this is the
multi-guild fix described in §1.)

#### Edit D — `src/web_server/routes/reports.py`: activity handler

Replace (line ~482)

```python
        excluded_users.update(getattr(ctx, "recorded_bot_user_ids", set()))
```

with

```python
        excluded_users.update(ctx.guild_config(guild_id).recorded_bot_user_ids)
```

#### Edit E — `src/dungeonkeeper/__main__.py`: reports batch-warm tz

Change the db_utils import line

```python
from bot_modules.core.db_utils import migrate_grant_roles, open_db
```

to

```python
from bot_modules.core.db_utils import get_tz_offset_hours, migrate_grant_roles, open_db
```

and in `_reports_batch_loop`, replace

```python
                    gid = guild.id
                    tz = getattr(ctx, "tz_offset_hours", 0.0)
```

with

```python
                    gid = guild.id
                    with open_db(db_path) as conn:
                        tz = get_tz_offset_hours(conn, gid)
```

(One extra short-lived connection per guild per 15-minute pass — negligible.
The warm-cache values now match what the live `/api/reports/*` endpoints
compute, which already call `get_tz_offset_hours(conn, guild_id)`.)

#### Edit F — `src/bot_modules/cogs/events_cog.py`: startup log lines

In `on_ready`, replace (lines ~301-315)

```python
        log.info(
            "Primary guild %s (ID: %s, guarding: %s)",
            _primary_guild.name if _primary_guild else self.ctx.guild_id,
            self.ctx.guild_id,
            [_ch(c) for c in self.ctx.spoiler_required_channels],
        )
        log.info(
            "XP config loaded: level-%s role=%s level-up-log=%s level-%s-log=%s.",
            self.ctx.xp_settings.role_grant_level,
            _ro(self.ctx.level_5_role_id),
            _ch(self.ctx.level_up_log_channel_id),
            self.ctx.xp_settings.role_grant_level,
            _ch(self.ctx.level_5_log_channel_id),
        )
        log.debug("XP excluded channels: %s", sorted(self.ctx.xp_excluded_channel_ids))
```

with

```python
        cfg = self.ctx.guild_config(self.ctx.guild_id)
        log.info(
            "Primary guild %s (ID: %s, guarding: %s)",
            _primary_guild.name if _primary_guild else self.ctx.guild_id,
            self.ctx.guild_id,
            [_ch(c) for c in cfg.spoiler_required_channels],
        )
        log.info(
            "XP config loaded: level-%s role=%s level-up-log=%s level-%s-log=%s.",
            cfg.xp_settings.role_grant_level,
            _ro(cfg.level_5_role_id),
            _ch(cfg.level_up_log_channel_id),
            cfg.xp_settings.role_grant_level,
            _ch(cfg.level_5_log_channel_id),
        )
        log.debug("XP excluded channels: %s", sorted(cfg.xp_excluded_channel_ids))
```

One DB read at startup — cheap, and it primes the home-guild snapshot cache.
`tests/test_events.py`'s `_make_ctx` already stubs
`ctx.guild_config -> _StubGuildConfig(...)` with real sets and
`xp_settings=DEFAULT_XP_SETTINGS` (verified), so the existing `on_ready` test
passes unchanged.

### 2.2 Stage 2 (destructive) — remove the flat fields, RuntimeConfig, web-route mutations, reload_* methods

#### Decision — the `reload_*` methods are **deleted**, not kept as wrappers

`reload_xp_settings`, `reload_permission_roles`, and `reload_grant_roles` only
refresh the flat copies; they never touch `_guild_config_cache` (verified), so
they are not cache primers, and every web route that calls one *already* calls
`ctx.invalidate_guild_config(guild_id)` on the same path. A thin
invalidate-only wrapper would be a second, redundant, misleading API for the
same thing. The three boot calls (`__main__.py:148-150`) only primed flat
copies; `guild_config()` loads lazily on first use, so no priming replacement
is needed. Callers updated in this stage: `__main__.py` (3 calls) and
`config.py` (3 route sites); test fakes drop their stub implementations.

#### Edit G — `src/bot_modules/core/app_context.py`

**G1.** Delete the entire `RuntimeConfig` TypedDict and `load_runtime_config`
function (lines ~67-170), and the now-orphaned `_parse_float_config` helper
(its only caller). In their place (directly after `_parse_int_config`), add:

```python
def resolve_guild_id(db_path: Path, *, default_guild_id: int = 0) -> int:
    """Resolve the home guild id: config-table row, then caller default,
    then the ``GUILD_ID`` environment variable."""
    with open_db(db_path) as conn:
        guild_id = _parse_int_config(
            get_config_value(conn, "guild_id", "0"), key="guild_id"
        )
    if guild_id == 0:
        guild_id = default_guild_id or _parse_int_config(
            os.environ.get("GUILD_ID", "0"), key="GUILD_ID"
        )
    return guild_id
```

(Identical resolution chain to the old loader. Keep the `import os`.)

**G2.** Update the imports: drop `TypedDict` from the `typing` import (its
only user was `RuntimeConfig`) and drop `DEFAULT_XP_SETTINGS` from the
`xp_system` import (its only user was the flat `xp_settings` default):

```python
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias
```

```python
from bot_modules.core.xp_system import XpSettings, load_xp_settings
```

(`XpSettings` is still used by the `GuildConfig.xp_settings` annotation;
`load_xp_settings`, `parse_bool`, `GrantRoleConfig`, `get_grant_roles`,
`can_use_grant` are all still used — do not touch the db_utils import.)

**G3.** Replace the `AppContext` field block

```python
@dataclass
class AppContext:
    bot: Bot
    log: logging.Logger
    db_path: Path
    guild_id: int
    debug: bool
    mod_channel_id: int
    spoiler_required_channels: set[int]
    bypass_role_ids: set[int]
    xp_grant_allowed_user_ids: set[int]
    xp_excluded_channel_ids: set[int]
    recorded_bot_user_ids: set[int]
    level_5_role_id: int
    level_5_log_channel_id: int
    level_up_log_channel_id: int
    greeter_role_id: int
    greeter_chat_channel_id: int
    join_leave_log_channel_id: int
    welcome_channel_id: int
    welcome_message: str
    welcome_ping_role_id: int
    leave_channel_id: int
    leave_message: str
    tz_offset_hours: float = 0.0
    xp_settings: XpSettings = field(default_factory=lambda: DEFAULT_XP_SETTINGS)
    grant_roles: dict[str, GrantRoleConfig] = field(default_factory=dict)
    xp_pair_states: dict[int, Any] = field(default_factory=dict)
    watched_users: dict[int, set[int]] = field(default_factory=dict)
    mod_role_ids: set[int] = field(default_factory=set)
    admin_role_ids: set[int] = field(default_factory=set)
    _guild_config_cache: dict[int, GuildConfig] = field(default_factory=dict)
```

with

```python
@dataclass
class AppContext:
    """Process-global runtime state. All guild-scoped config is read via
    :meth:`guild_config`; there are deliberately no per-guild fields here."""

    bot: Bot
    log: logging.Logger
    db_path: Path
    guild_id: int
    debug: bool
    xp_pair_states: dict[int, Any] = field(default_factory=dict)
    watched_users: dict[int, set[int]] = field(default_factory=dict)
    _guild_config_cache: dict[int, GuildConfig] = field(default_factory=dict)
```

**G4.** Delete the three reload methods in full:
`reload_xp_settings` (with its body), `reload_permission_roles` (with its
docstring and body), and `reload_grant_roles`.

**G5.** `set_config_value`: remove the flat-cache sync. Replace

```python
        gid = self.guild_id if guild_id is None else guild_id
        with self.open_db() as conn:
            _db_set_config_value(conn, key, value, gid)
            result = get_config_value(conn, key, value, gid)
        if gid == self.guild_id:
            if key == "mod_role_ids":
                self.mod_role_ids = {int(x) for x in result.split(",") if x.strip().isdigit()}
            elif key == "admin_role_ids":
                self.admin_role_ids = {int(x) for x in result.split(",") if x.strip().isdigit()}
        self.invalidate_guild_config(gid)
        return result
```

with

```python
        gid = self.guild_id if guild_id is None else guild_id
        with self.open_db() as conn:
            _db_set_config_value(conn, key, value, gid)
            result = get_config_value(conn, key, value, gid)
        self.invalidate_guild_config(gid)
        return result
```

and update its docstring second line from "Keeps the home flat caches
consistent and invalidates the per-guild snapshot" to "Invalidates the
per-guild snapshot so ``guild_config()`` readers see the write."

**G6.** `delete_config_value`: replace

```python
        with self.open_db() as conn:
            delete_config_value(conn, key, self.guild_id)
        if key == "mod_role_ids":
            self.mod_role_ids = set()
        elif key == "admin_role_ids":
            self.admin_role_ids = set()
        self.invalidate_guild_config(self.guild_id)
```

with

```python
        with self.open_db() as conn:
            delete_config_value(conn, key, self.guild_id)
        self.invalidate_guild_config(self.guild_id)
```

#### Edit H — `src/dungeonkeeper/__main__.py`

**H1.** Import line: change

```python
from bot_modules.core.app_context import AppContext, Bot, load_runtime_config
```

to

```python
from bot_modules.core.app_context import AppContext, Bot, resolve_guild_id
```

**H2.** Replace the config-load + context block (lines ~106-150), i.e.
everything from `cfg = load_runtime_config(...)` through the three
`ctx.reload_*()` calls, with:

```python
    guild_id = resolve_guild_id(db_path, default_guild_id=boot_cfg.guild_id)

    intents = discord.Intents.default()
    intents.members = True
    intents.presences = True
    intents.message_content = True

    bot = Bot(intents=intents, debug=args.debug, guild_id=guild_id)

    ctx = AppContext(
        bot=bot,
        log=log,
        db_path=db_path,
        guild_id=guild_id,
        debug=args.debug,
    )

    # ==============================
    # Populate runtime state from DB
    # ==============================
    with open_db(db_path) as conn:
        migrate_grant_roles(conn, guild_id)
        ctx.watched_users = load_watched_users(conn, guild_id)
```

Keep the `# Runtime config + context` / `# Cog extensions` section-banner
comments around it as they are. `args.debug` is exactly what the old
`cfg["debug"]` carried (the loader stored the `debug` kwarg verbatim), and
`cfg["guild_id"]` is exactly what `resolve_guild_id` returns — verify no
other `cfg[` reference remains in the file (`grep -n 'cfg\[' src/dungeonkeeper/__main__.py`
must be empty afterwards).

#### Edit I — `src/web_server/routes/config.py` (five routes)

**I1. `update_global`.** Delete the line
`    is_home = guild_id == ctx.guild_id` and all four flat-mutation blocks:

```python
                if is_home:
                    ctx.tz_offset_hours = body.tz_offset_hours
```

```python
                if is_home:
                    ctx.mod_channel_id = int(body.mod_channel_id)
```

```python
                if is_home:
                    ctx.bypass_role_ids = {int(r) for r in body.bypass_role_ids}
```

```python
                if is_home:
                    ctx.recorded_bot_user_ids = {
                        int(u) for u in body.recorded_bot_user_ids
                    }
```

The `set_config_value` / bucket writes and the trailing
`ctx.invalidate_guild_config(guild_id)` stay untouched.

**I2. `update_welcome`.** Replace the per-field loop

```python
            for field_name, config_key in _FIELDS.items():
                val = getattr(body, field_name)
                if val is not None:
                    set_config_value(conn, config_key, val, guild_id)
                    # Keep the home guild's flat ctx fields fresh for straggler
                    # readers not yet migrated to guild_config() (e.g. the
                    # reports route reads ctx.greeter_role_id /
                    # ctx.join_leave_log_channel_id). Only for the home guild —
                    # mutating these for another guild would corrupt home state.
                    if guild_id == ctx.guild_id and hasattr(ctx, config_key):
                        try:
                            setattr(ctx, config_key, int(val))
                        except ValueError:
                            setattr(ctx, config_key, val)
```

with

```python
            for field_name, config_key in _FIELDS.items():
                val = getattr(body, field_name)
                if val is not None:
                    set_config_value(conn, config_key, val, guild_id)
```

(The comment's claim is obsolete after Stage 1 — the reports route now reads
`guild_config`.)

**I3. `update_xp`.** Delete the comment + `is_home` assignment

```python
    # Per-guild aware: XP config is read at runtime via ctx.guild_config(gid).
    # In-memory flat-field updates below are kept for the home guild only (some
    # cosmetic home readers remain); other guilds are refreshed via the
    # invalidate_guild_config call after the write.
    is_home = guild_id == ctx.guild_id
```

and all five flat-mutation blocks inside `_q` (each is an
`if is_home:` followed by one `ctx.<field> = ...` statement — for
`level_5_role_id`, `level_5_log_channel_id`, `level_up_log_channel_id`,
`xp_grant_allowed_user_ids`, `xp_excluded_channel_ids`), and the reload block:

```python
            # Reload live XP settings on ctx (home guild only — reload_xp_settings
            # reads ctx.guild_id).
            if is_home and hasattr(ctx, "reload_xp_settings"):
                ctx.reload_xp_settings()
```

The coefficient persistence loop and the trailing
`ctx.invalidate_guild_config(guild_id)` stay.

**I4. `update_moderation`.** Delete

```python
    if guild_id == ctx.guild_id:
        # Keep the home guild's flat role caches in sync for stragglers not yet
        # migrated to guild_config() (e.g. setup_cog reads ctx.mod_role_ids).
        ctx.reload_permission_roles()
```

(The comment is factually wrong — `setup_cog` does **not** read
`ctx.mod_role_ids`; verified by grep. Note its removal in the commit message.)

**I5. `update_role_grant` and `delete_role_grant`.** In both routes, delete

```python
    if guild_id == ctx.guild_id:
        ctx.reload_grant_roles()
```

(the `ctx.invalidate_guild_config(guild_id)` line above each stays).

**I6. `update_spoiler`.** Delete `    is_home = guild_id == ctx.guild_id` and

```python
                if is_home:
                    ctx.spoiler_required_channels = {
                        int(c) for c in body.spoiler_required_channels
                    }
```

#### Edit J — test updates

**J1. `tests/test_app_context.py`.**

- Slim `_make_ctx` to:

  ```python
  def _make_ctx(db_path, guild_id: int = 123) -> AppContext:
      """Construct a minimal AppContext backed by a real (migrated) temp DB."""
      apply_migrations_sync(db_path)
      return AppContext(
          bot=MagicMock(),
          log=logging.getLogger("test"),
          db_path=db_path,
          guild_id=guild_id,
          debug=True,
      )
  ```

- In `test_set_config_value_invalidates_guild_config_cache`, delete the final
  flat assertion and its comment tail. Replace

  ```python
      # The next read must reflect the write (cache was invalidated), and agree
      # with the flat cache that set_config_value maintains.
      assert ctx.guild_config(ctx.guild_id).mod_role_ids == frozenset({900, 901})
      assert ctx.mod_role_ids == {900, 901}
  ```

  with

  ```python
      # The next read must reflect the write (cache was invalidated).
      assert ctx.guild_config(ctx.guild_id).mod_role_ids == frozenset({900, 901})
  ```

- Replace `test_reload_permission_roles_scoped_to_home_guild` (the method no
  longer exists; the guarded behavior — per-guild scoping — is re-targeted at
  `guild_config`):

  ```python
  def test_guild_config_mod_roles_scoped_per_guild(tmp_path):
      """Per-guild snapshots must NOT leak another guild's role IDs."""
      ctx = _make_ctx(tmp_path / "ctx_reload.db", guild_id=10)
      with open_db(ctx.db_path) as conn:
          _db_set(conn, "mod_role_ids", "100,101", guild_id=10)
          _db_set(conn, "mod_role_ids", "200,201", guild_id=20)

      assert ctx.guild_config(10).mod_role_ids == frozenset({100, 101})
      assert ctx.guild_config(20).mod_role_ids == frozenset({200, 201})
  ```

**J2. `tests/test_jail_commands.py` and `tests/test_jail_apply.py`.** Each
file has one `_make_ctx` helper constructing `AppContext(...)` with the full
flat-field list. In both, delete every kwarg after `debug=True,` (i.e.
`mod_channel_id=0,` through `leave_message="",`), leaving:

```python
    return AppContext(
        bot=MagicMock(),
        log=logging.getLogger("test"),
        db_path=db_path,
        guild_id=guild_id,
        debug=True,
    )
```

(Keep each file's own `guild_id` default/signature as-is.)

**J3. `tests/web/conftest.py`.** Slim `FakeCtx` — delete all flat-field
assignments, `_xp_reload_count`, and the three `reload_*` methods, keeping:

```python
class FakeCtx:
    """Minimal AppContext substitute for web route tests."""

    def __init__(self, db_path: Path, guild_id: int = 123):
        self.db_path = db_path
        self.guild_id = guild_id
        self.bot = None
        self._guild_config_cache: dict = {}

    def open_db(self):
        return open_db(self.db_path)

    def guild_config(self, guild_id: int):
        from bot_modules.core.app_context import GuildConfig

        cfg = self._guild_config_cache.get(guild_id)
        if cfg is None:
            with self.open_db() as conn:
                cfg = GuildConfig.load(
                    conn, guild_id, allow_legacy_fallback=(guild_id == self.guild_id)
                )
            self._guild_config_cache[guild_id] = cfg
        return cfg

    def invalidate_guild_config(self, guild_id: int) -> None:
        self._guild_config_cache.pop(guild_id, None)
```

**J4. `tests/web/test_config_routes.py`.** Delete two now-redundant tests
whole (each is exactly duplicated, against `guild_config`, by the
`*_invalidates_guild_config_cache` test immediately above it — verified):

- `test_update_welcome_syncs_flat_field_for_home_guild`
- `test_update_moderation_syncs_flat_role_caches_for_home_guild`

**J5. `tests/test_web_routes.py`.**

- Slim `_TestCtx` the same way as `FakeCtx` (J3): keep `db_path`, `guild_id`,
  `bot: Any = None`, `_guild_config_cache`, `open_db`, `guild_config`,
  `invalidate_guild_config`; delete all flat fields,
  `reload_xp_settings_calls`, and the three `reload_*` methods.
- Add `get_tz_offset_hours` to the existing
  `from bot_modules.core.db_utils import (...)` block.
- In `test_update_global_persists_fields_and_updates_context`, replace the
  four flat assertions

  ```python
      assert ctx.tz_offset_hours == -4.5
      assert ctx.mod_channel_id == 111
      assert ctx.bypass_role_ids == {1, 2}
      assert ctx.recorded_bot_user_ids == {9}
  ```

  with

  ```python
      with open_db(ctx.db_path) as conn:
          assert get_tz_offset_hours(conn, ctx.guild_id) == -4.5
      cfg = ctx.guild_config(ctx.guild_id)
      assert cfg.mod_channel_id == 111
      assert cfg.bypass_role_ids == {1, 2}
      assert cfg.recorded_bot_user_ids == {9}
  ```

- In `test_update_welcome_persists_and_updates_live_context`, replace the
  eight flat assertions (`assert ctx.welcome_channel_id == 500` through
  `assert ctx.join_leave_log_channel_id == 505`) with

  ```python
      cfg = ctx.guild_config(ctx.guild_id)
      assert cfg.welcome_channel_id == 500
      assert cfg.welcome_message == "Hello there"
      assert cfg.welcome_ping_role_id == 501
      assert cfg.leave_channel_id == 502
      assert cfg.leave_message == "Goodbye"
      assert cfg.greeter_role_id == 503
      assert cfg.greeter_chat_channel_id == 504
      assert cfg.join_leave_log_channel_id == 505
  ```

  (`cfg.greeter_role_id` works because Stage 1 added the field.)
- In `test_update_xp_persists_coefficients_and_reloads_context`, replace the
  six flat/counter assertions (`assert ctx.level_5_role_id == 10` through
  `assert ctx.reload_xp_settings_calls == 1`) with

  ```python
      cfg = ctx.guild_config(ctx.guild_id)
      assert cfg.level_5_role_id == 10
      assert cfg.level_5_log_channel_id == 11
      assert cfg.level_up_log_channel_id == 12
      assert cfg.xp_grant_allowed_user_ids == {13, 14}
      assert cfg.xp_excluded_channel_ids == {15}
      assert cfg.xp_settings.message_word_xp == 1.5
  ```

  The last line replaces the `reload_xp_settings_calls` counter: it proves
  the written coefficient is visible to runtime readers, which is what the
  reload used to guarantee. (`XpSettings.message_word_xp` exists — verified,
  `xp_system.py:19` — and the request writes `xp_coeff_message_word_xp=1.5`.)

**Left alone deliberately (MagicMock-based, resilient):**
`tests/test_events.py` `_make_ctx` (sets flat attrs on a MagicMock; the code
under test reads the stubbed `guild_config`), `tests/test_commands.py`
`_make_ctx` (same), `tests/cogs/test_todo_cog.py` `_build_mod_ctx` (MagicMock;
`todo_cog` has no flat-field reads — verified). Setting extra attributes on a
MagicMock is harmless; cleaning them up is optional follow-up, not this pass.

## 3. Stage plan (Sonnet implementers — follow verbatim)

All commands run from `/home/ben/discord-bots/dungeon-keeper`. Each stage is
independently committable; commit at the end of each stage.

### Stage 1 — Additive: GuildConfig.greeter_role_id + straggler readers switch

**Files:** `src/bot_modules/core/app_context.py`,
`src/web_server/routes/reports.py`, `src/dungeonkeeper/__main__.py`,
`src/bot_modules/cogs/events_cog.py`, `tests/test_app_context.py`

**Edits:** §2.1 (Edits A-F) — exactly as written. Do not remove any flat
field, `RuntimeConfig`, or web-route mutation in this stage.

**Verification (all must pass):**

```bash
.venv/bin/python -m py_compile src/bot_modules/core/app_context.py src/web_server/routes/reports.py src/dungeonkeeper/__main__.py src/bot_modules/cogs/events_cog.py
.venv/bin/ruff check src tests
.venv/bin/pyright          # expect: 0 errors, 0 warnings
grep -n 'getattr(ctx, "' src/web_server/routes/reports.py
# expect: only the two getattr(ctx, "bot", None) lines — no config-field reads
grep -c 'tz_offset_hours' src/dungeonkeeper/__main__.py
# expect: 0
PYTHONPATH=src .venv/bin/python - <<'EOF'
import tempfile, pathlib
from migrations import apply_migrations_sync
from bot_modules.core.app_context import GuildConfig
from bot_modules.core.db_utils import open_db, set_config_value
p = pathlib.Path(tempfile.mkdtemp()) / "smoke.db"
apply_migrations_sync(p)
with open_db(p) as conn:
    set_config_value(conn, "greeter_role_id", "42", 7)
    cfg = GuildConfig.load(conn, 7, allow_legacy_fallback=False)
assert cfg.greeter_role_id == 42, cfg.greeter_role_id
print("greeter_role_id OK")
EOF
BOT_ENV=dev DISCORD_TOKEN_DEV=fake-token GUILD_ID_DEV=9001 DB_PATH_DEV=dk_dev.db AUDIT_CHANNEL_DEV=0 .venv/bin/python -m pytest -q
# expect: all pass, no new failures
```

**STOP conditions:** any pre-existing test fails after Edits A-F → abort and
report (do not fix by widening the change). The reports.py grep shows a
config-field `getattr(ctx, ...)` read this plan didn't list → abort and
report the line (the verified inventory was incomplete). Pyright reports a
missing-argument error for `GuildConfig(...)` anywhere other than
`tests/test_app_context.py` → abort and report the file.

**Commit:** `Config unification: GuildConfig gains greeter_role_id; last flat-field readers switch to guild_config (S2-config3 #12, stage 1)`

### Stage 2 — Destructive: flat fields, RuntimeConfig, web mutations, reload_* die

**Files:** `src/bot_modules/core/app_context.py`,
`src/dungeonkeeper/__main__.py`, `src/web_server/routes/config.py`,
`tests/test_app_context.py`, `tests/test_jail_commands.py`,
`tests/test_jail_apply.py`, `tests/web/conftest.py`,
`tests/web/test_config_routes.py`, `tests/test_web_routes.py`

**Edits:** §2.2 (Edits G-J) — exactly as written.

**Verification (all must pass):**

```bash
.venv/bin/python -m py_compile src/bot_modules/core/app_context.py src/dungeonkeeper/__main__.py src/web_server/routes/config.py
.venv/bin/ruff check src tests
.venv/bin/pyright          # expect: 0 errors, 0 warnings
grep -rn 'load_runtime_config\|RuntimeConfig' src tests --include='*.py'
# expect: only the prose mention in src/bot_modules/core/config.py's module
# docstring (update that sentence to name resolve_guild_id) — no code refs
grep -rn 'reload_xp_settings\|reload_permission_roles\|reload_grant_roles' src tests --include='*.py'
# expect: no output
grep -rn 'is_home' src/web_server/routes/config.py
# expect: no output
grep -rnE 'ctx\.(mod_channel_id|spoiler_required_channels|bypass_role_ids|xp_grant_allowed_user_ids|xp_excluded_channel_ids|recorded_bot_user_ids|level_5_role_id|level_5_log_channel_id|level_up_log_channel_id|greeter_role_id|greeter_chat_channel_id|join_leave_log_channel_id|welcome_channel_id|welcome_message|welcome_ping_role_id|leave_channel_id|leave_message|tz_offset_hours|mod_role_ids|admin_role_ids)\b' src --include='*.py'
# expect: no output
grep -n 'cfg\[' src/dungeonkeeper/__main__.py
# expect: no output
PYTHONPATH=src .venv/bin/python - <<'EOF'
import logging, tempfile, pathlib
from unittest.mock import MagicMock
from migrations import apply_migrations_sync
from bot_modules.core.app_context import AppContext, resolve_guild_id
p = pathlib.Path(tempfile.mkdtemp()) / "smoke2.db"
apply_migrations_sync(p)
assert resolve_guild_id(p, default_guild_id=9001) == 9001
ctx = AppContext(bot=MagicMock(), log=logging.getLogger("t"), db_path=p, guild_id=9001, debug=True)
assert ctx.guild_config(9001).mod_role_ids == frozenset()
ctx.set_config_value("mod_role_ids", "1,2")
assert ctx.guild_config(9001).mod_role_ids == frozenset({1, 2})
print("slim AppContext OK")
EOF
BOT_ENV=dev DISCORD_TOKEN_DEV=fake-token GUILD_ID_DEV=9001 DB_PATH_DEV=dk_dev.db AUDIT_CHANNEL_DEV=0 .venv/bin/python -m pytest -q
# expect: all pass — pay attention to tests/web/, test_web_routes,
# test_app_context, test_jail_*
```

Note on the `RuntimeConfig` grep: `src/bot_modules/core/config.py` line 4 is
a docstring sentence ("Separate from app_context.RuntimeConfig, which manages
guild-scoped runtime ..."). Rewrite that sentence to reference
`app_context.resolve_guild_id` / `GuildConfig` instead — prose only, no code
change in that file.

**STOP conditions:** the flat-field grep finds a reader in a file not listed
in this plan → abort and report the file (means Stage 1's inventory missed
one; do not delete the field it needs without switching the reader first).
Any web-route test fails with `AttributeError` on `FakeCtx`/`_TestCtx` for an
attribute this plan didn't delete → abort and report (a route reads a flat
field the inventory missed). `pyright` reports errors in `games_*`,
`game_manager`, `ai_client`, or `pyproject.toml` diffs appear → you touched
the parallel workstream's files; revert those hunks and report.

**Commit:** `Config unification: AppContext drops guild-scoped flat fields; RuntimeConfig -> resolve_guild_id; reload_* retired (S2-config3 #12, stage 2)`

## 4. Risks & mitigations

- **A hidden flat-field reader survives to Stage 2.** Two sweeps performed
  (attribute-access grep and `getattr("...")` string-form grep) across `src`;
  the string-form sweep is repeated as a Stage 2 gate. `get_ctx()` in
  `web_server/deps.py` is untyped, so pyright can *not* catch web-route reads
  — the greps are the real gate there; bot-side reads are pyright-typed and
  would fail the 0-errors gate.
- **Reports greeter/tz values change.** For the home guild they are identical
  (flats mirrored home-guild DB rows, and `get_tz_offset_hours` resolves with
  the same legacy fallback the boot loader used). For non-home guilds the new
  per-guild reads are the *intended* fix and match what the request-path
  endpoints already serve.
- **Losing `reload_*` breaks freshness.** Every route that called them already
  invalidates the per-guild snapshot on the same path, and every runtime
  reader goes through `guild_config()`; freshness is unchanged. The boot-time
  calls only populated the flat copies nothing reads anymore.
- **`on_ready` DB read.** One synchronous `GuildConfig.load` on the event loop
  at startup; same cost as any first `guild_config()` call (which would happen
  on the first message anyway) and it warms the cache.
- **frozenset vs set.** All switched readers iterate, membership-test, or
  `sorted()` — no mutation. Test equality comparisons are element-based across
  set/frozenset, so `== {1, 2}` still passes.
- **Stale pycs.** `src/bot_modules/core/__pycache__` contains compiled refs to
  removed symbols; Python recompiles automatically — ignore pyc grep hits.

## 5. Rejected alternatives

- **Keep `reload_*` as invalidate-only wrappers:** preserves the API but every
  caller already invalidates on the same path, so the wrappers would be
  redundant no-ops with misleading names; the caller updates are 4 small
  sites. Deleted instead.
- **Add `tz_offset_hours` to `GuildConfig`:** every existing caller reads tz
  fresh via `get_tz_offset_hours(conn, gid)` inside a query it is already
  running; a cached copy would create a second freshness regime for one value.
- **Slim `RuntimeConfig` to `{guild_id, debug}` instead of deleting it:** a
  two-key TypedDict wrapping one DB read is ceremony; `debug` never came from
  the DB (the loader echoed its own kwarg), so only guild-id resolution is
  real logic → a plain `resolve_guild_id()` function.
- **Make `AppContext.guild_config` home-guild-property sugar
  (`ctx.home_config`):** two names for the same lookup invites drift; call
  sites are short enough as-is.
- **Fold the reports greeter fallback chain into `GuildConfig` properties:**
  single-caller logic; would grow the core dataclass for one web handler.

## 6. Follow-ups (out of scope)

- Type `web_server/deps.py:get_ctx()` (`-> AppContext`) so pyright checks web
  routes' ctx usage; blocked today by `FakeCtx`/`_TestCtx` duck-typing —
  consider a `Protocol`.
- Drop the now-vestigial flat attrs set on MagicMock ctxs in
  `tests/test_events.py`, `tests/test_commands.py`,
  `tests/cogs/test_todo_cog.py` (harmless clutter).
- `GuildConfig` growth: `unverified_role_id`, `welcome_trigger` etc. loaded
  but the moderation slice (`jailed_role_id`, ...) still reads the DB
  directly in `jail_commands._get_config` — a candidate second slice.
- Unify `tests/web/conftest.py:FakeCtx` and `tests/test_web_routes.py:_TestCtx`
  (now byte-identical after J3/J5) into one shared fixture.
