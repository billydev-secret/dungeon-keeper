# `routes/config.py` simplification pass

**Branch:** `config-routes-simplify` · **Started:** 2026-07-30

## Why

`src/web_server/routes/config.py` is **5,157 lines — the largest file in the
repo**, and it grew 4,705 → 5,157 in a single week as features added config
sections (inactive sweep, the panel split, Pools). That growth rate is the
point: the file is a per-feature accretion point, and every new feature adds
four things to it (a `_<feature>_section` reader, a `<Feature>ConfigUpdate`
model, a PUT handler, a line in `get_config`).

This is the **second per-area `/simplify` pass**. The first was
`economy_cog.py` (merge `0985883b`, 4,560 → 4,000). `/simplify` is not a
whole-repo tool — at 333k LOC across 1,028 files the repo gets worked
largest-file-first, each area shipping its own reviewable commit.

Measured shape at the start of this pass:

| | count |
|---|---|
| lines | 5,157 |
| route decorators | 84 |
| functions | 127 |
| Pydantic models | 57 (only **1** sets `extra="forbid"`) |
| `_<feature>_section` helpers | 23 |
| `def _q():` closures → `await run_query(_q)` | 64 → 66 |
| `tests/web/test_config_routes.py` | 2,301 lines, 135 tests |

A four-angle review (reuse / simplification / efficiency / altitude) found the
duplication is exactly the shape the size predicted: near-identical section
helpers, near-identical models, and ~40 PUT handlers sharing one
"validate → `_q()` → `run_query`" skeleton. **The fix is a shared builder, not
clever rewriting.**

### Constraint that governs every stage

**Behaviour must not change.** No renamed JSON keys (the vanilla-JS dashboard
panels read them by name), no altered validation bounds, no changed defaults,
no changed HTTP status codes. Two repo sweeps are load-bearing here and must
keep passing:

- the **authz sweep** — every route rejects an unauthenticated caller, so no
  stage may drop a `Depends(require_perms(...))`;
- the **snowflake-precision sweep** — no Discord id > 2^53 returned as a bare
  JSON number. The scattered `str(...)` wrappers (e.g. `str(s.pools_channel_id)`)
  look like noise and are **not**. Stage 2 *strengthens* this by replacing 31
  hand-repeated `str()` calls with one named helper the sweep can point at.

Coverage floor in `pyproject.toml` is not lowered at any stage.

## Outcome (all six stages shipped 2026-07-30)

| after stage | lines | |
|---|---|---|
| baseline | 5,157 | |
| 1 — games tests | 5,157 | tests only |
| 2 — shared helpers | 5,068 | −89 |
| 3 — efficiency | 5,081 | +13 (the `*_with_conn` siblings) |
| 4 — duel games | 4,976 | −105 |
| 5 — field tables | 4,944 | −32 |
| 6 — section registry | 4,949 | +5 |
| **final** | **4,949** | **−208 (−4%)** |

**The line reduction came in well under the review's ~570-line estimate, and
that is worth recording rather than glossing.** The four agents counted gross
deletions; in practice the shared helper, the spec tables and the extracted
section functions cost back most of what the inline code gave up. Stage 6 in
particular is net *positive* on lines. The estimate was wrong in a predictable
direction — dedupe of this shape trades many duplicated lines for fewer,
denser, named ones, and the payoff is where you have to look to change
something, not the total.

What actually improved:

- `get_config` 243 → 70 lines, and now a flat registry of one-line section
  calls rather than half-registry, half-inline-blob.
- The six duel-game handlers 284 → 103 lines.
- `/api/config/games-*` went from **0 tests to 59**.
- `GET /api/config`: **4 connections → 2, 17.16 ms → 11.75 ms (−32%)**,
  measured best-of-15 on the same fixture guild.
- All **84 route decorators preserved** — no route added or lost, so the authz
  sweep's coverage is unchanged.

Behaviour equivalence was verified per stage by diffing the full `GET
/api/config` response (every key path, type, scalar value, and the top-level
key order) and, for stage 5, every row written to the `config` table, against
the pre-stage build. Identical every time.

## Stages

Each stage is one commit and references its stage number. Stage 1 must precede
stage 4; the rest are independent.

### Stage 1 — close the games coverage gap (tests only, no source change)

`grep -rn "config/games" tests/` returns **zero hits**. All six duel-game
routes — `/api/config/games-{pressure,quickdraw,hot-potato,hot-potato-group,chicken,musical-chairs}`,
~289 lines of handler — have **no test coverage at all**.

Add six round-trip tests (PUT → `GET /config` reflects the change) plus the
clamp boundaries, as `pytest.param` rows over one test function rather than six
near-identical functions. This is net-new regression safety regardless of the
rest of the pass, and it is what turns stage 4 from a medium-risk edit into a
mechanical one.

### Stage 2 — Tier A mechanical dedupe (~250–290 lines)

Behaviour-neutral, no new abstractions beyond four small helpers:

- `_id_str()` for the 23–25 sites spelling `str(_int_val(...))` across 3 lines
  (~50). One site takes a `fallback_key=` — `join_leave_log_channel_id`
  falls back to `leave_channel_id`.
- `_id_str_list()` for the 8 `[str(i) for i in _id_set_list(...)]` sites (~30).
- `replace_config_id_bucket()` in `core/db_utils.py` — beside the two
  primitives it composes — for the 9 `clear_config_id_bucket` + `add_config_id`
  loop sites (~20).
- `_resolve_text_channel()` for the repeated bot→guild→TextChannel 503/400
  preamble (~26). **Keep each call site's own status codes and message
  strings** — `config.py:1690` ("Bot is not connected to this guild.") and
  `:1737` ("Guild not available") differ deliberately and are API surface.

Plus: the six identical `_DUEL_GAMES["shared_fields"]` tuples and the
now-always-true guard at `_duel_game_section` (~24); `allow_legacy_fallback`
kwarg on the four `_*_val` helpers so `_bulk_cleanup_section` stops hand-rolling
coercion (~30, default preserves all 60+ existing callers); merging the
confessions block/unblock twins (~17); one `ChannelIdBody` for the four
identical single-field models (~9); shared starboard defaults (~8);
`delete_config_value` for the 2 raw `DELETE FROM config` statements (~6);
`ctx.open_db()` for the 9 `open_db(ctx.db_path)` stragglers; and the two
genuinely dead statements — the redundant `get_config_value` re-import at
`:228` and `import time as _time` at `:884`, both shadowing module-level
imports.

### Stage 3 — measured efficiency fixes

Instrumented baseline for one `GET /api/config`: **4 connections, 285 SQL
statements, ~17.9 ms**.

- **C1 (the real win).** Inside `get_config`'s `_q()`, `_get_prune_rule(ctx.db_path, …)`
  and `get_prune_exception_ids(ctx.db_path, …)` each open a *second and third*
  connection while `conn` is already held. The connect is cheap; the five
  PRAGMAs `open_db` runs are not — **~4.8 ms of an ~18 ms request (27%)** for
  two single-row queries the held connection answers in ~20 µs. Add
  `*_with_conn` siblings to `inactivity_prune_service.py`; the suffix is
  already this file's convention (`get_dms_config_with_conn`,
  `get_rungs_for_guild_with_conn`, `list_auto_delete_rules_for_guild_with_conn`).
- **C2.** `_swatch_listing` does 2 `mkdir` + 3 `listdir` of the same directory
  and may open its own DB; nine `async def` handlers touch the DB without
  `run_query`, blocking the event loop instead of going to a thread.

**Deliberately skipped:** batching the 109 per-key config SELECTs into one
snapshot. Measured payoff is only ~1.2 ms of 17.9 ms — 4× smaller than C1 — at
medium risk, because the `allow_legacy_fallback=False` variants must be
preserved exactly or `_bulk_cleanup_section` silently starts inheriting guild-0
values. Not worth it.

### Stage 4 — duel-game handler consolidation (~93–160 lines)

The 12-line shared-tier clamp block is byte-identical six times
(`cooldown_hours`, `sentence_hours`, `channel_allowlist`, `max_nick_length`,
`max_stakes_length`), and the same five fields are re-declared in all six
Pydantic models. Extract `_duel_shared_updates(body)` and a
`DuelSharedConfigUpdate` base; fold per-game clamp floors into the existing
`_DUEL_GAMES` table.

**Keep the six models and six concrete routes** — FastAPI needs a concrete body
annotation per route, and generating them in a loop is a fight with no payoff.
Every bound moves into the table verbatim.

### Stage 5 — the `set_config_value` field table (~100 lines)

31–42 handler blocks are a bare `if body.X is not None: set_config_value(...)`,
consuming ~170 lines at deep indentation. **This file already invented the fix
and never generalised it** — `update_welcome:1439` and `update_moderation:2117`
both build a local `{field: config_key}` dict and loop.

Hoist one `_apply(conn, guild_id, body, spec, setter=set_config_value)` where
`spec` is `{field: (config_key, coerce)}`. The `setter=` parameter absorbs
`set_guess_config_value` / `set_whisper_config_value`, whose argument order
differs. Every clamp and coercion moves verbatim, including
`str(int(body.sfw_log_channel_id or 0))` and the `.strip()` calls. Write order
within a handler becomes mapping order — all writes are independent upserts on
distinct keys, so nothing observable changes.

### Stage 6 — `get_config` symmetry + XP coefficient table (~120 lines)

`get_config` is half-registry: ~31 lines already delegate to `_*_section`
helpers, but 12 more sections (`global`, `privacy`, `welcome`, `intake`, `xp`,
`spoiler`, `auto_role`, `moderation`, `roles`, `booster_roles`, `prune`,
`auto_delete`) are still spelled out inline across ~250 lines at 16–24 spaces of
indent. Extract them into helpers matching the file's own established
convention. The hoisted-local preamble exists *only* because those blocks are
inline and dissolves with them.

JSON key **order** changes; panels read by name so this is safe, but it makes
the diff hard to eyeball — transcribe keys exactly, including the
`**_xp_coefficients(conn, guild_id)` splat and the `prune` fallbacks.

Also fold the XP coefficients, currently written out three times in this file
(`_xp_coefficients`, `XpConfigUpdate`, `_COEFF_FIELDS`), into one table.
**Do not** dedupe these against `xp_system.load_xp_settings` — that function
silently discards a malformed tuple and falls back to the default, where
`_xp_coefficients` returns the stored value verbatim. A guild with a bad stored
`xp_coeff_cooldown_thresholds_seconds` would see its dashboard display change.
That is a behaviour change and is out of scope.

## Found and deliberately NOT changed

Recorded so they don't get re-litigated, and because two are genuine issues
that want their own decision rather than a quiet fold-in:

- **~12 manual bounds checks duplicate `Field(ge=…, le=…)`** used elsewhere in
  the same file. Converting them changes the rejection from **400 with a plain
  `detail` string** to Pydantic's **422 with a different body shape**.
  `update_guess_config` carries a comment documenting exactly why the status
  matters ("the panel treats any 2xx as a successful save"). **This duplication
  is load-bearing.**
- **`extra="forbid"` on 1 of 57 models.** It is the convention in `economy.py`,
  `economy_manager.py` and `announcements.py`. Adding it to the other 56 would
  turn a stale dashboard field into a 422 on save — a deliberate decision, not
  a quality-pass fold-in.
- **Real drift:** `settings_registry.py:240` declares
  `greeting_watch_window_minutes` with `minimum=1, maximum=1440`;
  `config.py:2820` stores it with **no clamp at all**. Genuine inconsistency;
  fixing it is a behaviour change.
- ~~**Possible bug, for `/code-review` not `/simplify`:** of the four "post a
  panel to a channel" routes, only `post_dms_panel` prechecks send/embed
  permissions. The other three can post into a channel the bot cannot write to
  and report success.~~ **Investigated 2026-07-30 — the claim was wrong.**
  Struck rather than deleted, because the correction is the useful part: an
  agent finding relayed without tracing the downstream calls was wrong in both
  directions at once, overstating three routes and missing the real defect.
  *None* of the four reports success on failure — `setup_inactive_channel`
  catches `Forbidden`/`HTTPException` and returns `(False, reason)` → a clean
  400 with a helpful message, and `_send_confess_launcher` catches
  `HTTPException` → `False` → a 500 telling the admin to check permissions.
  The actual defect was narrower and worse than "reports success":
  `post_or_update_booster_panel` **deletes every existing panel message before**
  its three unguarded `channel.send` calls, so a channel missing Send Messages
  destroyed the panel outright, returned a bare 500, and left every repost
  failing the same way. Fixed by `_require_post_permissions()`, applied
  *before* the destructive call and shared with `post_dms_panel`, whose inline
  copy it replaces. Two tests, the first written to fail first.
- **`_casino_section`'s 30 hand-listed fields** are a deliberate allow-list (its
  docstring says `panel_*` bookkeeping is excluded on purpose). A generic
  `asdict()` + stringify would let a future dataclass field silently leak into
  the API.
- **`static/js/config-helpers.js:70`** — `loadConfig()` has **no cache guard**,
  unlike its three siblings `loadChannels`/`loadRoles`/`loadCategories` directly
  below it. 53 call sites across ~35 panels, so clicking through five config
  panels runs the full 41-section aggregate five times. This is the multiplier
  that makes C1 worth fixing, but a correct fix needs real
  invalidation-on-every-PUT, not a blanket guard. Out of scope for this file.

## Deferred — Tier D (needs its own plan)

Larger structural moves, all out of scope here:

1. **Package split** — `routes/config/` with per-area modules sharing one
   router (`_router.py` holding the `APIRouter` to avoid an `__init__` cycle).
   `server.py` needs zero changes; only 7 test couplings exist repo-wide, all
   satisfiable from `__init__`. **Much cleaner after this pass than before it** —
   splitting today would just relocate the duplication into six files. One real
   hazard: a module missing from `__init__` silently drops its routes and the
   authz sweep won't catch it, so the split must land with a test asserting the
   registered `/api/config*` route count.
2. **Registry unification.** `src/bot_modules/services/settings_registry.py`
   already exists (523 lines, 18 `Feature`s declaring keys, labels, defaults,
   bounds and enums) — but it is consumed by `channel_health` and the advisor,
   **not** by `config.py`, so the same key inventory is declared 2–4× and
   drifts (the `greeting_watch_window_minutes` case above). A two-tier registry
   with `custom_reader=` escape hatches would remove ~1,000 lines from
   `config.py`; ~13 of 23 sections fit cleanly and ~10 genuinely do not
   (`_bot_identity_section` reads gateway state, `_bump_tracker_section`
   computes live countdowns, `_voice_transcription_section` probes host
   capability, several join ids to Discord display names). **High risk** —
   touches every panel's data source at once.
3. **Cog-private imports → proper stores.** This file imports 22 underscore-private
   functions from cogs (`needle_cog._upsert_channel`, `bump_tracker_cog._add_site`,
   `pen_pals_cog._get_config`, …). All are pure sync sqlite3 CRUD with no Discord
   dependency; they belong in `services/*_store.py`. An underscore name carries
   no compatibility promise, so a cog author refactoring one has no signal that
   the dashboard breaks.
4. **~460 lines of business logic** that belongs in services per CLAUDE.md's
   glue-layer rule: the avatar SSRF guard (the repo's *only* one), intake step
   canonicalisation, quote-border image analysis, prune preview selection, the
   NSFW metrics SQL, and the leap-day birthday math.
