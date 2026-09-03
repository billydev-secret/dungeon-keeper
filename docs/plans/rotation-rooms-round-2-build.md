# Rotation Rooms — Round 2: build plan

**Status:** PLAN — nothing built. Written 2026-09-02 against main `54c22250`, after a
nine-seam verification of the spec against `src/` (every seam surveyed by one agent and
re-checked by a skeptic; the corrections are in §2), then a three-lens adversarial review
of this document (seams · shippability · safety) whose confirmed findings are folded in.
Main moved to `dfe908d2` (15 commits) while this was written; the git-state facts below
are re-stated against that HEAD. **First move of stage 0: rebase this branch onto main.**
**Spec:** [rotation-rooms-round-2.md](rotation-rooms-round-2.md) (v2, verbatim). This
document does not restate it: §§1–3 of the spec stay the design of each room; this is
the order, the plumbing, the corrections and the cost.
**INDEX.md classification:** Design (both files).

> The one sentence to carry: **the spec assumes a rotation that is not running and a hook
> that does not exist.** Billy's call (2026-09-02): *the rooms must work without the
> rotation.* So stage 0 builds a room clock that runs on a fixed cadence and merely
> aligns to featured days when the rotation is on. Every "at open" job in the spec keys
> to that clock, never to the rotation service directly.

---

## 1. Prod reality and the decision it forced

Verified read-only against the live DB on 2026-09-02:

| Fact | Consequence |
|---|---|
| `feature_rotation_config` has **0 rows** → `enabled=False`, `rooms_per_day=1` (store.py:36-40) | No guild has ever flipped. `_tick_guild` returns before any "open" (feature_rotation_service.py:393). |
| Pool has **4** rooms (confessions, guess-who, whisper, ama), not the spec's five — Risky Rolls is absent; every row has blank `label`, `quest_kinds`, `launch_game` | §0.6's "pool 5 → 8, ship at `rooms_per_day: 2`" is first-time setup, not a dial change. §0.4's "a cycle is four days" is arithmetic on a pool that does not exist yet. |
| Prod schema is at migration **199**; main now carries **200 twice and 201 twice** (`200_birthday_channels`, `200_confession_approval_queue`, `201_newcomer_funnel_indexes`, `201_todo_auto_complete`), all unapplied — no restart since those merges | Nothing here is live until a restart Billy pushes. Stage 0's migration is **202** (re-check at ship time; never add a third 201). |
| `econ_income_sources` has 0 rows | `source_enabled` treats an absent row as ON (economy_quests_service.py:1753), so every new kind is live in every guild (including the second, ~8× economy) the moment the code restarts. The guard is the room clock, not the faucet: **a room in `off` mode refuses every paying act** (nominate, vote, seal, answer, read) in the service, not just in the panel footer — so a guild that never configures the room pays nothing. One `pytest.param` row per room proves it. |

**Decision (Billy, 2026-09-02): stage 1 must work without rotation.** The three rooms
therefore own their own clock. A room is in one of three modes, resolved at every tick:

| Mode | When (rows are checked in this order) | Cycle boundary |
|---|---|---|
| **off** | room not `enabled`, or no channel set | no boundaries; every paying act refused; panel says so |
| **rotation** | guild rotation `enabled` **and** the room's channel is an `in_rotation` pool row | **the flip** — 00:00 guild-local on the room's featured day. The rotation flips at local midnight and only `announce_hour` is configurable (feature_rotation_service.py:9-13), so `open_hour` and `cycle_days` are **ignored** in this mode and the dashboard hides both dials with the reason inline (an editable dial nothing reads is the unenforced-toggle shape CLAUDE.md forbids). A run of consecutive featured days (pool not a multiple of `rooms_per_day`, logic.py:144-157) is **one** cycle whose `cycle_id` is the run's first day — never two one-day cycles that would overlap the three-job chain. |
| **fixed** | otherwise | boundary 0 = `anchor_day` at `open_hour`, then every `cycle_days` (default 4). `anchor_day` is stamped server-side by the room's save route when `enabled` goes 0→1 (re-enable re-stamps). If the room is enabled after `open_hour`, the 60 s loop finds boundary 0 already passed and draws within a minute — that is the visible "it works" moment. |

`cycle_id` is the boundary's guild-local date in both modes, so every once-per-cycle
table, payout and job is mode-agnostic. Switching the rotation on later just moves the
boundaries; nothing is rebuilt. Stage 5 becomes a prod checklist rather than a rewrite.

The shared dials every room carries (`channel_id`, `enabled`, `cycle_days`, `open_hour`,
`anchor_day`) live in **one stage-0 table, `rotation_room_config(guild_id, room, …,
PRIMARY KEY(guild_id, room))`**, written by each room's dashboard route; a room's own
`<room>_config` holds only its own dials. Without this the stage-0 clock has nothing to
read and cannot be tested end to end.

What has to be true in prod for stage 1 to be *useful* (not merely shippable): one
restart, a channel picked on the Superlatives page, a handful of prompts in its bank,
and the room enabled. Nothing else.

---

## 2. Spec corrections (verified against `src/`)

The spec says "go look" for every seam. We did. Where the code disagrees, this table is
what the build follows.

| # | Spec says | Code says | Plan does |
|---|---|---|---|
| C1 | §0.4/§5.2: rooms attach jobs to the registry's on-open hook | No hook. The only per-open callback is `bot.game_launchers[launch_game]`, allow-listed to `ama`/`risky_roll` (routes/feature_rotation.py:74) and refused when a game is live in the channel (service.py:283-297). The flip claim is guild-wide (store.py:255) and any raise inside it re-runs hide/show for every room. | Stage 0 builds a **rooms clock + per-(guild, room, job, cycle) claim** in a new `rotation_rooms/` package with its own 60 s loop. Never registers a fake launcher; never raises into the flip. |
| C2 | §0.4: rooms "honour `pause_when_off`" | Does not exist (migration 192:38: "deliberately absent"). Only `is_hidden_by_rotation` (store.py:136) exists, consumed solely by the games scheduler. | Nothing posts except at a boundary, and in rotation mode a boundary *is* the featured day — honoured by construction. Member-initiated posts (Publish my Mirror) check `is_hidden_by_rotation` and disable while hidden. |
| C3 | §0.4: `cycle_id` = featured-day date; "cycle is four days" | No helper computes "last/next featured day of room X"; the dashboard only computes today and tomorrow (routes:178). Fixed 8-room pairs hold only while all 8 stay `in_rotation` and positions don't move (logic.py:144-157). | Stage 0 adds pure `cycle_for` / `next_boundary` in `rotation_rooms/cycle_logic.py`. Panel copy derives the cycle length; nothing hard-codes 4. |
| C4 | §0.5: dedup is "the QOTD shape" | QOTD keys on `(qotd_id, user_id)`. The `(guild, member, day)` anchor is **photo_post**: `econ_photo_rewards` (migration 101) checked by `INSERT OR IGNORE` rowcount then `apply_credit` (economy_photo_service.py:130). | Copy photo, with `cycle_id` for `local_day`. Tables named `econ_<room>_rewards` so the register's `econ_*` wildcard row covers them, and added to `economy_service._PURGE_USER_ID_TABLES`. |
| C5 | §0.5/§0.3.6: "quiet" is one ledger attribute, the AMA precedent | `ama_ask` is quest-only (no flat faucet). Quiet is **three** mechanisms: `quests.ANON_KINDS` (quest payouts + refuses sign-off, quests.py:357), `register.SKIP_KINDS` + `_KIND_DISPLAY` (flat ledger rows, register.py:67/88), and `meta.anon=1` on any ledger row (register.py:236-249). photo_post is in none — it posts publicly. | Each new kind goes in `ANON_KINDS`; the flat award passes `meta={'cycle': id, 'anon': 1}` and gets a `_KIND_DISPLAY` row for `/bank`. Test rows in `test_economy_register.py` and the kind tuple at `test_economy_quests_service.py:3025`. |
| C6 | §0.5: faucet toggles on Income Sources | A toggle exists for every `TRIGGER_KINDS` key automatically; the **rate dial** is four hand-maintained lists (`EconSettings`, routes/economy.py Field, economy_manager faucets dict, `economy-income-sources.js` FAUCET_FIELDS — a JS row without a route Field 422s the whole save) plus two copy sites (guide.py earn_rows, quest_views QUEST_STATE_LABEL). | Rate dials live on Income Sources with the other `reward_*` rows, not on the room pages. The §5 checklist lists all six sites. |
| C7 | §1.9/§3.14/§5.1: "reuse the shared bank editor" | The shared editor (`mountGamePanel`, games-panel-shared.js:28) edits `question_text` + tags on `games_question_bank`, which is **global across guilds** (no `guild_id`; round-robin state shared with the second guild), has no active/weight/options/teaser/rerun columns, and heat is only the reserved `nsfw` tag. `economy-bank-manager.js` is the coin economy, not questions. | **Private guild-scoped tables** per room, editor cloned from the bios Icebreakers tab (config-bios.js:503, routes/bios.py:435). Heat is a two-value column `mild|spicy`; spicy served only when `question_source.channel_allows_nsfw(room_channel)` (call it, don't reimplement — branch `nsfw-gate-audit` makes it category-aware). A shared pure draw helper handles weight + recency + rerun + N-at-once (none of the existing draws do all four; `bios.logic.draw_weighted` and `chat_revive_service.pick_question` are the two halves). |
| C8 | §2.6/§3.5: "the Confessions content filter, same word list" | Confessions has no word filter (only a per-user blocklist and length). The filter that exists is `duels/filters.contains_disallowed_content` (filters.py:68; NFKC + zero-width fold, built-in denylist + optional extras; Guess Who and Risky Rolls use it). The mention stripper exists too: `games_legitlibs/validation.strip_mass_mentions` (validation.py:115, covers `<@id>`, `<@!id>`, `<@&id>`, @everyone, @here) but lives inside a cog package. | Reuse both. Stage 0 lifts the stripper into `core/mentions.py` (legitlibs re-exports). Rejection copy is room-specific (the Guess Who string at guess_cog.py:2307 already names the wrong feature — do not copy it). No new word list. |
| C9 | §1.7: winners DM'd "via the DM-permission service"; cards use `resolve_accent_color` | `dm_perms_service` is member↔member consent, unused for bot DMs. `resolve_accent_color` raises and a repo test bans direct callers. | DMs via `dm_branding.send_branded_dm` (pref-agnostic; the economy mute is coin-specific). Cards via `safe_resolve_accent(bot, guild, default=DEFAULT_ACCENT_COLOR)`. |
| C10 | §5.3: Compliment derangement callable with exclusion sets | `random_derangement(participants)` is a Sattolo cycle with no exclusions and no `rng` (derangement.py:4). Compliment **and** MFK consult no-contact nowhere (two live rule gaps — raised in §8, not fixed here). The closest shape is `games_mfk/logic.assign_targets` (k=3, no self, seedable; no forbidden pairs, uneven in-degree). | Stage 4 builds `assign_describers(describers, subjects, k, forbidden, rng)` in `services/slate_service.py`: forbidden-free path = one derangement + k hops (exact in-degree k); otherwise bounded restarts + greedy min-in-degree repair; returns per-subject in-degree so threshold delivery can tell "not yet" from "unreachable". |
| C11 | §0.3.4: "slates are reshaped to full size" per the no-contact spec | The no-contact spec prescribes silent filtering only; `candidate_members_for` (no_contact_logic.py:270) drops partners and never refills. | Reshape is room logic: draw eligible pool minus {self, opted-out, heat-below, partners, jailed}, then sample N. Stage 0's `eligible_targets` + `draw_slate` in `slate_service.py`, used by Superlatives slates and Second Sight pickers. |
| C12 | §1.10/§3.15: add `target_id`/`subject_id` to `SUBJECT_ID_COLUMNS` | Both are already there (on main, `SUBJECT_ID_COLUMNS` is privacy_service.py:788 after the gdpr-review merge). **`nominator_id`, `perceiver_id`, `describer_id` are not.** Pair tables also need `THIRD_PARTY_TABLES` (:860) for the Art 15(4) flag; nothing tests that list. | Add the three missing names; register the five pair tables in `THIRD_PARTY_TABLES`; literal `DELETE` blocks per feature (the purge→export guard test can't see the generic tuple); a seeded purge test per feature. |
| C13 | §2.7: `/delete_me` destroys pending envelopes and deletes published rows "as with confessions" | `/delete_me` clears Discord messages only and promises "the server's own records always stay" (privacy_cog.py:445). It deletes no confession rows either; that is `purge_user_data`, run out-of-band from the runbook. | That spec row means **erasure** (`purge_user_data`). `/delete_me` is untouched. Spec row corrected on landing. |
| C14 | §2.7/§3.10: "the hourly sweep" | No shared sweep; each feature owns a loop (confessions hourly, anon audit six-hourly). `on_member_remove` misses departures while the bot is down (pen_pals_cog.py:596). | Stage 0 builds one `rotation_rooms` sweep (hourly claim inside the 60 s loop) with registered callables; stage 1 registers the first (Superlatives pair-row retention), stage 2 envelope destroy-on-leave (listener **and** membership check), stage 3 Second Sight retention. |
| C15 | §2.8: `envelope_review` approvals from the todo board's 🧾 Approvals section | 🧾 is an economy-typed queue (`ApprovalQueue.product` is a `SubmissionProduct`). Confessions could not join it and took a separate 🕵️ button — "the fifth and last button the board can carry" (todo_cog.py:298-299). | **`envelope_review` is not built in v1.** The dashboard preview + Pull is the review surface. A toggle with no enforcement would violate the house rule; a second board row is a separate decision. |
| C16 | §1.8: one panel with a select per prompt for nominations *and* ballot | Six selects = six action rows against Discord's five-row cap; a select holds 25 options. | Panel is buttons; each action opens a sub-view (mahjong `MemberPanelView` shape, views.py:308). Four-name slate = a string `Select` of display names (the AMA `HotSeatSelect` form); >25-candidate pickers use whisper's pager. |
| C17 | §2.2/§2.11: drops every N featured days; `envelopes.drop_id` | No featured-day counter exists; no drops table is defined. | Stage 2 adds `envelope_drops` (per-guild sequence, one row per opened drop) and a per-guild "cycles since last drop" counter advanced by the clock job. Projected dates come from `next_boundary` × cycles remaining. |
| C18 | §0.3.2: three ids "under a Rotation Rooms heading alongside the existing rooms" | No heading. Confessions/Whisper/Guess sit flat in the moderator-gated Social section; AMA and Risky Rolls in Games › Live Games (gated game-host). `allPages()` accepts `groups`, so a heading is nav-only and ids stay frozen; moving AMA/Risky changes their audience gate and `test_nav_visibility.py` EXPECTED rows. Branch `games-nav-split` (live) rewrites this nav. | Heading holds the three new ids plus Confessions/Whisper/Guess, inside Social. AMA and Risky stay put. Regroup lands whenever `games-nav-split` has merged (stage 1 if so, else stage 5); until then each room adds a flat Social item. |
| C19 | §0.3.5: write audit rows under new slugs | `insert_event` stores any slug, but the Anonymity Audit filter and labels come from `KNOWN_FEATURES`/`FEATURE_LABELS` (anon_audit_service.py:58/72) and the nav search keywords (app.js:129). `record_event` cannot set `created_at`; `insert_event` can (:179). | Three constants + labels + keywords per room. Envelopes write the audit row inside the publish transaction via `insert_event` so §2.5's clock starts at publication. |
| C20 | §0.6/§4 stage 5: "register all three, pair quest triggers" is a build step | Rooms are dashboard pool rows; pairing is ticking kinds on the Feature Rotation page; `_clean_kinds` drops any kind not in `TRIGGER_KINDS`. "Survives hiding" = leave the 🔒 box unticked. | The only code is the kind existing. Stage 5 is a prod checklist for Billy. |
| C21 | §1.9: Superlatives `season_length_cycles` | Superlatives publishes no running number that a season would reset. | Dial dropped from Superlatives. Seasons exist in Second Sight only. |

---

## 3. Stage 0 — shared plumbing (`rotation_rooms/`)

Independently shippable, inert until a room uses it. Not user-facing (no `Testing:`
section). Can be the first half of the Superlatives session if Billy prefers fewer
merges; it is split out here because stage 1 is already the size of Risky Rolls.

### 3.1 New package `src/bot_modules/rotation_rooms/`

| File | Contents |
|---|---|
| `cycle_logic.py` (pure) | `RoomClock` dataclass `{mode, cycle_id, boundary_ts, next_boundary_ts, cycle_days_estimate}`; `cycle_for(rooms, channel_id, day, rooms_per_day)` and `next_featured_day(...)` walking `featured_channel_ids` over the ordinal (O(pool²) integer ops); `fixed_cycle_for(anchor_day, cycle_days, day)` / `next_fixed_boundary(...)`; `resolve_mode(rotation_cfg, pool_row, room_cfg)`. Guild-local days via the existing fixed-offset `local_day` (logic.py:104) — never UTC. `next_featured_day` collapses a run of consecutive featured days into one cycle (see §1); the test matrix at pool 4 / `rooms_per_day` 3 hits this on day one, so the expected behaviour is written before the test. |
| `store.py` | Migration 202 (renumber at ship): `rotation_room_config` (§1; the shared dials, no member data) and `rotation_room_jobs(guild_id, room, job, cycle_id, claimed_at, done_at, PRIMARY KEY(guild_id, room, job, cycle_id))`. `claim_job` = `INSERT OR IGNORE` rowcount (the `econ_photo_rewards` anchor precedent — **not** the rotation's `UPDATE … WHERE date < today`, which self-heals tomorrow where a row-per-cycle table does not); `release_job` deletes the row on failure so the next tick retries; `mark_done`. **Stale-claim rule:** a row with `done_at IS NULL AND claimed_at < now − 15 min` is reclaimable (`UPDATE … WHERE done_at IS NULL AND claimed_at < ?`), so a process death between claim and done retries next tick instead of losing the cycle. Reads of `rotation_room_config` for `resolve_clock`. |
| `service.py` | `resolve_clock(conn, guild_id, room_key, now)`; `rotation_rooms_loop` (60 s, registered in `__main__.py` beside `feature_rotation_loop`): for each guild × registered room × job whose boundary has passed and is unclaimed → claim → run → done; exception → release + log, retry next tick. Jobs register in `setup()` as `bot.rotation_room_jobs[room_key] = [RoomJob(name, coro)]` (the `game_launchers` registry pattern, app_context.py:133-149) — a room job receives `(guild_id, cycle_id, channel)` and keys off its own room's state (e.g. `superlative_slates.phase`), never "N−2" arithmetic — in rotation mode a pool edit makes N−2 undefined. A job that finds no prior state (the very first boundary) marks itself done without posting. Hourly `sweep` claim in the same loop for registered sweep callables (stage 1 registers the first). Posting helper that **raises `RoomHiddenError`** when `is_hidden_by_rotation` (rotation mode only) so the claim is released and the job retries each minute until the flip lands (a failed flip is retried by `_tick_guild` the same way; a helper that returned normally would let the job mark done and lose the cycle). Admin-only `advance(guild_id, room)` — claims and runs the room's jobs for a synthetic next `cycle_id` — exposed by each room's dashboard route as "Run the next boundary now"; it is the only way a `Testing:` card can be completed in one sitting and the only way to provoke the first boundary on demand. |

### 3.2 Shared helpers lifted or added

| What | Where | Why |
|---|---|---|
| `eligible_targets(pool, viewer, *, opted_out, partners, jailed, extra)` + `draw_slate(eligible, n, rng)` | new `services/slate_service.py` | C11; used by Superlatives slates and Second Sight pickers. `candidate_members_for` handles partners; this adds self/opt-out/heat/jail. `rng: random.Random | None` house convention. |
| `_MemberScopedView` | lifted from economy_cog.py:427 into `core/views.py` (economy subclasses it) | every room panel needs the "only the opener can drive it" guard without importing a cog module. The base gets a neutral `SCOPE_DENIAL`; the shop string (economy_cog.py:435) stays on the economy subclass. Both new `core/` files trip the gate's broadly-shared-file rule: expect the deferred full-suite run on the stage-0 commit. |
| `strip_mentions` | `core/mentions.py`; legitlibs `validation.py` re-exports | C8 |
| `random_derangement(..., rng=None)` | derangement.py, backward-compatible kwarg | stage 4's forbidden-free path; compliment test stops seeding the global RNG |
| Prompt draw `draw_prompts(rows, n, *, allow_spicy, now, rng)` | `rotation_rooms/bank_logic.py` | C7; rows are `(id, heat, weight, active, last_served_at, rerun_after_days)`; recency tie-break reuses `question_source._pick_least_recently_served`, which compares `last_served_at` as an ISO **string** (question_source.py:114-117) — the bank columns store ISO text, not an epoch; `pytest.param` table |

### 3.3 Tests, docs, size

- `tests/test_rotation_rooms_cycle_logic.py` (mode table; boundary walk at rooms_per_day 1/2/3 and pool 4/7/8; un-ticked room; fixed cadence across a month; DST-free offset), `tests/test_rotation_rooms_store.py` (claim/release/done; two claims for one cycle; retry after release; a claim that crashed mid-job is reclaimed after the stale window), `tests/test_rotation_rooms_service.py` (job dispatch; a raising job releases and does not touch other rooms; hidden-channel refusal leaves the claim unclaimed; first boundary with no prior state marks done and posts nothing; `advance` runs the chain once; off mode dispatches nothing), `tests/test_slate_service.py`, `tests/test_bank_logic.py`, `tests/test_mentions.py`.
- Docs: this file; `docs/plans/rotating-feature-channels.md` gains a "Round-2 rooms own their clock" note; `docs/INDEX.md` rows for both plan docs.
- Size: ~10 new files, **~2,000–2,800 lines** including tests (the earlier 1,200–1,600 was
  under: `feature_rotation` logic+store+service alone is 1,278 src + 921 test lines for one
  clock and one claim column, and stage 0 carries more than that). One migration.

---

## 4. Stages 1–5

Order changed from the spec: **Envelopes moves from 4th to 2nd.** It is the smallest room
(no bank, no scoring; a dashboard of dials plus the drop preview), it exercises the clock
in fixed mode most naturally (drops are a counter), it adds the destroy-on-leave sweep
callable Second Sight's retention then joins, and
it gives Billy two live rooms before the largest build starts. Superlatives stays first
because it proves the bank editor, the faucet registration and the boundary jobs that
everything inherits.

Every stage: its own `/dk-feature` session; migration numbered **at ship time** as
highest-on-main + 1 (202 as of `dfe908d2`; main already holds two 200s and two 201s).
`gdpr-review` has merged, so privacy edits go straight into the merged
`privacy_service.py`.

**One slash command, not three.** A static count finds 97 top-level application
commands (62 `@app_commands.command` + 35 top-level `app_commands.Group`); Discord's cap
is 100 per application, and `/info` (member-info branch, unmerged) is queued. The three
rooms share one `app_commands.Group` — `/rooms superlatives`, `/rooms envelopes`,
`/rooms sight` — which is also the "collapse controls" shape CLAUDE.md asks for. Stage 1
verifies the count against the live tree, not by grep.

### Stage 1 — Superlatives

**Contents.** Bank (`superlative_prompts`: guild_id, text, heat, active, weight,
last_served_at) + editor; prefs (opt-out, heat ceiling); slates with `draw_slate`;
two-phase cycle jobs; tally cards; withdraw; `superlative_vote` faucet + kind; dashboard
`superlatives` (settings · bank · report); `/superlatives` panel.

**Cycle jobs** (three claims, run in order at each boundary, each retry-safe):
`tally` (the slate in phase `balloting` → cards, winner DMs), `ballot` (the slate in phase
`nominating` → top `ballot_size` ≥ `min_nominations`, ties to +2, re-check opt-out/heat/
membership/jail, finalist DMs), `draw` (slate for this cycle: `prompts_per_cycle` via
`draw_prompts`). Jobs key off `phase`, not cycle arithmetic; with no slate in the phase
they need (the first two boundaries) they mark done and post nothing, and the panel's
empty state says "first prompts open at <next boundary>". A new table the spec lacks:
`superlative_slates(guild_id, cycle_id, prompt_id, position, phase, message_id,
posted_at)` — no member id, no register row. `message_id` is what "Last round" links and
the winner DM carries, and it makes `tally` idempotent: each prompt's card posts inside
its own savepoint, records its id, and a retry skips prompts that already have one (the
rotation's one-flip claim is not a precedent for an N-message job). **A failed DM never
releases the claim** — log it and count it on the report, as stage 2 does.

**Panel** (C16): row 1 consent buttons (Count me in mild / spicy · Leave me out); row 2
Nominate · Ballot · Withdraw (finalists only) · Last round. Nominate → prompt select →
four-name string select + 🔀 (one per prompt per cycle). Ballot → prompt select →
finalist select, no-contact filtered per viewer; a viewer whose only finalists are
partners sees the same "nothing to vote on this round" copy the turnout-floor path uses,
never a visibly empty picker (no_contact_spec.md disclosure rule). Last round → the
latest cards, with partner names filtered at render time. Footer: mode, next boundary,
counts. **Spicy double gate:** spicy prompt text renders only when the room channel
*and* `interaction.channel` both pass `channel_allows_nsfw` (the panel is ephemeral and
works while hidden, so the room's own age-gate alone never runs for a member who opened
`/rooms superlatives` elsewhere) **and** the member pressed "Count me in spicy" — the
button is member consent, not a bot-side NSFW toggle. Elsewhere the panel says "open this
in the room for spicy prompts". Both branches tested.

**Safety.** Slates via `eligible_targets` with `no_contact_partners_conn` per viewer,
`active_jailed_user_ids`, `active_member_ids(days=slate_activity_days)` **intersected with
live `guild.members` at draw time** — `member_activity` keeps rows for members who left
(economy_quests_service.py:3164-3175), so without the intersection a departed member can
be named on a slate. Off mode refuses nominate/vote in the service. Tally tie with a
no-contact pair (`no_contact_pairs_among_conn`): keep the higher count, else the earlier
nominee id; report shows why. Opt-out discards the member's cast nominations/votes and any
naming them. Tally card = embed with `name_fn`, winner ping in `content=` allow-listed.
Audit slug `superlatives` on nominate/vote/withdraw.

**Data.** `superlative_nominations`, `superlative_ballot_votes`, `superlative_finalists`,
`superlative_prefs`, `econ_superlative_rewards`, `superlative_slates`, `superlative_prompts`,
`superlative_config`. `nominator_id` → `SUBJECT_ID_COLUMNS`; the two pair tables →
pair-purge loop + `THIRD_PARTY_TABLES`; register rows; manual privacy-table row.
**Retention:** who-nominated-whom and who-voted-for-whom are pair data with no reason to
live forever — the shared sweep (§3.1; this room registers the first callable) deletes
nominations and ballot votes `superlative_retention_cycles` (default 12) after their
tally; finalists and winners (the published outcome) stay. The
register row states the cap.

**Tests (logic layer).** slate filtering (self/opt-out/heat/partner/jail/departed, each a
`pytest.param` row); shuffle limit; ballot build thresholds and tie expansion; re-check
drops without promotion; turnout floor; tie/no-contact rule; opt-out discard; once-per-
cycle faucet across nominate-then-vote; quiet ledger (meta.anon); off mode writes no
reward row and fires no trigger; tally retry skips already-posted prompts; Last round
hides a partner whose win predates the pair; retention sweep; purge both directions.

**Size.** **~5,500–7,000 lines.** Risky Rolls (4,608 incl. tests) is the floor, not the
band: its whole dashboard is a 122-line panel with no routes file and no bank editor,
while this stage adds the editor, the report, routes tests, six faucet sites, the privacy
blocks and three panel sub-views on top. Migrations: 2 (tables, rewards).

**Prod steps after restart.** Pick the channel, add prompts (mild first), set
`min_nominations` and `min_ballot_votes` to 1 while testing (defaults 2 and 5 mean a
three-tester guild never names anyone), enable; tick `superlative_vote` on the pool row
if the rotation is ever turned on. **Exit:** one full nominate → ballot → tally sequence
driven by "Run the next boundary now" — the tally for a slate drawn at boundary 0 lands at
boundary 2, so at `cycle_days` 1 the natural path takes two days and cannot be a
`Testing:` line.

### Stage 2 — Sealed Envelopes

**Contents.** Seal / My envelopes (read, burn) / Stop sealing; `envelope_drops` sequence
+ counter (C17); drop job at boundary (header card + one message per envelope in seal
order; empty drop posts nothing); to-me DMs; filter + `strip_mentions` (mass and role mentions in **both** modes; user
mentions too for anonymous) + `defang_everyone_here` at seal, with the non-empty check
run **after** stripping (a body that was only a mention becomes ""); **every envelope
posts with `AllowedMentions.none()`** (the Confessions rule, confessions_cog.py:424) — a
named envelope carrying `<@id>` would otherwise become a bot-authored ping to that member,
including a no-contact partner; hold-on-jail (room envelopes only; destroyed at the second
consecutive held drop); destroy-on-leave via listener **and** the hourly sweep membership
check (C14) — the sweep acts only on `guild.chunked` guilds or after `fetch_member`
returns 404, never on a cache miss; audit row at publish via `insert_event` inside the publish transaction with
`extra={'sealed_at'}` (C19); `envelope_seal` faucet + kind; dashboard `sealed-envelopes`
(dials · upcoming-drop preview of *room* envelopes with Pull · report); `/envelopes` panel.

**Seal modal**: body `TextInput(max_length=body_max_chars)` + three `ui.Label` selects
(drop next/+2/+3 with projected dates, to room/me, named/anonymous) — fits the 5-component
modal cap (Birthday precedent, birthday_cog.py:282-303).

**Pull** = dashboard `promptDialog` → `POST /api/sealed-envelopes/{id}/pull` → destroy,
then best-effort `send_branded_dm` with the reason (the QOTD-sponsor Withdraw path,
economy-qotd-submissions.js:274). A failed DM still destroys; the report counts it.

**Decisions.** `envelope_review` not shipped (C15). A to-me envelope whose DM fails is
retried at the next boundary once, then burned; the report counts it. Founders' drop:
the drop counter is seeded to `drop_every_n − 1` when the room is enabled, so the first
boundary after enabling is a drop (a counter starting at 0 would first fire eight cycles
— 32 days — later). Off mode refuses seal in the service: seal needs no slate or
boundary, so without this a member in the second guild could be paid with no room
configured. No-contact row for the room: "no pair can form" — the bot never addresses a
member on another's behalf.

**Data.** `envelopes` (literal purge block — the one table whose body is the member's
words), `envelope_prefs`, `econ_envelope_rewards`, `envelope_drops`, `envelope_config`.
Register rows; manual privacy row *"Moderators can read envelopes addressed to the room
before they open; they cannot read envelopes addressed to you."*

**Tests.** burn; destroy-on-leave (listener path and sweep path; sweep skips an
un-chunked guild); to-me unaffected by jail; held-then-destroyed; audit age 0 at day-91
publish; preview excludes to-me; mention strip in both modes and mention-only body
rejected; filter rejection; first boundary after enable is a drop; drop cadence at N=8
with a pool change mid-way; fuse beyond `max_fuse_drops` refused; pending cap; off mode
pays nothing.

**Size.** ~3,500–4,500 lines. Migrations: 2. **Exit:** a seal today opens on the next
boundary; a mod pull DMs the author; a jailed author's room envelope is held.

### Stage 3 — Second Sight core

**Contents.** Bank `sight_prompts` (type closed/open, options JSON, heat, active, weight,
teaser, rerun_after_days) + editor; answer / change / pass; Read (closed) and Tell (open,
guessable vs sealed); Brier scoring (pure, `pytest.param` table over the three
confidences × outcome); reveal job (distribution only at ≥ `min_answers_to_show_distribution`;
best-read line named only when `perceptiveness_public`); My card (this cycle, season
perceptiveness/breadth/depth, who-reads-you with `min_reads_for_pair_score`); seasons
(`season_length_cycles`, closing card, public numbers recomputed from a season start
cycle stored in config — never stored totals); `sight_answer` + `sight_read` faucets
(read pays per correct read at reveal, capped via `cat_coins_earned_since`-style ledger
sum + per-read `INSERT OR IGNORE` anchor, intake's SAVEPOINT-per-unit shape);
`answer_retention_days` (730) on the shared sweep, applied to **every** `sight_*`
member table (answers, perceptions, later mirror assignments and reactions — the spec
caps only `sight_answers`, but the pair tables are the sensitive ones); **Clear my
history** (the whisper `_do_forget_user` shape, both sides); dashboard `second-sight`;
`/rooms sight` panel. **My card filters at render:** who-reads-you and the per-member
depth list drop `no_contact_partners_conn(subject)` — a perception placed before the pair
was added survives in the table (the either-party loop is erasure, not no-contact), so
the filter is the only thing that keeps a partner off the card. Reveal text posts with
`AllowedMentions.none()`.

**Pickers.** Read/Tell target = string select of answered members minus self/opted-out/
partners (`eligible_targets`), whisper pager above 25. Tell shows unattributed guessable
answers shuffled per viewer. Open answer `TextInput(max_length=140)` + guessable/sealed
Label select in one modal; filter + strip applied.

**Data.** `sight_answers`, `sight_perceptions`, `sight_prefs`, `econ_sight_answer_rewards`,
`econ_sight_read_rewards`, `sight_prompts`, `sight_config`. `perceiver_id` →
`SUBJECT_ID_COLUMNS`; `sight_perceptions` → pair loop + `THIRD_PARTY_TABLES`. Register
note: a target purge recomputes other members' season numbers (they are never stored).

**Tests.** consent-by-answering (unanswered member absent from every slate); pass records
nothing; change-until-reveal scored against final answer; scoring table; pair-score
minimum; correct-only payout and the cap; distribution threshold; season reset; retention
sweep over every member table; My card hides a reader whose perception predates the
pair; clear-history both sides; quiet ledger; off mode pays nothing; purge both
directions.

**Size.** ~5,500–7,000 lines — the largest stage; split the session in two (engine +
reveal, then panel + dashboard) if it runs long. Migrations: 2–3. **Exit:** two prompts
answered, read, revealed and scored on the fixed cadence; My card shows a Brier number.

### Stage 4 — Second Sight Mirror + witness

**Contents.** `assign_describers` in `slate_service.py` (C10) with its test table (N ∈
{0,1,2,3,4,5,8,20}, k=3, forbidden ∈ {∅, sparse, one-subject-forbidden-by-all}; no self,
no duplicate, no forbidden edge either orientation, excluded subject in-degree 0 but still
a describer, spread ≤ 1 when forbidden-free, same seed ⇒ same output); Mirror prompt type;
`sight_adjectives` (guild_id, word, active, sort) seeded via a "Restore defaults" route
with the Johari list **minus its self-critical words** (nervous, tense, shy,
self-conscious — the spec's "positive list; Nohari v2" line), ~52 words; describe =
**three** `ui.Label` selects (a string select holds 25 options; the 56-word list splits
28/28 at L|M, so two cannot work), each an ordinal third of the *active* rows, max 5 picks
each, plus server-side "exactly five" validation (C16) — with the mirror prompt text that
is four of the five modal components; the adjectives route refuses more than 75 active
words so an edit can never push a third past 25; `sight_mirror_assignments`; threshold delivery
(`mirror_min_describers`) with Arena/Blind spot/Façade regions; **Publish my Mirror**
(refuses while `is_hidden_by_rotation`, copy says when it opens; posts with
`AllowedMentions.none()`); witness reactions
(`sight_reactions`, private aggregate on My card, `witness_enabled`).

**Data.** `sight_mirror_assignments` (`describer_id` → `SUBJECT_ID_COLUMNS`),
`sight_reactions`, `sight_adjectives`. Pair loop + `THIRD_PARTY_TABLES` for both member
tables.

**Tests.** assignment table above; describer threshold; region computation
(`pytest.param` over pick/see overlaps); reaction aggregate never exposes who; publish
refused while hidden; select split never exceeds 25 per third; >75 active words refused.

**Size.** ~2,500–3,000 lines. Migrations: 1–2.

### Stage 5 — rotation enablement

Almost entirely prod work for Billy, listed in §7. Code: nav regroup if deferred (C18),
`docs/plans/rotating-feature-channels.md` updated, both plan docs retired to shipped in
INDEX.md. Optional: the two existing rooms that post into their own channel with no hidden
check (Whisper feed sends, whisper_cog.py:2587; Confessions publish) — raised in §8, not in
scope.

---

## 5. Per-room checklist (the cross-cutting obligations, once)

Each room's first commit carries all of these; the seam survey found every one of them
hand-maintained and most of them unmentioned in the spec.

**Economy kind + faucet** (per kind): `quests.TRIGGER_KINDS`, `TRIGGER_KIND_INFO`,
`ANON_KINDS`, optional `TRIGGER_FLAVOR`; `economy-sources-shared.js` `KIND_LABELS`
(hard-fails otherwise); `register._KIND_DISPLAY`; `guide.py` earn_rows; `quest_views.py`
`QUEST_STATE_LABEL`; `EconSettings.reward_<kind>` + `routes/economy.py` Field +
`economy_manager.py` faucets dict + `economy-income-sources.js` FAUCET_FIELDS (all four or
the Save 422s); `economy_service._PURGE_USER_ID_TABLES` for the `econ_*` anchor, and the
register's `econ_*` wildcard row (data_register.md:41) gets its count bumped and the new
table named inside it; test rows
in `test_economy_quests_service.py:3025` tuple, `test_economy_register.py` param rows;
`docs/economy_spec.md` kind table + faucet defaults; manual Income Sources sentence.
Flat award: `payout_possible` pre-check in the cog, `source_enabled`, `INSERT OR IGNORE`
anchor, `apply_credit(..., meta={'cycle': id, 'anon': 1})`, `fire_trigger_quests(...,
occurrence=cycle_id, anon=True)`.

**Anonymity audit**: `FEATURE_<ROOM>` constant, `KNOWN_FEATURES`, `FEATURE_LABELS`,
app.js:129 search keywords; `docs/anon_audit_spec.md` row.

**No-contact**: `SURFACE_<ROOM>` + `SURFACE_LABELS` in `no_contact_logic.py`;
`docs/no_contact_spec.md` gated-surfaces row; `check_and_record` only where a pair is
actually formed.

**Privacy**: literal purge block per feature in `privacy_service.py` (comment names the
migration, the register row and the Art 17(3) reasoning); pair tuples in the either-party
loop; `SUBJECT_ID_COLUMNS` additions; `THIRD_PARTY_TABLES`; never a member-id list in JSON
(`LIST_VALUED_MEMBER_COLUMNS` is the documented blind spot); `docs/data_register.md` rows
appended at the end of the table, each stating its retention (a cap and the sweep that
enforces it, or "indefinite" with the reason); `manual.html` §Your Data & Privacy table row + 🎭
callout slug + self-service bullet where a clear-history control exists; seeded purge test
both directions.

**Dashboard**: `app.js` nav entry (id = bare room name, frozen forever); `panels/<id>.js`
on `mountAsync` (report half on `mountReloadable`); `routes/<id>.py` + `include_router`;
`help-sections.js` row + `manual.html` `<h2 id=…>` (a text test fails on a missing
anchor); `docs/dashboard_ia.md` row; `test_nav_visibility.py` row; `tests/web/
test_<id>_routes.py`; `lockUnlessAdmin` on the settings half, moderator read; every
snowflake as a string; `npx eslint` + `stylelint` before commit.

**Bot surface**: the `/rooms <room>` subcommand in `manual.html`'s command table and
`/help` (one shared `app_commands.Group`, §4 preamble);
`tests/test_embed_accent_contract.py` `case()` row per new card builder; every card takes
`name_fn`; `AllowedMentions` allow-lists exactly the pinged member.

**Commit**: `Scope: summary`, prose body, `Testing:` checklist written for a volunteer
tester (one action, one observable result per box, nothing that waits on a boundary —
the card says "an admin presses Run the next boundary now" and, for stage 1, "with the
two minimum dials set to 1").

---

## 6. Land order and live collisions

Re-checked 2026-09-02 against main `dfe908d2`. `gdpr-review`, `website-voice-notes` and
`chore-auto-signoff` have **merged** since the first draft (their migrations are the two
200s and two 201s in §1), so the rules that waited on them are gone; rebase this branch
onto main and the conflicts they would have caused are already resolved. Still live:

| Branch (live) | Touches | Rule for this plan |
|---|---|---|
| `games-nav-split` | `app.js` nav, `games-panel-shared.js`, `dashboard_ia.md` | Flat nav entries only until it merges; the heading regroup after (C18). |
| `nsfw-gate-audit` | `question_source.channel_allows_nsfw` → category-aware | Call `channel_allows_nsfw`; inherit the fix on merge. |
| `documentation-review` | 26 dirty `docs/*.md` incl. `no_contact_spec.md`, `anon_audit_spec.md`, `games_system_spec.md` | Spec-table rows the checklist requires go through Billy while that session is open. |
| `image-guard-regression-corpus` | `INDEX.md`, `data_register.md`, `gdpr_runbook.md` | Append-only edits merge; expect a textual conflict on INDEX. |

Also: main is 11 commits past `last-full-gate`; the first merge here inherits that debt —
gate main after it.

---

## 7. Prod steps (Billy), by stage

| After | Do |
|---|---|
| Stage 1 restart | Superlatives page: pick the room channel, add ~10 mild prompts, set `cycle_days` (1 while testing) and both minimum dials to 1 while testing, enable, then drive the chain with "Run the next boundary now". Income Sources: confirm `superlative_vote` shows and its rate. |
| Stage 2 restart | Sealed Envelopes page: channel, `drop_every_n` (1 while testing), enable. Seal a founders' envelope or two. |
| Stage 3 restart | Second Sight page: channel, 4–6 closed prompts + 2 open, enable; leave `perceptiveness_public` off. |
| Stage 4 restart | Restore default adjectives; first Mirror prompt. |
| Stage 5 (rotation on) | Feature Rotation page: label the four existing rooms, add Risky Rolls and the three new channels (8 rows), tick each room's kinds (🔒 unticked for the four new kinds), `rooms_per_day` 2, announce channel, enable. The three rooms switch to rotation mode on the next tick; their `cycle_days` and `open_hour` dials disappear from the room pages (hidden with the reason, not left editable) and the panels say the boundary is the flip. |

---

## 8. Decisions this plan takes (override if wrong) and open items

Taken here, each defaulted the way a careful colleague would; none blocks stage 0:

1. **Envelopes before Second Sight** (§4 rationale).
2. **`envelope_review` not in v1** (C15) — the dial would be a toggle with no door.
3. **`/delete_me` untouched**; §2.7's row means erasure (C13).
4. **Rotation Rooms heading = Social only**; AMA and Risky Rolls stay in Games (C18).
5. **Private guild-scoped banks**, heat as a two-value column (C7).
6. **Rate dials on Income Sources**, not on the room pages (C6).
7. **DMs pref-agnostic** via `send_branded_dm` (C9).
8. **Superlatives has no seasons** (C21).
9. **Column names kept** (`nominator_id`, `perceiver_id`, `describer_id`) and added to
   `SUBJECT_ID_COLUMNS` rather than renamed (C12).
10. **One `/rooms` command group** for the three rooms, against the 100-command cap (§4).
11. **An admin "Run the next boundary now" button per room** — without it no `Testing:`
    card can be completed in one sitting.
12. **Off mode refuses every paying act in the service**, which is what keeps the second
    guild's default-on faucets from paying for a room nobody configured there.
13. **Spicy needs the room channel, the current channel and the member's opt-in** (double
    gate, stage 1 panel); no bot-side NSFW toggle anywhere.
14. **Superlatives pair rows expire** (`superlative_retention_cycles`, default 12); every
    `sight_*` member table shares the 730-day cap.
15. **`open_hour` and `cycle_days` are hidden, not advisory, in rotation mode.**

Raised, not in scope (each wants its own todo row):

- **Spin-the-Compliment and MFK pair members with no no-contact check** — two live
  violations of the CLAUDE.md contact rule; stage 4's `assign_describers` is the helper
  that would close both with a one-line swap each.
- **Whisper and Confessions post into their own room with no hidden check**; at
  `rooms_per_day: 2` over eight rooms a hidden Whisper feed accumulates posts nobody sees.
- **A channel can be in both `hidden_channels` and the rotation pool**; both snapshot
  overwrites, so a doubly-hidden room restores the wrong ones. Worth a guard before stage 5
  adds three channels.
- **`games_question_bank`, `revive_questions`, `bio_questions`, `legitlibs_templates` carry
  member-id columns with no register row** — outside this plan.

Questions for Billy (confirmations, not blockers):

1. Is Risky Rolls meant to join the pool at stage 5? The spec counts it; prod does not
   have it. The 8-room / 4-day arithmetic depends on the answer.
2. Stage 0 as its own session, or folded into the Superlatives session?
