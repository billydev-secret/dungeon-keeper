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
| 1 | `econ_quest_board_daily` | 5 | **3** | 4,432/wk | −300 ⚠ |
| 2 | `econ_quest_board_weekly` | 3 | **2** | 2,366/wk | −150 ⚠ |
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
| | **Faucet subtotal** | | | | **−9,830** |
| 9 | `econ_demurrage_rate_pct` | 8 | **12** | } 7,542/wk sink | } +4,100 sink |
| 9 | `econ_demurrage_threshold` | 400 | **350** | } | } |
| 10 | `casino_jackpot_cut_pct` | 5 | **3** ✔ | 1,264/wk escrowed | +580 sink |
| 11 | `econ_wager_rake_pct` | 10 | **15** | } | } +300 sink |
| 11 | `econ_bounty_rake_pct` | 10 | **15** | } | } |
| | **Sink subtotal** | | | | **+4,980** |
| | **TOTAL** | | | | **≈ 14,810/wk** |

> ⚠ **Corrected 2026-08-28 (same day).** Lines 1–2 originally read −1,773 and
> −789, pro-rated from board slots. That was wrong: members are **not
> board-limited**. Of 909 member-days with any daily-quest claim, **67% are a
> single clear** and only **3% clear all five** — so shrinking the board removes
> options almost nobody was using. This is the same dial-arithmetic error round
> 2's postmortem caught on `reward_game_win`, made again. The board dials stay in
> the package (they cap the ceiling and cost nothing) but are now credited at
> roughly a sixth of the original estimate. **Per-quest reward is the real lever
> on this lane — see §8.**

**Expected landing: +24,058/wk → ≈ +9,248/wk**, i.e. float growth from **+12.3%
to ≈ +4.7% per week**. That is 62% of the gap, deliberately not 100% — see below.

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
4. ~~No `casino_play` trigger kind exists~~ — **built 2026-08-28.** `take_stake`
   now fires `casino_play` on a charged bet, keyed to the stake's ledger row.
   Quest 89 "Take a Seat" works; see §11 for how to price it.
5. **No `auction_bid` trigger kind exists.** Quest 131 "At the Block" is still
   unclearable. Not built — auctions see far less traffic than the casino and
   the quest drives no sink, so **deactivate quest 131** unless you want the
   trigger too (same shape of change, ~20 lines).

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

---

## 8. Stage 0 — trim the quests everyone clears (Billy, 2026-08-28)

> "Let's make some of the quests smaller, the ones everyone gets done and see
> what happens."

Read as **reward size, not task size**. Applied via the dashboard
(**Economy → Quests**), which edits `reward` directly — no SQL, no restart.

### Why this is the right lever on the quest lane

Members are not board-limited, so the *board size* dial barely bites (§4 ⚠).
What they do instead is clear the **one or two cheapest quests and stop**:

| Daily-quest clears in one member-day | Member-days | Share |
|---:|---:|---:|
| 1 | 609 | 67% |
| 2 | 206 | 23% |
| 3 | 64 | 7% |
| 4 | 19 | 2% |
| 5 (full board) | 11 | **1%** |

`econ_board_overrides` confirms the behaviour from the other side: it is the
reroll log, and members reroll *out of* "Set Your Bio" / "Guess the Whisperer" /
"Post a Voice Message" and almost always *into* quest 1 (Send Messages) or 2
(Reply to Messages). The easy quests are actively sought.

So cutting the reward on the easy quests hits the coins directly, with little
room to substitute — most members were never clearing a second quest anyway.

### The "everyone clears it" set

Ranked by distinct members clearing it over 28 days (~165 earners, ~82 daily
active). These five dailies are **60% of all daily-quest coins**:

| id | Quest | Reward | Clears | Members | Clears/board-day | Coins 28d |
|---:|---|---:|---:|---:|---:|---:|
| 3 | Give Reactions | 12 | 213 | 83 | 7.6 | 2,556 |
| 1 | Send Messages | 10 | 209 | 83 | 7.2 | 2,090 |
| 2 | Reply to Messages | 12 | 188 | 82 | 6.5 | 2,256 |
| 16 | Post an Image | 15 | 179 | 84 | 6.4 | 2,685 |
| 64 | React to Different Members | 12 | 95 | 50 | 3.7 | 1,140 |

Their weekly counterparts — the same trivial actions on a week's cadence — are
**48% of all weekly-quest coins**.

### Proposed rewards

| id | Quest | Now | **New** | Clears 28d | Δ coins/wk |
|---:|---|---:|---:|---:|---:|
| 1 | Send Messages (daily) | 10 | **5** | 209 | −261 |
| 2 | Reply to Messages (daily) | 12 | **6** | 188 | −282 |
| 3 | Give Reactions (daily) | 12 | **6** | 213 | −320 |
| 16 | Post an Image (daily) | 15 | **8** | 179 | −313 |
| 64 | React to Different Members (daily) | 12 | **7** | 95 | −119 |
| 6 | Send Messages (weekly) | 40 | **25** | 21 | −79 |
| 7 | Reply to Messages (weekly) | 40 | **25** | 19 | −71 |
| 8 | Give Reactions (weekly) | 30 | **20** | 24 | −60 |
| 66 | Reply to Different Members (weekly) | 45 | **30** | 16 | −60 |
| 67 | Get Replies from Different Members (weekly) | 40 | **25** | 20 | −75 |
| 68 | Be Active on Different Days (weekly) | 50 | **35** | 14 | −53 |
| | **Total** | | | | **−1,693/wk** |

**Expected effect: +24,058 → +22,365/wk, i.e. +12.3% → +11.5% weekly float
growth.** Set expectations accordingly — this is **7% of the gap**, not a fix.
Its value is as an experiment, not a retune.

### Two things to know before applying

1. **The panel will show an advisory on the five dailies.** `REWARD_BANDS` in
   `economy-quests.js` is a hardcoded `daily: [10, 20]`, so anything under 10
   reads "Outside the suggested daily band (10–20). Saves fine." It saves. The
   band is a fixed constant that predates the current economy — at a median
   weekly income of **80**, a daily band of 10–20 means one trivial clear is
   worth 12–25% of a member's whole week. The band is stale, not the new values.
2. **Weeklies stay inside their band** (25–75), so no advisory there.

### What "see what happens" should look for

The point of doing this first, alone, is that it answers a question the dial
arithmetic cannot: **are these quests cleared for the coins, or for the habit?**

- **Clears hold steady, coins fall ~1,700/wk** → habit-driven. Reward cuts work
  on this lane and can go further (the other 22 dailies average 16 and are
  untouched).
- **Clears fall with the reward** → coin-driven. Stop cutting rewards here; the
  lane is doing engagement work and the float has to be fixed elsewhere.
- **Clears shift to other quests, coins flat** → substitution after all. Then the
  board dials matter more than this section assumes, and §4 ⚠ needs revisiting.

Checkpoint at **10 days**, watching `clears_per_user` per week (currently 3.3–4.5)
alongside the coin total — the ratio is what separates the three cases, and the
raw coin figure alone cannot.

---

## 9. Stage 0b — rebalance the board toward what isn't done (Billy, 2026-08-28)

> "I think I want to push users into ones that aren't done, we should increase
> the payout" / "Remove the common ones or make them pay lite"

This supersedes §8's flat trim. Same lane, better shape: **hold the quest budget
roughly flat and move it from the trivial quests to the effortful ones**, so the
board teaches what the server values instead of paying for noise.

### First: two quests are impossible, not unpopular

| id | Quest | Reward | Clears 28d | `trigger_kind` |
|---:|---|---:|---:|---|
| 89 | Take a Seat | 12 | **0** | `casino_play` |
| 131 | At the Block | 12 | **0** | `auction_bid` |

Neither kind exists. The vocabulary is 54 kinds (`KIND_LABELS`,
`economy-sources-shared.js`); prod has fired 56 distinct kinds all-time; **neither
`casino_play` nor `auction_bid` appears in either list**, and neither string
occurs anywhere in `src/`. These two quests can never be cleared, and they sit
in the 27-quest daily pool consuming board draws — a member who is dealt one
gets a slot they cannot complete.

**Deactivate both.** Do not raise their payout: they are not under-done, they are
unreachable. (Worth building rather than deleting — see §10.)

### Second: three "under-done" quests are one-time actions

| id | Quest | Reward | Clears 28d |
|---:|---|---:|---:|
| 26 | Set Your Bio | 50 | 17 |
| 39 | Set Your Birthday | 25 | 4 |
| 56 | Pick Your Roles | 25 | 4 |

You can only do each of these once, ever. Their clear count is low because the
eligible pool shrinks every day, not because the reward is too small — raising it
buys nothing but a bigger payment to the dwindling set who haven't done it yet.
`Set Your Bio` at 50 is already the richest daily on the board. **Leave all three.**
They are onboarding quests wearing a daily cadence, which is the real oddity.

### Do not remove all the common ones — they are the on-ramp

Measured before deciding:

- **74%** of single-clear member-days (450 of 609) are one of the five common quests.
- **29 of 121 daily questers (24%)** have cleared *nothing but* common quests in 28 days.

Stripping all five would leave a quarter of your questers with no quest they
currently touch. So: **remove the two that are redundant with each other, keep one
cheap on-ramp, pay-lite the rest.**

| id | Quest | Now | **Action** | Clears 28d |
|---:|---|---:|---|---:|
| 1 | Send Messages | 10 | **deactivate** (redundant with 2) | 213 |
| 2 | Reply to Messages | 12 | **deactivate** (redundant with 1) | 188 |
| 3 | Give Reactions | 12 | **5** — kept as the on-ramp | 215 |
| 16 | Post an Image | 15 | **8** | 180 |
| 64 | React to Different Members | 12 | **7** | 98 |

Frees **7,641/28d = 1,910/wk** to spend on the raises.

### The raises, anchored to effort

| id | Quest | Now | **New** | Clears 28d |
|---:|---|---:|---:|---:|
| 78 | Catch a Cat | 10 | **12** | 28 |
| 65 | Post in Different Channels | 12 | **15** | 52 |
| 48 | Make a Guess | 12 | **18** | 38 |
| 51 | Answer a Greeting | 12 | **18** | 39 |
| 49 | Send a Whisper | 12 | **20** | 38 |
| 29 | Queue a Song | 12 | **20** | 1 |
| 5 | Answer the QOTD | 15 | **22** | 57 |
| 31 | Answer a Chat Revive | 12 | **22** | 35 |
| 35 | Guess the Whisperer | 15 | **22** | 15 |
| 50 | Guess the Whisperer | 15 | **22** | 19 |
| 47 | Submit a Guess Who Round | 15 | **25** | 18 |
| 36 | Win a Guess Who Round | 15 | **25** | 7 |
| 30 | Post a Voice Message | 15 | **25** | 16 |
| 4 | Join Voice Chat | 15 | **28** | 37 |
| 34 | Join a Game Session | 15 | **28** | 15 |
| 57 | Make a Shop Purchase | 25 | **35** | 14 |
| 33 | Host a Voice Room | 18 | **40** | 2 |

Average reward on the raised set goes **13.6 → 21.1**. The spread is the point:
a cleared reaction is worth 5, hosting a voice room is worth 40, and the board
now says so.

**Quest 57 (Make a Shop Purchase) is the standout.** It is the only quest that
drives a *sink* — it pays 35 but induces a purchase of 40–1,200. Every extra
clear is net-negative on the float. If any single raise deserves to go further,
it is this one.

### The honest arithmetic: steering costs money

| Uptake on the raised set | Clears/28d | Net effect |
|---|---:|---:|
| 1.00× (no behaviour change) | 431 | **−1,100/wk** |
| **1.48× — break-even** | 638 | **0** |
| 2.00× | 862 | **+1,177/wk** |
| 2.50× | 1,078 | **+2,315/wk** |

**If this works as intended, it adds mint.** Below a 48% lift in clears on the
raised set it is deflationary; above it, inflationary. There is no version of
"raise payouts to drive uptake" that also flattens the float — I checked: holding
the lane neutral at 2× uptake requires an average reward of 15.7 against today's
13.6, i.e. essentially no raise at all.

So this is an **engagement-steering change, not a flattening one.** It belongs
*alongside* §4's package, not instead of it. The §4 levers (demurrage, login,
drops, cat-catch, cah_win_max) are untouched by this and still carry the
flattening; treat §9 as budget-neutral and judge it on behaviour, not coins.

### What to watch

Per-quest clears on the 17 raised quests, at 10 days:

- **Voice and game quests (4, 30, 33, 34) move** → the steer works; this is the
  result worth having, and it is worth the mint.
- **Nothing moves** → these quests are gated by opportunity, not reward (you
  cannot "host a voice room" alone), and the coins were never the obstacle.
  Revert the raises rather than doubling them.
- **Total daily-quest clears fall** → deactivating 1 and 2 cut deeper than the
  on-ramp could absorb. Reactivate quest 1 at 5 and re-measure.

Watch the **29 common-only questers** specifically: if their clear count goes to
zero, the on-ramp broke and quest 3 at 5 is too thin.


---

## 11. Pricing quest 89 "Take a Seat" (now that it works)

The trigger shipped 2026-08-28. Before activating the quest, know what it costs:
**at its current settings it is a faucet, not a sink.**

Prod today: `casino_min_bet` **5**, average stake **~36**, casino hold **6.7%**.
Quest 89 is `target_count` **3**, reward **12**.

| Member plays | Handle | Hold to the house | Quest pays | **Net float** |
|---|---:|---:|---:|---:|
| 3 × min bet (5) | 15 | 1 | 12 | **+11** |
| 3 × average (36) | 108 | 7 | 12 | **+5** |
| 3 × 60 | 180 | 12 | 12 | **0** |
| 10 × average | 360 | 24 | 12 | **−12** |

**Break-even is ~180 coins of handle** — five average bets, or three of 60. A
member who clears it with three minimum bets is paid 12 for putting ~1 coin at
real risk, and at 82 daily-active members that farms to as much as 7,000/wk if
everyone takes it.

The quest only pays for itself if it pulls people into *real* sessions rather
than three token spins. Three ways to make that likely, in order of preference:

1. **Raise `target_count` to 5** and leave the reward at 12 — the cheapest fix,
   and it moves break-even to within reach of ordinary play.
2. **Cut the reward to 8.** Break-even falls to ~120 handle.
3. **Raise `casino_min_bet`** above 5 — a real fix for farming generally, but it
   is a casino-wide change that also hits members who like small stakes, so it
   should be judged on its own merits, not as a quest patch.

There is no stake-size floor in the quest system — a trigger cannot say "a bet
of at least N" — so this has to be handled with `target_count` and reward, or
not at all. **Recommendation: `target_count` 5, reward 12, and watch the casino
handle rather than the clear count** — handle going up is the outcome worth
having, because the hold is what closes the float gap.


---

## 12. Sizing the jackpot cut (Billy, 2026-08-28)

> "Can we put a smaller share to the jackpot too?"

Yes — it is a dashboard dial: **Casino → "Share of Each Losing Bet"**
(`jackpot_cut_pct`, 0–100). No code needed. But the mechanic is not what the
percentage suggests, so the numbers are worth having first.

### The cut has a floor, and the floor does the work

`feed_jackpot` skims `lost_amount * pct // 100` from a **fully-lost stake only** —
not from every bet, and not from the house hold. That is **integer floor
division**, so any lost stake below `100/pct` contributes exactly nothing:

| Cut | A lost stake must be ≥ | Share of real stakes that qualify | To the pot | **Stays burned** |
|---:|---:|---:|---:|---:|
| **5%** (now) | 20 | 49% | 1,264/wk | — |
| 4% | 25 | 42% | 1,028/wk | **+236/wk** |
| 3% | 34 | 34% | 684/wk | **+580/wk** |
| **2%** | 50 | 33% | 488/wk | **+776/wk** |
| 1% | 100 | 22% | 188/wk | **+1,076/wk** |

Modelled over the real 28-day stake distribution (9,205 stakes, 466,814 handle,
average 50.7), anchored to the observed pot growth since the 08-22 win
(seed 100 → 1,183 in six days ≈ 1,264/wk at the current 5%).

Two things fall out that the percentage alone hides:

1. **Half the casino already feeds the jackpot nothing.** At 5%, 51% of stakes
   are under the 20-coin floor. Dropping to 2% raises the floor to 50 and only
   a third of bets can contribute — the pot becomes a big-bettors' pool, funded
   by a shrinking minority and won by anyone. That may be fine, or even fair;
   it should just be a choice rather than a surprise.
2. **Cutting the share is a real sink, not a deferral.** Over a full cycle the
   jackpot is net-neutral — the coins were burned at the stake and re-minted at
   the win — so every coin *not* skimmed is a coin that stays permanently burned.
   The +776/wk at 2% is genuine.

### The cost

The pot grows **2.6× slower** at 2%. Today's cadence produced an 18,228 win; at
2% the same interval yields roughly 7,000. That is the trade: a steadier sink for
a smaller headline moment, and the 18k hit was memorable enough that members
noticed it.

**Recommendation: 3%.** It banks +580/wk — three quarters of what 2% gets — while
the pot still grows fast enough to stay an event, and the floor rises only to 34
rather than 50.

> **Decided 2026-08-28: 3%.** Billy took the recommendation. The §4 package was
> costed at 2% and is now restated at 3% (line 10: +580, not +776), which is
> where its −196/wk difference went. **Still to apply on the dashboard** —
> Casino → "Share of Each Losing Bet" → `3`.

Either way this also removes the measurement problem the jackpot has been
causing: a single lumpy re-mint is what made the casino read as a 0.2% sink in
§2 and what distorts any short window it lands in.
