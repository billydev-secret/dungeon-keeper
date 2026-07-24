# Economy sources & sinks — deep review and engagement plan

**Date:** 2026-07-22
**Status:** Review + proposal. Nothing here is built yet.
**Data window:** 2026-07-13 → 2026-07-23 (the economy's entire life to date), main
guild `1469491362444480666`, read from production `dungeonkeeper.db`.

Supersedes the framing in `economy-sinks-round-2.md` and `-round-3.md`. Those
rounds diagnosed the problem as *not enough sinks*. The data says the sinks are
fine in kind and the problem is **demand**, plus a faucet mix that pays people
for what they were already doing.

---

## 1. What the live economy actually looks like

### 1.1 Headline flow

Main guild `1469491362444480666` only. (A second live guild,
`1476525656115515484`, runs its own younger economy — 108 wallets, 6,894 minted,
130 burned. It is excluded throughout; mixing them inflates every inequality
measure.)

| Metric | Value |
|---|---|
| Coins minted | 43,581 |
| Coins burned (true destruction) | 4,065 |
| Player-to-player transfers (destroy nothing) | 1,799 |
| **Absorption rate** | **9.3%** |
| Wallets | 114 |
| Wallet float | 37,717 |
| Wallet Gini | **0.6162** |

The practitioner floor for a Discord economy that isn't dying is ~20–30%
absorption. We are at 9.3%, and a further 1,799 coins have moved via transfers,
which relocate rather than remove.

A Gini of 0.62 is high but not pathological for a 10-day-old economy — for
reference the Pardus virtual-economy study measured 0.653 overall. This is worth
watching rather than treating as an emergency.

### 1.1b Is it just too early? — cohort test

The economy is 10 days old, so the obvious objection to any "nobody spends"
finding is that members simply haven't discovered the shop yet. That objection is
testable, and it is **partly right at the bottom of the distribution and wrong in
the middle**.

**Time-to-first-spend is 1–9 days, median ~5.** Nobody spends on day one. So a
ramp is real and the economy genuinely cannot be judged on 10 days.

But controlling for tenure *and* affordability at once:

| Cohort | Users | Spent | Rate |
|---|---|---|---|
| 7+ days tenure, peak balance ever ≥ 35 | 74 | 10 | **13.5%** |
| 7+ days tenure, never reached 35 | 10 | 0 | 0% |
| <7 days tenure, peak balance ever ≥ 35 | 14 | 3 | 21.4% |
| <7 days tenure, never reached 35 | 16 | 0 | 0% |

Among members who have had a full week *and* have at some point held enough for
the cheapest perk, **86% still never spent**. And the newer cohort spends at a
*higher* rate than the older one (small n, but it certainly doesn't support "they
just need more time"). Tenure is not the explanatory variable.

Affordability is a real floor at the bottom, though: 26 of 114 wallets never
reached 35 coins at any point, and none of them spent. That is not a demand
problem — those members have nothing to spend, which is a faucet-reach problem.

Spend rate by current balance band:

| Balance | Users | Spenders | Rate |
|---|---|---|---|
| under 35 | 26 | 0 | 0% |
| 35–49 | 6 | 0 | 0% |
| 50–149 | 23 | 1 | 4.3% |
| 150–499 | 37 | 5 | 13.5% |
| 500+ | 22 | 7 | **31.8%** |

**The awareness lever is barely firing.** Only **9 members** have ever claimed the
`shop_purchase` setup quest — the one-time quest explicitly designed to teach the
earn→spend loop. It sits in the daily pool of 20 with a board size of 3, so
reaching it is a lottery draw rather than a guaranteed onboarding beat. This is
the strongest evidence *for* the awareness hypothesis, and the cheapest thing on
this entire document to fix.

**Confidence note.** On 10 days of data the direction of these findings is solid
but the magnitudes are not. Re-run this analysis at 4 and 8 weeks before
committing to the larger Stage 2 builds.

### 1.2 The participation funnel

| Stage | Users | % of economy participants |
|---|---|---|
| Touched the economy at all | 114 | — |
| Did anything beyond a login claim | 97 | 85% |
| Completed a quest | 91 | 80% |
| **Ever spent a coin** | **13** | **11%** |
| Ever rented a perk | 12 | 11% |

The drop-off is not at the top of the funnel. Quest engagement is genuinely
healthy — 80% of participants have completed one. The cliff is entirely at the
spend step.

### 1.3 Earning is concentrated, and concentration tracks breadth

| Distinct faucet kinds used | Users | Coins earned | Avg/user |
|---|---|---|---|
| 1 | 17 | 165 | 9 |
| 2–3 | 23 | 1,468 | 63 |
| 4–6 | 51 | 15,854 | 310 |
| **7+** | **23** | **26,094** | **1,134** |

23 people (20% of participants) earned **60%** of everything minted, and average
126× what a single-faucet member earns. Breadth of feature use, not time spent,
is what separates them — which is the lever §3 Stage 1 pulls on.

### 1.4 The quest board pays for ambient behaviour

Quest claims by trigger kind (top and bottom):

| Kind | Claims | Users | | Kind | Claims |
|---|---|---|---|---|---|
| cat_catch | 195 | 16 | | bio_set | 2 |
| message_sent | 111 | 58 | | bump | 2 |
| reaction_given | 111 | 58 | | voice_room_host | 2 |
| media_post | 105 | 58 | | guess | 3 |
| reply_sent | 99 | 54 | | music_request | 1 |
| voice_session | 32 | 20 | | duel / duel_win / duel_lose | 1 each |
| | | | | pen_pal_complete | 1 |

The five kinds you fire by *chatting normally* account for the overwhelming
majority of completions. Every kind that would push a member into a tool they
don't already use is in low single digits.

This is the core finding. **The quest system is not a discovery engine; it is a
chat-activity rebate.** The pool is well-built — 68 active quests spanning 45+
trigger kinds — but with a daily board of 3 drawn from 20, the odds of drawing a
"go try a new feature" quest are low, and when one is drawn it's usually ignored
in favour of the ambient ones that complete themselves.

Board sizes: daily 3 (pool 20), weekly 2 (pool 25), monthly 1 (pool 14).

### 1.5 Multiplayer: one host, thin attendance

**Corrected 2026-07-23.** An earlier draft of this section claimed multiplayer
was "cold" and recommended consolidating to a single scheduled game. That was
wrong — it generalised from the PvP duel games (Pressure Cooker 3, Quickdraw 3,
Chicken 5 in the window) to the whole surface. The party-game surface is busy.

**30 party games in 10 days across 8 types** (clapback, traditional, ffa, ttl,
rushmore, wyr, ama, photo), running on 10 of 11 days, plus **3 active daily
schedules** already configured (two `risky_roll`, one `photo`). Scheduling is
built and in use; recommending it would have been recommending the status quo.

The real problems are different, and both are concentration:

**Hosting is one person.** Since 2026-07-13:

| Host | Games | Distinct types |
|---|---|---|
| Billy | 23 | 7 |
| eeps | 4 | 1 |
| Chloeee | 3 | 3 |

77% of games in the window and 79 of 122 all-time come from one host, who also
owns all three daily schedules. This is a single point of failure: if that
person is busy for a fortnight, the games programme stops.

**Attendance is thin.** clapback averages 2.9 players (max 8), ttl 2.3 (max 11),
on a server with 125 members who sent 50+ messages in the window. That is a
*visibility* problem — whether people know a game is live — not a supply problem.

The casino is a genuine exception: **2 stakes, 1 user** since launching
2026-07-22, with zero blackjack or roulette hands. Too new to read.

### 1.6 Community weeklies are sized to be impossible

| Quest | Target | Current | % |
|---|---|---|---|
| Server Buzz (message_sent) | 16,635 | 1,919 | 11.5% |
| Talk It Out (reply_sent) | 10,263 | 859 | 8.4% |

At first read this looked like the auto-sizer was producing impossible targets.
It isn't — see the retraction below. Neither goal has tripped a tier
(`notified_tier = 0`) yet, and each pays **10 coins**, which remains too small
for a server-wide event regardless.

**Investigated 2026-07-23 — the original finding above was wrong, and is
retracted.** There is no counter mismatch and no bug. Both numbers come from the
same place: `record_kind_activity` and `_bump_community_kind` are incremented in
the *same* `fire_trigger_quests` call
(`economy_quests_service.py:1389` and `:1450`), separated only by the
income-source gate — and no income source is disabled in this guild.

Measured directly over 24 hours:

| Kind | Progress delta | `econ_kind_activity` delta |
|---|---|---|
| `reply_sent` | 249 | 249 |
| `message_sent` | 415 | 409 (+ tail of the prior day) |

They track 1:1. The sizer is also correct: trailing-28-day `message_sent` was
48,678, and 48,678 / 4 / 0.75 = 16,226 against the 16,635 target.

**The real explanation** is that both community quests were created
**2026-07-21 18:10 — a Tuesday evening — and given a full Monday–Sunday
target.** This week's counter started roughly two days late against a
seven-day goal, which is why it reads as hopeless. On a clean full week the
projection is:

- `message_sent` ≈ 2,450/day × 7 ≈ 17,150 against 16,635 → **~103%, tier 3 clears**
- `reply_sent` ≈ 1,215/day × 7 ≈ 8,500 against 10,263 → **~83%, tier 2 clears**

That is exactly the specced intent ("a typical week lands ~75% and a push clears
it"). The mechanic is working; it just hasn't had a full week yet.

**What is still worth fixing** is much smaller than "the targets are impossible":
a community weekly activated mid-week inherits a full-week target, so its first
partial week is structurally short and reads to members as a goal nobody can
move. Either prorate the target for a partial first week, or hold activation to
the next week roll. The 10-coin reward is a separate, real issue.

### 1.7 What is working

Two things, and they share a shape: cheap, visible, zero-friction.

- **Login streaks.** 16 members sit at an 11-day streak, the maximum possible in
  the window; 44 are at 8+. The streak mechanic has real grip.
- **Coin drops.** 7 posted, **7 claimed — a 100% claim rate.** Drops only started
  2026-07-22. Configured at 8/day, so this is running far below its budget.

### 1.8 Sink and dial status

| Sink | Price | Status |
|---|---|---|
| role_color | 50 | active, 6 renters |
| role_name | 35 | active, 6 renters |
| role_gradient | 150 | active, 5 renters |
| role_icon | 500 | active, 5 renters |
| role_holographic | 300 | active, 3 renters |
| voice_style | 30 | active, 4 renters |
| streak_shield | 30 | 4 purchases |
| quest_reroll | 10 | 2 purchases |
| raffle ticket | 10 | **dark** (`econ_raffle_enabled = 0`) |
| pin of the day | **0** | price unset |
| bounty | — | **dark** (`econ_bounty_channel_id = 0`) |
| wager rake | **0%** | dark |
| demurrage | 3% over 500 | enabled but **has never run** |

Two corrections to the working assumption that these are "unbuilt":

- **Raffle and demurrage are fully built and wired** into the weekly ISO roll in
  `economy_loop.py:346-357`. Both are pure config flips, not engineering.
- **Demurrage is already enabled** in the main guild (`econ_demurrage_rate_pct =
  3`). Zero sweeps have run only because the rate was set after the last week
  roll; the first sweep fires at the next one. At current settings it will tax
  22 wallets for **364 coins** — about 1.5% of a week's mint. It is enabled but
  far too weak to matter.

### 1.9 Code-level defects and dead surface found during the sweep

These are distinct from balance and should be fixed regardless of which plan
stages we adopt.

1. **`photo_post` ignores the guild's booster multiplier.**
   `cogs/economy_cog.py:3706` passes `booster=booster` but omits
   `multiplier=settings.booster_multiplier`, so it silently falls back to
   `apply_credit`'s hardcoded 1.5. Every other faucet passes it. A guild that
   tunes the multiplier gets one payout that diverges. Small, real, easy.

2. **`price_text_room` / `price_voice_room` (200/200) are dead knobs.** The
   settings exist in `EconSettings`, the dashboard, and the stats panel, but
   there is **no purchase path anywhere** in `src/bot_modules/`. Private rooms
   are Stage-6 design-only per `economy_spec.md`. Either hide the knobs or build
   the path — right now the dashboard advertises a sink that cannot be bought.

3. **The login faucet under-rewards voice.** `login` pays whichever source fires
   first in a guild-local day. Text base is 5, voice base is 15 — so for anyone
   who types before they join a call, the 15 never lands. This inverts the
   intent: voice presence is exactly the "quiet majority" signal that leveling
   bots like Arcane reward deliberately, and we are paying it a third of list
   price in practice.

4. **The jackpot re-mints.** The 25% cut of lost stakes is already burned by the
   `casino_stake` debit and is re-minted on a triple-7 claim, and
   `jackpot_seed = 100` mints 100 from nothing on the first-ever claim of an
   unfed pot. Not urgent at 2 lifetime plays, but it means the casino's net-sink
   maths is RTP *plus* pot cycling, not RTP alone.

5. **`/bank pay` is entirely frictionless.** Zero fee, zero rake, no daily limit,
   no cooldown; the only guard is a confirm dialog above 100. 1,799 coins have
   already moved this way. Fine while the community is small and friendly, but
   it's the obvious vector if any leaderboard ever ranks balance, and it lets a
   whale bankroll anyone.

6. **The dynamic quest target is a treadmill.** Band quests resolve to
   `clamp(round(trailing_median × 1.15), min, max)` (`DYNAMIC_STRETCH = 1.15`,
   `quests.py:249`). Asking for 15% above your own median every period means the
   ask ratchets upward as you comply, until it pins at `target_max`. The clamp
   band saves it from running away, but the felt experience for an improving
   member is that the goalposts move every week. Worth a design conversation —
   the spec's stated principle is "effort-equity, not output-pay."

7. **Board sizes drifted from defaults.** Code defaults are 2/2/2; live is
   daily 3, weekly 2, monthly 1. Not a bug, but Stage 1 below assumes the live
   values.

---

## 2. Diagnosis

Three distinct problems, which the sink rounds conflated into one.

**P1 — The faucet rewards ambient behaviour, so it doesn't drive discovery.**
Payouts land on chatting, reacting, and posting media: things members do anyway.
This has two costs. It concentrates earnings on whoever already uses the most
features,
and it risks the *overjustification effect* — attaching coins to intrinsically
enjoyable activities (QOTD, photo challenges) can crowd out the intrinsic motive,
so when the novelty of the reward fades the activity fades with it. The big
rewards should sit on things people would *not* otherwise do.

**P0 — The onboarding beat that teaches spending reaches almost nobody.**
Nine members have ever claimed the `shop_purchase` setup quest. It is the
designed mechanism for teaching the earn→spend loop and it is delivered by
lottery, competing with 19 other quests for 3 daily slots. Before concluding
anything structural about hoarding, fix this and re-measure — it is the cheapest
intervention available and it plausibly accounts for a chunk of the spend gap.

**P2 — Nothing creates demand, so coins accumulate as optionality.**
Weekly rentals actively punish spending: buy now and it's gone in seven days;
wait, and you keep the option plus the balance. Combined with a static catalogue
and no scarcity anywhere, holding strictly dominates spending. This is the
endowment effect plus rational option value, and it fully explains 8.4%
absorption and 17 lifetime spenders. Adding more sinks to a static catalogue will
not fix it — round 2 and round 3 already tried that.

**P3 — The games programme has one host and thin audiences.**
Not a cold-start problem (§1.5): games run daily and scheduling already exists.
One person hosts 77% of them, so the programme is one busy fortnight away from
stopping, and typical attendance is under 3 players against a pool of 125
regulars. The two fixes are recruiting hosts two-through-five and making a live
game *visible* to people who aren't already in the channel.

---

## 3. Plan

Sequenced so the cheapest, highest-confidence moves land first. Every stage ships
with tests in the same commit per the working agreement; economy logic lives in
`*_service.py` / `*_logic.py` and is tested there.

### Stage 0 — Config-only, no code (do this first)

Zero engineering; all dashboard/config changes. Measure for one week before
building anything.

0. **Guarantee the `shop_purchase` setup quest lands early.** Pin it to a daily
   board slot for members who haven't completed it (or fire it once they first
   cross the cheapest perk price) instead of leaving it to a 3-in-20 draw. Same
   for the other setup kinds. This is the single highest-value change in the
   document relative to its cost, and it must land *before* we conclude anything
   structural about hoarding — measure the spend rate again 2 weeks after.

1. **Leave the community weekly targets alone** — the auto-sizer is correct
   (§1.6). Raise the **reward** from 10, which is too small to read as a
   server-wide event, and **don't judge the mechanic until W31 completes** — the
   projection says both goals clear a tier on a clean full week.
2. **Turn the raffle on** (`econ_raffle_enabled = 1`). Built, tested, wired.
3. **Pin of the Day and the bounty board stay dark** — confirmed 2026-07-23 as a
   deliberate hold pending testing and an announcement once the initial churn
   settles. Not a config oversight; don't flip them.
4. **Raise drops toward their configured budget.** 100% claim rate is the
   strongest engagement signal in the dataset and we're running 7 drops where
   config allows 8/day.
5. **Leave faucet payouts alone.** Do not nerf earning to fix absorption — EVE's
   monthly economic reports show measurable login decline when players feel too
   poor to risk anything. Fix demand, not supply.

Alongside these, the small code fixes from §1.9 that are pure defect repair: the
`photo_post` multiplier omission, and either hiding or building the dead
text/voice-room knobs. Both ship with tests in the same commit.

**Done 2026-07-23:** the community-weekly counter investigation closed with no
bug found (§1.6). The one small gap it did surface — a goal activated mid-week
inherits a full-week target — is worth fixing on its own: either prorate the
first partial week or hold activation to the next week roll.

### Stage 1 — Make the quest board a discovery engine

Targets P1 directly, and it's mostly tuning of a system that already exists.

1. **Guarantee variety on the daily board.** Of the 3 daily slots, reserve at
   least one for a non-ambient kind (anything that isn't message_sent /
   reply_sent / reaction_given / media_post). The pool already has the quests;
   they just rarely get drawn.
2. **Add a variety streak.** Not a bonus — a *streak*, because loss aversion
   outperforms gain framing (the Duolingo streak-freeze insight). "Distinct
   features used this week"; breaking it costs something visible. This is the
   single most direct lever on the user's stated goal of even tool use.
3. **Per-source diminishing returns on ambient kinds.** After N claims from
   message_sent in a week, its yield decays. Keeps the loop available, stops it
   dominating, and pushes the top decile outward into other features rather than
   deeper into chat.
4. **Cross-feature missions** ("use 2 different games this week"), sized to what
   the population can actually supply — an impossible mission teaches people to
   ignore the board.

5. **Fix the voice login inversion** (§1.9.3). Pay the voice base when a member
   joins voice that day even if text already fired, or credit the difference.
   Voice presence is the cheapest way to reach members who never trigger
   command-based faucets, and right now we advertise 15 and pay 5.

6. **Revisit `DYNAMIC_STRETCH`** (§1.9.6). A permanent +15% ask on your own
   median is in tension with the spec's "effort-equity, not output-pay"
   principle. Consider stretching only below a band midpoint, or not at all.

### Stage 2 — Create demand (the absorption fix)

Targets P2. This is where the 8.4% moves.

1. **Rotating limited-stock shop.** A weekly featured rack: a few items available
   *this week only*. This directly destroys the option value that makes hoarding
   rational, and it's the cheapest large win available. The rental infrastructure
   already exists; this is a catalogue and scheduling layer on top.
2. **Weekly auction — one unique cosmetic, one winner, closes Sunday.** Best
   possible mechanic for our specific shape: it burns from the *top* of a
   Gini-0.70 distribution, prices by revealed demand instead of guesswork,
   creates a recurring event with no scheduling burden, and generates exactly the
   chat a dead economy lacks. Already in the `economy_spec.md` parking lot.
3. **Spend-milestone recognition.** Lifetime-burn tiers with a visible badge.
   Converts the hoarder's status motive from balance to burn. Note the existing
   all-time biggest-spender board is the right instinct — this extends it. Do
   **not** add an all-time *wealth* leaderboard; that rewards the behaviour we're
   trying to break.
4. **Retune demurrage** once Stage 0 data is in. 1.5% of weekly mint is not a
   sink. Announce before changing it — the voice-lease pattern.

Deferred, deliberately: the **dual-currency split** (bankable prestige points +
decaying spendable stipend) is the most structurally correct fix for hoarding,
and it's the largest build in this document. Revisit only if Stage 2 doesn't move
absorption past ~25%.

### Stage 3 — Spread the hosting, widen the audience

Targets P3. Do **not** add another game, and do **not** consolidate the ones we
have — variety runs daily and works (§1.5).

1. **Host bounty + host-a-game quests — BUILT 2026-07-23 (ships dark).** Hosting
   a party game that at least one other member joins now (a) pays the host a
   bounty scaled by attendance (`host_bounty_per_joiner`, capped at
   `host_bounty_cap` joiners; 0 rate = dark) and (b) fires a `game_host` quest
   trigger usable as a personal daily *and* a community counted weekly. The
   attendance gate is the anti-farm: a host of an empty game earns nothing and
   fires nothing (74 of 122 historical games had zero recorded joiners, so this
   matters). Party games only — duels are peer challenges and external CAH has
   no member host. The user's read (2026-07-23) is that this is a **recruitment
   task**: enable people via the `/ask` command surface plus better advertising
   from the host, so the coin stays small and the *quest/recognition* does the
   work — matching the prosocial-design finding.

   **Activation (dashboard, when ready):** set `host_bounty_per_joiner` on
   Income Sources to light the bounty; create a `game_host` **daily** quest and
   a `game_host` **community** quest in the Quests panel (the biweekly scheduler
   rotates the community one in). The `game_host` income source is on by
   default. Hold until the setup-quest churn settles, same as the other dark
   sinks.
2. **Make a live game visible outside its own channel.** Attendance under 3
   against 125 regulars is a discoverability gap, not a demand gap. A
   ping-on-start for an opt-in role, or a "game in progress — join here" line on
   an existing sticky panel, is the cheapest test of that hypothesis.
3. **A claim-race format (the Mudae/Karuta pattern).** Bot spawns something
   contested; first claimant takes it. No lobby, no schedule, no minimum
   headcount — and, crucially, **no host**, which is the one resource we're
   short of. Given drops sit at a 100% claim rate here (§1.7), this is the
   highest-confidence new multiplayer mechanic available to us.

### Stage 4 — Measurement

Make absorption a first-class, always-visible number, not something we
re-discover in a quarterly review.

- Add **absorption rate**, **distinct-faucet-breadth distribution**, **spender
  count**, and **wallet Gini** to the Economy → Statistics panel.
- Weekly rollup already exists (`econ_metrics_weekly`); extend it rather than
  building a parallel path.
- **Re-run §1.1b's cohort test at 4 and 8 weeks.** The tenure-controlled spend
  rate among affordable-and-established members (currently 13.5%) is the single
  number that tells us whether this is a structural demand problem or an
  onboarding problem. Stage 2's large builds should be gated on it.
- Success criteria at 8 weeks (main guild):
  - absorption ≥ 25% (from 9.3%)
  - lifetime spenders ≥ 45 of ~114 participants (from 13)
  - tenure-controlled spend rate ≥ 40% (from 13.5%)
  - members using 4+ distinct faucet kinds ≥ 90 (from 74)
  - Gini ≤ 0.55 (from 0.62)
  - at least one community weekly clearing tier 2 on a full week (W31 is the
    first clean test — the projection says it should, unaided)

---

## 4. Explicitly rejected

- **Nerfing existing faucet payouts.** Drives the "feeling poor" churn documented
  in EVE's economic reports. Fix demand.
- **Power-granting sinks.** Accelerates the concentration we're trying to reverse.
  Cosmetics, status, and access only — the current catalogue is right in kind.
- **An all-time cumulative wealth leaderboard.** Rewards hoarding and demotivates
  everyone outside the top three.
- **More parallel multiplayer games.** We have eight types running and one host;
  supply is not the constraint. (An earlier draft went further and proposed
  *consolidating* to one scheduled game — also wrong, and retracted in §1.5.)
- **Raising QOTD/photo-challenge payouts to boost engagement.** Overjustification
  risk — it would hollow out intrinsic motivation on the two activities that are
  supposed to be fun on their own.
- **A hard currency wipe / season reset.** Path of Exile's league model is the
  strongest anti-hoarding tool in the industry, but at ~150 members a wipe risks
  alienating exactly the invested members we can least afford to lose. Soft reset
  only, if ever.

---

## 5. Sources

Live production data queried 2026-07-22 from `dungeonkeeper.db` (read-only).
Design research: Pardus wealth-inequality study (PLOS ONE), EVE Monthly Economic
Reports, Path of Exile league economy, Elite Dangerous Community Goals, Warframe
bounty/daily-tribute structure, Animal Crossing rotating stock, Mudae/Karuta
claim races, Habitica parties, Yu-kai Chou on leaderboard splicing and operant
conditioning, the overjustification-effect literature, and low-population
multiplayer design guidance. Full citation list in the research appendix of the
session that produced this document.
