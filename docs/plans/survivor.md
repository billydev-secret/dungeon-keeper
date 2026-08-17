# Survivor — staging plan

**Status (2026-08-17):** stage 1 shipped (schema — migration 165 after a
renumber around main's 164 — service, panel, privacy wiring) + a same-day
code-review fix pass. Stage 2 next.

**Spec:** [`docs/survivor_spec.md`](../survivor_spec.md) (v2.2, amended 2026-08-17)
**Deadline:** NFL Week 1 kickoff — **Thursday Sept 10 2026**, ~24 days out from 08-17.
**Branch/worktree:** `survivor-cog`.

## The one hard constraint

The season opens on a date nobody can move. So the plan is ordered by *what a
playable Week 1 requires*, not by what's most interesting to build. Everything in
**Tier 1** must be live by Sept 10. Everything in **Tier 2** has a real in-season
deadline but that deadline is later than Week 1, and the game runs without it.
Tier 3 is post-launch.

The spec's §7 build order is already close to priority order; this plan keeps its
sequence and pins dates and cut lines to it.

---

## Tier 1 — required for Week 1 (Sept 10)

A season cannot open without these. Target **all of Tier 1 merged by Sept 3**, a
week of slack before kickoff, with enrollment opening that day.

| Stage | §7 | What | Target |
|---|---|---|---|
| **1. Schema + config** | 1 | Migration for the five tables, `survivor_service.py` season/config accessors, dashboard panel (create season, all §5 dials, role pickers). Data register rows + privacy notice land here. | Aug 20 |
| **2. ESPN ingest + parser** | 2 | `survivor_espn.py` — scoreboard parse, schedule ingest into `nfl_games`, daily refresh, favorite capture. Fixture-based tests on saved JSON; **no network in the suite**. | Aug 23 |
| **3. Join / pick / status / board** | 3 | Lock + validation logic (`survivor_logic.py`), `/survivor pick` with autocomplete, slate button dual-select, `/survivor status`, `/survivor board`, season announcement embed + Join button. Economy: buy-in debit. | Aug 27 |
| **4. Poll + settle engine** | 4 | 10-min polling in game windows, idempotent settle, strike accounting, tie-as-loss, void handling, auto-assign at final kickoff + cap. `/survivor admin settle`, `preview-reckoning`. | Aug 30 |
| **5. Reckoning + slate + last-call** | 5 | The three-act Tuesday post, Wed slate task, Sat last-call DMs, flavor corpus + its dashboard CRUD. Guild-local scheduling via `tz_offset_hours`. | Sep 2 |
| **5b. Gauntlet replay + receipt** | 5b | Deterministic replay engine + private receipt embed, gauntlet fee debit and its routing. | Sep 3 |
| **6a. Ghost roles + Ghost Streak** | 6 | Elimination → Ghost role + condolence DM, ghost picking flow, streak tracking. | Sep 5 |

**Why 5b and 6a are Tier 1 despite being late in §7.** They look deferrable and
are not. The spec's core promise is *"the door is always open"* — enrollment never
closes. A joiner in Week 4 goes through the Gauntlet (5b) and, if the replay kills
them, lands in Ghost Streak (6a). Without both, a Week 4 joiner either can't join
at all or joins into a dead-end with nothing to play. **Ship 5b and 6a or the late
entry promise is a lie**, and it's the promise that keeps the game alive after the
first wave of deaths in October.

The *streak side-pot payout* is not needed Week 1 — only streak *tracking* is, so
the counter is correct from day one. Payout rides with stage 7.

## Tier 2 — in-season, deadline later than Week 1

| Stage | What | Real deadline | Why it can wait |
|---|---|---|---|
| **6b. Wipeout / annul** | Week annulled through Wk 13, equal split Wk 14+ | Wk 2 (first plausible mass death) | Needs to exist before a wipeout *can* happen, which is not Week 1 — but this is the shortest leash in Tier 2, so it lands right after launch. |
| **6c. Double-pick** | Two slots, independent locks | **Week 14 — Dec 4** | `double_pick_start_week: 14`. Fifteen weeks of runway. The slot column ships in stage 1's schema so no migration is needed later. |
| **6d. The Accord** | Vote flow, unanimity, Tue–Thu window | When ≤6 alive — **Nov at the earliest** | Gated on `accord_max_alive: 6`; with 20–50 entrants and a strike, that's deep in the season. |
| **6e. Endgame + payouts** | Ceremony, Sole Survivor role, main + ghost pot payouts | **~Jan (Wk 18)** | The single latest-binding piece. Four months of runway. |
| **7. Notifications panel** | `/survivor notifications` per-category DM toggles | Wk 2 | Last-call DMs default ON and honor opt-out from stage 5; the self-service toggle can follow by a week. |

## Tier 3 — post-season

§7.7's v2 backlog: buyback window, dead pool, loser-pool and underdog variants,
playoff capstone. Not scheduled.

---

## Cut lines, if a stage slips

Ordered by what I'd sacrifice first. Each is a real degradation, stated honestly:

1. **`preview-reckoning`** — nice, not load-bearing. You'd read the Reckoning live.
2. **Board meta-stats** (most-burned teams) — the roster and graveyard are the point.
3. **Slate dual-select button** — `/survivor pick` still works; casuals face slash
   syntax, which the spec explicitly wanted to avoid. Costs accessibility, not play.
4. **Flavor corpus CRUD panel** — ship a seeded corpus in the migration, add the
   editor later. The Reckoning still has voice; you just can't retune it from the web.
5. **Last-call DMs** — auto-assign still covers the pickless, so nobody is
   eliminated by silence. Costs the nudge, not correctness.

**Never cut:** per-game lock enforcement, no-reuse, idempotent settle, the
gauntlet replay's determinism, the auto-assign cap. These are correctness, and a
survival pool that eliminates the wrong person has no second chance to be right.

## Testing posture

Per CLAUDE.md, the unit under test is the logic/service layer — no Discord mocks.

- **Gauntlet replay determinism is the headline test.** §4.2 makes it a pure
  function of stored `favorite` values and stored winners. Two joiners entering the
  same week must inherit byte-identical lines; that gets pinned explicitly, and a
  replay must never re-fetch.
- **§6's fourteen edge cases are a test checklist**, not prose. #10 (joiner with
  no legal team) and #13 (auto-assign dead end) are explicitly *must not crash*
  and get named tests.
- **ESPN parsing is fixture-based** on saved JSON. No network calls in the suite, ever.
- Lock enforcement per game, no-reuse across the satchel, strike accounting,
  tie-as-loss, void handling, auto-assign cap, double-pick slot independence,
  wipeout/annul boundary at week 13/14, accord window + unanimity + leave-as-decline.

## House obligations tracked against this plan

- **Data register** — `survivor_players` and `survivor_picks` get rows in
  `docs/data_register.md` in the **same commit as the migration** (stage 1), each
  with an explicit purge-or-preserve decision. `user_id` is already in
  `privacy_service.SUBJECT_ID_COLUMNS` (verified 08-17, `privacy_service.py:270`),
  so the export sees both without changes there.
- **Privacy notice** — a line in `manual.html` §Your Data & Privacy, same commit.
- **manual.html** — every stage that adds a command or panel updates it.
- **INDEX.md** — `survivor_spec.md` moves Design → Reference as stages land.
- **Economy** — every coin movement through `economy_service` with its own ledger
  kind (spec §5.1). The 10,000 seed is a **faucet**; it is booked at create-season
  and minted once at payout.
- **Scheduling** — reuse `get_tz_offset_hours` + the `local_hour` pattern from
  `chat_revive/logic.py` and `birthday_cog.py`. No new guild-local clock.
