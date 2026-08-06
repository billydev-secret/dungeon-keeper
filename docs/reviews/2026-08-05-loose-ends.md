# Loose-ends / prod-drift audit — 2026-08-05 (Wave 2.5 of staged review)

Status of every known dangling item, checked against prod DB (read-only) and
git. Decisions marked **BEN** need a human call; nothing here was changed.

## 1. Economy retune checkpoints (were due 08-02 / 08-04) — ⚠ target missed

- **Demurrage did fire** at the 08-03 W31 roll: 56 members taxed, **5,031**
  (prediction was ~3,500; +44% over — rate 8%/floor 400 caught 56 members vs
  31 in W30 at 755 total).
- Report vs `economy-baseline-2026-07-30.json` (5-day window):
  - **Float still growing ~+5,000/day** (Pools line +4,993) — the 07-30 retune
    target was +1,300..+1,900/day. **Not achieved.**
  - Constraint that mattered *did* hold: median balance **186 → 206** (cuts
    didn't land on ordinary members); burn ratio 40.3% → 46.8%.
  - Top faucets now: quest 5,745, **cat_catch 5,444**, participation 4,408,
    login 3,859 (week window). cat_catch is hardcoded in
    `games_external/parser.py` — it is now the ~#2 faucet and untunable
    without a code change.
  - An 08-02 mini-retune happened (rollback file present: adds
    `econ_reward_cah_win_max`).
- **BEN:** second retune round? The remaining levers are ~~cat_catch (needs
  code → make it a dial)~~, quest faucet, participation. Rollback files remain
  valid until this verdict is accepted.
- **Re-verified 2026-08-06** (read-only snapshot via the sqlite3 backup API,
  `economy_tuning_report.py --days 5 --baseline economy-baseline-2026-07-30`):
  the Pools line still reads **+4,993.5/day** and the last five guild-local
  days run +4,275 / +4,993 / +2,708 / +4,997 / +3,905. Median balance 194
  (was 186), burn ratio 45.6%. Nothing here has drifted — the finding stands
  as written, and the round-2 proposal's estimates are still sized correctly.
  cat_catch **is** now a dial (`98395fdb`), so every lever in the proposal is
  reachable from the dashboard.

## 2. Image Guard / Marqo swap — ✅ resolved 2026-08-06 (web panel IS the trail)

- The swap **is merged** (`fe244a7f`, memory saying "unmerged branch" is
  stale).
- Correction to the original finding: the durable trail **already exists on
  the web** — `nsfw_blocks` records every gate deletion and Moderation →
  Blocked Images renders it. Verified 2026-08-06: the recording call is
  wired on the enforcement path (events_cog:475 → record_block_safely,
  covered by test_post_monitoring) and the table is empty only because no
  gate has destroyed an image since the swap (43 observe-mode
  classifications, all compliant). The `0` log-channel keys govern only the
  optional Discord-side fan-out, which Ben declined 2026-08-06 — the web
  panel is the audit surface of record. Closed.

## 3. External game host payouts — ✅ closed

`game_host` ledger entries run 2026-07-25 → today, 51 payouts / 7,270 total.
The fix + backfill landed; continuous flow since. No action.

## 4. Dev-session snapshot/restore — ✅ merged 2026-08-05; ⚠ linger still unrun

**Superseded 2026-08-06.** The branch **merged** as `81035491`
(`25ac6e32` Dev sessions: snapshot + restore), and the timer was enabled:

| Unit / setting | State (verified 2026-08-06) |
|---|---|
| `dk-snapshot.timer` | **enabled** ✅ |
| `dk-restore.service` | disabled — correct, it is oneshot-on-demand |
| `loginctl show-user ben -p Linger` | **`Linger=no`** ⚠ |

**Remaining action (BEN, one command):** `loginctl enable-linger ben`.
Without linger the user manager stops when the last session ends, so the
snapshot timer does not run across the reboot the feature exists to survive
— the snapshot would be stale exactly when restore needs it.

## 5. Rollback SQL files in repo root — keep, but move somewhere git-clean-safe

`econ-config-rollback-2026-07-30.sql`, `econ-reprice-rollback-2026-07-30.sql`,
`econ-config-rollback-2026-08-02.sql`, `intake-steps-rollback-2026-08-01.sql`
— all untracked in the production checkout root; **one `git clean -fd` from
gone**. They stay live until the retune verdict (item 1) is accepted.
Recommendation: move to `~/dk-rollbacks/` (outside the repo) or commit them
under `docs/reviews/` as historical record once obsolete.

## 6. `Discord Messages/` in repo root — relocate out of the repo

385 per-channel dirs (`c<snowflake>/channel.json + messages.json`) with full
message contents — this is Billy's own **Discord data-package export**
(includes other servers, e.g. "The Hut"), not bot data. Dated 08-04, likely
the ToD-MCP scrape source material. It's personal data sitting untracked in
a git repo root (same `git clean` hazard, plus accidental-commit risk).
**Recommendation:** move to `~/discord-export-2026-08-04/` and add
`Discord Messages/` to `.gitignore` if it must stay.

## 7. Unmerged session branches — ✅ mostly cleared 2026-08-06

Four of the five shipped (`git branch --no-merged main`, 2026-08-06):

| Branch | State |
|---|---|
| confessions-log-channel-optional | merged `78053cf4` |
| jail-member-left | merged `cdb73a17` |
| pen-pals-reply-reminder | merged `011c6a2f` |
| testing-cards-not-clearing | gone — never had a branch |
| **quest-ideas** (`i-m-trying-to-think-of-more-quests…`) | **still unmerged** |

**BEN:** ship or reap quest-ideas — it is the last one, and its worktree
still pins a `.venv` copy.

## 8. Wellness prod-DB hand-edits (07-30) — ✅ reconciled (see 2026-08-05-wellness.md)

Memory says wellness config rows were hand-edited ahead of the 445a5da4
merge and never reconciled with shipped defaults. The Wave 1 wellness battery
will diff prod config rows against the shipped settings registry.
