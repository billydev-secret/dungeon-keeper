# Dungeon Keeper — Deep Review

**Date:** 2026-07-01
**Scope:** Whole-system audit across four dimensions — game mechanics, software practices, docs & commands, UX & design.
**Type:** Audit only. No code or docs were changed. Every finding cites `file:line` evidence; recommendations are captured for follow-up.
**Method:** Parallel subagent workstreams (Opus for deep judgment, Sonnet for breadth sweeps, Fable for player-facing copy), each starting from confirmed recon seeds, verifying live, then sweeping for siblings.

**System size:** ~109k LOC Python (356 files, 64 loaded extensions, ~170 slash commands per the verified catalog, 6 context menus, ~28 command groups); FastAPI + vanilla-ES-module web portal (~250 endpoints, 106 config/report panels, 3404-line stylesheet); ~33 markdown specs; 187 test files.

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
| 1 | **S1** | Mechanics/Safety | **Consent system is a live-UI no-op.** `check_consent()` hard-returns `True`; `/consent` opt-out is stored but never enforced anywhere. The web manual explicitly promises mention-protection that cannot fire. |
| 2 | **S1** | Software | **270 blocking sync-SQLite calls run directly on the shared async event loop** — including per-message/per-reaction/per-voice gateway handlers — on a 2-core CPU-only prod box that *also* serves the dashboard on the same loop. |
| 3 | **S1** | Mechanics/Safety | **AMA host can force any guild member into the public "hot seat"** with no consent/decline path (`games_ama_cog.py:721-736`); they are @-pinged and fed anonymous questions. |
| 4 | **S1** | Mechanics/Safety | **Concurrent duel sentences corrupt nickname reverts.** A target can be in active duels with multiple challengers at once; two overlapping `duel_nicks` rows can restore the wrong "original" nick (`duels/base_game.py:287-294`). |
| 5 | **S2** | Software/Sec | **Auth fails OPEN:** if `DISCORD_CLIENT_ID` or `SESSION_SECRET` is ever unset, the portal silently serves `OpenAuth` (admin-to-everyone), and the code default bind is `0.0.0.0`. Prod is currently safe (OAuth + loopback), so this is latent, not live. |
| 6 | **S2** | Mechanics | **NSFW is served by default and the "clean mode" toggle is inoperative.** Root cause is `allow_nsfw=True` default in `question_source.py:182-211`; the scheduler/slash `nsfw:false` option never reaches the fetch layer (key mismatch `nsfw` vs `allow_nsfw`). Commit `8bca3c7` made Clapback NSFW default-on (one instance of the pattern) — but the UI/help still imply an off switch that doesn't exist. |
| 7 | **S2** | Software | **`web_server/routes/config.py` is a 105KB / 237-def god-module** holding ~40 config sections; plus a cluster of 40k+ mega-files (`activity_graphs.py`, `whisper_cog.py`, `guess_cog.py`, `*_commands.py`). |
| 8 | **S2** | Docs | **Three specs are aspirational to the point of misleading** (`wellness_guardian_spec` documents ~22 slash commands that don't exist; `games_system_spec` uses a dead command format + phantom admin surface; `dk_pvp_games_suite_spec` fully specs two never-built games). No docs index exists to tell readers which specs are real. |
| 9 | **S2** | Mechanics | **Games have no persistent progression** — zero games/duels code touches XP; wins produce nothing durable. Recommended resolution is to *keep them ephemeral by design* and fix the README, NOT to add farmable rewards on top of the open consent/NSFW gaps. |
| 10 | **S2** | CI | **CI silently skips JS lint entirely and pyright excludes the whole games cog surface** — type/lint errors in the largest, most-edited feature area never fail CI. |
| 11 | **S2** | UX/a11y | Custom widgets (comboboxes, dialogs, modals, tab-strips, nav headers) lack ARIA roles, focus traps, and keyboard operability; toasts have no `aria-live` region. The portal is substantially unusable by keyboard/screen-reader. |
| 12 | **S2** | UX/iOS | Only 4 `@media` queries in a 3404-line stylesheet; every form input is 13px (triggers iOS zoom-on-focus); non-`.data-table` tables clip at phone widths. Primary access path is iOS Safari. |

---

## Corrections to recon assumptions (verified during the review)

The review disproved or refined several starting hypotheses. Recording these so the report doesn't overclaim:

- **God-object is `AppContext` + 7 monkey-patched `bot.*` registries, not the `Bot` subclass** (which is lean). `bot.ctx` is read 89×; `bot.active_views` 143×.
- **Duplicate migration numbers are a naming hazard, not a correctness bug** — the dedupe key is the full filename, apply order is deterministic; both members of each pair apply exactly once.
- **Hammer.js is NOT dead code** — it is a required runtime dependency of `chartjs-plugin-zoom` for chart pinch/pan on touch. Do not remove.
- **Extension registry has no live drift** — all 62 cog modules on disk are in the load list; the risk is purely future-process (a new cog added without editing the list loads silently nowhere).
- **Nickname reverts and Risky Roll state ARE database-durable** — both survive a bot restart. (Corrects two prior in-memory concerns.)
- **Guess cog has a genuinely good consent model** — explicit opt-in + automatic opt-out on role removal.
- **Prod security posture is sound** — OAuth + loopback bind + 44-char secret + `SameSite=Lax` cookies; route authorization coverage is 100% on every sensitive router; no SQL injection; `esc()` applied consistently. The security findings are latent/hardening, not live breaches.
- **`dungeonkeeper.db` (468MB) and `.env` are gitignored, not tracked** — no secrets-in-repo issue.

---

## Dimension 1 — Game mechanics

### Safety & consent (highest priority)

| ID | Sev | Evidence | Finding | Recommendation |
|---|---|---|---|---|
| M1a | **S1** | `games/utils/consent_check.py:5-7` | `check_consent()` hard-returns `True`; the entire opt-in/out system fails open. | Honor stored `tod_consent`, or remove the UI so it stops implying protection. |
| M1b | **S1** | `consent_check.py:17-44`, `games_consent_cog.py:100-113` | The one live enforcement path (`scan_mentions_for_consent`, wired into `on_message`) never deletes anything because M1a short-circuits it. | Fixing M1a re-arms it; until then don't advertise mention-scrubbing. |
| M1c | S2 | `games_consent_cog.py:31-53,86-90` | Opt-out choices are written to DB and read *only for display* — collected but never enforced (data-minimization/dark-pattern). | Honor the value or stop collecting it. |
| M1d | S2 | `consent_check.py:10-14` | `format_name` (the name-masking protection) has zero callers; games ping members directly. | Wire it in or delete it. |
| M2-ama-a | **S1** | `games_ama_cog.py:664-673,721-736` | Host/mod can force **any** guild member into the public hot seat via a `UserSelect`, with no consent/decline. | Restrict to volunteers in the queue, or require the target to accept. |
| M8-b | **S1** | `guess_cog.py:924-934` | No per-user submission rate limit on `/guess submit` — a role member can flood the guess channel with NSFW images. | Per-user cooldown / daily cap. |
| M0-base-b | **S1** | `duels/base_game.py:287-294`, `base_duel.py:94-99` | A target can hold PENDING/ACTIVE duels with multiple challengers simultaneously; two overlapping nick sentences can restore the wrong original nick. | Block challenge if target is in any active/pending game; re-check `_check_no_active_nick` at nick-submit. |
| M0-base-d | S2 | `duels/base_game.py:187-192` | Leaving and rejoining the guild escapes an active nick sentence (no `on_member_join` re-apply). | Add `on_member_join` re-application of unexpired sentences. |
| M0-base-e | S2 | `duels/filters.py:11-15,30-49` | Nick slur-denylist uses NFC (not NFKC) normalization → bypassable with Unicode homoglyphs/fullwidth/combining chars. | One-line change to `NFKC`. |
| M7-c / M8-e / M7-b | S2/S3 | `risky_roll/views.py:414-421`; `guess_cog.py:1745-1789`; `risky_roll_cog.py:136-155` | Free-text question/confession fields (risky roll, guess confess) have no content filter; `start_no_ping`+`skip_min_game_time` lets an opener cherry-pick when to close a round. | Apply the nick denylist to free text; enforce `min_game_seconds`/min-rolls on no-ping rounds. |

### NSFW gating (systemic across the party suite)

| ID | Sev | Evidence | Finding |
|---|---|---|---|
| M4-nsfw | **S2** | `question_source.py:182-211` (`allow_nsfw=True` default); `games_ffa_cog.py:272-281`; `games_clapback_cog.py:62,579`; `constants.py:91,95` | NSFW rows are eligible by default; the `nsfw`/`allow_nsfw` key mismatch means the "spicier prompts: off" toggle is a no-op in FFA/Clapback and most bank games expose no toggle at all. The static-bank fallback separately hard-codes SFW, so NSFW behavior flips invisibly on whether the DB bank has content. |

Note: making NSFW default-on may be intentional (commit `8bca3c7`). The defect is the **inoperative toggle + help/manual copy that promises a clean mode** (see M4c/M4d). Decide the intended behavior, then make code and copy agree.

### Balance, scoring & griefing (per-game)

| ID | Sev | Game | Evidence | Finding |
|---|---|---|---|---|
| M2-nhie-a | S2 | NHIE | `games_nhie_cog.py:125-137` | Late joiners are lazily granted full lives on first vote → abstain-then-join for a full heart bar while rivals are low. Add a join lobby / pro-rate lives. |
| M2-mlt-a | S2 | MLT | `games_mlt_cog.py:396-406` | Cumulative crowns are tracked but **no final standings embed is ever shown** — the only cross-round score in the suite is invisible. |
| M2-mlt-d | S3 | MLT | `games_mlt_cog.py:497-503` | Start gate (≥3) inconsistent with continue gate (≥2); leaving mid-game can drop below the advertised minimum. |
| M2-wyr-b / M2-mlt-b | S2 | WYR/MLT/NHIE | `games_wyr_cog.py:147-153`; `games_mlt_cog.py:265-271` | "Pose Question/Prompt" is open to all members with no cap/approval → queue-flood griefing. Cap per-user queue depth; MLT already has `is_eligible_voter` to reuse. |
| M2-wyr-c | S2 | WYR | `games_wyr_cog.py:436` | Post-restart recovery splits on `" OR "`; options containing that literal corrupt on reload. Store options as a 2-element list. |
| M2-fan-a / M2-price-a | S3 | Fantasies/Price | `games_fantasies_cog.py:217-241`; `games_price_cog.py:210-248` | Authors can vote on their own entry / for themselves (Clapback and Rushmore correctly block this — apply the same guard). |
| M2-price-b / M2-ht-a | S3 | Price/HotTakes | `games_price_cog.py` `_run_round`; `games_hottakes_cog.py:122-126` | Vote phase can start with 1 submission (degenerate). Require ≥2. |
| M2-ttl-a | S3 | TTL | `games_ttl/logic.py:187-191` | "Best Guesser (0 correct)" lists *every* player when nobody guesses right. Guard `max_correct > 0`. |
| M2-traditional-a/c | S3 | Traditional | `games_traditional_cog.py:161-221` | Host can be picked to answer their own question; fully host-driven with no automation (stalls if host idles — mods can cover). |
| M2-clap-a | S4 | Clapback | `games_clapback/logic.py:82-85,166-168` | The +25 CLAPBACK bonus is structurally unreachable in 3-player games with no spectators (needs ≥2 voters). Document the floor. |
| M3-a (HP1v1) | S2 | Hot Potato 1v1 | `hot_potato/cog.py:175-184` | Challenger is hardcoded as the first bomb-holder (first-mover disadvantage); the group variant correctly randomizes. |
| M4-a (HPgroup) | S2 | Hot Potato Group | `hot_potato_group/game.py:68-77` | Fixed clockwise pass order lets a coalition always target one player. Allow choosing the recipient or randomize. |
| M5-a | S2 | Chicken | `chicken/game.py:89-93` | Simultaneous-crash loser = `min(crashers)` (lowest Snowflake) → older accounts systematically penalized. Randomize. |
| M2-a (Pressure) | S2 | Pressure Cooker | `pressure_cooker/game.py:71-134` | Zero player agency — outcome is pure RNG, with real nickname stakes. Design consideration. |

### Dead / phantom / unplayable modes

| ID | Sev | Evidence | Finding |
|---|---|---|---|
| M2-ll-a | S2 | `games_legitlibs/__init__.py:43-47,115-116` | LegitLibs "Hot Seat" is an offered slash choice but a stub; selecting it returns a misleading "no published templates" error. Remove the choice or return "coming soon." |
| M2-ll-b | S2 | `games_legitlibs/__init__.py:18-19` | The advertised `/legitlibs-admin killswitch` doesn't exist anywhere; `_MODULE_DISABLED` can never be set — a false emergency-off expectation. |
| M2-photo-a | S2 | `question_source.py:148-155`; `games_photo_cog.py:162-165` | Photo Challenge is bank-only with no AI fallback; scheduled runs skip silently on an empty bank. Elevate to WARNING + dashboard alert. |
| M2-ffa-b | S4 | `games_ffa_cog.py:205` | No length cap on custom prompt text (up to 6000 chars) → unreadable card. |

### Verified clean (do not re-flag)

Clapback & Rushmore self-vote blocks; TTL/Price tie handling; Story-Builder stall termination; nick-revert durability; Risky Roll SQLite state; Guess opt-in/opt-out consent model.

### Progression (M3)

Games and duels persist nothing to the XP economy (`xp_system.py:120-124` sources are text/reply/voice/image_react/grant only; grep of games/duels for XP = 0). `games_game_history` stores one opaque per-game row with no per-player score column; duels store only the temporary nick + cooldown. **Recommendation: keep games ephemeral by design and fix the README's "cohesive progression suite" framing.** Adding XP-for-games (Option B) or a separate currency (Option C) would layer a farmable incentive directly on top of the unresolved consent/NSFW gaps and demands new anti-abuse work; defer until those gaps close, and only then as a scoped, participation-based, capped Option B.

---

## Dimension 2 — Software practices

| ID | Sev | Evidence | Finding | Recommendation |
|---|---|---|---|---|
| S1-loop | **S1** | `core/db_utils.py:24`; 270 AST-verified call sites; hot paths `events_cog.py:381,444,506`, `starboard_cog.py:80-164`, `voice_master_cog.py:415` | Sync `sqlite3` (fresh connect + 5 PRAGMAs incl. 256MB mmap per call) invoked directly inside `async def` gateway/command handlers; the FastAPI dashboard shares the same event loop (`server.py:354-368`). On a 2-core CPU-only box this is real latency, not theoretical. | Wrap DB access in `asyncio.to_thread` (pattern already correct at `xp_cog.py:236`); prioritize per-event hot paths and shared-loop web routes; converge on `aiosqlite`. |
| S2-god | S2 | `app_context.py:432` (89 reads); `__main__.py:156-175` (7 `# type: ignore` registries; `active_views` 143 reads) | Shared state is `AppContext` + ad-hoc monkey-patched `bot.*` attributes, not a clean boundary. | Collect games registries into a typed `GameRuntime` owned by `AppContext`. |
| S2-config3 | S2 | `app_context.py:63,290,432` | Guild config modeled three times (`RuntimeConfig` TypedDict, mutable `AppContext` flat fields, frozen `GuildConfig`); call sites read two ways. | Make `ctx.guild_config(gid)` the single source of truth. |
| S3-swallow | S3 | 118 AST-verified `except Exception: pass`, concentrated in `games_price_cog.py` (18), `games_rushmore_cog.py` (17), `games_ama_cog.py` (11) | Broad silent swallows discard bugs (KeyError/AttributeError) and bypass the central tree handler. | Narrow to expected types or add `log.exception`. |
| S3-deadhandler | S3 | `events_cog.py:1010-1021` | `on_app_command_error` cog listener is never dispatched by discord.py (verified against installed lib) — dead. | Delete or rename to `cog_app_command_error`. |
| S3-migrate | S3 | `migrations/__init__.py:70,97` | Idempotency only catches "duplicate column"; a crash between final commit and the version INSERT re-runs statements → a `CREATE TABLE` without `IF NOT EXISTS` would raise. | Wrap each migration's statements + version-insert in one transaction. |
| S3-extdrift | S3 | `__main__.py:176-241` | Manual 62-entry load list; a new cog omitted from it loads silently nowhere (no current drift). | Boot-time assert list vs discovered `cogs/*`. |

### Security (web portal) — latent/hardening (prod currently sound)

| ID | Sev | Evidence | Finding |
|---|---|---|---|
| S3-authopen | S2 | `server.py:151-168`; `__main__.py:637` | Missing OAuth env → silent `OpenAuth` (admin-to-all); code default bind `0.0.0.0`. **Fail closed** (refuse/force loopback) instead. |
| S3-ratelimit | S2 | `server.py:119` | Rate-limit buckets key on `request.client.host` = `127.0.0.1` for all users behind the Cloudflare loopback origin → one global bucket; any client can lock everyone out of `/login`. Enable trusted-proxy forwarded-IP. |
| S3-cookie | S3 | `auth.py:120,152-164` | Session cookie is signed (not encrypted) and carries the live Discord access token in readable base64. Drop the token or encrypt. |
| S3-staleperms | S3 | `auth.py:238-252`; `meta.py:118` | On guild-cache miss, stale login-time perms are trusted and removed members retain access until cookie expiry. Fail closed on cache miss. |
| S3-secretlen / S3-logout | S3 | `server.py:160`; `oauth.py:311-315` | `SESSION_SECRET` accepted with no length check; `/logout` is a CSRF-able GET. |

### Tests & CI

| ID | Sev | Evidence | Finding |
|---|---|---|---|
| S1c-pyright | S2 | `pyproject.toml:44-47` | Pyright **excludes the entire games cog surface** (`games_*`, `games_legitlibs`, `games/`) — type errors in the largest, most-edited area are invisible. |
| S1b-jslint | S2 | `test.yml`, `test_lint.py` | eslint/stylelint `pytest.skip()` because CI never runs `npm install` → **frontend JS is never linted in CI**. |
| S2a-oauth | S2 | `routes/oauth.py`, `spotify_oauth.py` | The OAuth callback (session-cookie-setting, security-sensitive) has **zero tests**. |
| S2b/2d/2e | S3 | `routes/reports.py` (18/28 untested), `scheduled_games.py`, `rules_watch.py` | Large untested route surfaces, incl. `/run-now` and AI-label mutation. |
| S2c-qsource | S3 | `games/utils/question_source.py` | The bank+AI dual-fallback path (every `get_*` + `_ai_generate`) is untested and pyright-excluded. |
| S1a-cov | S3 | `test.yml`, `pyproject.toml` | No coverage collection/threshold in CI. |

*Note: pytest is not installed in `.venv`; the test analysis is static (see project memory on the verify workflow). CI itself does run ruff → pyright → pytest on py3.10.*

---

## Dimension 3 — Docs & commands

### Documentation drift matrix (sample)

| Spec | Status | Evidence |
|---|---|---|
| `wellness_guardian_spec.md` | **PHANTOM** | Documents ~22 `/wellness …` commands; code has 3 (`setup`, `away on/off`). Rest is web-dashboard-only. |
| `games_system_spec.md` | **STALE + PHANTOM + COUNT** | "19-game" header (code 17); standalone `/ffa` format (real `/games play ffa`); phantom admin commands (portal-grant, legitlibs-admin); omits photo. |
| `dk_pvp_games_suite_spec.md` | **STALE + PHANTOM** | `dk/cogs/games/` path doesn't exist; `BaseGame / BaseGame` copy-paste bug; §9.3 Minesweeper Duel & §9.6 Liar's Dice fully specced, zero code. |
| `guess_spec.md` | PHANTOM | `/guess optout` and `/guess stats` don't exist; real `/guess prompt` undocumented. |
| `README.md` | COUNT + STALE | "16-game" (code 17, photo missing); `/games consent` (real `/consent`); Pen Pals + `/rename` undocumented. |
| `duel_minigame_flows_v2.md` | PHANTOM | Liar's Dice + Minesweeper flows; unbuilt. |
| CURRENT (verified accurate) | — | `DEPLOYMENT.md`, `xp_spec.md`, `risky_roll_spec.md`, `ai_moderation_spec.md`, `whisper_spec.md`, `confessions_spec.md`, `starboard_spec.md`, `voice_master_spec.md`, `dungeon_keeper_jail_ticket_spec.md`, `rules_watch_cog.md`, `bios_cog_spec.md`. |

**No docs index exists.** Aspirational design-specs are co-mingled with accurate operational references and a reader can't tell which is which. Recommend a `docs/INDEX.md` with one-line summaries + a "design-spec vs. reference" + "not-yet-implemented" distinction, and moving the 3 root specs into `docs/`.

### Command surface

| ID | Sev | Evidence | Finding |
|---|---|---|---|
| Cmd-perms | S2 | `games_config_cog.py:169`; game-package `config` subcommands (`chicken/cog.py:412` etc.) | Several mod/admin commands rely on runtime checks but lack `@app_commands.default_permissions`, so Discord shows them to everyone until invocation fails. |
| Cmd-naming | S3 | `dm_perms_cog.py`, `xp_cog.py`, `music_cog.py:611`, `hot_potato_group` | Inconsistent naming: snake_case (`/dm_help`, `/xp_give`, `/247_status`) and un-separated groups (`/games hotpotatogroup`, `musicalchairs`) vs the bot's own kebab-case convention. |
| Cmd-nesting | S3 | `command_groups.py:14` vs game packages | `/games play <game>` (party) vs `/games <game> start` (duels) — two inconsistent depths with no discoverable rule. |
| Cmd-dupe | S3/S4 | `games_config_cog.py:61` vs `:183`; `emoji_stealer_cog.py:301,373` | `/games end` duplicates `/games config game-end`; `/steal_emoji` duplicates the "Steal Emoji" context menu. |
| Cmd-dead | S4 | `commands/drama_commands.py:166`, `role_grant_commands.py:173` | Two `register_*` functions defining commands (`/chilling_effect`, a second `/grant`) are never called. |

### Player-facing help copy

| ID | Sev | Evidence | Finding |
|---|---|---|---|
| M4a | S2 | `games_help/logic.py:20-38` | `/games help` lists standalone `/ffa`, `/wyr`, … for **every** game — all now `/games play <name>`; copying them yields "command not found." |
| M4b | S2 | `games_help/embeds.py:43-52` | Help renders broken rows for Pressure Cooker (`/pressure`) and Risky Rolls (`/risky_roll`) with blank descriptions; neither command exists. |
| M4c | S2 | `manual.html:1330-1391` vs `consent_check.py:5-7` | Manual promises consent enforcement that is disabled — the flagship truth-in-advertising problem. |
| M4d | S2 | `constants.py:186,195`; `games_ffa_cog.py:202` | HOW_TO_PLAY/param help advertise an NSFW off-switch that doesn't work (see M4-nsfw). |
| M4e/M4f | S2 | `constants.py:348-361`; `constants.py:340` | Pressure Cooker help lists 4 deleted commands; LegitLibs help names Quiplash as default (real default `classic`) and advertises the unimplemented Hot Seat. |
| M4h/M4k | S3 | `constants.py:308-321`; `constants.py:175-362` | Price/Rushmore break the numbered-step house format (dev jargon "modal", no buttons/timer named); Risky Rolls has no HOW_TO_PLAY entry; manual omits Photo Challenge. |

---

## Dimension 4 — UX & design

### Accessibility

| ID | Sev | Evidence | Finding |
|---|---|---|---|
| U1a | S2 | `js/ui.js:1-9` | Toasts/status have no `aria-live` region → silent to screen readers. |
| U1b/U1c | S2 | `js/ui.js:27-87`; `transcript-modal.js:20-64` | Dialogs/modals lack `role="dialog"`/`aria-modal`, focus trap, focus return; `confirmDialog` has no Escape. |
| U1d | S2 | 6 panels (`mod-jails`, `mod-tickets`, `mod-policy-tickets`, `todo`, `rules-watch`) | `role="tablist"` declared but children lack `role="tab"`/`aria-selected` + no arrow-key nav — an invalid partial ARIA pattern. |
| U1e | S2 | `js/filter-select.js:79-326` | Custom combobox missing the entire ARIA combobox pattern; no keyboard option traversal → unusable without a mouse. |
| U1f/U1g/U1h | S2/S3 | `js/app.js:465,495,600-611`; `panels/rules-watch.js:43,293` | Nav section headers and guild picker are click-only `<div>`/`<li>` (not focusable, no `aria-expanded`); `role="button"` rows respond to click but not Enter/Space. |
| U1i/U1j | S3 | `index.html:30`; `js/slider.js:27-28` | Nav filter input and range sliders have no labels/`aria-valuetext`. |
| U1k | S3 | `app.css` tokens | `--ink-mute` (#80848e) on `--bg-card` (#2b2d31) ≈ 4.1:1 — fails WCAG AA for the small nav/hint text it's used on (estimated; verify with a contrast tool). |

### Responsiveness / iOS Safari

| ID | Sev | Evidence | Finding |
|---|---|---|---|
| U2a | S2 | `app.css:2756` | Only `.data-table` gets mobile horizontal-scroll; `.table`/`.w-table`/`.rw-table`/`.hm-table` clip at ~390px under the panel's `overflow-x:hidden`. |
| U2b | S3 | `app.css:643,689,1011` | Every input is 13px → iOS Safari zoom-on-focus on every form. Bump to 16px on mobile. |
| U2c | S3 | `app.css:61`; `connection-graph.js:50` | `height:100vh` on the app shell → iOS dynamic-toolbar overflow; switch to `100dvh`. |
| U2d/U2e | S3/S4 | `app.css:603,2717,3214` | Sub-44px tap targets; fixed hamburger/toasts ignore `env(safe-area-inset-*)` (viewport meta lacks `viewport-fit=cover`). |
| U2f | S4 | `app.css:1489` | `.mod-stats` stays 4-up at 390px. |

### Design-system consistency

| ID | Sev | Evidence | Finding |
|---|---|---|---|
| U3a | S3 | 631 `style=` occurrences; worst: `games-legitlibs.js` (55), `games-panel-shared.js` (37) | Inline styles undercut the CSS-token system. Add spacing/text/`hidden` utility classes; migrate worst offenders first. |
| U3b/U3c | S3 | 7 panels raw `fetch()`; `api.js`/`config-helpers.js`/`wellness-helpers.js` | Three divergent fetch-wrapper ecosystems with different 401-redirect behavior; 7 panels bypass the wrapper (some skip `res.ok`). Extend `api.js` with `apiPut/apiDelete`, consolidate. |
| U3d/U3h | S3/S4 | `esc()` redefined in 15 panels; `escText` in `app.js:636` | HTML-escape helper copy-pasted ~17 ways instead of imported from `api.js`. |
| U3e | S3 | `mod-jails.js:316` + 4 more | Filter/tab-strip DOM+handler copy-pasted across 5 panels (also the source of U1d). Extract `makeTabStrip()` with correct ARIA. |
| U3f/U3g | S3/S4 | `api.js:36-84`; ~140 empty + ~175 loading sites | `api()`/`apiPost()` duplicate their 401/error blocks; empty/loading states hand-rolled per panel. Add `renderLoading()`/`renderEmpty()`. |

---

## Remediation backlog (ranked by impact ÷ effort)

> Status annotations added 2026-07-21; unannotated items remain open.

Each item traces to pinned findings above. Grouped by tier; within a tier, cheaper-first.

### Do first — S1, low/medium effort
1. **Decide consent policy, then make code+copy agree** (M1a–d, M2-ama-a, M4c). Either re-arm `check_consent`/`format_name` + restrict AMA hot-seat to volunteers, or remove the UI/manual promises. *This is the single highest-value fix — safety + truth-in-advertising in one.*
2. **Reconcile NSFW toggle** (M4-nsfw, M4d): thread one `allow_nsfw` value end-to-end, unify the `nsfw`/`allow_nsfw` key, and fix help/manual copy to match the chosen default. *Low effort, closes a safety+honesty gap.*
3. **Rate-limit `/guess submit`** (M8-b) and **apply the nick denylist to free-text question/confession fields** (M7-c, M8-e); switch nick filter to NFKC (M0-base-e). *Small, contained.* *(M8-b ✅ done — 842302e, dashboard-configurable submission flood cap + per-round cap; the denylist/NFKC parts unverified.)*
4. **Guard concurrent duel sentences** (M0-base-b) and **re-apply nick on rejoin** (M0-base-d). *Prevents durable griefing + data corruption.*
5. **Fix `/games help` command strings** (M4a, M4b) — mechanical, removes "command not found" for every game.

### Do next — S2, medium effort
6. **Offload the hottest blocking DB calls to `asyncio.to_thread`** (S1-loop), starting with the per-event gateway handlers and shared-loop web routes. *Biggest latency win on the constrained box; incremental.*
7. **Fail closed on missing auth env / loopback bind** (S3-authopen) and **fix rate-limit IP keying** (S3-ratelimit).
8. **Re-enable pyright on games cogs incrementally** (S1c) and **add `npm ci` + JS lint to CI** (S1b). *Stops the largest feature area from being unchecked.*
9. **Remove/deflag dead & phantom game modes** (M2-ll-a/b, M2-photo-a): drop LegitLibs Hot Seat choice, implement or remove the killswitch, alert on empty photo bank.
10. **Add missing `default_permissions`** to mod/admin commands (Cmd-perms).
11. **MLT final standings embed** (M2-mlt-a) + queue-flood caps on Pose Question/Prompt (M2-wyr-b, M2-mlt-b) + self-vote guards (M2-fan-a, M2-price-a).
12. **Accessibility core**: `aria-live` toasts, dialog/modal roles+focus trap, `makeTabStrip()` with ARIA (U1a–e, U3e). *One shared-widget pass fixes many panels at once.* *(U3e ✅ done — 617c855 shared tab-strip; the toast/dialog ARIA scope unverified.)*
13. **iOS quick wins**: 16px inputs, `100dvh`, `.data-table` scroll rule generalized (U2a–c).

### Docs & cleanup — S2/S3, low effort
14. **Fix README** (game count, `/consent`, Pen Pals, `/rename`, drop "progression suite" framing per M3).
15. **Add `docs/INDEX.md`** + mark the 3 aspirational specs (`wellness_guardian`, `games_system`, `dk_pvp_games_suite`) as design-spec/not-yet-implemented; move root specs into `docs/`. ✅ done — `docs/INDEX.md` exists and classifies every spec (aspirational specs flagged; 2026-07-15 correction pass folded in).
16. **Delete dead code**: unused `register_*` command functions (Cmd-dead), dead error handler (S3-deadhandler), `escText`/duplicate `esc` (U3d/h). ✅ done — eeb52b6 (dead-code removal); `esc` convergence in e782b4b.

### Backlog — S3/S4
17. Config-model unification (S2-config3), `GameRuntime` typing (S2-god), narrow broad excepts (S3-swallow), migration transactions (S3-migrate), extension-registry assert (S3-extdrift), command naming/nesting consistency (Cmd-naming/nesting), design-system utility classes (U3a), fetch-wrapper consolidation (U3b/c), remaining per-game clarity nits. *(S2-config3 ✅ done — 88d81d6 + 2e85a52; S2-god ✅ done — typed-Bot refactor, `plans/typed-bot-refactor.md` stages 1–3 complete; U3a/U3b/c ✅ done — 894987b, e782b4b, 617c855. The rest of this item remains open.)*

### Explicitly deferred
- **Games↔XP progression** (M3): keep ephemeral by design; revisit only after consent/NSFW gaps close, as a scoped capped participation-XP.

---

## Coverage ledger

> Point-in-time snapshot as of the 2026-07-01 review — not maintained since.

**Independently confirmed by the author (re-read live):** the consent no-op (M1a–d); the gitignored DB/`.env` (non-finding); and, on the adversarial spot-check pass, the three headline S1s — the AMA forced hot-seat (M2-ama-a), the concurrent-duel-sentence corruption chain (M0-base-b), and a sampled hot-path blocking DB call (`events_cog.py:381`).

**Reported with `file:line` by workstream agents (evidence cited, not each independently re-read):** all 16 party games + 6 PvP games + Risky Roll + Guess mechanics; NSFW gating; XP/progression integration; architecture spine (app_context, DB idioms, migrations, error handling, extension loading); web-portal security (auth, authz coverage, sessions, rate-limit, SQL, XSS, CSRF); full command surface catalog (~170 cmds/28 groups/6 menus); ~15-spec doc drift matrix; test suite + CI gates (static); web-portal accessibility (106 panels), responsiveness/iOS, design-system; player-facing help + manual copy. These are well-evidenced but a full finding-by-finding re-verification was not performed; treat any single finding as confirmable-in-one-read via its citation rather than as author-verified.

**Sampled, not exhaustive:** the 501 `innerHTML` sinks (spot-checked the user-content-rendering ones — clean); the 270 blocking-DB sites (AST-verified count is a floor); per-spec drift beyond the ~15 sampled; per-command handler bodies within mega-cogs.

**Skipped / not runnable:** live pytest + coverage % (pytest absent from `.venv`); actual NSFW bank row contents; live browser/screen-reader testing (heuristic only); music/lavalink runtime behavior; wellness/reports analytics correctness (reviewed only for structure/security, not statistical validity).

---

*Generated by an audit-only multi-agent review. All findings are evidence-pinned; recommendations are proposals for a follow-up implementation session, not changes made here.*
