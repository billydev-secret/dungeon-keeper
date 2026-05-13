# DungeonKeeper — Overcomplication Audit

**Date:** 2026-04-14
**Scope:** Full repo (~45K Python LOC + ~12K JS LOC)
**Method:** Three parallel review agents covering Python backend, web layer, and cross-cutting architecture. Findings consolidated and deduplicated below.

---

## Executive Summary

The repo is **not bloated in volume** but has **architectural entropy** from fast iterative development. Roughly **8000 LOC** can be removed without behavior change through mechanical refactors (parametric routes, shared panel helpers, dedup). A further **~5000 LOC** and significant clarity improvements come from three structural consolidations (reports split, scheduler unification, schema registry).

Hot spots:
- `reports.py` / `services/reports_data.py` / `web/routes/reports.py` — triple-layered reporting ownership.
- 59 panel JS files with copy-pasted mount/fetch/render scaffolding.
- 1000+ line route files (`health.py`, `reports.py`, `config.py`) that are N near-identical endpoints.
- 18 scattered `init_*_tables()` calls at startup; 146 MB SQLite file (worth investigating independently).
- Wellness subsystem spread across 6 service files + 2 command files with 3 independent scheduler loops.

---

## Top 5 Structural Problems

### 1. Triple reporting system (High severity / Medium effort)
- `reports.py` (1305L, top-level) — command handlers **and** query logic.
- `services/reports_data.py` (1623L) — query layer.
- `web/routes/reports.py` (1094L) — HTTP routes.

Both `reports.py` and `web/routes/reports.py` import from `reports_data`, but `reports.py` mixes responsibilities.

**Action:** Move command handlers from `reports.py` → `commands/reports_commands.py`. Keep `services/reports_data.py` as the single query source. Routes and commands both call into it; neither duplicates queries.

### 2. Route explosion in large route files (High / Easy)
- `web/routes/health.py` (1153L): ~14 `@router.get("/health/{tile}")` endpoints — each fetches compute fn → caches → resolves names → returns JSON.
- `web/routes/reports.py` (1094L): ~18 endpoints calling `reports_data.X()` → resolve names → return schema.
- `web/routes/config.py` (923L): ~14 PUT/DELETE endpoints reading helpers (`_id_set_list`, `_int_val`, `_str_val`) and validators.

**Action:** Collapse each to a parametric endpoint + dispatch dict. Estimated: health.py → ~300L, reports.py → ~250L, config.py → ~200L. **~2000 LOC saved.**

### 3. Panel frontend boilerplate (High / Medium)
59 panel files in `web/static/js/panels/` share identical mount pattern: `innerHTML` setup → DOM refs → event listeners → async load → chart create/destroy → error render.

Also:
- `esc()` redefined locally in **17 panels** despite existing in `api.js`.
- `filterSelect()` dropdown hand-rolled in 4+ panels.
- 30+ panels reimplement `api(url,params).catch(...)` try-load-catch-render.

**Action:** Create `js/panel-base.js` (base class or helper bundle), export `esc()`/`fetchReport()`/`createFilterSelect()` from shared modules. **~5500 LOC saved.**

### 4. Scheduler/background-task sprawl (Medium / Medium)
Independent asyncio loops with their own logging and error handling:
- voice_xp_service
- auto_delete_service
- inactivity_prune_service
- wellness_scheduler (3 loops internally: tick, weekly_report, active_list)
- db_backup

**Action:** Unify under `services/scheduler.py` with a task registry: `scheduler.register(name, interval, coro)`. One cancellation path, one logger, one monitoring surface.

### 5. DB schema fragmentation (Medium / Easy)
- `dungeonkeeper.py:131-147` calls 18 separate `init_*_tables()` functions.
- 57 `CREATE TABLE` statements spread across service files.
- `dungeonkeeper.db` is **146 MB** — unusually large for this scope. Likely indicates missing indexes, un-pruned message logs, or orphan tables.

**Action:** Create `services/db_schema.py` that collects all schema statements with a version number and runs them at startup. Investigate the 146 MB size separately — run `.schema` + per-table `count(*)` and `page_count` to find the offender.

---

## Safe Deletions (verify imports before removing)

| Path | Evidence | Action |
|---|---|---|
| `mockup/` | Dead HTML prototypes, no imports | Delete |
| `post_monitoring.py` (86L) | No external importers found; spoiler logic subsumed by `handlers/events.py` | Delete after grep confirms |
| `dashboard.py` | Duplicates FastAPI app embedded in `dungeonkeeper.py:441-449` | Delete or move to `scripts/` as dev-only launcher |
| `log.txt` | Runtime artifact at repo root | `.gitignore` it |
| Root-level `*.md` specs (TGM-Dashboard, wellness_guardian, jail_ticket) | 107 KB of design docs at root | Move to `docs/` |

---

## Targeted Duplication Fixes

| Item | Locations | Target |
|---|---|---|
| `parse_duration` | `commands/wellness_commands.py`, `services/moderation.py` (most complete), `services/auto_delete_service.py` (`parse_duration_seconds`) | Move canonical version to `utils.py` |
| `_resolve_names()` / `_resolve_user_names()` | `web/routes/health.py`, `reports.py`, `moderation.py` | Extract to `web/helpers.py::resolve_names()` |
| Auth dependencies | `web/wellness_routes/deps.py` duplicates `web/deps.py` (`get_ctx`, `get_guild_id`, `get_current_user`) | Import from `web/deps.py`; keep only wellness-specific `require_user` |
| Single-call config wrappers | `commands/jail_commands.py:79-111` (`_get_mod_role_ids`, `_get_admin_role_ids`, `_get_config`) | Inline at 3-4 call sites |
| `channel_is_xp_allowed` | `services/xp_service.py:26-35` — 10-line wrapper around `is_channel_xp_eligible` | Inline |
| Logging setup | `dashboard.py:29-34` vs `dungeonkeeper.py:111-119` | Shared helper |
| Config loading | `dashboard.py:65-114` duplicates `app_context.py:84-170` (`load_runtime_config`) | Reuse loader |
| `_FLOAT_COEFFS`/`_INT_COEFFS`/`_TUPLE_*` lists in `xp_system.py:41-60` | Hand-maintained coefficient lists | Derive from dataclass field types |

---

## Routes / Web Layer Specifics

### Wellness vs. main routes split
`web/wellness_routes/` (api.py 845L + admin.py 254L) is mounted on the same FastAPI app as `web/routes/`. The split adds mental overhead with no isolation benefit.

**Action:** Move to `web/routes/wellness_*.py`. Single routes directory.

### Naming confusion: health vs. wellness
Three semantic uses of "health" in the codebase:
- `services/health_metrics.py` — observability metrics for the server (DAU/MAU, churn, etc.)
- `services/health_service.py` — caching layer for above
- Wellness — user-facing mental-health/checkin features

**Action:** Rename the metrics side to `metrics_*` to clarify. Dashboard section headers should match.

### Schema duplication
`web/schemas.py` (584L) exists but routes frequently hand-build response dicts instead of returning Pydantic models — 50/50 mixed mode.

**Action:** Pick one. If keeping schemas, enforce via response_model. Otherwise drop unused ones.

### CSS cleanup
`web/static/app.css` (2770L) contains dead selectors — `.widget-handle`, `.widget-remove`, `.widget-resize` (only edit-mode, rarely used), orphaned `.ai-*` classes if/when panels removed, duplicated `.home-card` styles.

**Action:** Audit 276 unique class names against JS/HTML usage; remove ~50 unused. Estimated reduction: ~470 LOC.

### App.js sidebar rendering
`app.js:242-295` — 54 lines of manual nav generation with 6 nested forEach loops.

**Action:** Template string + CSS `:has()` for collapse state instead of JS toggles. ~15 LOC.

### Widget grid + registry
`widget-grid.js` (317L) + `widget-registry.js` (98L) + `panels/home.js` form a "tiles" system (6 home + 15 health hardcoded) parallel to the hash-routed panels system — two render paths.

**Action:** Unify — make the home dashboard navigate to a `/home` panel. Merge registry data into `app.js` SECTIONS. ~400 LOC saved plus one fewer mental model.

---

## Python Backend Specifics

### Bloated files (not for deletion, but for navigability)
- `services/activity_graphs.py` (2576L) — mixes graph generation (legit ~500L) with matplotlib rendering boilerplate (~600L). Consider extracting `_hour_buckets`/`_day_buckets`/etc. to `bucket_helpers.py` and renderers to `graph_rendering.py`. Purely organizational.
- `services/health_metrics.py` (1583L) — genuinely complex; keep.
- `services/interaction_graph.py` (1483L) — heavy computation with thread pool; keep.

### Top-level monolith vs. modular services
`xp_system.py` (1207L top-level) contains data models, config loading, XP computation, and table init together. Coexists with `services/xp_service.py`.

**Action:** Split into `services/xp/{models,config,tables,compute}.py` subpackage. Or at minimum, move `init_xp_tables` to the consolidated schema registry above.

### Configuration sprawl
Config reads happen in 5+ places:
- `settings.py` (constants)
- `app_context.py` (`load_runtime_config`, 16 KB)
- Embedded defaults in `xp_system.py`, `auto_delete_service.py`, `wellness_service.py`

**Action:** `services/config_registry.py` that loads everything at startup, returns frozen dataclasses passed to services.

### Wellness subsystem
8 entrypoints:
- `services/wellness_service.py` (1828L)
- `services/wellness_scheduler.py`
- `services/wellness_enforcement.py` (620L)
- `services/wellness_ai.py`
- `services/wellness_partners.py`
- `commands/wellness_commands.py` (1649L)
- `commands/wellness_admin_commands.py` (913L)
- `web/wellness_routes/` (api + admin)

24 async loop functions across the scheduler alone; 3 independent loops calling same DB getters.

**Action:** Reorganize into `services/wellness/` subpackage: `core.py`, `scheduler.py`, `enforcement.py`, `ai.py`, `partners.py`. Single scheduler coroutine spawning staged tasks. Commands stay at top-level `commands/`.

### Handlers layer
`handlers/events.py` (648L) is deeply nested event-handler closures. Commands import from handlers, which import services, which may import commands — audit for cycles.

**Action:** Not urgent. If cycles appear, split per event type.

---

## Keep As-Is (load-bearing)

| What | Why |
|---|---|
| `services/activity_graphs.py` (2576L) | Matplotlib pipeline for 10+ chart types; separation is correct, just internally reorganizable |
| `services/health_metrics.py` (1583L) | Research-backed formulas for 12 health dimensions |
| `services/interaction_graph.py` (1483L) | Thread-pool graph layout; isolation is intentional |
| 21 command files in `commands/` | Good modular split by feature area |
| `tests/` (1935L) | Decent coverage; not a simplification target |
| Spec .md files | Load-bearing design docs (but relocate to `docs/`) |

---

## Rough LOC Impact

| Category | Estimated reduction |
|---|---|
| Parametric routes (health/reports/config) | ~2000 |
| Panel base + shared helpers | ~5500 |
| Widget grid/registry unification | ~400 |
| CSS cleanup | ~470 |
| `_resolve_names` consolidation | ~100 |
| Config wrappers / inlined helpers | ~100 |
| `parse_duration` / logging / auth deps dedup | ~200 |
| **Mechanical total** | **~8770 LOC** |
| Reports layer restructure | ~500-1000 (plus clarity) |
| Scheduler unification | ~200 (plus operability) |
| DB schema registry | neutral LOC (big clarity win) |

---

## Suggested Rollout Order

1. **Investigate the 146 MB DB first** — `.schema` + `SELECT name, page_count FROM dbstat GROUP BY name`. May surface bigger issues before refactoring around it.
2. **Safe deletions** (`mockup/`, `post_monitoring.py`, `dashboard.py`, log.txt). Low risk, immediate clarity.
3. **Dedup `esc()`, `parse_duration`, `_resolve_names`, config wrappers.** Pure mechanical, no behavior change.
4. **Parametric routes** one file at a time (start with `health.py` — simplest pattern).
5. **Panel base class** — extract helpers, migrate panels incrementally.
6. **Scheduler consolidation** — once stable, unify under one task registry.
7. **Reports layer split** — requires careful import audit.
8. **Schema registry + wellness subpackage** — largest churn, do last.

---

## Investigation Checklist (before fixing)

- [ ] `.gitignore` — is `log.txt` committed? Is `dungeonkeeper.db` in repo history (146 MB)?
- [ ] Grep `post_monitoring` repo-wide — confirm no importers.
- [ ] Grep `dashboard.py` — confirm it's not entrypoint for any script/service.
- [ ] Run the test suite baseline; capture current pass count.
- [ ] Check `widget-registry.js` — list registered tiles, confirm which ones are actually rendered.
- [ ] Run `sqlite3 dungeonkeeper.db "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 20"` for table size breakdown.
