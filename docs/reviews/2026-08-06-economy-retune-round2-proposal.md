# Economy retune — round 2 proposal (NOT applied), 2026-08-06

Status: **awaiting Ben's sign-off. Nothing here has touched prod.**

> **Revised 2026-08-06** against the empirical audit
> (`2026-08-06-economy-ledger-data-audit.md`). The first draft's estimates were
> derived from dial arithmetic; these are derived from measured per-kind
> totals. Three lines changed materially — see *What the audit corrected*.

Context: loose-ends §1 — the 07-30 retune missed its target. Measured on
08-01..08-05 the raw float grows **+3,992/day** (not the +3,330/day the first
draft assumed); the Pools line reads +4,993 because it includes casino swing
attribution. Median balance rose 186→206, so round 2 can keep cutting faucets
without hurting ordinary members.

## What the audit corrected

1. **`reward_game_participation` is the wrong dial.** 99.2% of
   `game_participation` money comes from `pay_cah_game_by_score`, which pays
   external Gamebot games (Wordle / Co-ordle / Anagrams / CAH) out of
   `reward_cah_win_max`. The native dial moves **+8/day**, so cutting it 3→2
   is worth ≈ **−3/day**, not −210. Dropped from the table.
2. **That dial has already been cut.** `econ_reward_cah_win_max` went 50→15 on
   2026-08-05 between 13:39 and 15:01 UTC (dated off the ledger). That is
   ≈ **−690/day** already banked but almost entirely *outside* the 08-01..08-05
   window this proposal is measured on — so the real starting point is nearer
   **+3,300/day**, and less cutting is needed than the raw number suggests.
3. **The 07-30 retune's `reward_game_win 50→25` did nothing**, for the same
   reason (measured 0.89× where 0.5× was intended). Not a new cut; recorded so
   nobody re-proposes it.

## Proposed dial changes (all via dashboard / config KV, live on save)

| Dial | Now | Proposed | Measured base | Est. Δ/day |
|---|---|---|---:|---:|
| catcatch common/uncommon/rare/epic/mythic/divine | 1/3/11/35/102/300 | **0/1/5/18/60/180** | +1,356/day | ≈ −800 |
| `cat_catch_daily_cap` | 0 (uncapped) | **150** | — | ≈ −150 |
| quest_board_daily | 3 | **2** | +1,404/day | ≈ −270 |
| login_text_base | 5 | **3** | +925/day | ≈ −220 |
| community tier payouts (quest_community) | — | **halve tier amounts** | +636/day | ≈ −318 |
| demurrage_rate_pct / threshold | 8 / 400 | **10 / 350** | −1,006/day sink | ≈ +250 sink |

Estimated net effect: **≈ −2,008/day** against a corrected baseline of
≈ +3,300/day, landing near **+1,300/day** — the bottom of the target band.
If that reads too aggressive, drop the `login_text_base` line (−220) first;
it is the habit loop and the one members feel daily.

**`cat_catch_daily_cap` is new to this round.** The dial was staged into prod
config on 2026-08-02 but its enforcing code never left an unmerged branch, so
cat catches have been uncapped the whole time (nine member-days over 150 since,
topping out at 653). The code landed 2026-08-06; setting the cap is now a real
change rather than a no-op. Estimate is deliberately conservative — it only
bites the handful of members above the ceiling.

## Rationale / guardrails

- Keeps the 07-30 constraints: drop *count* untouched, host bounty untouched
  (hosts stay favored), and the cuts again target volume faucets rather than
  per-member floors — median balance must stay ≥206 at the checkpoint or
  round 2 rolls back.
- cat_catch common→0 makes junk catches cosmetic (the fun is the catch, not
  the coin); the top tiers keep their pull (divine 180 still dwarfs
  everything else). The cap then handles the volume farmers the per-tier cuts
  can't distinguish.
- login_text 5→3 rather than 0: logins are the habit loop; halving, not
  killing.

## Procedure when approved

1. Apply the dials via the dashboard config (or SQL with a rollback file, same
   as 07-30, saved **outside the repo root** this time).
2. Save the baseline at apply time, per guild:
   `python scripts/economy_tuning_report.py --all-guilds --days 4 \
    --save-baseline docs/reviews/economy-baseline-2026-08-06.json`
3. Checkpoint **4 days later** with the same command plus `--baseline`.
   Success = raw net +1,300..+1,900/day AND median ≥ 206. Judge against the
   raw net **and** the Pools line, which reads higher.
4. Miss low (over-cut) → restore `login_text_base` 5 and lift the cat cap to
   250 first; miss high → deepen cat_catch mid-tiers.

## Out of scope here: the second guild

Guild `1476525656115515484` mints **+19,046/day**, nearly five times guild A,
and holds a larger float. None of the dials above apply to it — every retune
so far has been guild-A-only. It needs its own proposal; see
`2026-08-06-economy-guild-b-reprice.md`.
