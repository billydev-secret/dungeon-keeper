# Economy retune — round 2 proposal (NOT applied), 2026-08-06

Status: **awaiting Ben's sign-off. Nothing here has touched prod.**
Context: loose-ends §1 — the 07-30 retune missed its target (Pools line
still ~+4,993/day vs +1,300..+1,900 goal; raw mint−burn ≈ +3,330/day over
the 5-day window). Median balance rose 186→206, so round 2 can keep
cutting faucets without hurting ordinary members. cat_catch is now
tunable (`catcatch_coins_*` dials, shipped with this proposal's commit).

## Proposed dial changes (all via dashboard / config KV, live on save)

| Dial | Now | Proposed | Est. Δ/day |
|---|---|---|---|
| catcatch common/uncommon/rare/epic/mythic/divine | 1/3/11/35/102/300 | **0/1/5/18/60/180** | ≈ −450 |
| quest_board_daily | 3 | **2** | ≈ −270 |
| reward_game_participation | 3 | **2** | ≈ −210 |
| login_text_base | 5 | **3** | ≈ −220 |
| community tier payouts (quest_community) | — | **halve tier amounts** | ≈ −230 |
| demurrage_rate_pct / threshold | 8 / 400 | **10 / 350** | ≈ +250 sink |

Estimated net effect: **≈ −1,630/day**, landing raw net around
+1,700/day (target band) — the Pools line will read higher since it
includes casino swing attribution; judge against **both** numbers.

## Rationale / guardrails

- Keeps the 07-30 constraints: drop *count* untouched, host bounty
  untouched (hosts stay favored), and the cuts again target volume
  faucets rather than per-member floors — median balance must stay ≥206
  at the checkpoint or round 2 rolls back.
- cat_catch common→0 makes junk catches cosmetic (the fun is the catch,
  not the coin); the top tiers keep their pull (divine 180 still dwarfs
  everything else).
- login_text 5→3 rather than 0: logins are the habit loop; halving, not
  killing.

## Procedure when approved

1. I apply the dials via the dashboard config (or SQL with a rollback
   file, same as 07-30, saved outside the repo root this time).
2. Checkpoint **4 days later**: `python scripts/economy_tuning_report.py
   --days 4 --baseline docs/reviews/economy-baseline-2026-08-06.json`
   (baseline to be saved at apply time). Success = raw net +1,300..
   +1,900/day AND median ≥ 206.
3. Miss low (over-cut) → restore login_text 4 and participation 3 first;
   miss high → deepen cat_catch mid-tiers.
