# Staged full-repo review — 2026-08-05 → 2026-08-06 (Thursday midnight)

## Progress (the running session updates this; state survives compaction)

- [x] Wave 0 — register skeleton (`docs/data_register.md`) + prior-review skim
- [x] Wave 2.5 — loose-ends audit → `2026-08-05-loose-ends.md` (3 closed, 5 need Ben)
- [x] W1: Privacy core & data layer → `2026-08-05-privacy-core.md` (A1 purge blowout, U1 disclosure gap, G1 register)
- [x] W1: Whisper → `2026-08-05-whisper.md` (A1 cross-guild forget-me bug — high; GDPR model feature otherwise)
- [x] W1: Image Guard / NSFW + Guess → `2026-08-05-image-guard-guess.md` (Guard clean; Guess consent package U1 — high)
- [x] W1: Wellness → `2026-08-05-wellness.md` (clean bill; model citizen; §8 reconciled)
- [x] W1: Intake/greeting/welcome + Confessions/anon-audit → `2026-08-05-intake-confessions.md` (no highs; confessions 7d TTL = model)
- [x] W1: AI moderation + rules watch + sentiment → `2026-08-05-ai-moderation.md` (all-local processors; rules_events excerpt decision)
- [x] W1: DM perms + no-contact + Voice transcription → `2026-08-05-dmperms-nocontact-voicetx.md` (all clean)
- [x] W1: Health/analytics → `2026-08-05-health-analytics.md` (**G1 mod-assigned gender — high policy finding**)
- [x] W1: Pen Pals + Bios → `2026-08-05-penpals-bios.md` (no content stored in PP ✓; bios archive TTL decision) — **WAVE 1 COMPLETE**
- [x] W2: Economy ledger core → `2026-08-05-economy-core.md` (funnel+atomic debit verified; silent-renewal A2; G1 purge helper)
- [x] W2: Economy sources+sinks → `2026-08-05-economy-sources-sinks.md` (**Anthropic = live cloud processor via advisor — G1**)
- [x] W2: Casino → `2026-08-05-casino.md` (clean; best money code in repo)
- [x] W2: Games platform + XP → `2026-08-05-games-platform-xp.md` (dead consent scaffolding; xp_reaction_awards purge gap)
- [x] W2: Duels + party games → `2026-08-05-duels-party-games.md` (consolidation already done) — **WAVE 2 COMPLETE**
- [x] W3: batch A → `2026-08-05-games-batch-a.md` (copy-evolution fear answered; clean)
- [x] W3: batches B+C → `2026-08-05-games-batch-bc.md` (external buffer sweep A1) — **WAVE 3 COMPLETE**
- [x] W4 batch 1 → `2026-08-05-modtools-voice-music-roles.md` (sanctions preserve; Spotify=2nd cloud processor)
- [x] W4 fixtures → `2026-08-05-community-fixtures.md` (advisor context audited & closed) — **WAVE 4 COMPLETE**
- [x] W4.5: security + deps → `2026-08-06-sweep-security-deps.md` (**aiohttp CVEs — bump to 3.14.3**; else strong)
- [x] W4.5: reliability + DB/perf → `2026-08-06-sweep-reliability-dbperf.md` (xp_events 1M-row retention P1)
- [x] W4.5: tests + logging → `2026-08-06-sweep-tests-logging.md` (slim plan is tracker of record; journald note) — **WAVE 4.5 COMPLETE**
- [x] W5: synthesis → `2026-08-06-review-synthesis.md` — **PLAN COMPLETE 2026-08-06**

Mode: single automated session (self-paced /loop), findings-first; /simplify
applied in-place only where low-risk, batched commits at wave boundaries.

Burn-down plan for a five-dimension review battery over every cog/feature,
sized against the token budget available before the weekly reset.

## Budget & pacing

- Spent since last reset (07-30): ~$1,039 API-equivalent, 3.79M output tokens.
- Window remaining: ~40h. Matching last week's spend means sustaining
  **2–3 concurrent sessions** (~$25–35/hr equivalent). One serial session
  will not exhaust a "giant" budget in time.
- Mechanics: use `/dk-feature <wave-name>` per concurrent lane (worktree +
  tmux + claude, per docs/dev_sessions.md). Review *findings* are read-only
  and can land on main; `/simplify` *fix application* happens in the lane's
  worktree and ships via `/dk-ship`.
- Known frictions (memory): npm install --no-save per worktree before JS
  gates; commit hook needs a long timeout (remote runner); simplify line-saving
  estimates run ~2.5× optimistic — judge by structure, not claimed LOC.

## The battery (run all five per feature bundle)

Findings for dims 1, 3, 4, 5 go in **one file per bundle**:
`docs/reviews/2026-08-05-<bundle>.md` with sections `## Architecture`,
`## UX`, `## Docs`, `## GDPR`. Dim 2 (`/simplify`) applies fixes directly.

1. **Architecture** — cog thinness (logic in `*_service.py`/`*_logic.py`?),
   DB schema sanity, background-loop lifecycle, cross-feature coupling,
   test mapping (does the scoped gate find this bundle's tests?), dead code.
2. **/simplify** — run the skill scoped to the bundle's files; apply fixes in
   the lane worktree; scoped gate green before commit.
3. **UX** — against CLAUDE.md philosophy: admin config on the web (any
   lingering admin slash commands = finding), member surface is one ephemeral
   panel not subcommand sprawl, collapse overlapping toggles, embed accents
   via `resolve_accent_color`, dashboard panel passes mobile layout scan.
4. **Docs** — spec in `docs/` matches code (code wins), INDEX.md
   classification still right, `manual.html`/`help-sections.js` covers every
   user-facing surface, README only if the feature's existence changed.
5. **GDPR** — per bundle: (a) what personal data is stored, which tables;
   (b) lawful basis — is sensitive collection opt-in, NSFW gated on
   `channel.is_nsfw()`; (c) retention & deletion — does the privacy purge
   path actually cover these tables; (d) minimization — metadata derived at
   ingest, content off by default; (e) third-party processors (Ollama, Marqo,
   Lavalink, Spotify, any LLM API) — what leaves the box; (f) mod-facing
   audit panels — who can see what; (g) subject access — could we export this
   user's data if asked.

## Wave 0 — setup (30 min, do first, one session)

- Create `docs/data_register.md` skeleton: a table of
  `table → feature → data class → retention → purge-covered? → processor`.
  Every bundle's GDPR pass appends rows; Wave 5 dedupes.
- Skim `docs/INDEX.md` and open findings in `docs/reviews/` so lanes cite
  rather than rediscover (economy health 07-30, deep reviews 07-01/07-22,
  website UX 07-22, rules-watch follow-ups).

## Wave 1 — data-sensitive features (GDPR value is highest here) — ~$450–550

Priority order within the wave; each row is one battery run.

| Bundle | Code | Panels | Spec | Est. |
|---|---|---|---|---|
| Privacy core & data layer | privacy_cog, privacy/, privacy_service, message_store, usage_telemetry_* , db_backup | usage-telemetry, message-search | privacy_spec | $40 |
| Whisper | whisper_cog (2.7k), whisper/, whisper_{service,repo,models} | config-whisper, mod-whisper-audit | whisper spec | $45 |
| Image Guard / NSFW + Guess | guess_cog (2k), guess_* (7 svcs), nsfw_classifier_service, marqo_nsfw | config-guess, guess-audit, nsfw-* (3), config-spoiler | guess_spec, nsfw_classifier_spec | $60 |
| Wellness | wellness_cog + 5 wellness_* svcs | wellness-* (7) | wellness spec | $50 |
| Intake / greeting / welcome | intake_* (4), greeting_watch_* (2), welcome_service | intake, intake-report, intake-settings, config-greeting-watch, config-welcome, greeter-response | intake_spec, greeting_watch_spec | $45 |
| AI moderation + rules watch + sentiment | ai_mod_cog, rules_watch/ (2.1k), ai_moderation_service, moderation, sentiment_service | config-moderation, config-ai, rules-watch* (3), mod-warnings | ai_moderation_spec, rules_watch specs | $50 |
| Confessions + anon audit | confessions_cog, confessions_service, anon_audit_service | config-confessions, mod-confessions-audit, mod-anon-audit | confessions_spec, anon_audit_spec | $30 |
| DM perms + no-contact | dm_perms_cog (1.3k), dm_perms/, no_contact_{cog,logic,service} | config-dms, mod-dm-audit, no-contact | dm_perms_spec, no_contact_spec | $35 |
| Voice transcription | voice_transcription_{cog,service}, voice_xp_service | config-voice-transcription, voice-activity | spec | $25 |
| Health / analytics suite | health_*, activity_graphs, interaction_graph, graph_metrics, member_quality_score, attention_report, gender_service | health-* (9), quality-score, one-sided-attention, retention, gender-admin, interaction-graph | post_monitoring_spec | $60 |
| Pen Pals | pen_pals_cog (2.1k), pen pals svcs | pen-pals, pen-pals-settings | pen_pals_spec | $35 |
| Bios | bios/ (2.7k), bios_cog | config-bios | bios_cog_spec | $30 |

## Wave 2 — heavyweights — ~$350–400

| Bundle | Code | Est. |
|---|---|---|
| Economy: ledger core | economy/ (9.2k) + economy_cog (4k) core paths, economy_service, economy_loop, demurrage, boost_reconcile | $60 |
| Economy: sources | drops, quests, bounties, qotd_sponsor, pins, photo, raffle, wager + panels (economy-*, ~10) | $50 |
| Economy: sinks & shop | rentals, auction, emoji, icon_catalog, stats/metrics + bank-manager/sinks panels | $40 |
| Casino | cogs/casino/ (4.8k), casino_{logic,service} + config-casino | $50 |
| Games platform | games/ (2.1k), games_{session,config,consent,help}, games_db, scheduled_games, game_start_ping + games-config/scheduling/logs/panel-shared | $40 |
| Duels + party games | duels/ (2.4k), hot_potato ×2, musical_chairs, pressure_cooker, quickdraw, chicken, needle_cog, risky_roll | $55 |
| XP | xp_cog, xp_service, message_xp, voice_xp + xp panels (3) | $25 |

Cite `docs/reviews/2026-07-30-economy-health.md` and the 07-25 sources/sinks
review in the economy bundles; the 07-30 retune dials (memory) are prod state,
not code — don't "fix" them.

## Wave 2.5 — loose-ends / prod-drift audit — ~$30 (cheap, time-sensitive, run early)

One session sweeps known dangling state to closure-or-documented:

- Marqo NSFW swap (911e1222) still on an unmerged branch; log channel still 0
  — decide merge/park, wire the log channel or record why not.
- Economy retune checkpoints: 08-02 check and 08-04 demurrage check — verify
  they happened; run them if not; rollback SQL files (4, untracked in repo
  root) — archive or delete once the retune is confirmed healthy.
- Wellness prod-DB hand-edits (2026-07-30) still uncommitted as config.
- Dev-session systemd user units installed but disabled; enable-linger unrun.
- External game host payouts: replay --apply window (memory) — closed or not?
- `Discord Messages/` untracked dir in repo root — identify, relocate/ignore.

## Wave 3 — prompt-game batch — ~$150–200

Eighteen structurally-similar cogs; batch 4–6 per battery run, one findings
file per batch. Architecture dim should explicitly hunt **cross-game
duplication** (shared base-class candidates) since these evolved by copy:

- Batch A (large): ama (2.3k), rushmore (2k), clapback (2k), legitlibs (2.8k)
- Batch B: price (1.7k), external (1.4k), mlt, ttl, story, traditional
- Batch C: nhie, wyr, hottakes, fantasies, mfk, ffa, compliment, photo

## Wave 4 — utilities & mod tools — ~$200–250

- Jail + tickets + mod: jail/ (1.2k) + jail_cog (1.7k), tickets, mod_cog,
  support_cog, policy tickets panels, dungeon_keeper_jail_ticket_spec — $45
- Voice Master: voice_master/ (1.5k) + cog (1.7k) + service, voice panels — $35
- Music: music_cog, lavalink_manager, music_queue, now_playing,
  spotify_resolver (+ its oauth route) — $30
- Roles: role_menus (1.2k), role_grant + audit svc, booster_roles, auto_role,
  grant-audit/role-menus panels — $30
- Community fixtures batch 1: events_cog (2.1k), starboard, quotes + renderer,
  emoji_stealer, auto_react — $35
- Community fixtures batch 2: announcements, birthday (3 panels), bump_tracker,
  chat_revive, invite_tracker, inactive/prune (1.1k), hidden_channels, todo,
  qa, docs cog, dev/advisor cogs — $40

## Wave 4.5 — horizontal sweeps (cross-cutting, one lane) — ~$200–250

These slice across the repo rather than per feature; run each as its own
findings doc `docs/reviews/2026-08-06-sweep-<name>.md`:

- **Security** (~$60): run `/security-review` on the dashboard surface —
  all 35 route files, OAuth/session handling, admin-vs-member privilege
  boundaries per route (the authz sweep only proves unauthenticated
  rejection), SQL string-building, SSRF in fetch-y features (emoji stealer,
  spotify resolver, quote renderer), secret/token storage.
- **Reliability & failure modes** (~$50): every background loop's crash/
  restart behavior; each external dependency (Ollama, Marqo, Lavalink,
  Spotify, LLM APIs) down/slow/garbage; persistent-view re-registration and
  in-flight game state across a bot restart; Discord rate-limit handling;
  Discord permission assumptions (category grants don't cascade — audit
  other places that assume they do).
- **DB & performance** (~$45): index coverage vs real query patterns, N+1s
  in panel endpoints, WAL/checkpoint behavior, growth-rate of append-heavy
  tables (message_store metadata, telemetry), migration hygiene and the
  migration-number-collision hazard.
- **Test-suite quality** (~$45): find remaining cog tests that re-prove
  service behavior through Discord mocks (the documented anti-pattern),
  safety gates with no test, slowest tests, remote-runner flakiness
  (asset-dependent tests must skip not return).
- **Logging & PII-in-logs** (~$25, pairs with GDPR register): what each
  feature logs, whether content/PII lands in logs, and whether the
  wiped-every-boot log.txt situation is acceptable per feature or some
  need durable audit trails.
- **Dependency audit** (~$15): pip audit on the locks, .txt↔.lock drift,
  unused direct deps, npm audit for the dashboard dev tooling.

Folded into existing dims rather than added: dashboard accessibility →
UX dim; embed/ping allow-listing → UX dim (embed_style_guide.md).

## Wave 5 — synthesis (last 3–4h, one session) — ~$50

1. Merge per-bundle GDPR rows into the register; write the gap list
   (tables missing from purge, processors without documented flows).
2. Dedupe architecture/UX findings across bundles into a single prioritized
   fix queue appended to `docs/data_register.md`'s sibling,
   `2026-08-06-review-synthesis.md`.
3. Docs dim output: one commit updating INDEX.md classifications + manual.html
   gaps found across all bundles.
4. `/dk-ship` any lanes still holding applied simplify fixes.

## Suggested lane split (3 concurrent tmux lanes)

- **Lane 1 (privacy):** Wave 1 top-to-bottom — the GDPR-critical dozen.
- **Lane 2 (economy/games):** Wave 2.5 first (time-sensitive), then Wave 2,
  then Wave 3.
- **Lane 3 (utilities):** Wave 4, then Wave 4.5 horizontal sweeps.
- Whoever finishes first runs Wave 5.

Total estimate: **~$1,500–1,750 API-equivalent (~5.5–6.5M output tokens)** —
on par with last week's actual spend, achievable in the window with 3 lanes.
If budget runs short, Waves 3–4 degrade gracefully (batch harder, skip
/simplify application, findings only). If budget runs long, deepen Wave 1
GDPR into actual purge-path test coverage per table.
