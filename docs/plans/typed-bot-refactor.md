# Typed Bot refactor — kill the monkey-patched runtime registries

Review finding S2-god / #11. Status: **implemented (stages 1-3 complete, 2026-07-02).**

## 1. Context & goal

`__main__.py` currently bolts seven attributes onto the `Bot` instance after
construction (`games_db`, `active_views`, `game_launchers`, `game_recoverers`,
`game_busy_checks`, `game_joiners`, `game_leavers`), each with
`# type: ignore[attr-defined]`. Four more ad-hoc AppContext aliases
(`_booster_ctx`, `_mod_ctx`, `_pen_pals_ctx`, `_vm_ctx`) are scattered across
`__main__` and three cogs — all verified to hold the **same object** as
`bot.ctx` (jail, pen-pals, and voice-master cog setups all receive `bot.ctx`;
`_booster_ctx` is assigned the same `ctx` local that becomes `bot.ctx`).

This refactor:

- declares/initializes all seven registries as **typed attributes on `Bot`**
  (`src/bot_modules/core/app_context.py`), keeping every one of the 200+ call
  sites (`bot.active_views[...]`, etc.) byte-for-byte unchanged;
- retires the four `_xxx_ctx` aliases in favor of `bot.ctx` (15 prod refs +
  12 test refs);
- annotates the 19 `games_*` cogs' `bot` params as the project `Bot` (same
  pattern as the six duel cogs in commit `6dfe993`);
- removes 22 `# type: ignore[attr-defined]` suppressions.

**Runtime behavior must be 100% unchanged.** Payoff: of the 575 pyright errors
currently hidden by the games exclusion, this kills ~190 (166 registry-attr +
24 `bot.ctx`), leaving ~385 for follow-up. We do **not** un-exclude games in
pyproject this pass.

Toolchain facts (verified 2026-07-02): pyright baseline is **0 errors**
(`.venv/bin/pyright`), ruff clean on `src` and `tests`, pytest **is**
installed (9.1.1; the memory note saying otherwise is stale) and runs via
`.venv/bin/python -m pytest -q`.

## 2. The design

### 2.1 `src/bot_modules/core/app_context.py`

The file already has `from __future__ import annotations` (line 1),
`import sqlite3`, `import discord`, and `Callable`/`Coroutine` imports —
so annotations below are free at runtime and only `RecoverySentinel`/`GamesDb`
need guarding.

**Edit A — typing import (line 10).** Change

```python
from typing import Any, TypeAlias, TypedDict
```

to

```python
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, TypedDict
```

**Edit B — TYPE_CHECKING block.** Immediately after the last top-level import
(`from bot_modules.core.xp_system import ...`), add:

```python
if TYPE_CHECKING:
    from bot_modules.games.utils.recovery import RecoverySentinel
    from bot_modules.services.games_db import GamesDb
```

(The runtime import chain `recovery -> game_manager -> stdlib/discord` was
verified cycle-free, but TYPE_CHECKING costs nothing and is future-proof, so
we use it anyway. `ActiveGameView` below is therefore a *string* alias.)

**Edit C — registry types.** Directly above `class Bot(commands.Bot):`
(currently line 169), insert:

```python
# ---------------------------------------------------------------------------
# Game runtime registry types
# ---------------------------------------------------------------------------
# Value stored in ``Bot.active_views``: the live top-level View for a game, or
# a RecoverySentinel placeholder while a crashed game is being re-driven.
ActiveGameView: TypeAlias = "discord.ui.View | RecoverySentinel"


class GameLauncher(Protocol):
    """Interaction-free game launcher, registered per game_type in cog setup().

    Called by the scheduler and /games start. Returns the new game_id, or
    ``None`` when the launch was refused (missing perms, channel busy, ...).
    ``channel`` is deliberately ``Any``: channel-union narrowing is an
    explicit follow-up outside this refactor.
    """

    def __call__(
        self,
        *,
        channel: Any,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict[str, Any],
    ) -> Coroutine[Any, Any, str | None]: ...


# Crash-recovery handler: (games_active_games row, decoded JSON payload,
# resolved anchor channel, anchor message or None) -> True if re-armed.
GameRecoverer: TypeAlias = Callable[
    [sqlite3.Row, dict[str, Any], Any, discord.Message | None],
    Coroutine[Any, Any, bool],
]
# Optional "channel busy" check for games that track active rounds outside
# the games_active_games table (e.g. risky_roll, in-memory).
GameBusyCheck: TypeAlias = Callable[[int], Coroutine[Any, Any, bool]]
# Mid-game roster handler: (channel, game_id, member) -> (ok, user_message).
GameRosterHandler: TypeAlias = Callable[
    [Any, str, discord.Member],
    Coroutine[Any, Any, tuple[bool, str]],
]
```

Notes for the implementer:
- `GameLauncher` must be a Protocol (keyword-only params can't be expressed
  with `Callable`). Bound methods match it structurally; the cogs' untyped
  `channel` params (implicit `Any`) and bare `options: dict` are compatible.
- `ActiveGameView` must stay a quoted string (RecoverySentinel is
  TYPE_CHECKING-only). Pyright resolves string-valued `TypeAlias`.
- Do NOT quote `discord.Message | None` etc. — `discord` and `sqlite3` are
  runtime imports here.

**Edit D — the Bot class.** Replace the class header + `__init__` with:

```python
class Bot(commands.Bot):
    ctx: AppContext  # set by the entry point before bot.run()
    games_db: GamesDb  # set by the entry point before bot.run() (needs db_path)

    def __init__(self, *, intents: discord.Intents, debug: bool, guild_id: int | str):
        super().__init__(intents=intents, command_prefix=commands.when_mentioned)
        self.debug = debug
        self.guild_id = _parse_int_config(str(guild_id), key="guild_id")
        self.startup_task_factories: list[Callable[[], Coroutine[Any, Any, None]]] = []
        self.startup_tasks: list[asyncio.Task[None]] = []
        self.extension_names: list[str] = []
        # Game runtime registries. Created here (not injected by the entry
        # point) so they exist before setup_hook() loads extensions — cog
        # setup() functions write into them.
        # Live views (or RecoverySentinel placeholders) for in-flight games,
        # keyed by game_id.
        self.active_views: dict[str, ActiveGameView] = {}
        # Interaction-free launchers, keyed by game_type. Each party game cog
        # registers its launch() here in setup(); the scheduler calls them.
        self.game_launchers: dict[str, GameLauncher] = {}
        # Crash-recovery handlers, keyed by game_type. Each party game cog
        # registers its recover_game() here in setup(); the startup recovery
        # task re-registers in-flight games' views/timers after a restart.
        self.game_recoverers: dict[str, GameRecoverer] = {}
        # Optional "channel busy" checks, keyed by game_type. Games that track
        # active rounds outside the games_active_games table (e.g. risky_roll,
        # in-memory) register an async check(channel_id) -> bool here so the
        # scheduler can see they're busy and skip the occurrence instead of
        # pinging then failing.
        self.game_busy_checks: dict[str, GameBusyCheck] = {}
        # Mid-game roster handlers, keyed by game_type. Roster-based games
        # register async add/remove callbacks here in setup(); /games join and
        # /games leave dispatch to them so people can join or leave a game
        # that's already running.
        self.game_joiners: dict[str, GameRosterHandler] = {}
        self.game_leavers: dict[str, GameRosterHandler] = {}
```

Everything else in the class (`setup_hook`, `_warn_on_extension_drift`, etc.)
is untouched. The extension-drift warning and setup_hook behavior must not
change.

**Decision — GamesDb wiring:** `games_db` stays a class-level annotation set
by the entry point (mirroring the existing `ctx: AppContext` pattern), because
`GamesDb` needs `db_path`, which lives in config loaded inside `main()`.
`Bot.__init__` keeps its current `(intents, debug, guild_id)` signature —
no constructor churn.

### 2.2 `src/dungeonkeeper/__main__.py` wiring (lines 155–175)

Replace this block:

```python
    bot.ctx = ctx
    bot.games_db = GamesDb(db_path)  # type: ignore[attr-defined]
    bot.active_views: dict = {}  # type: ignore[attr-defined]
    ... (all comment lines) ...
    bot.game_joiners: dict = {}  # type: ignore[attr-defined]
    bot.game_leavers: dict = {}  # type: ignore[attr-defined]
```

with exactly:

```python
    bot.ctx = ctx
    bot.games_db = GamesDb(db_path)
```

The six registry assignments and their explanatory comments are deleted — the
registries are now created in `Bot.__init__` and the comments moved there
(Edit D). The `bot.extension_names = [...]` list that follows is untouched.
The `from bot_modules.services.games_db import GamesDb` import at line 32
stays.

### 2.3 Now-dead `type: ignore`s on registry writes (same stage)

- `src/bot_modules/cogs/risky_roll_cog.py:415-416` — delete the trailing
  `  # type: ignore[attr-defined]` from both lines (keep the assignments):

  ```python
      bot.game_launchers["risky_roll"] = cog.launch
      bot.game_busy_checks["risky_roll"] = cog.channel_has_active_round
  ```

- `src/bot_modules/duels/base_game.py:63` — change

  ```python
        return self.bot.games_db  # type: ignore[attr-defined]
  ```

  to

  ```python
        return self.bot.games_db
  ```

  (this file already types `bot: Bot` since commit `6dfe993`).

Do NOT touch `scheduled_games_service.py`'s defensive
`getattr(bot, "game_busy_checks", {})` / `hasattr(bot, "game_launchers")`
reads — behavior-preserving pass; tightening them is follow-up.

### 2.4 Alias retirement (Stage 2) — per-file edits

All four aliases are the same object as `bot.ctx`, so every read becomes a
`bot.ctx` read. Access pattern for interaction callbacks (which only see
`interaction.client: discord.Client`): `cast("Bot", interaction.client).ctx`
— except voice_master, where tests inject MagicMock clients and rely on a
None fallback, so it keeps duck-typed `getattr` (see below).

**`src/dungeonkeeper/__main__.py` (line 261):** delete the line

```python
    bot._booster_ctx = ctx  # type: ignore[attr-defined]
```

Keep the preceding comment ("Register persistent booster-role buttons…") and
the `bot.add_dynamic_items(BoosterRoleDynamicButton)` line.

**`src/bot_modules/services/booster_roles.py`:**

- Line 12: `from typing import TypedDict` → `from typing import TYPE_CHECKING, TypedDict, cast`
- After the import block (after `from bot_modules.core.db_utils import ...`), add:

  ```python
  if TYPE_CHECKING:
      from bot_modules.core.app_context import Bot
  ```

- Line 200: change

  ```python
        ctx = interaction.client._booster_ctx  # type: ignore[attr-defined]
  ```

  to

  ```python
        ctx = cast("Bot", interaction.client).ctx
  ```

**`src/bot_modules/cogs/jail_cog.py` (line ~289-290):** delete both lines:

```python
        # Store ctx on bot so persistent view callbacks can reach it
        bot._mod_ctx = ctx  # type: ignore[attr-defined]
```

**`src/bot_modules/commands/jail_commands.py`:**

- Line 15: `from typing import TYPE_CHECKING` → `from typing import TYPE_CHECKING, cast`
- Line 63-64 TYPE_CHECKING block: add `Bot`:

  ```python
  if TYPE_CHECKING:
      from bot_modules.core.app_context import AppContext, Bot
  ```

- Six sites (lines 426, 514, 815, 1002, 1152, 1639), each preceded by
  `bot = interaction.client`. At every site change

  ```python
      ctx: AppContext = bot._mod_ctx  # type: ignore[attr-defined]
  ```

  to

  ```python
      ctx: AppContext = cast("Bot", bot).ctx
  ```

  (preserve each site's indentation; leave the `bot = interaction.client`
  lines and the `# Get ctx from bot` comment at 425 alone). Verify exactly 6
  replacements: `grep -c '_mod_ctx' src/bot_modules/commands/jail_commands.py`
  must return 0 afterwards.

**`src/bot_modules/cogs/pen_pals_cog.py`:**

- Line 11: `from typing import TYPE_CHECKING` → `from typing import TYPE_CHECKING, cast`
  (the TYPE_CHECKING block at line 21-22 already imports `AppContext, Bot`).
- Lines 673 and 771: change

  ```python
        ctx = interaction.client._pen_pals_ctx  # type: ignore[attr-defined]
  ```

  to

  ```python
        ctx = cast("Bot", interaction.client).ctx
  ```

- Line 879 (in `cog_load`): delete the line

  ```python
        bot._pen_pals_ctx = self.ctx  # type: ignore[attr-defined]
  ```

**`src/bot_modules/cogs/voice_master_cog.py` (lines 146-148):** delete the
comment + setattr:

```python
        # Expose the AppContext to panel DynamicItem callbacks (which only
        # see ``interaction.client``). Mirrors the jail cog's _mod_ctx pattern.
        setattr(self.bot, "_vm_ctx", self.ctx)
```

**`src/bot_modules/commands/voice_master_commands.py`:** keep the duck-typed
getattr shape (tests inject `MagicMock` clients, and two code paths depend on
a None fallback), but read the canonical attribute:

- Line 94-95: change `_ctx_from_interaction` to

  ```python
  def _ctx_from_interaction(interaction: discord.Interaction) -> "AppContext | None":
      # DynamicItem callbacks only see ``interaction.client: discord.Client``;
      # duck-typed getattr (rather than a cast) keeps the None fallback and
      # lets tests inject fake clients.
      return getattr(interaction.client, "ctx", None)
  ```

- Line 1826: change

  ```python
        ctx = getattr(interaction.client, "_vm_ctx", None)
  ```

  to

  ```python
        ctx = getattr(interaction.client, "ctx", None)
  ```

  (the `if ctx is None:` guard below it stays).

**Tests (rename the injected attribute, nothing else):**

- `tests/test_voice_master_cog.py` line 95: `setattr(inter.client, "_vm_ctx", ctx)`
  → `setattr(inter.client, "ctx", ctx)`
- `tests/test_voice_master_commands_glue.py`: 11 occurrences of
  `setattr(inter.client, "_vm_ctx", ...)` (one `ctx`, ten `None`) — replace
  `"_vm_ctx"` with `"ctx"` in all of them.
  `grep -c '_vm_ctx' tests/` must return 0 matches afterwards (ignore
  `__pycache__`).

Note: `tests/cogs/test_todo_cog.py`'s `_build_mod_ctx()` is just a helper
function name, not an alias reference — do not touch it.

### 2.5 Games-cog `bot: Bot` annotations (Stage 3)

Pattern from commit `6dfe993`, adapted: these files do **not** have
`from __future__ import annotations` (verified; only `games_dev_cog.py` does),
so use **quoted** annotations — do not add the future import.

For each of the 19 files
`games_ama_cog.py, games_clapback_cog.py, games_compliment_cog.py,
games_config_cog.py, games_dev_cog.py, games_fantasies_cog.py,
games_ffa_cog.py, games_hottakes_cog.py, games_mfk_cog.py, games_mlt_cog.py,
games_nhie_cog.py, games_photo_cog.py, games_price_cog.py,
games_rushmore_cog.py, games_session_cog.py, games_story_cog.py,
games_traditional_cog.py, games_ttl_cog.py, games_wyr_cog.py`
(all under `src/bot_modules/cogs/`):

1. Add to the import block (skip pieces that already exist):

   ```python
   from typing import TYPE_CHECKING

   if TYPE_CHECKING:
       from bot_modules.core.app_context import Bot
   ```

   If the file already imports from `typing`, extend that line instead of
   adding a second one. In `games_dev_cog.py` (has future annotations) bare
   `Bot` is fine; elsewhere annotations must be quoted.

2. Replace both `commands.Bot` annotations (cog `__init__` param and module
   `setup()` param) with `"Bot"`, e.g.:

   ```python
   def __init__(self, bot: "Bot"):
   ...
   async def setup(bot: "Bot"):
   ```

   Keep any existing `-> None` returns exactly as found. `games_help_cog.py`
   and `games_legitlibs/__init__.py` have no `commands.Bot` annotations —
   leave them alone unless grep shows otherwise at implementation time.

These files are pyright-excluded today, so this stage's payoff only shows in
the Stage 3 measurement — but the edits must still compile and import cleanly.

## 3. Stage plan (Sonnet implementers — follow verbatim)

All commands run from `/home/ben/discord-bots/dungeon-keeper`. Each stage is
independently committable; commit at the end of each stage.

### Stage 1 — Typed registries on Bot

**Files:** `src/bot_modules/core/app_context.py`, `src/dungeonkeeper/__main__.py`,
`src/bot_modules/cogs/risky_roll_cog.py`, `src/bot_modules/duels/base_game.py`

**Edits:** §2.1 (Edits A–D), §2.2, §2.3 — exactly as written.

**Verification (all must pass):**

```bash
.venv/bin/python -m py_compile src/bot_modules/core/app_context.py src/dungeonkeeper/__main__.py src/bot_modules/cogs/risky_roll_cog.py src/bot_modules/duels/base_game.py
.venv/bin/ruff check src tests
.venv/bin/pyright          # expect: 0 errors, 0 warnings
.venv/bin/python -m pytest -q   # expect: all pass, no new failures
PYTHONPATH=src .venv/bin/python -c "
import discord
from bot_modules.core.app_context import Bot
b = Bot(intents=discord.Intents.none(), debug=False, guild_id=0)
assert b.active_views == {} and b.game_launchers == {} and b.game_recoverers == {}
assert b.game_busy_checks == {} and b.game_joiners == {} and b.game_leavers == {}
print('Bot registries OK')
"
PYTHONPATH=src .venv/bin/python -c "import bot_modules.games.utils.recovery; import bot_modules.services.scheduled_games_service; print('imports OK')"
```

**STOP conditions:** pyright reports any error mentioning `RecoverySentinel`,
`GamesDb`, or an import cycle → abort and report (the TYPE_CHECKING guard
should prevent this; do not improvise runtime imports). Any pre-existing test
starts failing → abort and report; do not modify tests in this stage.

**Commit:** `Typed Bot: registries become initialized attributes (S2-god #11, stage 1)`

### Stage 2 — Retire the four _xxx_ctx aliases

**Files:** `src/dungeonkeeper/__main__.py`, `src/bot_modules/services/booster_roles.py`,
`src/bot_modules/cogs/jail_cog.py`, `src/bot_modules/commands/jail_commands.py`,
`src/bot_modules/cogs/pen_pals_cog.py`, `src/bot_modules/cogs/voice_master_cog.py`,
`src/bot_modules/commands/voice_master_commands.py`,
`tests/test_voice_master_cog.py`, `tests/test_voice_master_commands_glue.py`

**Edits:** §2.4 exactly as written.

**Verification:**

```bash
grep -rn "_booster_ctx\|_mod_ctx\|_pen_pals_ctx\|_vm_ctx" src tests --include='*.py' | grep -v _build_mod_ctx
# expect: no output
.venv/bin/python -m py_compile src/dungeonkeeper/__main__.py src/bot_modules/services/booster_roles.py src/bot_modules/cogs/jail_cog.py src/bot_modules/commands/jail_commands.py src/bot_modules/cogs/pen_pals_cog.py src/bot_modules/cogs/voice_master_cog.py src/bot_modules/commands/voice_master_commands.py
.venv/bin/ruff check src tests
.venv/bin/pyright          # expect: 0 errors
.venv/bin/python -m pytest -q   # expect: all pass — pay attention to test_voice_master_*
```

**STOP conditions:** any voice_master test fails after the `"_vm_ctx"` →
`"ctx"` rename → abort and report (do not "fix" tests by reverting prod code
piecemeal). The grep above finds a leftover alias reference in a file not
listed here → abort and report the file (means the verified inventory was
incomplete).

**Commit:** `Typed Bot: retire _booster/_mod/_pen_pals/_vm ctx aliases for bot.ctx (stage 2)`

### Stage 3 — Games-cog Bot annotations + payoff measurement

**Files:** the 19 `games_*` cogs listed in §2.5; this plan doc (record the number).

**Edits:** §2.5, then run the measurement in §4 and fill in the
"Measured payoff" line below.

**Verification:**

```bash
.venv/bin/python -m py_compile src/bot_modules/cogs/games_*.py
grep -rn "commands.Bot" src/bot_modules/cogs/games_*.py
# expect: no output
for m in ama clapback compliment config dev fantasies ffa hottakes mfk mlt nhie photo price rushmore session story traditional ttl wyr; do
  PYTHONPATH=src .venv/bin/python -c "import bot_modules.cogs.games_${m}_cog" || echo "IMPORT FAILED: $m";
done
.venv/bin/ruff check src tests
.venv/bin/pyright          # expect: 0 errors (games still excluded)
.venv/bin/python -m pytest -q
git diff -- pyproject.toml   # expect: empty (excludes restored after measurement)
```

**STOP conditions:** any cog import fails (quoted-annotation mistake or a
file without the TYPE_CHECKING import) → abort and report which file. The
measurement (§4) yields a number wildly off ~385 (say, outside 340–430) →
still restore pyproject, record the actual number, and flag it in the report
rather than "fixing" anything.

**Commit:** `Typed Bot: games cogs annotate bot: Bot; record pyright payoff (stage 3)`

## 4. Pyright payoff measurement (part of Stage 3)

1. Temporarily empty the exclude list in `pyproject.toml` `[tool.pyright]`:
   change the three-entry `exclude = [...]` block to `exclude = []`.
2. Run and record:

   ```bash
   .venv/bin/pyright --outputjson | tail -8   # read summary.errorCount
   ```

3. Restore immediately: `git restore pyproject.toml` (pyproject has no other
   working-tree changes; verify with `git status --short pyproject.toml`).
4. Record here — **do not** leave the excludes lifted; un-excluding games is
   explicitly out of scope this pass:

   - Baseline before refactor (excludes lifted): **575 errors**
   - Expected after refactor: **~385 errors** (−166 registry-attr, −24 bot.ctx)
   - Measured after Stage 3: **411 errors**

## 5. Risks & mitigations

- **Import cycle from app_context → games code.** Mitigated: `RecoverySentinel`
  and `GamesDb` are imported under `TYPE_CHECKING` only; `app_context.py` has
  `from __future__ import annotations`, so no runtime evaluation. (`GamesDb`'s
  chain — only `db_utils` — and `recovery`'s chain — `game_manager` →
  stdlib/discord — were both verified cycle-free anyway.)
- **Cog load order.** Cog `setup()` functions write into the registries when
  extensions load in `setup_hook()`. Previously the registries were created in
  `main()` before `bot.run()`; moving creation into `Bot.__init__` makes them
  exist strictly *earlier*, so ordering can only improve. `bot.ctx` and
  `bot.games_db` are still assigned in `main()` before `bot.run()` (hence
  before `setup_hook`), same as today.
- **Protocol vs. bound methods.** `GameLauncher` is matched structurally by
  bound methods with untyped `channel`/bare `dict` params (implicit-Any params
  are assignable). The only registration sites pyright checks today are
  `risky_roll_cog.py` (its `launch` signature matches keyword-for-keyword —
  verified) and `duels/base_game.py`. If a games-excluded file has a deviant
  signature, it surfaces only when excludes are lifted (follow-up), not now.
- **Voice-master None fallback.** Tests exercise the "no ctx" path with
  MagicMock clients; that's why `_ctx_from_interaction` keeps `getattr`
  instead of a cast. The attribute rename `_vm_ctx` → `ctx` is mirrored in
  the two test files in the same stage, so the suite stays green.
- **Behavioral identity of alias retirement.** Every alias was verified to be
  the same object as `bot.ctx` (all three cog setups receive `bot.ctx`;
  `_booster_ctx` gets the `ctx` local later assigned to `bot.ctx`), so reads
  return the identical object. `cast()` is a no-op at runtime.
- **Stale-pyc surprises in tests.** `tests/__pycache__` contains compiled
  refs to `_vm_ctx`; pytest recompiles automatically — ignore pyc grep hits.

## 6. Rejected alternatives

- **`GameRuntime` grouping object** (`bot.games.active_views`, …): conceptually
  cleaner ownership, but renames 200+ call sites (146 for `active_views`
  alone) — churn forbidden this pass. An *additive* property view was also
  skipped: two names for one dict invites drift. Revisit as follow-up.
- **`Bot.__init__(db_path=...)` constructing GamesDb:** changes the
  constructor signature and moves config knowledge into the Bot class for no
  type-safety gain over the class-annotation pattern already used by `ctx`.
- **Runtime import of RecoverySentinel in app_context:** works (chain is
  cycle-free) but makes core import games code forever; TYPE_CHECKING is free.
- **`isinstance(client, Bot)` in voice_master `_ctx_from_interaction`:**
  most honest typing, but breaks 12 MagicMock-based tests (fakes aren't Bot
  instances) and would silently disable panels in tests; getattr keeps exact
  behavior.
- **Typing `channel`/`member` params tightly in the registry aliases:**
  channel unions are a 174-error follow-up category; tight types here would
  mint new errors at scheduler call sites in this pass.

## 7. Follow-ups (out of scope)

- The ~385 remaining hidden errors once games excludes are lifted:
  channel-union narrowing (~174), View/Item typing (~55), other (~156).
- Un-excluding `games_*` / `games_legitlibs` / `bot_modules/games` in
  `[tool.pyright]` once those are burned down.
- Tighten `scheduled_games_service.py`'s defensive
  `getattr(bot, "game_launchers", {})` / `hasattr` reads to direct typed
  access (registries now always exist).
- Optional `GameRuntime` grouping (additive property or real move) for
  conceptual ownership of the six registries.
- `bot_modules/bios/views.py:59` and `whisper_cog.py:786` carry unrelated
  `attr-defined` ignores — separate cleanup.
