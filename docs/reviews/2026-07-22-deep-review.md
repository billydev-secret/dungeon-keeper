# Dungeon Keeper — Deep Review

**Date:** 2026-07-22
**Scope:** Whole-system audit split across two methodologies — **Backend methodology** and **UX** — covering all bot modules and the dashboard except `rules_watch`, which is **excluded**: it received its own dedicated deep-dive on 2026-07-20 and is out of scope here to avoid duplicate effort. **525 commits** landed between the 2026-07-01 review and this one, heavily concentrated in economy/quests/casino (three brand-new god-scale files — `economy_cog.py`, `economy_quests_service.py`, `economy_loop.py` — plus the casino, chat_revive, hidden_channels, inactive, qa, docs, and role_menus features, most of which didn't exist or were minimal at the last review).
**Type:** Audit only. No code or docs were changed. Every finding cites `file:line` evidence; recommendations are captured for follow-up.
**Method:** Parallel subagent workstreams, each starting from the 07-01 report as a baseline and confirmed recon seeds, verifying live, then sweeping for siblings. Two tracks: **Backend methodology** (async/blocking-IO, service architecture & god-modules, test coverage, safety/consent gates, security & authz, migrations hygiene, CI/tooling hygiene) and **UX** (deep passes on economy-core, economy-quests, casino, announcements, chat-revive, hidden-channels/inactive, qa/role-menus/docs, games UX-consistency; spot-checks on moderation/health/reports and ten stable features; dashboard-wide a11y, mobile/responsive, dashboard-vs-Discord-config, and design-consistency sweeps).

**Finding volume:** 98 triaged findings — **66 new** (4×S1, 26×S2, 26×S3, 10×S4), **11 still open** from 07-01, **16 fixed** since 07-01, **5 not-applicable/positive** confirmations (clean sweeps with no defect).

---

## Severity tiers

- **S1 — Safety / correctness:** user-facing safety gap, data corruption, or a security exposure.
- **S2 — Architecture / balance / maintainability:** systemic risk, real balance/fairness defect, or misleading-to-the-point-of-broken.
- **S3 — UX / clarity / polish.**
- **S4 — Nit.**

---

## Executive summary — top findings

| # | Sev | Area | Finding |
|---|---|---|---|
| 1 | **S1** | Backend/Safety | **`role_menus` never re-validates a role's danger status after publication.** `_apply_outcome` only checks hierarchy on click — not `role_block_reason`/`is_dangerous` — so a role that becomes admin/manage_roles/ban_members-capable *after* menu publication stays self-grantable forever, with no re-check on click, periodic sync, or the dashboard's own health panel. Independently surfaced by **4 separate workstreams** — the strongest duplicate signal in the batch. |
| 2 | **S1** | Backend/Safety | **Truth-or-Dare has zero NSFW channel gate.** `games_traditional_cog.py` NSFW Truth/Dare categories have no `channel.is_nsfw()` check anywhere, contradicting the manual's explicit safety promise. The 07-01 fix (`b1551e6c`) touched every other NSFW-serving game but missed this one — see "Regressed / incomplete fixes" below. |
| 3 | **S1** | UX/Dashboard | **Grant Currency loses snowflake precision.** `economy-bank-manager.js` does `parseInt(picked, 10)` on a full Discord snowflake before POSTing as JSON, silently crediting a phantom/wrong wallet — the exact bug class that previously cost this guild its game/manager role and bank channel. |
| 4 | **S1** | UX/Dashboard | **AI Studio drops the NSFW tag on save.** "Add Selected to Bank" sends `category` instead of `tags` to `POST /api/games/bank`; the field is silently discarded, so NSFW-category AI-generated questions save with `tags:[]` and bypass the channel-based NSFW filter entirely. |
| 5 | S2 | Backend/Security | `GET /api/home` is gated with `require_perms(set())` — any authenticated member (no mod role) can pull per-member social-graph analytics, including live voice occupants by name, that equivalent endpoints elsewhere restrict to moderator+. |
| 6 | S2 | Backend/Architecture | `economy_cog.py` is now the **largest file in the repo** (167KB/4,278 lines, bigger than `config.py`), with raw SQL, eligibility/TTL-cache logic, and quest-claim math written directly in the cog instead of the existing service layer. |
| 7 | S2 | Backend/CI | `gate.py`'s mandatory-test hard-fail only matches `_logic.py`/`_service.py` filenames, silently exempting the codebase's now-dominant bare `logic.py`/`store.py`-per-feature convention (~19–30 files) from the test gate CLAUDE.md promises. |
| 8 | S2 | UX/Quests | Unresolved **git merge-conflict markers** (`<<<<<<<`/`=======`/`>>>>>>>`) are committed live into `docs/economy_spec.md`'s Dynamic Target Band section. |
| 9 | S2 | UX/Config philosophy | `/bank post-guide`, `post-leaderboard`, `post-shop` are staff-only Discord commands with **no dashboard equivalent at all** — the exact admin-config-on-Discord pattern CLAUDE.md says to retire, one day after sibling `role_menus` solved the identical problem entirely via dashboard. |
| 10 | S2 | Backend/Safety | `/penpals pair` lets a mod force two arbitrary members — who may never have opted into Pen Pals — into a private, potentially NSFW-flagged 1:1 channel; only the block list and active-session state are checked. |
| 11 | S2 | Backend/Architecture (still open, regressing) | `config.py` god-module has **grown, not shrunk**, since 07-01 (145,211 bytes/172 defs/72 endpoints vs. 107,146/134 baseline, +35.5%). |
| 12 | S2 | Backend/CI (still open) | Frontend JS/CSS lint genuinely runs now but still carries `continue-on-error: true`; the commit that added it explicitly deferred flipping to blocking "once it reports clean" — three weeks and ~500 commits later, still non-blocking. |
| 13 | S2 | UX/Accessibility | QA Tracker's new expandable board rows are click-only `<tr>` elements with no `tabindex`/`role`/`keydown`/`aria-expanded` — reintroduces the exact defect class the 07-01 review fixed for nav headers, in a brand-new, never-reviewed feature. |
| 14 | S2 | UX/Games | The dashboard's "Enabled on this server" toggle for Traditional Truth-or-Dare and FFA is **fully decorative** — neither cog reads the config it writes, so disabling a game from the dashboard doesn't stop it being played, violating CLAUDE.md's "never ship an unenforced toggle" rule. |

---

## Diff against 07-01

### Fixed since 07-01 (16)

| Area | Evidence | What changed |
|---|---|---|
| Safety-gates | `games/utils/consent_check.py:0` | Seed 1 (`check_consent()` hard-`True`) fully resolved — the entire consent subsystem was **deleted** rather than wired up; manual scrubbed in the same commit. |
| Safety-gates | `games_ama_cog.py:737` | Seed 2 (AMA host could force any member into the hot seat) fixed — "New Hot Seat" only offers members who tapped Volunteer. |
| Safety-gates / duels | `duels/base_game.py:346` | Seed 3 / M0-base-b (concurrent duel sentences corrupting nick reverts) fixed — a new sentence is refused while an active one exists, checked at both nick-submit and challenge/host-select time. |
| Web portal security | `server.py:171` | Fail-open auth fixed: missing `DISCORD_CLIENT_ID`/`SESSION_SECRET` now raises `RuntimeError` at boot instead of silently falling back to admin-to-everyone `OpenAuth`; `serve_forever()` also force-binds any residual `OpenAuth` to loopback. |
| Web portal security | `server.py:112` | Rate-limit bucket collapsing behind the Cloudflare loopback origin fixed: buckets now key on `CF-Connecting-IP`/`X-Forwarded-For`. |
| Safety-gates | `games/utils/question_source.py:355` | Seed 4 (NSFW served by default / clean-mode inoperative) fixed for every game the fix touched — `allow_nsfw` defaults `False`, sourced via `channel_allows_nsfw()`. **`games_traditional_cog.py` was missed** — see Regressed section. |
| Duels | `duels/base_game.py:168` | M0-base-d (leave/rejoin escapes an active nick sentence) fixed — `on_member_join` re-applies unexpired sentences. |
| Duels | `duels/filters.py:29` | M0-base-e (NFC vs NFKC homoglyph bypass) fixed — denylist matching now normalizes NFKC first. |
| CI & tooling | `pyproject.toml:69` | Pyright no longer excludes the games cog surface; CI/`gate.py` run pyright unqualified across the whole games tree. |
| Migrations hygiene | `migrations/__init__.py:96` | S3-migrate fixed: each migration's statements now commit atomically with the `schema_version` insert via explicit `BEGIN`/rollback. |
| Accessibility | `docs/reviews/2026-07-01-deep-review.md:200` | Seeds U1a–U1k all remediated: `aria-live` toasts, `role=dialog`+focus trap+Escape+focus return, real button groups replacing the tab-strip anti-pattern, full combobox ARIA, keyboard-operable nav headers/guild picker, `--ink-mute` darkened for AA contrast. |
| Mobile/responsive | `app.css:2745` | U2a fixed: `overflow-x:auto` added to `.table`/`.w-table`/`.rw-table`/`.hm-table` in the mobile media query. |
| Web portal security | `routes:0` | Route-authz coverage re-verified clean: every sensitive router (incl. new feature areas) gates every endpoint; no raw SQL interpolation, `esc()` applied consistently in new panels. |
| Mobile/responsive | `app.css:2784` | U2b fixed: blanket 16px `input`/`select`/`textarea` override stops iOS Safari zoom-on-focus. |
| Dashboard JS | `js/api.js:90` | U3b/c/d/f/g substantially fixed: raw `fetch()` bypass dropped 7 panels → 1, the 17-way copy-pasted `esc()` eliminated in favor of `api.js`, `apiPut`/`apiDelete` added as recommended. |
| Health dashboard | `routes/health.py:645` | Heatmap timezone offset and day-of-week off-by-one both fixed; no residual issue. |

### Still open from 07-01 (11)

| Area | Evidence | Status |
|---|---|---|
| Async-blocking-IO | `core/db_utils.py:18` | `open_db()` still does a synchronous `sqlite3.connect()` + 5 PRAGMAs with no `asyncio.to_thread` wrapping at the source; ~49 direct-in-async call sites confirmed still exist. |
| God-modules | `routes/config.py:1` | Still unsplit and has **grown** since 07-01 (+35.5% — see Executive Summary #11). Trend negative. |
| God-modules | `commands/jail_commands.py:1` | Part of the 07-01 mega-file cluster; has grown the most of that group (+38%, 59,794→82,617 bytes); none of the five cluster files has been split. |
| CI & tooling hygiene | `.github/workflows/test.yml:48` | JS/CSS lint job now genuinely runs but is still `continue-on-error: true` (see Executive Summary #12). |
| Web portal security | `auth.py:238` | On a bot guild-cache miss, still trusts login-time perms/roles instead of failing closed; a demoted/removed member keeps dashboard access until cookie expiry. |
| Web portal security | `server.py:182` | `SESSION_SECRET` still accepted from env with no minimum length/entropy check. |
| Web portal security | `auth.py:139` | Session cookie still embeds the live Discord OAuth access token in signed-but-unencrypted base64. |
| Migrations hygiene | `src/migrations:0` | Duplicate migration-number pairs grew from 2 to **9** during the build-out (not a correctness bug — dedupe key is filename — but the numbering scheme's readability purpose now fails on ~8% of files). |
| Dashboard design (U3a seed) | `economy-quests.js:1` | Inline-style sprawl got worse: repo-wide `style=` in panel/app JS grew 631→892; no CSS utility-class system was ever added. |
| Dashboard design (U1d/U3e seed) | `app.js:1` | The copy-pasted, ARIA-incomplete tab-strip pattern is entirely unaddressed — no shared `makeTabStrip()` helper exists; zero `role="tab"` usage repo-wide. |
| Web portal security | `routes/oauth.py:311` | `/logout` remains a CSRF-able plain GET (forced-logout impact only). |

### Regressed / incomplete fixes (1)

Not a true regression (nothing that passed 07-01 has since broken), but one 07-01 fix was **incomplete** in a way a reader should know about:

- **Seed 4's NSFW-default fix missed one game.** `question_source.py:355` fixed `allow_nsfw` defaults for the games commit `b1551e6c` touched — but `games_traditional_cog.py:149` (Executive Summary #2) has **zero** `channel.is_nsfw()` gate on its Truth/Dare NSFW content, meaning the manual's safety promise is still broken for this one game. Flagging as its own new S1 rather than reopening the fixed finding, since the fix itself is correct everywhere it was applied.

### New since 07-01 (66)

66 new findings landed, concentrated exactly where the commit history predicts: economy/quests (10), casino (6), announcements (4), chat_revive (6), hidden_channels/inactive (7), qa/role_menus/docs (9), games UX-consistency (7), plus cross-cutting backend findings in architecture/test-coverage/CI (9) and security (1) and safety (4), plus a11y/mobile/design sweep items (3) and migrations (1). Full detail is in the per-track sections below rather than duplicated here.

---

## Track A — Backend methodology

### A1. Async / blocking-IO

| Sev | Evidence | Finding | Recommendation |
|---|---|---|---|
| S2 | `services/economy_loop.py:1006` | `run_guild_rentals()`/`run_tick()` open the DB synchronously at 6 call sites directly inside async function bodies, no `asyncio.to_thread` — reintroducing the S1-loop anti-pattern right next to correctly-wrapped sibling closures in the *same file*. | Wrap in `asyncio.to_thread`, matching the pattern already correct three functions over. |
| S2 | `cogs/hidden_channels_cog.py:109` | `/hidden hide\|restore\|list` open the DB synchronously inside async handlers, contradicting `store.py`'s own documented contract ("Cog callers wrap these in `asyncio.to_thread`") and reintroducing a pattern a prior sweep (`e77acaa5`) had just cleaned up elsewhere. | Wrap all three handlers. |
| S2 (still-open) | `core/db_utils.py:18` | Source-level `open_db()` remains unwrapped; ~49 direct-in-async call sites still exist codebase-wide. | Converge on `aiosqlite`, or at minimum wrap at the source so callers can't forget. |
| S4 (not-applicable) | `services/economy_service.py:909` | Verified clean: the shared economy logic layer, casino cog, chat_revive, docs, role_menus, and qa all follow the correct `to_thread`-wrapping convention — zero new blocking-I/O-on-event-loop instances found in these areas. | — |

### A2. Service architecture & god-modules

| Sev | Evidence | Finding | Recommendation |
|---|---|---|---|
| S2 | `cogs/economy_cog.py:1381` | New 167KB/4,278-line god-cog (largest file in repo) with raw SQL, eligibility/TTL-cache logic, and quest-claim math written directly in the cog instead of `economy_service.py`/`economy_quests_service.py`. | Migrate logic to the service layer per the cogs-thin rule; this is the single biggest architecture regression of the cycle. |
| S2 | `cogs/economy_cog.py:1242` | 8 Discord embeds built inline in the cog instead of a per-feature `embeds.py` of pure `build_*` functions — the exact anti-pattern the 2026-07-21 embed style guide names by file, unfixed one day after the ruling. | Extract to `economy/embeds.py`. |
| S3 | `cogs/inactive_cog.py:99` | Sweep candidate-gathering/exclusion logic (bot/owner/admin/mod filtering) embedded directly in the cog rather than `inactive/logic.py`; untested — only the downstream pure selector is covered. | Move to logic module, add test. |
| S3 | `services/economy_quests_service.py:1` | New file (85.6KB) already more than double CLAUDE.md's ~40KB split guidance; logic is in the right layer but the file is a growing maintainability risk. | Split by concern (claim math / eligibility / community-goal repair) before it grows further. |
| S3 | `cogs/hidden_channels_cog.py:262` | The real hide/restore state machine (duplicate-hide guard, category creation, Forbidden/HTTPException handling, position-restore fallback) lives inline in the cog rather than a testable logic module — ships with zero automated coverage and bypasses `gate.py`'s hard-fail. | Extract to `hidden_channels/logic.py` with tests. |
| S4 (positive) | `cogs/role_menus_cog.py:1` | Exemplary thin cog — 880 bytes/28 lines, registers persistent `DynamicItem`s and nothing else, all logic delegated. Noted as positive contrast. | — |
| S2 (still-open) | `routes/config.py:1` | God-module unsplit and grown +35.5% since 07-01. | See Executive Summary #11. |
| S2 (still-open) | `commands/jail_commands.py:1` | 07-01 mega-file cluster member, grown +38%; cluster still unsplit. | Prioritize this file — fastest-growing of the cluster. |

### A3. Test coverage

| Sev | Evidence | Finding | Recommendation |
|---|---|---|---|
| S2 | `hidden_channels/store.py:19` | Persistence layer for `/hidden` (`create_hidden`/`get_active_hidden`/`list_active_hidden`/`mark_restored`) has **zero** test coverage; the bare filename means `gate.py`'s mandatory-test hard-fail never catches the gap — only a soft "unmapped" warning. | Add `tests/test_hidden_channels_logic.py`; fix the gate mapping (see A7 below) so this class of gap is caught automatically going forward. |
| S2 | `scripts/gate.py:116` | `REQUIRE_TEST_SUFFIXES` only matches filenames literally ending `_logic.py`/`_service.py`, silently exempting the codebase's now-dominant bare `logic.py`/`store.py`-per-feature-directory convention (~19–30 files) from the mandatory-test gate CLAUDE.md promises. | Broaden the suffix match to cover `logic.py`/`store.py` regardless of prefix. |
| S2 | `scripts/gate.py:126` | `_tokens_for`'s feature-directory fallback resolves nested `bot_modules/cogs/<feature>/` layouts (e.g. casino) to the generic `cogs` token instead of the real feature name, so casino's cog/embeds/views glue files get zero mapped tests in the fast pre-commit tier. | Resolve nested feature directories to their actual feature name before the fallback. |
| S3 | `inactive/apply.py:153` | `guild.create_role()` and the per-channel `set_permissions` loop only catch `discord.Forbidden`, not `discord.HTTPException`; a transient Discord error aborts first-time `@Inactive` role setup unhandled. Path is completely untested. | Broaden the except clause; add a test for the failure path. |
| S3 (positive) | `mobile_layout_scan.py:1` | The claimed browser-suite convention (Playwright layout+console checks, scoped in pre-commit, full in nightly) is genuinely panel-agnostic and covers new panels automatically with no extra wiring needed. | — |

### A4. Safety / consent gates

| Sev | Evidence | Finding | Recommendation |
|---|---|---|---|
| S1 | `cogs/games_traditional_cog.py:149` | Truth-or-Dare's NSFW Truth/Dare categories have zero `channel.is_nsfw()` gate anywhere. See Executive Summary #2. | Apply the same `channel_allows_nsfw()` gate the 07-01 fix applied everywhere else. |
| S1 | `role_menus/views.py:368` | `_apply_outcome` never re-checks `role_block_reason`/`is_dangerous` on click. See Executive Summary #1. | Re-validate danger status at click time, at periodic sync, and surface it on the dashboard health-check panel. |
| S2 | `cogs/pen_pals_cog.py:1741` | `/penpals pair` lets a mod force two arbitrary members into a private (potentially NSFW-flagged) 1:1 channel with no opt-in check, unlike sibling `/penpals round` which only pairs opted-in members. | Require both targets to have opted in, matching `/penpals round`. |
| S2 | `chat_revive/actions.py:32` | `channel_is_busy()` fails **open** (returns "not busy") on any exception from the games_db lookup or a registered busy-check callback — the opposite of the codebase's established fail-safe convention (`channel_allows_nsfw`). | Flip to fail-closed. |
| S3 | `services/casino_service.py:150` | Only spend-limit control is a single admin-set, guild-wide `daily_wager_cap` applied identically to every member; no per-member self-service limit, cooldown, or opt-out, unlike the wagering-adjacent duels feature. | Consider a per-member self-service cap, matching duels. |
| S4 (positive) | `inactive/apply.py:88` | Broad general-safety spot-check across inactive/hidden_channels/qa/docs/chat_revive/announcements/casino: no new correctness/safety defects found beyond the role_menus finding — each area has the expected guard set, sweep safety caps, allow-listed pings, parameterized SQL, gated dashboard routes; each new logic/service file has a mapped test. | — |

### A5. Security & authz

| Sev | Evidence | Finding | Recommendation |
|---|---|---|---|
| S2 | `web_server/routes/home.py:22` | `GET /api/home` gated with `require_perms(set())` — any authenticated member can pull per-member social-graph analytics that equivalent endpoints restrict to moderator+. See Executive Summary #5. | Gate to moderator+ to match sibling analytics endpoints. |
| S3 (still-open) | `auth.py:238` | Guild-cache-miss path still trusts login-time perms instead of failing closed. | Fail closed on cache miss. |
| S3 (still-open) | `server.py:182` | `SESSION_SECRET` accepted with no minimum length/entropy check. | Add a minimum-length assertion at boot. |
| S3 (still-open) | `auth.py:139` | Session cookie still carries the live OAuth access token in signed-but-unencrypted base64. | Drop the token from the cookie or encrypt it. |
| S4 (still-open) | `routes/oauth.py:311` | `/logout` remains a CSRF-able plain GET. | Require POST + CSRF token, or accept the low-severity forced-logout risk explicitly. |

### A6. Migrations hygiene

| Sev | Evidence | Finding | Recommendation |
|---|---|---|---|
| S4 | `migrations/028_pressure_cooker.sql:30` | `pressure_config`/`pressure_cooldowns`/`pressure_nicks` are fully dead tables — Pressure Cooker now reads/writes the generic `duel_*` tables and nothing ever dropped the originals. Pure dead schema, no functional impact. | Drop in a follow-up migration when convenient. |
| S3 (still-open) | `src/migrations:0` | Duplicate migration-number pairs grew 2→9. | Not urgent (dedupe key is filename), but add a CI check to prevent new collisions before the scheme fails entirely. |
| S3 (not-applicable, positive) | `migrations/062_economy.sql:0` | Economy build-out's four table-rebuild migrations (widening `qtype`/perk CHECK constraints) are all correctly scoped and column-complete — no column silently dropped across four rebuilds, each runs inside the now-atomic migration transaction. | — |

### A7. CI / tooling hygiene

| Sev | Evidence | Finding | Recommendation |
|---|---|---|---|
| S2 | `scripts/gate.py:116` | Mandatory-test suffix matching gap. See A3. | — |
| S2 | `scripts/gate.py:126` | Nested feature-directory token resolution gap. See A3. | — |
| S2 | `scripts/post_testing_docs.py:182` | `qa_card_channel()` reads the dashboard-configured QA card channel with **no `guild_id` filter** even though config is keyed per-guild; on a multi-guild install this can post a commit's Testing checklist to, and tenant-tag the `qa_tests` row under, an arbitrary/wrong Discord server. | Filter by `guild_id` before selecting the target channel. |
| S2 (still-open) | `.github/workflows/test.yml:48` | JS/CSS lint job runs but is still non-blocking, three weeks after being explicitly deferred pending a clean report. See Executive Summary #12. | Flip to blocking now that it's been clean for three weeks — or explain why not. |
| S2 (fixed) | `pyproject.toml:69` | Pyright no longer excludes games cogs. | — |

---

## Track B — UX

### B1. Deep pass — Economy core

| Sev | Evidence | Finding |
|---|---|---|
| S1 | `economy-bank-manager.js:263` | Grant Currency's `parseInt(picked, 10)` loses snowflake precision past 2^53. See Executive Summary #3. |
| S2 | `economy/guide.py:35` | Members get two independent, overlapping DM-notification controls — `/bank mute` (blanket per-user mute, drops every DM) and the guide panel's 🔔 button (opt-in role gating only recurring notices) — discoverable via two unrelated surfaces with no cross-reference, violating CLAUDE.md's collapse-controls rule. |

### B2. Deep pass — Economy quests

| Sev | Evidence | Finding |
|---|---|---|
| S2 | `docs/economy_spec.md:763` | Unresolved git merge-conflict markers committed into the Dynamic Target Band section. See Executive Summary #8. |
| S2 | `economy/leaderboard.py:632` | The public leaderboard panel and the weekly flip announcement tell members to run `/quests`, a slash command that does not exist — the real command is `/bank quests`. |
| S2 | `economy-quests.js:11` | Dashboard's advisory monthly-quest reward band (75–200) is stale; migration 103 lowered the real intended band to 50–90 specifically to stop monthly quests being the richest per-claim faucet, but the authoring form's client-side hint was never updated — silently re-permitting the abuse pattern the migration closed. |
| S2 | `manual.html:966` | User-facing Help manual still describes community weeklies as a single "every other week" goal; never updated for the two-lane concurrency system that runs up to 2 community goals at once. |
| S2 | `services/economy_loop.py:500` | Dashboard's Quest Library "active" checkbox lets a manager directly activate an auto-tracking community quest, producing the exact orphan state the hourly self-repair sweep exists to fix — the manager's choice is silently undone (possibly replaced by a different quest) within the hour, with no warning anywhere in the UI. |
| S3 | `economy/quests.py:131` | Income Sources page's admin explainer for the `qotd_reply` trigger kind describes the wrong mechanic (first message in channel that day vs. an actual reply to the registered QOTD message). |
| S4 | `economy/quest_views.py:723` | `QuestClaimSelect` reads booster status via `member.premium_since` directly instead of the shared `member_is_booster()` helper — harmless today, a maintenance trap if the helper's logic ever changes. |
| S4 | `economy/quest_views.py:149` | New quest sign-off card mixes glyph-led and bare field names on one card and omits the section-spacing trailing blank line on every field — two documented embed-style-guide conventions missed in one new builder. |

### B3. Deep pass — Casino

| Sev | Evidence | Finding |
|---|---|---|
| S3 | `casino/views.py:163` | Hub panel buttons for a disabled game table stay fully clickable instead of greyed out, unlike the codebase's established `disabled=` convention; server-side gate still enforces, so no money is at risk — an affordance mismatch. |
| S3 | `casino/embeds.py:217` | `build_slots_embed` escalates a real win from semantic green to `COLOR_GOLD` on a "big win" — an unsanctioned third semantic tier not permitted by the 2026-07-21 "Games follow the accent" ruling. |
| S3 | `casino/cog.py:130` | Per-guild loops in `_boot()` (refund notices, timer re-arm, `ensure_panel`) aren't individually try/excepted, so one guild's failure silently aborts panel setup for every guild after it in iteration order. |
| S4 | `services/casino_service.py:857` | `idle_live_blackjack_hands`'s `older_than` filter is a no-op given its only caller's invocation, fetching every live hand instead of just idle ones; correctness unaffected (Python loop re-filters correctly) — clarity/dead-code nit. |
| S4 (not-applicable, positive) | `services/casino_service.py:161` | No defect found — casino money-movement code (take_stake/pay_out/refund/jackpot/blackjack/roulette) is exactly-once by construction throughout, matching the project's own house rules. |

### B4. Deep pass — Announcements

| Sev | Evidence | Finding |
|---|---|---|
| S2 | `panels/announcements.js:392` | Save button has no double-submit guard (unlike `role-menus.js`/`docs.js`), so a fast double-click fires two concurrent POST/PUT requests, creating duplicate scheduled rows and duplicate channel posts. |
| S2 | `services/announcements_service.py:190` | `update_announcement()` does a blind UPDATE with no status re-check, so a PUT/post-now request racing the loop's atomic claim can revert a just-sent row back to draft/scheduled while `sent_channel_id`/`sent_message_id` remain populated — risking a genuine duplicate post and hiding the send from the dashboard's Sent bucket. |
| S3 | `routes/announcements.py:437` | Post Now updates `post_at` but leaves `post_date`/`post_time_min` untouched, so the Queue row keeps displaying the stale, previously-scheduled date/time instead of reflecting the imminent send. |
| S4 | `routes/announcements.py:162` | Button-emoji validator rejects legitimate Discord keycap-sequence emoji (e.g. numbered options) as "not an emoji" since they begin with an ASCII digit; untested path. |

### B5. Deep pass — Chat Revive

| Sev | Evidence | Finding |
|---|---|---|
| S2 | `chat_revive/actions.py:32` | `channel_is_busy()` fails open on error — see A4. Cross-listed here as a direct UX-promise break: "never talks over an active room" can silently fail. |
| S3 | `docs/chat_revive_spec.md:118` | Spec's and mod-facing manual's ping-scarcity description ("once per channel per day"/"rolling 24h") no longer match the shipped, dashboard-configurable defaults added by migration 076 (3/day, 1h apart). |
| S3 | `routes/chat_revive.py:129` | A channel's category filter is free-text validated only for "single alphabetic word," not checked against the known category set, so a typo silently makes the channel never fire without surfacing an error at save time. |
| S3 | `services/chat_revive_service.py:452` | Guild-wide daily budget and breathing-room gates are read-then-decide-then-write across two independently concurrent trigger paths (monitor loop vs. dashboard manual fire) with no lock tying read to write, so two near-simultaneous fires can both pass the same budget check. |
| S4 | `docs/plans/chat-revive.md:67` | Plan's "Data model" section wasn't updated for two later migrations that added guild-config columns, and still names the events column `trigger` instead of the actual `trigger_kind`. |
| S4 | `services/chat_revive_loop.py:5` | Code comments and one user-facing error string reference `/revive check\|fire\|setup` slash commands that do not exist anywhere in the codebase — the feature is entirely dashboard-configured with no `/revive` command group. |

### B6. Deep pass — Hidden Channels / Inactive

| Sev | Evidence | Finding |
|---|---|---|
| S2 | `cogs/hidden_channels_cog.py:150` | `create_hidden()` runs after the irreversible `channel.edit()` with no try/except; a DB failure leaves the channel hidden on Discord with its original overwrites permanently lost and no restore path. |
| S2 | `cogs/inactive_cog.py:260` | Re-running `/inactive panel` to point at a new channel never revokes the `@Inactive` role's view/send grant on the old channel (`ensure_inactive_role` short-circuits after first creation) — ex-inactive channels stay visible to `@Inactive` forever. |
| S2 | `cogs/inactive_cog.py:239` | Inactive channel/role setup is wired via a Discord slash command rather than the dashboard, contradicting CLAUDE.md's config-on-dashboard rule; sibling knobs (threshold/cap/auto-sweep) were already migrated to the dashboard with the old command explicitly deleted — a partially-completed, inconsistent migration within the same feature. Cross-listed under B10 (dashboard-vs-Discord-config). |

*(`hidden_channels_cog.py:109` async gap, `hidden_channels/store.py:19` test gap, `inactive_cog.py:99` logic-in-cog, `inactive/apply.py:153` narrow except — all cross-listed under Track A.)*

### B7. Deep pass — QA / Role Menus / Docs

| Sev | Evidence | Finding |
|---|---|---|
| S1 | `role_menus/views.py:368` | Dangerous-role re-check gap. See Executive Summary #1; primary write-up under A4. |
| S2 | `panels/qa-tracker.js:153` | Expandable board rows are click-only `<tr>` elements with no `tabindex`/`role`/`keydown`/`aria-expanded`. See Executive Summary #13. |
| S2 | `scripts/post_testing_docs.py:182` | QA card channel has no `guild_id` filter. See A7. |
| S3 | `panels/docs.js:279` | `docs.js` and `role-menus.js` use native browser `confirm()`/`alert()`/`prompt()` for create/delete flows instead of the dashboard's own `confirmDialog()`/`promptDialog()`/`toast()` utilities every other recently-touched panel uses, breaking visual/interaction consistency. (Merged from 2 near-identical findings.) |
| S3 | `panels/help-sections.js:58` | Docs feature has a full dashboard panel and manual section but never got its own Help-sidebar entry, unlike every sibling admin feature under the same manual heading. |
| S3 | `README.md:304` | README's Slash Commands reference never documents `/docs post\|sync\|unpost\|list`, though CLAUDE.md requires the same commit to update it, and sibling features (QA Tracker, Role Menus) did get their bullet. |
| S3 | `routes/docs.py:183` | Uploaded doc images (up to 8MB each) are written to `static/doc-images` and never garbage-collected; deleting a doc/placement/edit leaves the file on disk forever, growing the static mount unboundedly. |
| S3 | `panels/role-menus.js:376` | Role Menus' and Docs' list rows are click-only, non-focusable `<div>`s; a keyboard user cannot Tab to or activate a menu/doc row to open its editor. |
| S3 | `role_menus/views.py:335` | role_menus' and chat_revive's member-facing ephemeral denial/error strings omit the mandatory ❌ prefix from the 2026-07-21 ruling, while the sibling brand-new qa feature applies it consistently. |
| S4 | `routes/docs.py:205` | `create_doc` does a check-then-insert on the `UNIQUE(guild_id, doc_key)` constraint without a try/except around the insert, so two concurrent "New" submissions with the same key can turn the second request into an unhandled 500 instead of a friendly 409. |

### B8. Deep pass — Games UX-consistency

| Sev | Evidence | Finding |
|---|---|---|
| S1 | `panels/games-studio.js:244` | AI Studio's "Add Selected to Bank" sends `category` instead of `tags`. See Executive Summary #4. |
| S2 | `panels/games-panel-shared.js:49` | The "Enabled on this server" toggle shown (and persisted) for Traditional Truth-or-Dare and FFA has no effect — neither cog ever reads `games_game_config`/`check_game_enabled`. See Executive Summary #14. |
| S2 | `panels/games-logs.js:5` | Overview & Logs' "Games by Type" breakdown and History filter are hardcoded to 8 game types, silently omitting Traditional, FFA, MFK, Compliment, TTL, Hot Takes, Story, and Fantasies (all log to the same shared history table) — understating overall activity. |
| S3 | `js/app.js:194` | Six registered game types (mfk, compliment, ttl, hottakes, story, fantasies) have no dashboard nav entry or config panel at all — no way to disable them from the dashboard. |
| S3 | `panels/games-panel-shared.js:213` | Default question-bank hint text says the `nsfw` tag is included "by default" — backwards from the actual opt-in, channel-gated behavior; the one control whose accuracy matters most for keeping adult content out of non-NSFW channels. |
| S3 | `panels/games-scheduling.js:10` | `games-scheduling.js` and `photo-challenge.js` independently reimplement the same schedule CRUD UI instead of sharing one module, and have already drifted apart in copy (differing status text for the same code). |
| S4 | `panels/games-legitlibs.js:603` | "AI prep" button label breaks Title Case, unlike its sibling buttons "Detect from Body" and "+ Row" in the same toolbar. Cosmetic only. |

### B9. Spot-checks — Moderation / Health / Reports / ten stable features

| Sev | Evidence | Finding |
|---|---|---|
| S4 (positive) | `inactive/apply.py:88` | Broad spot-check umbrella across inactive, hidden_channels, qa, docs, chat_revive, announcements, and casino: no new correctness/safety defects beyond the role_menus finding — expected guard sets, safety caps, allow-listed pings, parameterized SQL, gated routes all present; every new logic/service file has a mapped test. |
| S4 (positive) | `routes/reports.py:1621` | Grant Audit endpoint (new since 07-01) was added and correctly refactored to move all DB work off the event loop via `run_query`/`asyncio.to_thread` into a tested service module; spot-check found no defects. |
| S4 (fixed) | `routes/health.py:645` | Heatmap timezone/day-of-week bug fully resolved. |

Ten stable features (moderation, health, reports, plus seven others not separately itemized) were spot-checked and returned clean on this pass — no findings beyond the two above.

### B10. Dashboard-wide sweep — Accessibility

| Sev | Evidence | Finding |
|---|---|---|
| S2 | `panels/qa-tracker.js:153` | Click-only, non-keyboard-operable board rows. See B7/Executive Summary #13. |
| S3 | `panels/role-menus.js:376` | Click-only, non-focusable list rows in Role Menus and Docs. See B7. |
| S3 | `panels/economy-quests.js:621` | AI quest-idea cards in the Economy Quests authoring flow are click-only `<div>`s with no keyboard operability; a keyboard-only user cannot load a generated idea into the form. |

The 07-01 accessibility remediation (toasts, dialogs, tab-strips, combobox, nav headers) held completely — nothing regressed. But three brand-new panels reintroduce the exact click-only-element defect class the 07-01 pass eliminated everywhere else, confirming the pattern needs a shared, enforced primitive rather than per-panel discipline.

### B11. Dashboard-wide sweep — Mobile / responsive

| Sev | Evidence | Finding |
|---|---|---|
| S2 (fixed) | `app.css:2745` | U2a mobile table-scroll fix held. |
| S3 (fixed) | `app.css:2784` | U2b 16px-input iOS-zoom fix held. |
| S3 (positive) | `mobile_layout_scan.py:1` | Browser-suite convention is panel-agnostic and covers new panels automatically. |

No new mobile/responsive regressions found; both 07-01 fixes remain intact and the automated scan continues to catch new panels without extra wiring.

### B12. Dashboard-wide sweep — Dashboard-vs-Discord-config

| Sev | Evidence | Finding |
|---|---|---|
| S2 | `cogs/economy_cog.py:3649` | `/bank post-guide`, `post-leaderboard`, `post-shop` persist channel config with no dashboard equivalent. See Executive Summary #9. |
| S2 | `cogs/inactive_cog.py:239` | Inactive channel/role setup still Discord-command-only while sibling knobs already migrated (and the old command deleted) — a stalled, half-finished migration. See B6. |

Both are the same class of drift the 07-01 review's precedent (role_menus's clean dashboard-only config) was meant to prevent from recurring; both landed in the 525-commit window without a compensating dashboard panel.

### B13. Dashboard-wide sweep — Design-consistency

| Sev | Evidence | Finding |
|---|---|---|
| S2 | `cogs/economy_cog.py:1242` | 8 inline embeds in the cog instead of `embeds.py`, violating the 2026-07-21 embed style guide one day after the ruling. See A2. |
| S3 | `economy/bounty_views.py:221` | Community-bounty board's persistent Cancel button uses a ✖️ emoji, violating the style guide's "Cancel is plain text, no ✕/✗ glyph" ruling — the only economy view with the glyph. |
| S3 | `economy/bounty_views.py:74` | Four brand-new economy approval-card builders (bounty, quest sign-off, sponsor, pin) hard-code `discord.Color.green()`/`red()` instead of the canonical `COLOR_GREEN`/`COLOR_RED`, reintroducing the color drift the 2026-07-21 ruling was written to close, on the same day as that ruling. |
| S3 | `casino/embeds.py:217` | Unsanctioned `COLOR_GOLD` "big win" tier. See B3. |
| S3 | `role_menus/views.py:335` | Missing mandatory ❌ prefix on error copy. See B7. |
| S3 (still-open) | `economy-quests.js:1` | Inline-style sprawl grew 631→892 repo-wide since 07-01; no utility-class system added. |
| S3 (still-open) | `js/app.js:1` | Copy-pasted tab-strip ARIA anti-pattern still entirely unaddressed. |
| S4 | `economy/quest_views.py:149` | Mixed glyph/bare field names + missing section-spacing on new quest sign-off card. |
| S3 (fixed) | `js/api.js:90` | Fetch-wrapper/`esc()` duplication substantially fixed. |

The pattern across this sweep: **every brand-new economy/casino embed builder shipped since the 2026-07-21 style-guide ruling violates at least one of its own rules**, while the guide correctly caught nothing in already-existing, previously-reviewed code — the ruling isn't being applied to new work as it lands.

---

## Remediation backlog (ranked by impact ÷ effort)

### Do first — S1, low/medium effort
1. **Close the Truth-or-Dare NSFW gap** (games_traditional_cog.py:149) — apply the same `channel_allows_nsfw()` gate the 07-01 fix already applied everywhere else in the games suite. *Smallest possible diff, closes the last hole in a promise the manual already makes.*
2. **Fix the two dashboard field-mismatch bugs** (economy-bank-manager.js:263 snowflake `parseInt`, games-studio.js:244 `category`→`tags`) — both are one-line JS fixes that currently corrupt data silently on every use. *Highest value-per-line-changed in the whole batch; both are exactly the bug class that already caused a real incident.*
3. **Re-validate role danger status on every click** in role_menus (`_apply_outcome`), matching the announcements feature's existing pattern; also surface stale-danger roles on the dashboard health-check panel and in periodic sync. *4 independent workstreams flagged this — treat as confirmed, not provisional.*

### Do next — S2, medium effort
4. **Gate `/api/home` to moderator+** — one-line permission change closing a live social-graph data leak.
5. **Fix the `gate.py` test-mapping gaps** (suffix matching for `logic.py`/`store.py`, nested-feature-directory token resolution) — this is the fastest way to prevent the next `hidden_channels/store.py`-style zero-coverage gap from landing silently again.
6. **Un-block the JS/CSS lint gate** (`test.yml:48`) — it's been clean for three weeks; flip `continue-on-error` off now.
7. **Fix the merge-conflict markers in `docs/economy_spec.md`** and the `/quests`→`/bank quests` command-name errors in leaderboard.py/manual.html — cheap, high embarrassment-reduction docs fixes.
8. **Add a double-submit guard to announcements.js** and **make `update_announcement()` re-check status before writing** — closes a real duplicate-post risk.
9. **Wrap `economy_loop.py`'s 6 sync DB calls and `hidden_channels_cog.py`'s 3 handlers in `asyncio.to_thread`** — same fix pattern applied twice, contained blast radius.
10. **Require Pen Pals opt-in for mod-forced `/penpals pair`**, matching `/penpals round`.
11. **Fix `chat_revive`'s fail-open busy check** — flip to fail-closed to match the codebase's established safety convention.
12. **Make QA Tracker's board rows keyboard-operable** and **fix Economy Quests' AI-idea cards** — small, contained a11y fixes on two brand-new panels before the pattern spreads further.
13. **Give the "Enabled on this server" toggle teeth** for Traditional/FFA, or remove it — currently an outright lie to the admin who clicks it.
14. **Try/except `create_hidden()`'s DB write** after the irreversible `channel.edit()` call, and **fix `ensure_inactive_role`'s missed revoke** on channel re-point.
15. **Filter `post_testing_docs.py`'s QA card channel lookup by `guild_id`.**

### Docs & cleanup — S2/S3, low effort
16. **Fix `economy_cog.py`'s embed-style-guide violations** (COLOR_GREEN/RED, ❌ prefix, ✖️ Cancel glyph, COLOR_GOLD tier) across the four new bounty/quest/casino builders in one sweep — all are one-day-old violations of a ruling that already exists; catching them now is cheapest before more copy-paste spreads the pattern.
17. **Update stale docs**: `economy_spec.md` reward-band hint (75–200 → 50–90), `manual.html` community-goals two-lane description, `chat_revive_spec.md` ping-scarcity numbers, `chat-revive.md` plan data-model section, README `/docs` command bullet, `help-sections.js` Docs sidebar entry.
18. **Split `economy_cog.py`** into a proper service-layer delegation (extract embeds first — cheapest slice — then eligibility/TTL-cache logic, then the raw SQL). *Biggest single architecture debt of the cycle; do it before the file grows further, since the same team is actively adding to it.*

### Backlog — S3/S4
19. Remaining items: casino disabled-button greying, casino boot-loop per-guild isolation, `idle_live_blackjack_hands` dead filter, announcements keycap-emoji validator, docs image garbage collection, `create_doc` race→409, games-scheduling/photo-challenge de-duplication, dashboard nav entries for the 6 orphaned game types, `games-legitlibs.js` button casing, dead `pressure_*` tables drop, `economy_quests_service.py` split before it grows further, native-dialog→`confirmDialog()` migration in docs.js/role-menus.js, config-model/god-module items carried forward from 07-01 (`routes/config.py`, `jail_commands.py`), inline-style-sprawl utility classes, shared `makeTabStrip()` helper, `SESSION_SECRET` length check, session-cookie token encryption, `/logout` CSRF.

### Explicitly deferred
- Full split of `routes/config.py` and the 07-01 mega-file cluster (`jail_commands.py` et al.) — both continue to grow under active development; a scoped refactor plan (per CLAUDE.md's `docs/plans/` convention) is warranted before attempting a piecemeal split.

---

## Coverage ledger

**Independently confirmed by the author (re-read live):** the role_menus dangerous-role re-check gap (cross-validated by 4 workstreams independently); the Truth-or-Dare NSFW gate absence; the two dashboard field-mismatch bugs (snowflake precision, `category`/`tags`); the merge-conflict markers in `economy_spec.md`; the `/api/home` permission gate.

**Reported with `file:line` by workstream agents (evidence cited, not each independently re-read):** the full economy/quests/casino/announcements/chat_revive/hidden_channels/inactive/qa/role_menus/docs deep passes; games UX-consistency sweep; moderation/health/reports/ten-stable-features spot-checks; dashboard-wide a11y, mobile/responsive, dashboard-vs-Discord-config, and design-consistency sweeps; backend async/blocking-IO, architecture, test-coverage, security, migrations, and CI passes. These are well-evidenced but a full finding-by-finding re-verification was not performed; treat any single finding as confirmable-in-one-read via its citation.

**Sampled, not exhaustive:** the ~49-site async-blocking count is a floor, not a ceiling; the embed-style-guide sweep covered new-since-07-21 builders, not a full re-audit of pre-existing ones; the merge-conflict-marker check was not repeated across every spec file, only the ones flagged by content drift elsewhere.

**Skipped / not runnable:** live pytest + coverage % delta since 07-01; actual NSFW bank row contents; live browser/screen-reader testing (heuristic only); `rules_watch` (out of scope — see 2026-07-20 deep-dive).

---

*Generated by an audit-only multi-agent review. All findings are evidence-pinned; recommendations are proposals for a follow-up implementation session, not changes made here.*

---

# Addendum — independent verification & expansion pass (2026-07-22, same day)

A second, single-reviewer pass re-read the code behind every S1 and the highest-impact S2s — the tier the Coverage ledger marked "reported with file:line by workstream agents" — plus both corrections the re-read surfaced. Findings below either **upgrade** a claim to independently-confirmed, **correct** it, or **expand** it with root-cause/fix detail the main report lacked.

## Verification results

Re-read and **confirmed exactly as written**: the four S1s (#1–#4); `/penpals pair` (no opt-in check on either target — only enabled/blocked/active-session are checked, `pen_pals_cog.py:1760-1771`); `channel_is_busy()` fail-open (both exception paths log-and-continue to `return False`, `chat_revive/actions.py:36-46`); `update_announcement()` blind UPDATE (`WHERE id = ? AND guild_id = ?` with no status predicate, `announcements_service.py:190-201`); `create_hidden()` after irreversible `channel.edit()` with no try/except (`hidden_channels_cog.py:150-160` — the same block is also a sync DB write directly on the event loop, compounding the A1 finding); the decorative enabled-toggle (**zero** references to `check_game_enabled`/`games_game_config` in `games_traditional_cog.py` or `games_ffa_cog.py`, while siblings ama/clapback/mlt all call it); `economy_cog.py` at exactly 4,278 lines / 167,265 bytes; `post_testing_docs.py`'s guild-unfiltered `qa_channel_id` lookup (`SELECT value FROM config WHERE key = 'qa_channel_id' ... LIMIT 1`, no `guild_id` predicate, `post_testing_docs.py:182-186`); and the `/quests` phantom command (3 occurrences in `leaderboard.py:632,636,667`; the real command is `/bank quests`, registered at `economy_cog.py:2950`).

## Corrections (2)

1. **Executive Summary #5 (`/api/home`) is overstated as written — and the true defect is narrower but sharper.** The handler does *not* serve everything to any member: `home.py:39-46` strips the `moderation` group for non-mods and `mod_actions` for non-admins. The defect is that the strip list covers only 2 of 12 field groups, where the sibling `/health/tiles` (`health.py:126`, same `require_perms(set())` shape) correctly tiers *every* sensitive tile behind `if is_admin or is_mod:` / `if is_admin:` in-handler — the intended pattern exists one file over. The sharpest consequence, missed by the original finding: the voice-occupancy block (`home.py:76-88`) iterates **all** `guild.voice_channels` with no per-viewer permission check, so any authenticated member can enumerate live occupants (id + display name) of voice channels they cannot see in Discord — a mod/private-VC leak that crosses a Discord-side permission boundary, which is worse than "analytics gated too low." Severity stays S2; the fix is to tier home.py's remaining groups the way health_tiles already does, and either drop permission-hidden VCs from the payload or gate the `voice` group to moderator+.
2. **A3/A7's exempt-file count is understated.** The report says the bare `logic.py`/`store.py` convention covers "~19–30 files"; a direct count finds **34** (`src/bot_modules/*/logic.py` + `*/store.py`). The mechanism is confirmed at `gate.py:114`: `REQUIRE_TEST_SUFFIXES = ("_logic.py", "_service.py")` can never match a bare `logic.py` because the character before "logic" is a path separator, not an underscore.

## Expansions on the four S1s

**#1 role_menus (danger re-check).** Both halves independently confirmed: `routes/role_menus.py:280-300` refuses unmanageable roles and surfaces `is_dangerous(role)` at save time; `views.py::_apply_outcome` (:353-390) re-checks only bot `manage_roles`, role existence, and hierarchy (`role >= bot_member.top_role`) — never `is_dangerous`. Fix design: `_apply_outcome` already loops every outcome role to resolve and hierarchy-check it; add one `is_dangerous(role)` call (pure permission-bit check from `core.role_safety`, no I/O) in that same loop for the `add_roles` bucket, replying with the existing `MSG_BROKEN` pattern and firing `_alert_mods_once(ctx, guild, menu, f"**@{role.name}** now carries dangerous permissions")`. That single site closes the standing-menu hole; the dashboard health-panel surfacing and periodic sync from the main report remain worthwhile but are defense-in-depth, not the fix.

**#2 Truth-or-Dare NSFW.** Confirmed: the only "nsfw" strings in the entire cog are the four button labels/custom-ids. Expansion — there are two serve paths and both need the gate: (a) the `nsfw_truth`/`nsfw_dare` preference toggles (`:149-157`) should refuse to enable in a non-NSFW channel (cheapest single gate — no NSFW prefs means neither path can select an NSFW category); (b) `bank_round` (`:211+`) fetches via `get_traditional_question(self.db, cat, exclude=used)` with no channel argument, so bank NSFW questions post to whatever channel hosts the panel. Gating (a) at toggle time with the standard `channel_allows_nsfw()` also fixes (b), because `select_bank_categories_for_all` only draws from opted-in categories; belt-and-suspenders is one extra check in `bank_round`. Host-typed questions via `AskQuestionModal` can't be content-gated, but with NSFW categories un-selectable in SFW channels the modal is never invoked for an NSFW slot there.

**#3 Grant Currency snowflake.** Confirmed and blast radius sharpened. `apply_credit` (`routes/economy_manager.py:1244`) performs **no guild-membership validation** on `body.member_id` — it writes the ledger for whatever integer arrives, and the mod sees "Credited ✓". The precision math makes silent corruption near-certain: current snowflakes (~1.5×10¹⁸) sit far above 2⁵³ (~9.0×10¹⁵), where IEEE-754 doubles are spaced **256 apart** — so `parseInt` rounds to a multiple of 256, up to ±128 away from the real ID, virtually always a nonexistent member (phantom wallet). Two-part fix: (a) one line in `economy-bank-manager.js:263` — send `member_id: picked` as a string; `GrantBody.member_id: int` coerces numeric strings losslessly server-side, so no backend change is needed; (b) cheap hardening in the route — reject a grant whose `member_id` isn't in the guild's member cache, which also catches future non-JS callers. The pattern is confirmed isolated: this is the only `parseInt`-on-picker-value in the panels tree (line 264's `amount` parseInt is fine).

**#4 AI Studio tags.** Confirmed via schema: `BankCreateBody` (`routes/games.py:48-50`) has `tags: list[str] = []` and no `category` field; Pydantic's default config silently drops unknown fields. Expansion — there are **two distinct failure modes**, not one: for game types where NSFW is tag-carried, the question saves silently as SFW-served (the reported S1); but for `game_type == "traditional"`, `_validate_traditional_tags` (`:184`) requires exactly one of the four category tags and receives `[]` — the save 422s, the studio's bare `catch (_) { failed++ }` swallows the reason, and the mod sees "N failed" with no explanation. So the same one-line bug is a **silent safety leak** on one path and a **silent hard failure** on the other. Fix: send `tags: [cat]` (translating the studio's category naming to the bank's tag vocabulary), and surface the caught error text in the status line instead of a bare counter.

## New finding from this pass (1)

| Sev | Evidence | Finding |
|---|---|---|
| S2 | `routes/home.py:76` | Voice-occupancy leak across Discord-side channel permissions — any authenticated member can enumerate occupants of VCs hidden from them. Split out from corrected finding #5 above; same fix commit. |

`/health/tiles` (`health.py:126-615`) was also audited as the other `require_perms(set())` analytics surface and is **clean** — every member-sensitive tile is correctly tiered in-handler. It should be treated as the reference implementation when fixing home.py.

## Remediation backlog deltas

- Item 2 (field-mismatch bugs): both fixes are confirmed one-liners with no backend change required; add "surface the swallowed error text in games-studio.js" to the same commit.
- Item 3 (role_menus): the click-time re-check is a one-call addition to an existing loop — cheaper than the main report implies; do it first among the S1s.
- Item 4 (`/api/home`): reframe from "gate to moderator+" to "tier the field groups like `/health/tiles` and fix the voice-VC permission leak" — a blanket moderator+ gate would break the member-facing home page the ungated groups exist to serve.
- Item 5 (`gate.py`): the suffix fix must match `logic.py`/`store.py` as basenames (34 files currently exempt), not just broaden the underscore-suffix list.

*Verification pass performed single-reviewer, same day as the main report; every claim above was re-read directly from the working tree at the cited lines.*
