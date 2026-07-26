# Economy & casino — sources and sinks after one day of casino traffic

Read-only review of the live ledger (`dungeonkeeper.db`, main guild
`1469491362444480666`) plus the money-moving code paths, 2026-07-25.
Compares against [`economy-baseline-2026-07-20.json`](
economy-baseline-2026-07-20.json).

Bottom line: **the casino is working exactly as designed and is not the
problem.** The float nearly doubled in five days, and ~60% of that came
from the XP→coin conversion rate being cut ~20×. Two live settings on the
casino side (max bet 1,000 with the daily wager cap turned off) plus the
uncapped progressive jackpot mean a single spin can currently mint more
coins than the entire guild holds.

---

## 1. Where the money is

| | 2026-07-20 baseline | 2026-07-25 | Δ |
|---|---|---|---|
| wallets (>0) | 104 | 114 | +10 |
| **total float** | **26,471** | **49,427** | **+87%** |
| p50 balance | 171 | 209 | +22% |
| p90 balance | 550 | 994 | +81% |
| p95 balance | 691 | 1,609 | +133% |
| top wallet | 2,421 | 4,878 | +101% |

(Percentiles from `scripts/economy_tuning_report.py --baseline
docs/reviews/economy-baseline-2026-07-20.json`, so they match the baseline's
wallets-above-zero basis.)

Gini 0.72; top 10 wallets hold 37% of the float, top 20 hold 52%. The
growth is concentrated at the top — the median member gained 22% while p95
gained 133%.

## 2. Faucets (last 24h, main guild)

True mint — casino payouts, transfers and wager payouts excluded, since
those are recycled, not new coins:

| source | 24h | share | admin-tunable? |
|---|---:|---:|---|
| **conversion (XP→coin)** | **7,959** | **60%** | yes (`econ_xp_per_coin`) |
| **cat_catch** | **2,805** | **21%** | **no — hardcoded table** |
| login | 946 | 7% | yes |
| quest | 818 | 6% | yes |
| qotd | 215 | 2% | yes |
| game_host | 150 | 1% | yes |
| drop / milestone / photo / quest_bonus / game_* | 444 | 3% | yes |
| **total minted** | **13,337** | | |
| (rental refunds, not a faucet) | 1,002 | | |

Pre-casino week (7/13–7/19) minted 22,839 over seven days — **3,263/day**.
The faucet is now running at **4× that rate**.

### 2a. The conversion rate is the actual inflation event

`econ_xp_per_coin` is currently **0.5** (2 coins per XP). Ledger evidence
of the change:

- 2026-07-20: 318.25 XP → **32 coins** (≈10 XP/coin)
- 2026-07-25: 298.38 XP → **932 coins** (≈0.32 XP/coin, with the 1.5×
  booster multiplier on top)

Conversion ran ~200–680/day for the whole first week, was off 7/21–7/24
(rate 0 — the code deliberately skips accrual so no backlog builds), and
came back at **7,959 in one day across 70 members**. It is now larger than
every other faucet combined, and it is the *least* effort-gated one —
passive chat XP, no quest, no game, no login streak.

This single dial explains the float doubling. Suggested landing zone: **2–4
XP/coin**, which puts conversion at 1,000–2,000/day — meaningful, still
the largest single faucet, but no longer 60% of the economy.

### 2b. `cat_catch` is the second-largest faucet and has no dial

2,805/day (21% of mint), 302 catches from 19 members yesterday. The payout
table is a module constant in `games_external/parser.py:223` —
`common:1, uncommon:3, rare:11, epic:35, mythic:102, divine:300` — doubled
on a blessed catch, then multiplied by the 1.5× booster. There is no
`econ_*` config key for it, so it can't be tuned from the dashboard
Faucets page alongside every other faucet. Per CLAUDE.md's "configuration
lives on the web dashboard", this one is missing its panel.

### 2c. The quest faucet is collapsing

7/21: 2,749 → 7/22: 4,340 → 7/23: 2,172 → 7/24: 954 → 7/25: 818.

The casino opened 7/24. Quest claims fell ~80% in three days. The
earn-by-doing loop is losing to the slot machine — worth watching, since
quests were the faucet that actually bought engagement.

## 3. Sinks

### 3a. Real destruction, last 24h

| sink | 24h | notes |
|---|---:|---|
| casino house hold | 5,065 | but see escrow below |
| rentals (shop) | 1,570 | 1,002 refunded back the same day |
| everything else | 0 | |

Lifetime: 45,094 coins have left wallets, but **36,205 of that is casino
stakes, of which 28,405 came straight back**. Actual permanent destruction
since launch is ~7,500 against 103,807 minted — **a 7% burn ratio**.
(Baseline burn ratio was 3.9%, so the casino roughly doubled it — real
progress, just not enough.)

Net: **+9,100 coins/day on a 49,427 float ≈ +18%/day**, doubling in ~4
days.

### 3b. The casino is a rake, not a sink

| game | plays | wagered | returned | hold | realized RTP |
|---|---:|---:|---:|---:|---:|
| slots | 1,206 | 20,759 | 15,605 | 5,154 | 75.2% |
| blackjack | 129 | 8,123 | 6,407 | 1,716 | 78.9% |
| coinflip | 90 | 4,028 | 4,867 | −839 | 120.8% |
| derby | 33 | 1,975 | 856 | 1,119 | 43.3% |
| roulette | 27 | 820 | 290 | 530 | 35.4% |
| war / baccarat / dice / keno | 27 | 500 | 380 | 120 | 76.0% |
| **total** | **1,512** | **36,205** | **28,405** | **7,800** | **78.5%** |

The 78.5% headline looks like the house is eating 21%, but **5,211 of that
7,800 is sitting in the progressive jackpot**, not destroyed — deferred
payout. Net of escrow the house has burned **2,603 on 36,170 of handle =
7.2%**, against a blended design edge of ~5–7% (slots enumerate to exactly
93.32% RTP; coinflip 95%; roulette 97.3%; sic bo 97.22%; keno 94.7–95.5%).

**The math is behaving.** I verified blackjack's ledger against its own
hand table (43 wins + 10 pushes + 68 losses over 121 settled hands:
2,940×2 + 45×2.5 + 415 = 6,407 returned — exact), and the double-down path
debits and settles correctly. The low blackjack/slots realized RTP is
~2σ variance on small samples plus the jackpot diversion, not a bug.

The structural point: **at a 7% rake, sinking 13,337 coins/day requires
~190,000 coins/day of handle.** Yesterday's handle was 23,081 from 22
players. The casino cannot be the sink that balances this economy — it can
only be a churn machine with a small skim.

### 3c. Every other designed sink is dead

| sink | lifetime use |
|---|---|
| rentals (role color/name/icon/gradient/holo, rooms) | 47 rentals, 6,420 spent, **1,474 refunded (23%)** |
| auctions | 1 bid, 10 coins |
| bounties | 0 |
| raffle | **disabled** (`econ_raffle_enabled=0`) |
| emoji sponsor | 0 |
| QOTD sponsor | 0 |
| pin of the day | 0 — and **priced at 0**, so it's free |
| community pot | 245 contributions, **0 payouts** |
| PvP wager rake | **0%** — wagers are pure recycling (520 in, 520 out) |
| streak shield / quest reroll | 120 + 20 |

**18 members spent anything outside the casino since 7/20. 100 earned.**
The shop's whole catalogue is cheap enough (35–500) that a p90 wallet of
994 can buy the lot and still be up on the day.

### 3d. Demurrage is calibrated for the old economy — and hasn't run yet

`econ_demurrage_rate_pct=3`, `econ_demurrage_threshold=500`.
`econ_demurrage_sweeps` is **empty** — the sweep fires at the ISO-week roll
(`economy_loop.py:364`), so the first one lands Monday 2026-07-27. It is
untested in production.

At the current distribution it will collect **632 coins — 4.7% of one
day's mint.** The what-if grid:

| floor | wallets hit | excess | @3% | @10% | @20% |
|---:|---:|---:|---:|---:|---:|
| 500 | 29 | 21,088 | 632 | 2,108 | 4,217 |
| 750 | 18 | 15,631 | 468 | 1,563 | 3,126 |
| 1,000 | 11 | 11,582 | 347 | 1,158 | 2,316 |

Even at 20%/500 it burns 4,217/week against a 93,000/week faucet. Demurrage
is a hoarding disincentive, not a supply control — it should not be asked
to carry this.

---

## 4. Risks that need a decision

### 🔴 R1 — Max bet 1,000 with the daily wager cap off

Main guild has `casino_max_bet=1000` (spec default 100) and
`casino_daily_wager_cap=0` (spec default 500, i.e. **uncapped**). The other
guild running the casino kept 100/500.

Slots pays a flat **120× on triple 7️⃣**, and payouts are minted, not paid
from a house balance:

| triple | multiplier | p per spin | payout at 1,000 stake | as % of float |
|---|---:|---:|---:|---:|
| 7️⃣ | 120× | 1/17,576 | **120,000** | **243%** |
| 🍯 | 40× | 1/2,197 | 40,000 | 81% |
| 🦋 | 18× | 1/658 | 18,000 | 36% |
| 🌾 | 12× | 1/274 | 12,000 | 24% |

At yesterday's 1,206 spins/day, a butterfly-or-better triple is expected
**~2.4× per day**. The only thing preventing a float-breaking mint is that
members are mostly betting ≤100 — but two 1,000-coin stakes have already
been placed, and the top player wagered 9,580 in a day with no cap to stop
them.

**Fix (dashboard, no code):** restore `daily_wager_cap` to 500 and/or drop
`max_bet` to 100–200. This is the single highest-value change in this
document and takes ten seconds.

### 🔴 R2 — The progressive jackpot is an uncapped lottery

`jackpot_cut_pct=25` takes a quarter of **every fully-lost stake in every
game** and feeds one pot that can only be won by **slots triple-7️⃣**
(p = 1/17,576 per spin). Current pot: **5,311 after three days, growing
~2,400/day.**

Three problems:

1. **Uncapped.** Expected wait at current spin volume is ~14 days →
   a ~35–40k payout to one member, ~70–80% of the entire float, possibly
   on a 5-coin bet.
2. **Cross-subsidy.** Roulette, derby, blackjack, baccarat, dice, war and
   keno losers all fund a prize only slots players can win.
3. **`max(pot, 120×stake)`** — the flat multiplier was designed as "a floor
   under an early, barely-fed pot" (`casino_service.py:672`), but at
   max_bet 1,000 the floor is 120,000 and the pot becomes irrelevant.
   R1 and R2 compound.

**Options:** cap the pot (e.g. 2,500); pay 60–70% and reseed with the rest;
let more games claim it; or raise the hit rate and lower the size. My
preference is a pot cap plus a partial payout — it keeps the "watch it
grow" hook without the single-event blowup.

### 🟡 R3 — The tuning report is now blind

`scripts/economy_tuning_report.py` counts `casino_payout` as a faucet and
`casino_stake` as a burn (its `NON_FAUCET_KINDS` / `BURN_KINDS_EXCLUDED`
lists predate the casino). Run today it would show a ~45% burn ratio when
the real figure is 7%. It also only reports the *last full week*, which is
7/13–7/19 — everything in this document happened after that window, so the
script currently reports zero delta against its own baseline.

Needs: casino kinds netted to `casino_hold = stake − payout`, the jackpot
escrow reported separately, and a `--since`/`--days` window.

### 🟡 R4 — Nothing to buy

18 spenders vs 100 earners. The raffle is off, bounties and auctions are
unused, pin-of-the-day is free, the wager rake is 0%, and the community pot
has never paid out. Sinks that need a buyer only work if the catalogue
outruns income — right now a p50 member earns 161/week and the most
expensive thing in the shop is a 500-coin role icon.

---

## 5. Recommended order

1. **Now, dashboard only:** `casino_daily_wager_cap` → 500,
   `casino_max_bet` → 100–200. (R1)
2. **Now, dashboard only:** `econ_xp_per_coin` → 2–4. (2a) Announce it —
   members watched their conversion 20× overnight.
3. **This week, code:** cap the jackpot and/or make it pay partial. (R2)
4. **This week, dashboard:** turn on the raffle, price pin-of-the-day
   above 0, set a PvP wager rake of 3–5%. (R4)
5. **Before Monday's first sweep:** decide whether 3%/500 is the demurrage
   you want, knowing it collects 632. (3d)
6. **Then:** fix the tuning report so the next review is one command. (R3)
7. **Backlog:** give `cat_catch` a Faucets-page dial. (2b)

Nothing here is a correctness bug — the casino's money handling, exactly-once
settlement and jackpot accounting all check out against the ledger. Every
item is a tuning or design-shape decision.
