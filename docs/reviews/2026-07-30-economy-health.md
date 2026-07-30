# Economy health — two days after the 2026-07-28 retune

Read-only review of the live ledger (`dungeonkeeper.db`, main guild
`1469491362444480666`) plus the money-moving code paths, 2026-07-30.
Baseline saved as [`economy-baseline-2026-07-30.json`](
economy-baseline-2026-07-30.json) (3-day window, so re-runs diff like-for-like).

Follows [`2026-07-25-economy-casino-sources-sinks.md`](
2026-07-25-economy-casino-sources-sinks.md) and the four retune commits it
prompted: casino RTP trim (`3488750d`), jackpot skim 25%→5% (`bede17e9`),
XP-mint daily ceiling (`5a3e2945`), report netting (`05335ddb`).

Bottom line: **the retune worked on the casino and did nothing for the float.**
Supply is growing ~+5,200/day, and neither of the two things that looked wrong
actually is. The real finding is that **one dial (`econ_host_bounty_cap = 1`)
inverts a faucet's whole design**, and that the perk shop — a live, growing
subscription sink worth 4,700 coins/week — is the thing inflation genuinely
threatens.

---

## 1. Supply growth is real, but linear

From the report's own day series (`level` = float at close):

| day | net | level | | day | net | level |
|---|---|---|---|---|---|---|
| 07-23 | +4,093 | 41,653 | | 07-27 | +5,221 | 57,153 |
| 07-24 | −1,908 | 39,745 | | 07-28 | +5,443 | 62,596 |
| 07-25 | +8,441 | 48,186 | | 07-29 | +11,591 | 74,187 |
| 07-26 | +3,746 | 51,932 | | 07-30 | +349 | 74,536 (partial) |

Trailing median: **+5,221/day**. Float is up 19% in two days and 4.8× in
thirteen.

**It is not exponential.** Every faucet here is flat-rate per member per day —
login, quest, drops, cat_catch, game rewards. None scales with the float, and
wallets are saturated at 147 of ~149 active members. So this compounds only
through headcount, which has stopped growing. The honest projection is ~+5,200/day
*linear*: ~111k in a week (+49%), not a doubling, with the doubling time
stretching as the float grows.

Onboarding explains a minority: +31 wallets since the 07-26 baseline (25 of them
on 07-27), and median balance *fell* 249 → 186 on that dilution. The growth is
existing members accumulating.

### Genuine mint, the two complete post-retune days

Escrow returns (`auction_refund`, `rental_refund`) and `casino_payout` excluded —
those are recycled, not new coins. `bounty_payout` excluded on 07-29 (funded by
`bounty_stake`, so a transfer).

| faucet | /day | users | dial |
|---|---|---|---|
| quest | 1,670 | 37 | quest board |
| game_participation | 1,491 | 45 | `reward_game_participation` 5 |
| **drop** | **1,490** | **~13** | `drops_per_day` 16, `drops_max_coins` 200 |
| cat_catch | 1,403 | 17 | **hardcoded** (`parser.py:454`) |
| game_win | 1,201 | 8 | `reward_game_win` 50 |
| login | 1,016 | 67 | 5 text / 15 voice |
| game_host | 375 | 2–3 | `host_bounty_per_joiner` 100, `cap` 1 |
| quest_bonus | 275 | ~22 | set bonuses 10 / 25 |
| **total** | **~9,200** | | |

Against real burn of ~2,335/day: rental ~1,100, casino hold ~950, demurrage
~170 (weekly, amortised), shields/rerolls ~15.

Two flow-accounting notes:

- **Auctions are not a sink.** 6,830 bid vs 6,320 refunded = ~510 burned ever.
  The report books bids as sink and refunds as faucet, so both its `faucet_mix`
  and `sink_mix` are inflated by ~6k on a 7-day window. Read the netted
  `burned_week`, not the mix lines.
- **`conversion` is already dead.** It is the largest line in the 7-day mix at
  15,500, but `econ_xp_per_coin = 0`: it minted 7,959 and 7,541 on 07-25/26,
  then stopped on 07-27. The 250 ceiling from `5a3e2945` is moot while the rate
  is 0. Any 7-day flow number is badly overstated by this one dead faucet.

## 2. Casino RTP: on target. The 87.9% was a windowing artifact

House hold as % of handle, per guild-local day, Pools excluded:

| day | handle | hold | % |
|---|---|---|---|
| 07-24 | 18,332 | 4,154 | 22.7% |
| 07-25 | 18,188 | 3,749 | 20.6% |
| 07-26 | 55,022 | 10,628 | 19.3% |
| 07-27 | 16,438 | 4,629 | **28.2%** ← last pre-retune day |
| 07-28 | 21,855 | 3,017 | 13.8% |
| 07-29 | 19,810 | −875 | **−4.4%** (players won) |
| **07-28..30** | **41,665** | **2,142** | **5.1% → 94.9% RTP** |

Exactly the ~95% the new tables were tuned to. A rolling 72h window reaches back
into 07-27 and blends a 28% pre-retune day into a two-day post-retune sample,
which is where 12.08% came from.

Caveat: two days and 41.7k of handle, swinging 13.8% → −4.4%. Wide error bars —
but not evidence of a 12% edge.

**The pre-retune hold was mostly escrow, not burn.** At a 25% skim, `feed_jackpot`
banked a quarter of every lost stake in the pot, and the report counts that in
`casino_hold` because `feed_jackpot` writes no ledger row. `bede17e9` measured
the real burn at 7.2% of handle against a 21.6% apparent hold.

**Blackjack's share is the live risk.** `3488750d` sized its trim against a mix
of slots 57% / blackjack 22% / coinflip 11%. Post-retune, blackjack is **54% of
handle** (22,376 of 41,665). It is the lowest-edge table on the floor at 3:2 and
it now carries the volume, which dilutes the blended edge no matter what slots
and coinflip pay. The trim aimed at a mix that has since moved.

**Jackpot overhang:** the pot stands at **8,507 — 11.4% of the entire float** —
and re-mints in one lump on a triple 7. At 5% it now grows ~100–150/day instead
of ~1,700, so the overhang is capped, but expect it to land in one wallet.

## 3. Demurrage: not broken. Weekly, and under-rated

`econ_demurrage_sweeps` holds exactly one row: `2026-W30`, 31 members, 755 coins,
at 1785135600 = 2026-07-27 00:00 PDT — the week roll. Settings are rate 3%,
threshold 500. It is scheduled, it fired, it worked. Next fire is the **08-03**
roll, and today's excess over 500 (39,569) means it will take ~1,187.

That is ~170/day against +5,200/day. Not disabled, not thresholded out of reach,
not failing — just weekly at a rate that rounds to nothing.

## 4. `econ_host_bounty_cap = 1` inverts the faucet

`logic.host_bounty_amount` is `per_joiner × min(joiners, cap)`. At `cap = 1` that
collapses to a flat `per_joiner` for any game with at least one joiner. The
docstring is explicit that this is the opposite of the intent — "the whole point
is recruiting hosts two through five, not rewarding the act of typing the
command" — and the shipped default is `cap = 5`. Prod is 1.

The 07-29 backfill rows make it vivid. Nine games, `joiners` of 15, 8, 8, 12, 7,
10, 16, 14, 16 — **every one paid 100 coins.** A 16-person event and a 1-person
event are worth the same.

Those 9 rows are tagged `{"backfill": "traditional"}` and are the one-time replay
from the external-host payout fix, not recurring flow: 900 of 07-29's 1,350.
Organic host pay is ~3 events/day at a flat 150 (boosted), so **~375/day**, not
the ~825/day a naive read of 07-29 gives.

## 5. The perk shop is a working subscription sink

`rental` **is** the shop. Cosmetic perks are weekly subscriptions, not one-off
buys, so the charge lands as kind `rental` with the item in `meta.perk` — there
is no `role_color` ledger kind and there was never going to be one. Reading the
absence of one as absence of sales is wrong.

| perk | price/wk | active | cancelled | churn |
|---|---|---|---|---|
| role_icon | 500 | **5** | **0** | **0%** |
| role_holographic | 300 | 3 | 2 | 40% |
| role_gradient | 150 | 4 | **9** | **69%** |
| role_color | 50 | 8 | 5 | 38% |
| role_name | 35 | 8 | 5 | 38% |
| voice_style | 30 | 4 | 2 | 33% |

**32 active subscriptions across 22 users, 4,700 coins/week committed = 671/day**,
plus signups. It is the fastest-growing sink in the economy: 255/day on 07-20 →
1,240/day on 07-28/29.

This reframes the inflation question. There *is* a live price level — seven fixed
nominal weekly prices — and median balance is 186 with p90 at 1,304. If the float
doubles, role_icon at 500/wk goes from a genuine commitment to pocket change for
the top decile. That is real devaluation of a real product, not a hypothetical.

The churn column is also a pricing signal independent of inflation, and it says
prices are wrong in **both** directions: role_icon has perfect retention at the
top price (underpriced), while role_gradient is being rejected by the market at
150 (69% churn — raising it would just kill it).

Mechanics that matter for any reprice:

- Renewals **re-read the current config price** (`economy_rentals_service.py:131`
  — "an admin price edit lands at the next anniversary"), so a change hits
  existing subscribers, not just new ones.
- A renewal `charge` is **silent — no DM** (`economy_loop.py:1397`). A price rise
  arrives as an unexplained larger debit unless it is announced.
- 4 of the 5 role_icon subs bill the flat `price_role_icon`; one is catalog-tied
  to "Golden Crown" at 400 in `econ_icon_catalog`, a **separate table** a
  `price_role_icon` edit does not touch.
- `econ_rentals.perk` has a CHECK constraint allowing only the `role_*` /
  `voice_style` / `emoji` perks — **rooms are not in this table**, so
  `econ_price_voice_room` / `_text_room` bill through an unverified path with an
  unknown subscriber base. Left alone here.

## 6. What was already off, and what leaks

| setting | value | note |
|---|---|---|
| `econ_xp_per_coin` | 0 | conversion faucet off since 07-27 |
| `econ_raffle_enabled` | 0 | **pure burn** unused — prize is a `free_week` voucher, never coins (`economy_raffle_service.py:1`) |
| `econ_wager_rake_pct` | 0 | 2 wagers ever; ~0 either way |
| `econ_bounty_rake_pct` | no row (0) | bounty #1 staked 360, paid 360 — rake evaporated |
| `casino_pools_enabled` | **1** | already live, 5% takeout, but only 505 of handle → ~25 coins burned |
| `casino_jackpot_cut_pct` | 5 | the `bede17e9` cut *was* applied to this guild |
| `casino_daily_wager_cap` | 0 | unlimited; max bet 1,000 |

## 7. Recommended dials

Target chosen: flatten toward zero, **keep the drop count** (cut value, not
cadence) and **reward hosting well**.

### Faucets

| dial | from | to | Δ/day |
|---|---|---|---|
| `econ_drops_max_coins` | 200 | **50** | **−1,105** |
| `econ_drops_per_day` | 16 | *16* | unchanged, deliberately |
| `econ_reward_game_win` | 50 | **25** | −600 |
| `econ_reward_game_participation` | 5 | **3** | −596 |
| `econ_quest_set_bonus_daily` / `_weekly` | 10 / 25 | **5 / 15** | −125 |
| `econ_login_voice_base` | 15 | **8** | −350 |
| `econ_host_bounty_per_joiner` | 100 | **30** | **+341** |
| `econ_host_bounty_cap` | 1 | **8** | (a raise) |

Drops keep all 16/day and the same min of 5 — identical cadence, average value
106 → 27.5. Best ratio available anywhere in the economy: 74% less mint, zero
change to how often the feature fires.

The host change is a **2.4× raise for a well-attended event** (a 12-joiner game
goes 150 → 360 boosted) while a 1-joiner game drops to 45. It converts a
frequency reward into an attendance reward for +341/day. It interacts with the
participation cut — hosts now need joiners — so if events thin out, put
`reward_game_participation` back to 4.

`cat_catch` at 1,403/day is deliberately **not** in this table: `_TIER_COINS` is
hardcoded (`parser.py:454`, already tapered once from 3/8/20/50/120/300) and the
money is diffuse — 314 catches over two days averaging 8.9 coins, no tier
dominating. Halving it is worth ~−700/day but needs a commit, not a knob.

### Sinks

| dial | from | to | Δ/day |
|---|---|---|---|
| `econ_raffle_enabled` | 0 | **1** | **+640** (range 400–1,400) |
| `econ_price_raffle_ticket` | 10 | **25** | |
| `econ_demurrage_rate_pct` | 3 | **8** | +337 |
| `econ_demurrage_threshold` | 500 | **400** | |
| `econ_bounty_rake_pct` | 0 | **10** | +36 |
| `econ_wager_rake_pct` | 0 | **10** | ~0 |

The raffle is the highest-value unused thing here and costs nothing to try: a
100% burn whose prize is a rental voucher, and the only sink on the list members
might actively *want*. Demurrage at 8%/400 is the only sink besides the casino
that scales with the float, so it self-brakes as supply rises.

### Perk reprice (~+200/day, and it protects the price level)

| dial | from | to |
|---|---|---|
| `econ_price_role_icon` | 500 | **750** |
| `econ_price_role_holographic` | 300 | **350** |
| `econ_price_role_color` | 50 | **65** |
| `econ_price_role_name` | 35 | **45** |
| `econ_price_voice_style` | 30 | **40** |
| `econ_price_role_gradient` | 150 | *150* — leave; 69% churn already |

Committed revenue 4,700 → ~6,090/wk. The point is less the +200/day than holding
the real price level against a float that is growing 7%/day.

**Sequencing:** renewals are silent and re-read the live price, so this needs an
announcement *before* it lands. Nearest renewals from 2026-07-30 00:50 PDT are
two `voice_style` at ~10h and a `role_icon` at ~17h.

### Where this lands

- faucets: **−2,435/day**
- sinks: **+1,013/day**
- reprice: **+200/day**
- **net: +5,221/day → ~+1,570/day**

Not zero. Closing the rest means `cat_catch` (a commit, −700/day) and then the
**quest board** at 1,670/day to 37 members — the flagship engagement feature.
Recommendation is to stop at ~+1,570/day and revisit on 08-02, because the perk
shop has 5×'d in nine days and may close the gap on its own, and because this
package already visibly nerfs drops, game wins, participation and voice logins
for ~50 people in the same week as the last retune.

## 8. Check on 08-02

```
python3 scripts/economy_tuning_report.py \
  --db /home/ben/discord-bots/dungeon-keeper/dungeonkeeper.db \
  --days 3 --baseline docs/reviews/economy-baseline-2026-07-30.json
```

| signal | 07-30 | if it worked |
|---|---|---|
| trailing median net | +5,221/day | **+1,300 to +1,900** |
| float | 74,536 | ~78–80k (vs ~90k untouched) |
| `drop` in faucet mix | 1,490/day | **~400/day**, claim count still ~14/day |
| `game_host` | 375/day | ~700/day, payouts **varying** by `joiners` |
| `raffle_ticket` in sink mix | absent | **400–900/day**, first appearance |
| casino hold % of handle | 5.1% | still 5–9% on a bigger sample |
| committed weekly rental revenue | 4,700 | **>7,000** = reprice held; <5,500 = churn, walk role_icon back to 600 |
| **median balance** | **186** | **flat or up (190–220)** |

The last row is the real test: the goal is a flatter float *without* squeezing the
ordinary member. If median balance falls, the cuts landed on the median player
instead of the accumulators and the wrong faucets were chosen.

Outside the window: demurrage fires at the **08-03** week roll (755 → ~3,500),
so check it on the 4th, not the 2nd.

## 9. Open questions

1. **Blackjack at 54% of handle.** Do nothing (it is the most-played game and 3:2
   was a deliberate call in `3488750d`), or lower its max bet to rebalance the mix
   toward the higher-edge tables?
2. **`econ_xp_per_coin = 0`** — is conversion off permanently, or parked until the
   250 ceiling can be trusted? It is the one faucet with a working cap and no
   traffic.
3. **Pools has a 5% takeout and no volume.** It is enabled with a channel set;
   worth promoting, or was it left on by accident?
4. **`cat_catch` tiers are hardcoded.** Worth a dashboard panel, given it is the
   4th-largest faucet at 1,403/day?
5. **The 07-26 setup-pin swamp** (see `2026-07-22-deep-review.md` follow-ups and
   the quest-board notes) — quest is 1,670/day and the largest remaining faucet,
   but boards may still not be rolling normally, which would change what a quest
   cut actually does.
