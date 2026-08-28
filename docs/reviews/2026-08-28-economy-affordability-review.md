# Economy & affordability review — 2026-08-28 (guild A / TGM)

Status: **analysis + proposal. Nothing here has touched prod.** Every number
below is measured read-only from the live ledger over
**2026-08-01..2026-08-28 (28d)**, guild-local UTC−7.

Scope: **TGM (`1469491362444480666`) only.** Guild `1476525656115515484`
("nut") appears as context and is explicitly *not* proposed against — it is a
live economy run by someone else, denominated ~8× TGM. The third guild
(`1358148226850492618`, 26 wallets, one spender, 4,030 float) is out of scope.

---

## 1. Headline: the 07-30 retune worked. Growth ate it.

| Week | Mint | Burn | Earners | **Mint per earner** |
|---|---:|---:|---:|---:|
| W29 (pre-retune) | 56,673 | 5,740 | 104 | **545** |
| W30 | 69,409 | 14,745 | 154 | 451 |
| W31 | 46,409 | 15,636 | 167 | 278 |
| W32 | 56,511 | 15,314 | 168 | 336 |
| W33 | 52,711 | 19,320 | 165 | **319** |

Per-earner minting fell **41%** after 2026-07-30 — the retune hit its target on
a per-member basis. But earners went **104 → 165 (+59%)** over the same period,
so aggregate mint barely moved. Burn has stayed flat at 15–19k/week because
**every sink except demurrage is opt-in and does not scale with headcount**,
while every faucet does.

Net effect: the float went **138,419 → 195,730 in the last 14 days (+41%)**.

That is the finding. Billy's read — "the users are starting to be engaged" — is
correct, and engagement *is* the inflation mechanism. This will happen again
after any sink-side-only fix.

### The gap, precisely

| 28d steady state (ex-jackpot) | Per week |
|---|---:|
| Mint | 50,156 |
| Burn — real sinks | 17,169 |
| Burn — casino hold | 8,929 |
| **Net** | **+24,058/wk** (+3,437/day) |

On a 194,988 float that is **+12.3% per week**, compounding — a doubling roughly
every six weeks.

---

## 2. Correction: the casino *is* a sink

The 14-day report shows house hold 591 (0.2% of handle), which reads as "the
casino removes nothing". That is an artifact of **one 18,228 progressive-jackpot
hit on 2026-08-22**, which re-mints cut escrowed over the preceding months.

| Window | Handle | Hold as reported | **Hold ex-jackpot** |
|---|---:|---:|---:|
| 14d | 331,629 | 1,073 (0.2%) | **19,301 (5.8%)** |
| 30d | 500,893 | 18,484 (3.7%) | **36,712 (7.3%)** |

At ~8,929/wk the casino is TGM's **largest single sink**, ahead of demurrage
(7,542/wk) and rentals (8,660/wk).

The games are on-spec. `casino_logic.SLOTS_RTP_PCT` is **91.3%**, pinned to the
paytable enumeration by test; observed August base-game RTP (excluding wins
≥2,000) is **89.1%** across 6,057 spins — within variance. The pair multiplier
was already trimmed 1.5→1.45 on 2026-07-26. **No casino action is needed**, and
the paytable is hardcoded constants, not a dial, so none is available anyway.

The only casino lever that exists is `casino_jackpot_cut_pct` (5), which decides
how much hold is escrowed for later re-minting rather than burned permanently.

### And: nut's 18.6% hold is noise, not a structural difference

nut placed ~190 casino bets in 30 days against TGM's 10,260. Its "18.6% hold" is
small-sample variance and should carry no weight in any comparison.

---

## 3. Affordability: the price list is mostly fine

Median weekly income is **80** (last full week, 08-17..23). *Note the report's
"weekly income p50 169" under `--days 14` is a 14-day sum, not a weekly rate —
the pricing hints must be read against 80.*

Against the code's own `PRICING_FACTORS` (`economy/metrics.py`, "suggested price
≈ median weekly income × factor"):

| Perk | Price | Recurring | % wallets can afford | % of earners' weekly income | vs hint |
|---|---:|---|---:|---:|---:|
| quest_reroll | 10 | one-off | 99% | 98% | 1.2× |
| streak_shield | 30 | one-off | 96% | 97% | 1.2× |
| voice_style | 40 | weekly | 95% | 62% | 1.7× |
| role_name | 45 | weekly | 91% | 61% | 1.6× |
| emoji | 60 | weekly | 90% | 56% | — |
| role_color | 65 | weekly | 90% | 54% | 1.6× |
| role_preset | 80 | weekly | 87% | 50% | 1.2× |
| role_gradient | 150 | weekly | 68% | 41% | 0.8× |
| text_room | 200 | one-off | 61% | 35% | 1.2× |
| voice_room | 230 | one-off | 60% | 34% | 1.4× |
| role_holographic | 500 | weekly | 43% | 16% | 2.1× |
| role_icon (BYO) | 1,200 | weekly | **20%** | **5%** | **20×** |

**Everything at or below 90 is affordable to 83–99% of wallets.** That tier is
well-tuned and should not move. The cliff is between 150 and 500.

**Affordability is not the primary problem — inflation is.** Prices sit roughly
on-curve; the currency behind them is losing ~12% of its value per week. Cutting
prices now would make that worse.

### On `econ_price_role_icon` = 1,200

This is worth flagging but is **probably deliberate, not drift**. TGM stocks a
curated icon catalog — Brown Crown 50, Bronze 200, Silver 300, Golden 400 — and
`shop.py` folds the flat `price_role_icon` into that catalog's span. So 1,200
reads as the *bring-your-own-image* premium above a 50–400 curated ladder, and
two of the seven renters pay the catalog price.

Points against leaving it unexamined: it is a **weekly recurring** charge at 15×
median weekly income; there has been **no new BYO signup since 2026-07-24**; and
the three members still paying it supply **~31% of the entire rental sink**
(12,000 of 38,760 over 30d). Cutting it would cost real sink. See open questions.

---

## 4. Proposed dial changes

**Design principle: cut on the faucet side, because the faucet is what scales
with headcount.** A sink-side fix has to be redone every time the server grows —
which is exactly what happened to the 07-30 retune. Within the faucet, cut the
**passive/grindy** lanes and protect the **social/hosting** lanes (`game_host`,
`qotd`, `photo_post`, `quest_community`) that are producing the engagement.

### First, what happened to round 2

`2026-08-06-economy-retune-round2-proposal.md` was written and, it turns out,
**essentially never applied.** Checked against live config:

| Round-2 line | Proposed | Live now | Applied? |
|---|---|---|---|
| `cat_catch_daily_cap` | 150 | 150 | **yes** |
| catcatch tiers | 0/1/5/18/60/180 | 1/3/11/35/102/300 (defaults) | no |
| `quest_board_daily` | 3 → 2 | **5** | no — and since *raised* to 5 |
| `login_text_base` | 5 → 3 | 5 | no |
| demurrage rate/threshold | 10 / 350 | 8 / 400 | no |

So one line of five landed, and the quest board has since grown from 3 dailies
to 5 (worth ~+1,773/wk on its own). Most of what follows is round 2 revived at
today's measured numbers, not a new invention.

### The package

| # | Dial | Now | Proposed | Measured base | Est. Δ/wk |
|---|---|---:|---:|---:|---:|
| 1 | `econ_quest_board_daily` | 5 | **3** | 4,432/wk | −1,773 |
| 2 | `econ_quest_board_weekly` | 3 | **2** | 2,366/wk | −789 |
| 3 | catcatch tiers (6 dials) | 1/3/11/35/102/300 | **0/1/5/18/60/180** | 5,860/wk | −2,640 |
| 4 | `econ_cat_catch_daily_cap` | 150 | **75** | (on top of #3) | −600 |
| 5 | `econ_login_text_base` | 5 | **3** | } 6,828/wk | } −2,700 |
| 5 | `econ_login_voice_base` | 8 | **5** | } | } |
| 5 | `econ_streak_bonus_cap` | 10 | **6** | } | } |
| 6 | `econ_drops_per_day` | 16 | **10** | } 3,420/wk | } −1,920 |
| 6 | `econ_drops_max_coins` | 50 | **35** | } | } |
| 7 | `econ_reward_cah_win_max` | 15 | **10** | 3,514/wk | −1,000 |
| 8 | `econ_milestone_per_100` | 100 | **50** | } 1,320/wk | } −520 |
| 8 | `econ_milestone_day100` | 365 | **200** | } | } |
| | **Faucet subtotal** | | | | **−11,942** |
| 9 | `econ_demurrage_rate_pct` | 8 | **12** | } 7,542/wk sink | } +4,100 sink |
| 9 | `econ_demurrage_threshold` | 400 | **350** | } | } |
| 10 | `casino_jackpot_cut_pct` | 5 | **2** | ~1,260/wk escrowed | +750 sink |
| 11 | `econ_wager_rake_pct` | 10 | **15** | } | } +300 sink |
| 11 | `econ_bounty_rake_pct` | 10 | **15** | } | } |
| | **Sink subtotal** | | | | **+5,150** |
| | **TOTAL** | | | | **≈ 17,092/wk** |

**Expected landing: +24,058/wk → ≈ +6,966/wk**, i.e. float growth from **+12.3%
to ≈ +3.6% per week**. That is 71% of the gap, deliberately not 100% — see below.

### Why not flatten it in one move

Closing the last ~7,000/wk from here means either gutting the quest and
community lanes (17,331/wk combined — the thing actually driving engagement), or
pushing demurrage to ~16–18%, at which point a hoard halves in under a week.
Neither is worth doing blind. **Stage 2 after the checkpoint** — `demurrage_rate_pct`
12 → 16 alone is worth roughly a further −3,900/wk, and demurrage is the one
sink that scales with float and headcount automatically, so it is the right
place to carry the residual.

### Estimates are upper bounds

These are dial arithmetic against measured per-kind totals, and they assume
behaviour does not change. It will — round 2's own postmortem found dial
arithmetic overestimated (`reward_game_win` 50→25 measured 0.89× where 0.5× was
intended). Treat every figure above as a ceiling and judge on the checkpoint.

### Guardrail

Median balance is **362** today (round 2's guardrail was ≥206). If the
checkpoint shows median < 300, restore `econ_login_text_base` to 5 first — it is
the daily habit loop and the line members feel most.

---

## 5. What no dial can express

Flagging these rather than writing code, per the working agreement:

1. **There is no global faucet multiplier.** `booster_multiplier` (1.5) is a
   booster-only bonus, not an economy-wide rate. Retuning the faucet therefore
   means touching ~14 individual dials every time the member base moves — which
   is precisely why the 07-30 retune silently went stale. A single
   `econ_faucet_scale_pct` applied at mint time would make future retunes one
   number and one checkpoint.
2. **Quest rewards are per-row data, not dials.** Daily/weekly/community quest
   payouts live in `econ_quests.reward` (27 active dailies avg 16, 61 weeklies
   avg 39, community flat 10). Board *size* is a dial; reward *size* is an
   editorial pass over rows. The event-quest lane (3,449/wk) is entirely
   admin-created and has no dial at all.
3. **The casino paytables are hardcoded** (`casino_logic.py`). Correct as-is, so
   this is not urgent — noted so nobody looks for a dashboard control.

None of these are needed for the proposal above. (1) is the one worth building
if a round 4 looks likely.

---

## 6. Procedure

1. Apply via the dashboard config, with a rollback SQL file saved **outside the
   repo root** (`/home/ben/discord-bots/archive/db-rollbacks/`, as on 07-30).
2. Save a baseline at apply time:
   `python scripts/economy_tuning_report.py --all-guilds --days 7 \
    --save-baseline docs/reviews/economy-baseline-2026-08-28.json`
3. **Checkpoint 10 days later** (not 4 — the 28-day measurement above shows the
   daily series is lumpy enough that 4 days reads as noise), same command plus
   `--baseline`. Success = net **+4,000..+9,000/wk** AND median balance ≥ 300.
4. Miss low (over-cut): restore `login_text_base` 5, then lift the cat cap to 150.
   Miss high: stage 2 — `demurrage_rate_pct` 12 → 16.

---

## 7. Open questions

1. **`price_role_icon` 1,200** — deliberate BYO premium above the 50–400
   catalog, or drift? It is the one price far off-curve, but it is also ~31% of
   the rental sink from three members. Leaving it is defensible; cutting it
   needs the sink made up elsewhere.
2. **`casino_jackpot_cut_pct` 5 → 2** trades a fun, memorable, lumpy 18k
   windfall for ~750/wk of steady burn. Worth it, or is the jackpot moment part
   of what makes the casino work?
3. **Event quests (3,449/wk, no dial)** — should the next editorial pass over
   `econ_quests` rows bring event/community rewards down, or is that lane doing
   engagement work worth its cost?
