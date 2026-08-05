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
- **BEN:** second retune round? The remaining levers are cat_catch (needs
  code → make it a dial), quest faucet, participation. Rollback files remain
  valid until this verdict is accepted.

## 2. Image Guard / Marqo swap — merged, but flying blind

- The swap **is merged** (`fe244a7f`, memory saying "unmerged branch" is
  stale).
- Both log channels are still **0** while enforcement is on:
  `nsfw_log_channel_id=0`, `nsfw_sfw_prevention_log_channel_id=0`,
  `nsfw_sfw_prevention_mode='enforce'`, `nsfw_observe_age_gated=1`.
  Deletions/blocks currently leave **no audit trail anywhere** (log.txt is
  wiped every boot; Discord audit log only shows the bot deleting).
- **BEN:** pick a log channel id for both keys (dashboard: Image Guard
  settings). This is the cheapest fix on this list.

## 3. External game host payouts — ✅ closed

`game_host` ledger entries run 2026-07-25 → today, 51 payouts / 7,270 total.
The fix + backfill landed; continuous flow since. No action.

## 4. Dev-session snapshot/restore — still unmerged, units correctly off

Branch `investigate-a-control-…` (e88a4f9a) is **unmerged**; its systemd user
units are installed but disabled (`dk-restore.service` disabled,
`dk-snapshot.timer` disabled) and `Linger=no`. That's the agreed pre-merge
state — but it's been parked since ~07-31.
**BEN:** `/dk-ship` it (then `systemctl --user enable --now dk-snapshot.timer`
+ `loginctl enable-linger ben`), or abandon and remove the units.

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

## 7. Unmerged session branches — triage list

Five worktree branches not merged to main (besides item 4's):
confessions-log-channel-optional, jail-member-left, quest-ideas,
pen-pals-reply-reminder, and the testing-cards-not-clearing worktree
(no branch in --no-merged output; verify). **BEN:** ship or reap each;
worktrees also pin their `.venv` copies (~disk).

## 8. Wellness prod-DB hand-edits (07-30) — ✅ reconciled (see 2026-08-05-wellness.md)

Memory says wellness config rows were hand-edited ahead of the 445a5da4
merge and never reconciled with shipped defaults. The Wave 1 wellness battery
will diff prod config rows against the shipped settings registry.
