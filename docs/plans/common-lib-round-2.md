# Common library — round 2 review (2026-08-22)

Second consolidation pass over the whole repo (deep model + AST exact/near-clone
scans + four read-everything assessment agents). Round 1 (stage 1, merged
2026-08-21) is done and holding; this doc is the record of what remains, what
was verified as *deliberate* twinning, and the bugs the duplication was hiding.

Scanners: `clones.py` (exact, alpha-renamed AST hash), `near.py` (Jaccard
near-clones + name families), `jsclones.mjs` — copy them forward from the
2026-08-22 session scratchpad; each runs in seconds.

## Bugs found by the review (fix FIRST, as isolated commits, before refactors)

1. **Cross-guild refund bug (money, live).** `economy_emoji_service.py`
   `expire_stale_submissions` (~:396) has **no `guild_id` predicate** in its
   SELECT. Guild A's economy-loop pass expires + refunds guild B's pending
   emoji sponsorships using guild A's `emoji_sponsor_expire_days`; the
   after-the-fact filter at `economy_loop.py:1113` only suppresses the notice.
   Compare `run_sponsor_expiry` (:1141), which is guild-scoped and says why.
   Fix lands naturally with the shared `expire_stale` (see B below). Check
   prod for already-affected rows.
2. **Dashboard disable toggle not enforced in 6 of 15 games.**
   `check_game_enabled` is missing from the slash entries of ttl, hottakes,
   fantasies, story, compliment, mfk. Admin disables the game; it launches
   anyway. (CLAUDE.md: never ship a toggle that isn't enforced.)
3. **NHIE pose queue unbounded.** wyr/mlt cap queued questions at 15;
   `games_nhie_cog.py:71` appends unconditionally — floodable by any player.
4. **Missing view error handlers.** `hot_potato/views.py` and
   `quickdraw/views.py` have no `on_error`; exceptions die in discord.py's
   default handler with no context. (Fixed by the shared view, D below.)
5. **Missing unmount forwarding.** Dashboard pages `birthday.js` and
   `intake.js` return no `unmount` from `mount()` — same leak class as the
   wellness-caps F3b ResizeObserver bug. (Fixed by `composePage`, J below.)
6. Minor: `docs.js:88` hand-writes an error div instead of `renderError`
   (the one panel the July states.js pass missed); advisor + risky_roll
   `cog_app_command_error` re-derive `safe_ephemeral`'s done/followup dance.

## Verified deliberate / false-positive — do NOT re-litigate

- pin/sponsor `_card_embed`, `*_resolution_dm_text` (voice, per 915f43c9);
  `deny_submission` vs `withdraw_approved` (state-machine guards ARE the
  semantics); `_handle_resolution` x3 (divergent tails + ordering).
- `record_event` x4 — four unrelated contracts; two are privacy-specified
  (no_contact, anon_audit). Do not touch.
- The six mini-games' `on_game_start`/`render_*_state`/`handle_interaction`
  "clusters" — required overrides of `duels/base_game.py` abstract hooks
  (template-method pattern, ~1,420 lines already shared). 18 of 42 db.py
  name-family hits are already thin post-94341d5e wrappers.
- games embed builders (`build_lobby/recap/reveal/closed_embed`) — per-game
  copy; `_panel_ids` x6 — hooks of core/sticky.py, six different stores.
- `fetch_sweepable_games` x6 — threshold triples govern escrow sweeps; DO NOT
  unify. `create_game` x3 (column lists differ), `get_config` defaults dicts
  (per-game tuning), `game_from_row` x6 (mapping stays with the game).
- Settings repos guess/whisper (legacy guild-0 fallback is load-bearing);
  `_create_tables` x3 and `init_xp_tables`/`init_message_tables` (name/shape
  collision; the real question is lazy DDL vs migrations/, separately).
- Web: announcements vs scheduled_games route/service cores (race-guard +
  cascade differences are the product); `overview` x3, `get_history` x3,
  docs trio (correct layering); the `_q`/`run_query` closure idiom — only
  ~17% of 321 sites are single-expression; keep the idiom.
- JS: games-ama/wyr mounts (already the target state — 14-line specs over
  `mountGamePanel`); mod row-select handler x4; `refresh*` x4.
- `fmtAge`/`fmtTs` api.js vs panel variants (different inputs/rendering, per
  2ba1f7ba) — but the two identical *panel* copies do merge (D-cheap below).

## The queue (honest net ≈ 1,400–1,600 lines; estimates already discounted)

### A. Party-game cogs (~450–545 net) — biggest area
1. Shared slash-entry preamble `guard_and_launch` (~150–165) — carries bug
   fix 2; add enabled-guard tests FIRST (this path has none today).
2. `launch` tail `post_game_anchor` (~75–85) — send/Forbidden/end/pop +
   `update_game_message`+`update_session`; covered by
   test_games_launch_contract.
3. Round engine for nhie/wyr/mlt ONLY (`games/utils/round_engine.py`,
   ~60–90); delete dead `round_manager.py` (54, zero adopters). Do NOT
   force price/clapback/rushmore/ama into it. HARD CONSTRAINT: never
   normalize payload shapes (game_roster extractors key on them); never
   rename view attrs (`force_end_active_game` sniffs by name); custom_ids
   stay per-game (persisted in posted messages).
4. Riders: `register_game_cog` setup helper (~40), payout-footer send
   (~25), advance/pose button factory (~10–25, carries bug 3 + the dropped
   `_closed` guard in hottakes/fantasies), `toggle_side_vote` (~10–15),
   `add_player` promotion (mlt's capped version; rushmore/clapback gain the
   25-option cap), recover tail (~15–20), hand_off/run_again mixin (~15).

### B. Economy submissions + services (~190–210 net)
1. One commit on `economy_submission_store`: emoji `_refund` → refund_once
   (verbatim missed copy, ~19), shared `expire_stale` (~20 + **bug 1**;
   needs a guild-isolation test), `open_submission` x3 (~10).
2. `guess_model_cache.ensure_model` (face/pose, exact 13-line twin, ~12) +
   bonus `_box_for_indices` (~12). Zero risk.
3. `core/settings_store.py` — generic dataclass⇄config-KV load/save for
   econ/casino/music_playlist/qa (~70; the four hand-kept
   `_BOOL/INT/FLOAT/STR_KEYS` blocks are the real win — a standing bug
   class). Keep the one-query GLOB shape (hot money path, 41 call sites);
   beware `from __future__ import annotations` (field types are strings)
   and bool-before-int ordering.
4. `core/db_utils.patch_row` for color/icon catalogs (~15–30; guard column
   names with sql_identifier too).
5. Optional: `check_moderation_target` shared policy ladder for
   inactive/jail (~20, policy-drift insurance); `arm_schedule_now` in
   scheduled_games_service (~10, both routes).

### C. Duel/mini-game leftovers (~425 net)
1. Extract risky_roll's `on_error` (`services/risky_roll/views.py:302`) to
   core as `handle_view_error` — the ONLY correct one (debug-logs expired
   token 10062, falls back to followup). Adopt at 9 sites. DECIDE FIRST:
   expired-token log level (it's a monitoring-signal decision).
2. `SingleButtonGameView` in games/utils (~160; six views.py → constructor
   calls; carries bug 4). Voice = label/emoji/style stays at call sites.
3. Accent cache: converge all six on `core/branding.prime_accent_cache`
   (~80). Three incompatible contracts today (quickdraw and pressure take
   args in OPPOSITE order). DECIDE FIRST: prime-and-fallback vs
   return-with-default; branding.py:182-189 argues for prime.
4. `game_store.create_lobby` x3 (byte-identical, ~36); `fetch_by_state`
   over 15 two-liners (~30); result-footer fields on BaseGame (~50, embed
   text changes in 6 games — pin with visual diff); timers mixin +
   get_lobby_params + setup (~35). Escrow paths untouched by all of these.

### D. Dashboard (~350–450 net)
1. **`panels/config-games-shared.js` factory for the six config-games-*
   panels** (~200–280; 867 lines of lockstep-edit files → declarative
   specs; precedent: games-panel-shared.js / 14-line games-ama.js). Hooks
   needed: extraFields (musicalchairs checkbox), crossChecks array.
2. `public_base_url` three-way unification (oauth/spotify_oauth/helpers,
   ~20) — helpers' copy documents itself as a mirror but doesn't strip
   `/callback`; latent bug. Redirect-URI suffixes stay local.
3. `composePage` for the 7 report-over-settings pages (~40 + bug 5). Home:
   ui.js or panels/page-shell.js, NOT config-helpers.
4. wellness `wireConfirmDelete` (+ shared load-error preamble adopting
   caps' mountAsync/firstLoad shape — fixes missing first-load retry in
   blackouts/partners); ~22 now, ~150 if rolled to all 21 confirm+delete
   sites opportunistically.
5. Cheap: config-bios `card` → sectionCard (one line; makes the
   config-helpers comment true again); economy `memberName` x4 →
   config-helpers next to loadMembers (~12); `fmtSince` in audit-helpers
   for the two identical panel fmtAge copies (~8); `numInput140` (~9);
   `jump_url` in web_server/helpers.py x3 (~9).

### Python-side cheap riders (any session)
- `_resolve_card_image` ffa/photo — byte-identical minus log prefix.
- `_load_settings` async wrapper x3 in economy views → economy_service.
- Invoker-only `interaction_check` mixin (denial copy stays per view).
- Advisor/risky error handlers adopt `safe_ephemeral` (bug 6).

## Sequencing
Bugs (1–3 + prod check) → B1 → A1 → C1+C2 → D1 → then by appetite. Each
stage is a normal /dk-feature branch; tests ship with each per CLAUDE.md.
