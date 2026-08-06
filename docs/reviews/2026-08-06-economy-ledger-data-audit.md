# Economy ledger — empirical data audit against production, 2026-08-06

Lane: forensic, read-only. The 2026-08 economy reviews verified the *code*
(`2026-08-05-economy-core.md` rated the wallet funnel the best code in the
repo). This audits the *data*.

**Method.** Snapshot of the live DB taken 2026-08-05 22:22 local via the
sqlite3 backup API (`sqlite3.connect('file:…?mode=ro', uri=True).backup(dst)`),
never `cp`. All queries run against that snapshot. Prod was not written to and
no dials were changed. Ledger covers 22,000 rows / 33 kinds / 3 guilds,
2026-07-13 → 2026-08-06.

Cross-checked against `scripts/economy_tuning_report.py --days 5`, which
independently produced minted 36,734 / burned 16,761 over 08-01..08-05 —
matching this audit's +3,992/day to within 3 coins/day. Where I disagree with
the tool it is about *classification*, not arithmetic.

---

## Headline: the books balance exactly. The measurement around them does not.

Reconciliation and double-entry integrity are **clean** — see §6, which is the
most important negative result in this document. Every finding below is about
money the ledger records correctly but that nobody is *watching*.

---

## Findings

### H1 — A second guild is minting 4.8× the main guild and is invisible to every economy review (High)

**Severity: High.** It is the largest currency faucet in production, it has
grown past the guild every review has been about, and no measurement tool
points at it.

**Evidence** (verified by query against the snapshot):

Guild `1476525656115515484` (currency "nut", 163 wallets) went live
2026-07-30. Raw mint − burn, guild-local days:

| Day | Guild A (main, `1469…666`) | Guild B (`1476…484`) |
|---|---:|---:|
| 2026-07-30 | +4,997 | +15,346 |
| 2026-07-31 | +7,159 | +15,668 |
| 2026-08-01 | +4,066 | +18,085 |
| 2026-08-02 | +5,095 | +19,621 |
| 2026-08-03 | +2,438 | +20,531 |
| 2026-08-04 | +4,692 | +14,808 |
| 2026-08-05 | +3,670 | +22,184 |
| **08-01..08-05 mean** | **+3,992/day** | **+19,046/day** |

Float today: **guild B 138,117, guild A 106,053.** Guild B held ~11,874 before
07-30, so ~126,000 of its float — 91% — was minted in seven days.

Guild B has essentially no sink. Burn was **0 on three of the last five days**;
lifetime burn is 14,877 against 152,994 minted (**9.7%**). Guild A's burn ratio
over the same window is 45.6%.

Root cause is a scaling mismatch, not a bug. Guild B's dials were scaled up
from guild A's, but faucets and sinks were scaled by different multiples:

| | median B/A ratio |
|---|---:|
| 14 faucet dials (`login_*`, `reward_*`, `milestone_*`, `drops_max_coins`) | **15.0×** |
| 13 sink dials (`price_*`) | 17.8× |

The median hides the problem — the *expensive* sinks barely moved while the
volume faucets went up 15–100×:

| Dial | A | B | B/A |
|---|---:|---:|---:|
| `econ_reward_photo_post` | 5 | 500 | 100.0× |
| `econ_streak_bonus_cap` | 10 | 1000 | 100.0× |
| `econ_reward_game_participation` | 3 | 100 | 33.3× |
| `econ_reward_qotd` | 10 | 250 | 25.0× |
| … but … | | | |
| `econ_price_voice_room` | 230 | 200 | **0.9×** |
| `econ_price_role_icon` | 1200 | 2500 | **2.1×** |
| `econ_price_role_holographic` | 500 | 1500 | **3.0×** |
| `econ_price_role_gradient` | 150 | 500 | **3.3×** |

A member in guild B earns ~15–25× a guild-A member but buys the top cosmetic
for 2.1× the price — the premium perks are effectively **7–10× cheaper relative
to income** than in guild A. That, plus `econ_demurrage_rate_pct` 6 (vs 8) and
`econ_bounty_rake_pct`/`econ_wager_rake_pct` both **0**, is why nothing drains.

`scripts/economy_tuning_report.py:50` hardcodes `MAIN_GUILD =
1469491362444480666`, so every economy review to date — including the pending
round-2 proposal — measured guild A only.

**Fix.** Two parts, neither of which is a dial change I should make:
1. Make the reporting multi-guild: iterate guilds with wallets rather than
   defaulting to one, or at minimum have the round-2 checkpoint run
   `--guild 1476525656115515484` as well (**but see M3 first — the tool
   currently computes guild B's days 9 hours wrong**).
2. Re-derive guild B's sink prices against its own income curve. Bringing
   `price_role_icon`, `price_role_holographic`, `price_role_gradient` and
   `price_voice_room` to the same ~15× multiple as its faucets would put them
   at roughly 18,000 / 7,500 / 2,250 / 3,450. Recommendation only — not applied.

---

### H2 — `econ_cat_catch_daily_cap` is set in production and no code reads it (High)

**Severity: High.** CLAUDE.md: *"Never ship a preference or toggle that isn't
enforced."* An operator set a faucet cap, the value is sitting in prod config,
and the faucet has been uncapped ever since.

**Evidence** (verified by grep + query):

- `config` row `(1469491362444480666, 'econ_cat_catch_daily_cap', '150')` exists
  in the live DB.
- `grep -rn "cat_catch_daily_cap"` over the entire repo returns **nothing**.
- `git log --all -S'cat_catch_daily_cap'` finds it only in `e65782b4`
  ("Economy: make Cat Bot payouts a dial, with a per-member daily cap",
  2026-08-02) and `40a28a8a` ("stage the cat cap…", 2026-08-02). Both are on
  the unmerged branch
  `i-m-trying-to-think-of-more-quests-and-community-quests…`;
  `git merge-base --is-ancestor` confirms **neither is on `main`**.
- `98395fdb` (2026-08-05, the round-2 proposal commit) added the six
  `catcatch_coins_*` dials but not the cap.

So the config was staged ahead of code that never shipped. Ledger confirms it
is unenforced — 9 member-days have exceeded 150 coins of `cat_catch` **on or
after 2026-08-02**:

| User | Day | Coins | Catches |
|---|---|---:|---:|
| 252957506060419073 | 2026-08-02 | 653 | 8 |
| 1407937323529928774 | 2026-08-03 | 624 | 11 |
| 1383575324977139773 | 2026-08-02 | 613 | 57 |
| 1376250515976884288 | 2026-08-05 | 455 | 3 |
| 1407937323529928774 | 2026-08-05 | 321 | 15 |
| 1284869710847934544 | 2026-08-03 | 299 | 13 |
| 884813145833619466 | 2026-08-02 | 279 | 32 |
| 1069501326184153088 | 2026-08-02 | 183 | 28 |
| 1284869710847934544 | 2026-08-05 | 167 | 7 |

(The all-time worst predates the cap: 1,522 coins / 112 catches in one day on
2026-07-25.)

`cat_catch` is currently **+1,356/day** in guild A — the second-largest faucet.

**Fix.** Either land the cap from `e65782b4` onto main (a `sum(amount)` guard
per `(guild_id, user_id, local_day)` in `pay_cat_catch`,
`src/bot_modules/economy/game_rewards.py:222`), or delete the orphaned config
row so the dashboard stops implying a cap exists. Do **not** leave it as is.

**Swept for others.** I diffed every `econ_*` key in prod `config` against the
91 `EconSettings` dataclass fields. 83 distinct keys, **exactly two** with no
backing field:

| Orphaned key | Value | Status |
|---|---|---|
| `econ_cat_catch_daily_cap` | 150 (guild A) | **live faucet cap, unenforced** — this finding |
| `econ_price_gift_color` | 50 (both guilds) | harmless — see L3 |

So this is the only *consequential* orphan. Good news for the config surface
generally.

---

### M1 — The round-2 proposal's `reward_game_participation` line is wrong by ~70× (Medium)

**Severity: Medium.** The proposal (`2026-08-06-economy-retune-round2-proposal.md`)
is awaiting sign-off; acting on it would produce a miss and a wasted checkpoint.

**Evidence** (verified by reading `src/bot_modules/economy/game_rewards.py:362`
and by query):

`pay_score_rewards` pays score-proportional rewards for *all* score-based party
games out of `cap = settings.reward_cah_win_max`, writing rows tagged
`game_participation` / `game_win`. It never reads `reward_game_participation`
or `reward_game_win`. Splitting guild A's 08-01..08-05 rows by whether meta
carries `top_score`:

| Kind | Score path (`reward_cah_win_max`) | Flat path (`reward_game_*`) |
|---|---:|---:|
| `game_participation` | 4,933 (**99.2%**) — +987/day | 42 — +8/day |
| `game_win` | 2,304 (**96.8%**) — +461/day | 76 — +15/day |

So the proposal's *"reward_game_participation 3 → 2, ≈ −210/day"* would in
reality deliver about **−3/day**.

Two further consequences:

- The 07-30 retune's `reward_game_win 50 → 25` also did almost nothing for the
  same reason — measured pre/post averages (backfill rows excluded) moved only
  58.8 → 52.4, a 0.89× ratio where 0.5× was intended.
- The dial that *does* control this money was already cut. The ledger dates the
  change precisely: row 21491 at `2026-08-05 13:39:04Z` paid 50 (cap 50); row
  21528 at `2026-08-05 15:01:34Z` paid 15 (cap 15). `econ_reward_cah_win_max`
  now reads 15. That is a real **~−690/day** already banked in guild A that
  the proposal does not account for — and it happened *after* the 08-05 data
  the proposal was measured on.

**Fix.** Rewrite that proposal row to target `econ_reward_cah_win_max`, and
re-baseline: the 50→15 cut lands most of what the participation line was
supposed to buy. Separately, decide whether `reward_game_participation` /
`reward_game_win` should still exist as dials at all — they currently move 23
coins/day between them and the dashboard presents them as the game-payout
controls, which is misleading.

---

### M2 — `economy_tuning_report.py` books escrow round-trips as mint and burn (Medium)

**Severity: Medium.** It inflates the reported burn ratio, which is the number
the retunes are being judged against.

**Evidence** (verified by reading `pools_service.py:71-78` and by query):

`BURN_KINDS_EXCLUDED = ("transfer_out", "wager_stake", "casino_stake")`.
`bounty_stake` and `auction_bid` are **not** excluded, so escrowed money counts
as burned; `bounty_payout` and `auction_refund` are not in `NON_FAUCET_KINDS`,
so it counts as minted again when it comes back.

For guild A, 08-01..08-05, the tool reports `burned (real sinks) = 16,761` with
`bounty_stake = 4,073` as its third-largest sink. But of 6,308 lifetime bounty
contributions, **zero are refunded** and 4,703 is still escrowed against three
bounties in state `open` (ids 2, 4, 5). That is currency sitting in escrow, not
destroyed. Excluding it puts real burn at ~12,688 and the burn ratio at ~35%,
not the reported **45.6%**.

The net float number is unaffected (the round-trip cancels) — this is a
faucet/sink *mix* and burn-ratio error only.

**Fix.** Add `bounty_stake` and `auction_bid` to `BURN_KINDS_EXCLUDED` and
`bounty_payout` / `auction_refund` to `NON_FAUCET_KINDS`, then book the burned
residual (bounty rake, winning auction bid) explicitly the way `casino_hold`
already is. Note this changes the Pools settlement metric, so it needs the same
care the casino netting comment at `economy_tuning_report.py:158-168` describes.

---

### M3 — `economy_tuning_report.py` hardcodes a timezone but exposes `--guild` (Medium)

**Severity: Medium.** Latent, but it will fire the moment anyone acts on H1.

**Evidence** (verified by reading `economy_tuning_report.py:51-56` and querying
`config`):

```python
TZ_OFFSET_HOURS = -7.0
# The main guild has no tz row and inherits the global -7 — keep in sync…
```

Prod `config` says:

| guild | `tz_offset_hours` |
|---|---:|
| 0 (global default) | -7.0 |
| 1469491362444480666 (A/main) | **-7.0** |
| 1476525656115515484 (B/nut) | **+2.0** |
| 1358148226850492618 | -5.0 |

Two things: the comment is **stale** — guild A does have its own row (the value
coincidentally matches, so no current numeric error) — and `--guild` is an
argparse flag with no corresponding tz lookup, so pointing the tool at guild B
buckets its days with a **9-hour offset**.

**Fix.** Load `tz_offset_hours` for the requested guild (falling back to the
`guild_id=0` row) instead of the module constant, and delete the stale comment.

---

### M4 — Recomputed on today's data, the round-2 proposal undershoots its own target band (Medium)

**Severity: Medium.** The proposal's success criterion is a numeric band; on
current data it misses.

**Evidence** (verified by query, cross-checked against the tool):

The proposal states *"raw mint−burn ≈ +3,330/day over the 5-day window"*.
Recomputed on 08-01..08-05: **+3,992/day** (08-02..08-05: +3,974/day). The
Pools line is +4,993.5, matching the proposal's figure — so the discrepancy is
in the raw number, not the line.

Proposal's estimated effect is −1,630/day. Correcting the participation line
(M1: −210 → ≈ −3) makes it ≈ **−1,423/day**, landing at **≈ +2,570/day** —
above the stated +1,300..+1,900 target band. Even crediting the already-applied
`reward_cah_win_max` 50→15 cut (≈ −690/day, already inside the +3,992 baseline
for only its last ~9 hours) does not close the gap.

Measured 08-01..08-05 per-day contribution of each proposal target, guild A:

| Kind | Actual |
|---|---:|
| `quest` | +1,404/day |
| `cat_catch` | +1,356/day |
| `game_participation` | +995/day |
| `login` | +925/day |
| `quest_community` | +636/day |
| `demurrage` | −1,006/day |

**Fix.** Re-derive the proposal's per-line estimates from these measured totals
rather than from dial arithmetic, and re-run the checkpoint against a baseline
saved *after* the 08-05 `reward_cah_win_max` change. Recommendation only —
no dials changed.

---

### L1 — The raffle is switched on in both guilds and has never sold a ticket (Low)

**Severity: Low.** A sink the 07-30 retune deliberately enabled is inert; no
money is at risk.

**Evidence** (verified by query):

- `econ_raffle_enabled = 1` in **both** guild A and guild B;
  `econ_price_raffle_ticket` 25 / 50, `econ_raffle_max_tickets` 10 / 50.
- `econ_raffle_tickets`: **0 rows**, all time.
- `econ_raffle_draws`: 2 rows, both ISO week `2026-W31`, both
  `winner_id = NULL, tickets = 0, entrants = 0`.
- No ledger kind matching `%raffle%` exists.

The 07-30 rollback file confirms the retune flipped `econ_raffle_enabled` 0→1
as a sink. Seven days later it has burned **0**.

This also settles the brief's question about the deliberate 0-amount marker at
`economy_raffle_service.py:263`: it has **never been written**. There are
**zero** rows with `amount = 0` in the entire ledger, so that is not "the only
one" — it is none, and any consumer relying on it as a rental-marker signal is
reading an empty set.

**Fix.** Diagnose why tickets can't be bought (no entry point, or the panel
isn't posted) before counting the raffle in any sink projection.

---

### L2 — A staff grant went to the granter themselves; `/bank grant` has no self-grant guard and no ceiling (Low)

**Severity: Low.** Within permissions and fully attributed — reporting it
because the ledger made it visible, which is the system working.

**Evidence** (verified by query + reading `economy_cog.py:1399-1455`):

```
guild A  user 1384378931981058068  +1500  actor_id 1384378931981058068
         meta: {"reason": "Sugar daddy stuff", "granted_by": "1384378931981058068"}
         2026-07-30 17:29:10Z
```

`actor_id == user_id`. `bank_grant` gates on `_can_grant(actor, settings)` and
rejects bots and `amount < 1`, but has no self-grant check and no per-grant or
per-day ceiling. The other 19 grants (all guild B, actor 714942612217528402,
reasons "s&d" / "hotseat", 250–750 each) are third-party and unremarkable.

**Fix.** Optional. If wanted: refuse `member.id == actor.id`, or leave it and
rely on the ledger — `actor_id` is recorded, which is what made this findable.

---

### L3 — Stale `econ_price_gift_color` config rows survive a perk retired in migration 091 (Low)

**Severity: Low.** Dead data, no behavioral effect — recording it because it
surfaced from the same sweep as H2 and someone reading `config` could mistake
it for a live price.

**Evidence** (verified by the `EconSettings` sweep above + grep + git):

`config` holds `econ_price_gift_color = 50` for **both** guild A and guild B.
There is no `price_gift_color` field on `EconSettings`. The `gift_color` perk
kind was retired in migration 091 (commit `5b5e7a07`, "Economy: gift any perk —
gift_color retired, rentals CHECK widened"); the code retains the string only
so historical ledger rows still render
(`src/bot_modules/economy/perks.py:11,29`, `register.py:135-146`,
`economy_rentals_service.py:66`).

Unlike H2 this is genuinely inert — gifting now works through the ordinary perk
kinds with `beneficiary_id != user_id`, and nothing reads the key.

**Fix.** Delete the two rows, or leave them. No urgency; do not bundle with the
H2 fix if that would confuse the rollback.

---

## §6 — What is verifiably healthy (the important negative results)

These were the lane's primary questions. All passed, and the evidence is worth
recording so the next audit does not redo it.

**Reconciliation is exact.** Full outer join of `econ_wallets` against
`SUM(econ_ledger.amount)` grouped by `(guild_id, user_id)`:

- **343 member-wallet pairs, 343 reconcile. Zero drift.**
- Zero wallets whose balance ≠ its ledger sum.
- Zero ledger members with no wallet row; zero wallets with no ledger rows.
- Sum of all ledger amounts = 245,582 = sum of all wallet balances, exactly.
- Zero wallets with `balance < 0` (schema enforces `CHECK (balance >= 0)`).

Re-verified on a second independent snapshot taken two minutes later — same
result. There are no drifted wallets to explain, so the "handful with a common
cause" the brief anticipated does not exist.

**Double-entry integrity holds on every paired kind:**

| Pair | Debit | Credit | Residual | Explanation |
|---|---:|---:|---:|---|
| `transfer_out` / `transfer_in` | 3,314 (25 rows) | 3,314 (25 rows) | 0 | exact |
| `wager_stake` / `wager_payout` | 520 | 520 | 0 | exact |
| `auction_bid` / `auction_refund` | 6,830 (27) | 6,320 (25) | 510 | = the 2 winning bids, burned. `econ_auction_bids` states: 25 refunded, 2 won ✓ |
| `bounty_stake` / `bounty_payout` | 6,308 (21) | 1,481 (2) | 4,827 | = 124 rake burned + 4,703 escrowed in 3 `open` bounties ✓ |
| `casino_stake` / `casino_payout`+`refund` | 196,477 | 165,411 | 31,066 | house hold; jackpot pot standing at 9,230 |

No kind has an unexplained one-sided residual.

**Zero-amount rows: none.** 0 rows with `amount = 0` across all 22,000 ledger
rows (see L1).

**Both backfills landed cleanly, no double-pay:**

- Zero duplicate `(meta.game, user_id)` pairs on `game_participation`,
  `game_win`, or `game_host` — the idempotency key both backfill scripts rely on.
- `games_external_payouts`: 2,729 rows, **zero duplicate claim keys**.
- The 07-29 backfills are visible exactly where expected: `game_participation`
  spikes to 148 rows (110 of them in a single hour, 15:00Z) against a 20–40/day
  baseline, and `game_host` to 12 rows against 1–3/day. 213 `cat_catch` rows
  carry `meta.backfill`; 109 `game_participation` and 32 `game_host` likewise.
- No gap found. The 7 unrecoverable ToD games noted in the 07-29 memory remain
  unrecoverable by design — nothing in the data suggests they were partially
  paid.

**Quest payout "mismatches" are all explained.** 60+ `(quest_id, amount)`
combinations differ from `econ_quests.reward`. Every one resolves to
`reward × 1.5` (booster/patron bonus,
`economy_quests_service.py:2324`), `× 2` (weekly spotlight, `:2343-2350`), or
`× 3` (both) — or to a reward the operator edited later. The alarming case,
guild B quest 87 paying **750 against a configured reward of 60**, is a reward
edit: it paid 500/750 (= 250 × 2 / × 3) through 08-04 and 120/180
(= 60 × 2 / × 3) from 08-05, exactly as a 250→60 edit on 08-05 predicts.
`econ_spotlight_kind` confirms `pen_pal` was guild B's spotlight for W31 and
W32. **No overpayment.**

**Retune dial verification.** Pre-window 07-25..07-29 vs post-window
07-31..08-04, guild A, backfill rows excluded (they materially skew the
pre-window — 148 low-value rows on 07-29 — and any future before/after
comparison must exclude `meta.backfill`):

| Kind | Dial change | Pre avg | Post avg | Ratio | Verdict |
|---|---|---:|---:|---:|---|
| `drop` | `drops_max_coins` 200→50 | 85.2 | 30.4 | 0.36× | ✅ landed |
| `quest_bonus` | set bonus 10/25→5/15 | 11.8 | 5.9 | 0.50× | ✅ landed |
| `demurrage` | 3%/500 → 8%/400 | −24.4 | −89.8 | 3.69× | ✅ landed |
| `login` | `login_voice_base` 15→8 | 13.2 | 12.1 | 0.91× | ✅ consistent (voice logins only) |
| `game_win` | `reward_game_win` 50→25 | 58.8 | 52.4 | 0.89× | ❌ dial bypassed — see M1 |
| `game_participation` | `reward_game_participation` 5→3 | 31.3 | 39.9 | 1.27× | ❌ dial bypassed — see M1 |
| `game_host` | `per_joiner` 100→30, `cap` 1→8 | 143.8 | 185.6 | 1.29× | ⚠️ faucet *grew* (max 1×100=100 → 8×30=240), consistent with the proposal's "hosts stay favored" |

The `econ_host_bounty_cap = 1` inversion flagged in `2026-07-30-economy-health.md`
is **fixed** — it now reads 8 in both guilds.

**Cap enforcement, where caps exist.** `econ_conversion_daily_cap = 250`: 10
member-days exceed it, all on 07-25/07-26 — i.e. **all before** the 07-28
retune that introduced it, up to 1,038. No breach since. ✅ Enforced.
`econ_drops_per_day` is documented as an *average cadence*, not a cap
(`economy_drops_service.py:4,51`), so guild A's 18 drops on 08-03 against a
dial of 16 is expected behavior, not a breach.

**No exploitation found.** Top-10 wallet provenance in both guilds traces to
ordinary faucet mixes (largest single-source concentration: one guild-A member
at 63% `cat_catch`, one guild-B member at 77% `quest`). No member's balance
grew in a way the dials do not explain. The only *anomalous* per-member rates
are the uncapped `cat_catch` days in H2 and guild B's whole faucet structure in
H1 — both operator configuration, not member abuse.

---

## Not re-reported

Already recorded elsewhere; rediscovered and confirmed, not written up again:

- `econ_host_bounty_cap = 1` inverting the host faucet —
  `2026-07-30-economy-health.md`, and now **fixed** in prod (cap = 8).
- Casino RTP / windowing artifacts — `2026-07-30-economy-health.md` and
  `2026-08-05-casino.md`. Snapshot house hold is 14.4% of handle over
  08-01..08-05, on a 5-day window too short to read as RTP; no new finding.
- The wallet mutation funnel / `apply_debit` atomicity —
  `2026-08-05-economy-core.md`. The reconciliation result in §6 is empirical
  confirmation of that code review.

## Not checked

Stated so the next reader does not assume coverage:

- Guild `1358148226850492618` (17 wallets, 1,556 float) beyond confirming it
  reconciles. Too small to matter.
- GDPR register implications of any of this — no new user-data table was
  introduced; `econ_ledger` is already registered.
