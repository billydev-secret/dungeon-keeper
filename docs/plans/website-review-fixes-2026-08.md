# Website review fix queue — 2026-08

Works the queue in `docs/reviews/2026-08-06-website-deep-review.md` (deep review
of the dashboard: UX/organization, per-panel rendering, backend security and
performance). The empirical baseline at review time was green — browser suite,
168-panel layout scan, authz/snowflake sweeps, all linters — so every item here
is a code-level defect, not a reproduced rendering failure.

Branch: `website-review-fixes`, cut from main at 78053cf4. Main moved after the
review was written, so each stage re-verifies its finding against current code
before changing anything (one nit, the confessions jump links, was already
fixed upstream by 5a5b3c2e).

## Stages

### Stage 1 — shared frontend layer (enables the panel sweep)

The two unsafe-by-default shared widgets, plus the helpers the panel sweep
consumes.

- **S1** `table.js` escape-by-default + per-column `html` opt-in; six panels
  feeding raw Discord display names into it (voice-activity, greeter-response,
  interaction-graph, retention, xp-leaderboard, quality-score). Stored XSS.
- **S2** `resetMetaCaches()` exported from config-helpers and called in
  `applyMeData()` — stops config panels showing (and saving) the previous
  guild's channels/roles/members after a guild switch.
- **F1-helper** shared `mountAsync` wrapper (error state instead of a permanent
  spinner) — reference implementation only; the sweep is Stage 4.
- **F3** leaked poll in policy-tickets (discarded unmount handle) and the
  per-load ResizeObserver in wellness-caps.
- **F4-mechanism** shared "don't clobber sibling card edits on save" helper.
- Nits: md-preview link href capture group, todo.js hash id (`todo` →
  `mod-todo`), guardForm dirty false-positive from filter-select's search box,
  filter-select listener teardown.

### Stage 2 — backend security

- **B-SEC1** guild-scoped `perms_bits` (stale login-time bits must not grant
  admin in a second guild when the live cache misses) — fail closed.
- **B-SEC3** role menus: `elevated` flag is caller-supplied, so a moderator can
  publish a self-grant button for an admin role; require admin server-side.
- **B-SEC2 + B-PERF2** regex message search: stream, bound rows, bound match
  time. One fix for both the ReDoS (holds the GIL on the bot's own event loop)
  and the ~452k-row in-process pull.
- **B-SEC7** members see hidden channels in home `top_channels`.
- **B-SEC8** wellness `scope_target_id` bare-int snowflake.
- **B-SEC4/5/6** drop the unused OAuth token from the cookie; real logout
  invalidation; escape the Spotify callback error.
- **B-SEC10** Origin/Referer check on state-changing verbs.
- **B-PERF7 (partial)** role_menus N+1, `/meta/members` unbounded.

### Stage 3 — backend performance

- **B-PERF3** PIL upload work off the event loop (`to_thread`).
- **B-PERF1** new `(guild_id, created_at)` index on `xp_events` — independent of
  the deferred retention/rollup work.
- **B-PERF4** sentiment tile through the shared cache + an index/table-source
  fix for the scan.
- **B-PERF5** advisor inline DB reads off the loop.
- **B-PERF6** health deep-dives through `get_cached` — cache key must include
  every parameter that changes the population, or it serves wrong data.
- **B-PERF7 (partial)** games/economy_manager N+1s.
- **B-SEC9** pin the avatar fetch to the validated IP (DNS-rebind TOCTOU),
  keeping the per-hop redirect validation and 8MB cap.

### Stage 4 — panel sweep

- **F1** apply `mountAsync` across the ~27 config/settings panels that hang on
  "Loading…" when the first fetch fails.
- **F2** `config-cleanup.js` escapes channel/**thread** names (thread names are
  member-supplied — the one config panel importing no escaper).
- **F4** apply the Stage 1 mechanism to config-auto-react and the other
  multi-card panels.
- Nits: config-prune failed-write row removal (migrate to `mountExemptionList`),
  config-spoiler unvalidated threshold, games-config audit channel can't be
  cleared, economy table truncation notes, announcements listener stacking.

### Stage 5 — information architecture

- **IA1** regroup Games (Operations / Live Games / Question Banks) and re-home
  the four social features; Confessions is the genuinely misfiled one.
- **IA2** document the real rule — settings live with the data they produce —
  in the Configuration help page and `docs/`.
- **IA3** URL/deep-link state for the mod workflow panels (tickets, jails,
  rules-watch, qa-tracker, todo).
- **IA5** naming pass: "AI (Local LLM)" → "AI Models"; Help nav reads the
  per-guild assistant brand instead of hardcoding it; write the route-id
  convention into CLAUDE.md.
- **IA4** Ctrl/Cmd+K command palette over panels + manual headings — the one
  net-new *feature* in the queue rather than a fix. Built last, additive only,
  and dropped if it can't land cleanly with tests.

## Ground rules for every stage

- Tests ship with the fix, and for a bug fix the test fails first. Web tests in
  `tests/web/`; the XSS, cross-guild, escalation and cache-key-collision tests
  are the load-bearing ones.
- Behavior change ⇒ matching spec in `docs/` updated in the same commit; UI/UX
  change ⇒ `manual.html` too.
- Migration numbers: check main for the next free number before adding one
  (collisions with concurrent branches are a known hazard).
- Full suite green before push; CI on the push satisfies it.
