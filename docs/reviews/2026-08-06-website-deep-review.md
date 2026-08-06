# Website deep review — 2026-08-06

Full read-through of the FastAPI dashboard: UX/organization, every panel's
rendering + UX, and the backend for security and performance. Seven parallel
reviewers (backend security, backend performance, three panel groups, IA, and
an empirical browser/lint pass), each briefed against the prior reviews so this
is **net-new** findings, not a re-run of the 2026-08-06 staged-review fix queue
(`2026-08-06-review-synthesis.md`) or the 2026-07-22 UX review.

## Empirical baseline — green

Everything that can be executed passes, so **every finding below is
static/code analysis, not a live rendering failure**:

- Browser suite (Playwright, `-m browser`): **16 passed**, incl. panel-console
  health (no JS error on mount) and phone/tablet layout checks.
- `mobile_layout_scan.py`: **168 panels × 3 viewports, 0 findings** (viewport /
  clipped / collapsed / error all zero).
- authz sweep + snowflake-precision sweep + help-links: **18 passed, 1 skip**
  (the skip is a stale `#role-menus` example placeholder, not a defect).
- eslint 7 warnings (known-clean baseline, all `no-unused-vars`), stylelint
  clean, ruff clean, pyright 0/0/0.
- Non-fatal: ~84 `datetime.utcfromtimestamp()` deprecation hits at
  `bot_modules/services/health_metrics.py:568` — future-Python risk only.

## Two shared-layer fixes that cascade

These are the highest-leverage items in the whole review: each is a one-file
change that closes a class of bugs the panel reviewers kept re-finding.

### S1 — `table.js` does not escape cell content (stored XSS) — **High**

`src/web_server/static/js/table.js:78-81` interpolates both the raw value and
any `format()` return straight into `innerHTML`; escaping is opt-in per column.
Verified. Six panels feed it **raw Discord display names** (member-controlled,
32 chars is enough for `<img src=x onerror=…>`), which then execute in the
moderator dashboard:

- `panels/voice-activity.js:84`, `panels/greeter-response.js:36,41`,
  `panels/interaction-graph.js:142`, `panels/retention.js:77`,
  `panels/xp-leaderboard.js:149`, `panels/quality-score.js:159` — all
  `format: (v, r) => r.user_name || r.user_id`, no `esc()`.

That escaping is the caller's job is proven by `inactive-report.js:108` and
`intake-report.js:67`, which wrap the identical column in `esc()`. **Fix:** make
`table.js` escape by default (escape the `raw ?? ""` path; treat `format`
output as text unless a column opts in with an explicit `html: true`), then drop
the per-panel `esc()` wrappers. Closes every XSS instance found across all three
panel groups in one edit.

### S2 — meta caches never reset on guild switch (cross-guild data bleed) — **High**

`config-helpers.js:10-12` holds `_channels/_categories/_roles/_members/_bots` as
module globals; `/api/meta/*` is scoped to the **active** guild
(`routes/meta.py:366-367`). But `applyMeData()` (`app.js:913-935`) calls only
`_resetPanelSpecCache()` — nothing clears the meta caches (verified). After a
guild switch, every config panel lists the **previous** guild's channels / roles
/ members, and saving writes a foreign guild's snowflake into the new guild's
config. This is the sharpest data-integrity bug in the dashboard, and it lives
in the shared layer. **Fix:** export `resetMetaCaches()` from config-helpers and
call it in `applyMeData` next to `_resetPanelSpecCache()`.

## Backend — security

Overall the auth architecture is **sound and unusually disciplined**: one
`require_perms` dependency family, live per-request permission resolution from
the gateway cache (Discord demotions take effect instantly), fail-closed
bootstrap, ownership checks inside the write transaction (no IDOR found), and an
`app.openapi()`-driven authz sweep that makes a forgotten guard a CI failure.
The soft spots are all at the seams.

**High/Med:**

- **B-SEC1 (Med) — guild-switch carries stale permissions.**
  `auth.py:175-184` re-signs the session with a new `guild_id` but keeps
  login-time `perms_bits`/`role_ids` from the *original* guild;
  `select_guild` (`meta.py:116-199`) doesn't refresh them. The per-request
  fallback (`auth.py:238-252`) applies them unqualified. Live cache resolution
  masks it, but on a cache miss (startup window, bot kicked from the guild,
  standalone mode) an admin of guild A carries admin bits into guild B. **Fix:**
  key `perms_bits` by guild id; in the fallback use them only when
  `perms_guild_id == active_guild_id`, else empty perms.
- **B-SEC2 (Med) — moderator regex search can freeze the whole bot (ReDoS).**
  `messages.py:250-270`: an arbitrary mod-supplied regex runs in Python over the
  no-LIMIT result set (all ~452k guild messages with no other filter). A
  catastrophic-backtracking pattern holds the GIL inside `re.search`; because
  the dashboard runs on the **bot's event loop** (see performance section), the
  Discord gateway stalls too. **Fix:** require a narrowing filter or row cap
  before the regex branch, and match with a per-row timeout (the `regex` module,
  or a SQLite `REGEXP` budget). *Folds together with B-PERF2 — same code path.*
- **B-SEC3 (Med) — role menus are a moderator→admin escalation.**
  `role_menus.py`: all routes are moderator-gated (`:27`), and
  `_check_roles_against_guild` (`:277-310`) permits a dangerous (admin-bit) role
  whenever the option's `elevated` flag is set — a flag the *same moderator*
  supplies in the request body (`:46`). A mod can publish a self-serve button
  for an admin role and click it. Audited, not prevented. **Fix:** require
  `admin` on the caller when any option is dangerous.

**Low (documented in full in the sub-report; the notable ones):**

- **B-SEC4** — unused Discord OAuth access token stored in the signed-but-not-
  encrypted session cookie (`auth.py:150-164`); stop storing it.
- **B-SEC5** — logout is cosmetic; no server-side revocation, cookie valid for
  the full 30-day `max_age`. Add a session generation/nonce.
- **B-SEC6** — reflected XSS in the Spotify callback error branch
  (`spotify_oauth.py:101-104`, unescaped `error` param); very narrow reach.
  `html.escape(error)`.
- **B-SEC7** — members see all-channel top-5 (incl. hidden/staff channels) in
  `home.py:33-61`; `top_channels` isn't visibility-filtered like the voice list
  next to it.
- **B-SEC8** — wellness cap `scope_target_id` returned as a bare int snowflake
  (`wellness_routes/api.py:91`) — snowflake-sweep blind spot; `str()` it.
- **B-SEC9** — avatar SSRF guard has a DNS-rebinding TOCTOU
  (`config.py:4632-4637`); admin-only, so Low. Pin the connection to the
  validated IP.
- **B-SEC10** — CSRF posture rests entirely on `SameSite=Lax`; only
  state-changing GET is a harmless `/logout`. Acceptable; an Origin check on
  mutating verbs would harden it.

Well-contained by design (not findings): the `SUPPORT_USER_ID` backdoor
(per-guild admin opt-in, live-checked), the logs SSE firehose (admin-only), and
message-content exposure (moderator+-gated, consistent with the shipped
disclosure fix).

## Backend — performance

The dashboard runs uvicorn **on the bot's own event loop, in the bot's
process** (`server.py:464`), so loop-blocking is the severity multiplier. An AST
scan confirmed only **2 of 340+ handlers** bypass the off-thread `run_query`
convention. Findings are peripheral, not architectural.

- **B-PERF1 (Med) — `xp_events` missing a `(guild_id, created_at)` index.**
  `home.py:302-309` runs two `WHERE guild_id=? AND created_at>=?` aggregates on
  every landing-page load; the only indexes are guild-prefix-wider, so each
  walks all ~1M rows (~0.3–0.6s thread time per `/home`, uncached). Off-loop,
  but it's the biggest recurring query cost. **Fix:** one-line
  `CREATE INDEX idx_xp_events_guild_created ON xp_events(guild_id, created_at)`.
  Independent of the deferred `xp_events` retention/rollup work (synthesis §9).
- **B-PERF2 (Med) — regex search materializes ~452k rows in-process.**
  `messages.py:250-270` `fetchall()`s the whole content set (~150–300MB
  transient) before filtering in Python. **Fix:** `fetchmany(2000)` loop keeping
  only matches, or push `REGEXP` into SQL. *Same path as B-SEC2.*
- **B-PERF3 (Med) — PIL image work runs inline on the event loop.**
  `config.py:3285-3346` (quote-border upload: decode + alpha scan + thumbnail +
  save + `analyze_border_opening`) and `economy.py:373` (`_normalize_icon`) do
  0.5–2s of CPU **on the bot's loop** for an 8MB source — heartbeats and every
  other request stall. Admin-only/rare, but the only macroscopic loop-blockers
  found. **Fix:** `await asyncio.to_thread(...)` the decode/analyze/save block
  (already pure bytes-in/bytes-out).
- **B-PERF4 (Med-Low) — sentiment tile scans `message_sentiment` per request,
  uncached.** `health.py:261-302,734-758`: guild-prefix-only scan + rowid
  lookups + temp sort, 3–5× per tiles load, and it's the one tile that skips the
  15-min cache. **Fix:** query `messages` directly (it carries duplicated
  `sentiment` with `idx_messages_sentiment`), and route it through `get_cached`.
- **B-PERF5 (Low)** — advisor route (`advisor.py:107-133`) does inline blocking
  DB reads on the loop (one of the 2 convention bypasses; the other is a
  once-ever OAuth write). ms-scale + `ai`-tier limited; wrap in `to_thread`.
- **B-PERF6 (Low)** — health deep-dive endpoints (`health.py:557-953`) all
  bypass the tile cache and recompute per request; fine while click-driven,
  wire through `get_cached` if a panel ever polls one.
- **B-PERF7 (Low)** — small N+1s in low-volume admin surfaces
  (`games.py:787-795` per-template re-SELECT; `economy_manager.py:241-250`;
  `role_menus.py:175-179`) and `meta.py:287-346` `/meta/members` returns every
  current + departed user unpaginated (watch as churn grows).

Keep: the `run_query`/`cached_run_query` + report-cache + startup warmer stack,
the DB-backed 15-min health-tile cache, bulk name resolution everywhere, hard
LIMIT caps on audit/list surfaces, and per-boot cache-busting.

## Frontend — cross-panel systemic patterns

Beyond S1/S2, four patterns recur across the ~130 panels:

- **F1 (High) — ~27 config panels + several settings panels hang on
  "Loading…" forever on a failed initial fetch.** The async mount IIFE has no
  `.catch`, so a config 500 / network drop leaves a permanent spinner and a
  console rejection. Affected: most `config-*.js`, plus `economy-config`,
  `economy-qotd`, `economy-sinks`, `xp-settings`, `intake-settings`,
  `pen-pals-settings`, `birthday-settings`, `rules-watch-settings`,
  `policy-tickets-settings`, `config-bios` (3 tab loaders). The report/queue
  halves already learned to catch; the config/settings halves didn't. **Fix:**
  one shared `mountAsync(container, loader)` (or `mountConfigPanel`) wrapper →
  `renderError`. Highest-count frontend item.
- **F2 (Med) — config-cleanup XSS.** `config-cleanup.js:312-316` renders a
  channel/**thread** name (thread names are member-supplied) into innerHTML with
  no `esc()` — the one config panel that imports no escaper. `esc(chName)`.
  (Distinct from S1: this one is hand-built HTML, not `table.js`.)
- **F3 (Med) — leaked timers/observers on unmount where composition hides the
  handle.** `policy-tickets.js:28` drops the handle from `mountTickets`, so the
  45s poll in `mod-policy-tickets.js:214-223` never clears — one leaked poll per
  visit. `wellness-caps.js:297` creates a `ResizeObserver` per load, never
  disconnected, no `unmount`. Composer pages (`intake`, `birthday`, `pen-pals`)
  return nothing. Interval hygiene is otherwise good (economy-stats, system-
  stats, live-log all clean). **Fix:** forward the handle; a dev-mode warning
  when a panel with timers returns null would prevent recurrence.
- **F4 (Med) — re-render-on-save discards sibling edits.** In multi-card panels
  (`config-auto-react.js:168`, plus booster-roles / needle / roles), a
  successful save re-fetches and rebuilds all cards, silently dropping unsaved
  edits in other cards — and the dirty flag can't help because save cleared it.
  Update the saved card in place, or check `__dkDirty()` before re-rendering.

Assorted verified nits worth folding in when the file is touched: `md-preview.js:13`
uses link *text* as the href (`$1` not `$2`) — wrong link + forfeits the
https-only check; `todo.js:304` syncs to hash id `todo` but the route is
`mod-todo`, so reload → "Page Not Available"; `config-prune.js:258` removes a
preview row even when the exemption write failed (migrate to
`mountExemptionList`, which config-inactive already uses); `config-spoiler.js:197`
posts a blanked threshold as `0` (unvalidated); `games-config.js:242` audit
channel can't be cleared once set; `mod-anon-audit.js:126` /
`mod-confessions-audit.js:25` build `@me` jump links for guild messages (dead
links — use the guild id). Economy `economy-stats`/`bank-manager` tables
truncate at 100 with no "showing N of M" note.

**Sensitive-data audit came back clean:** every anonymity-piercing panel
(anon / confessions / whisper / DM audits, retention dials) is admin-gated
server-side (`moderation.py:1108-1449`) and matches its nav `adminOnly` flag; no
panel renders a real id next to content presented as anonymous to the viewer's
tier; snowflake-as-string discipline is explicit at the risky sites.

## Information architecture & UX organization

The 2026-07-22 UX review is **essentially fully addressed** — every nav/help
finding (W-N1…N15, W-H1…H7) is fixed or mostly-fixed, **nothing regressed**, and
a 9-feature manual-drift spot-check found **zero** misses (the per-commit
manual.html contract is being honored). The best IA work in the codebase is the
six report↔config panels merged into single panes with `lockUnlessAdmin`
read-only rendering, which dissolved duplicate labels and permission splits at
the root. Remaining structural items:

- **IA1 — Games is a 23-item flat list mixing four kinds of thing** (ops pages,
  per-game dials, question banks, and four social features). **Confessions** is
  genuinely misfiled — an anonymous-messaging feature whose audit lives under
  Moderation and help under "Games & Social." **Fix:** give Games the subgroup
  machinery Reports already uses (Operations / Live Games / Question Banks) and
  move Guess Who / Whisper / Pen Pals / Confessions to a small Social section
  (retiring the perms-exemption hack they need to survive the game-host gate).
- **IA2 — "settings live in Config" is now silently false for six features**
  (XP, Voice, Birthdays, Intake, Rules Watch, Policy Tickets moved into their
  report panels). Individually right, but the rule is written nowhere a user can
  see. **Fix:** state "settings live with the data they produce" in the
  Configuration help page + `docs/`.
- **IA3 — finish W-N1 (URL/deep-link state) where it was aimed: the mod
  workflow panels** (mod-tickets, mod-jails, rules-watch, qa-tracker, todo). The
  16 adopters so far are all *analytics* panels; the mod queues — where "link a
  colleague to this ticket tab" matters — still reset on refresh.
- **IA4 (growth)** — at 166 routes growing weekly, the highest-leverage
  structural bet is promoting the existing keyword nav filter to a **Ctrl/Cmd+K
  command palette** over panels *and* manual headings (80% of it already exists:
  AND-tokens, keywords, Enter-to-open). When lookup is instant, taxonomy
  disputes stop costing users. Second bet: declare "one feature = one panel" the
  target and finish the merge program (it's the only pattern that makes growth
  sublinear).
- **IA5 (naming)** — one pass: "AI (Local LLM)" → "AI Models"; make the Help nav
  read the assistant's per-guild brand instead of hardcoding "Ask Billy-bot";
  write the bare-id route convention into CLAUDE.md so the mixed vocabulary
  stops accreting.

## Suggested fix order

1. **S1 + S2** — two shared-file edits that kill the XSS class and the
   cross-guild write bug. Highest leverage.
2. **B-SEC2 / B-PERF2 together** (regex search: cap + timeout + streaming),
   **B-PERF3** (`to_thread` the image uploads), **B-PERF1** (the one-line
   index) — the loop-safety + cost items.
3. **B-SEC1** (guild-scoped perms) and **B-SEC3** (admin-gate elevated role
   menus) — the two auth-seam escalations.
4. **F1** — the `mountAsync` wrapper sweep (highest panel count), then **F2/F3**
   as small targeted fixes.
5. Lower-priority nits (link href, todo hash id, prune exemption, truncation
   notes) fold in when their files are next touched.
6. **IA** — schedule as its own pass; IA3 (mod-queue URL state) and IA1 (Games
   regroup) are the concrete near-term wins.

Full per-panel verdict tables and the complete Low/Info finding lists are in the
seven reviewer transcripts for this session; this document is the deduped,
prioritized synthesis.
