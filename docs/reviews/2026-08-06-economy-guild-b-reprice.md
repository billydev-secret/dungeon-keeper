# Guild B ("nut") — sink reprice proposal, 2026-08-06

Status: **recommendation only. Nothing here has touched prod, and I have not
been asked to apply it.** Guild `1476525656115515484` is administered by
someone else (grants there are issued by "The panda keeper"); this is a costed
proposal to hand over, not a change to make.

Source: `2026-08-06-economy-ledger-data-audit.md` H1, re-measured on live data
2026-08-06 with the now-tz-correct multi-guild report.

## The measurement

| | Guild A (main) | Guild B ("nut") |
|---|---:|---:|
| tz | UTC−7 | **UTC+2** |
| wallets | 164 | 166 |
| float | 108,626 | **143,337** |
| minted, 5d | 33,831 | **90,515** |
| burned, 5d | 15,321 | **2,817** |
| **burn ratio** | **45.3%** | **3.1%** |
| median 5d income | 48 | 382 |
| p90 5d income | 511 | 3,234 |
| median balance | 199 | **93** |
| p90 balance | 1,548 | **3,510** |
| p90 / p50 balance | 7.8× | **37.7×** |

## The finding is not "guild B pays too much"

That was the first read and the data does not support it. Guild B's currency
is simply denominated differently, and the **realized** exchange rate — from
what members actually earn, not from dial arithmetic — is **≈ 8×**:

| Normalizer | A | B | B/A |
|---|---:|---:|---:|
| median 5-day income | 48 | 382 | **8.0×** |
| p90 5-day income | 511 | 3,234 | **6.3×** |

At 8×, guild B's +19,046 nut/day is ≈ **+2,400 coin-equivalent/day** — *less*
than guild A's +3,992/day. Its float of 143,337 is ≈ 17,900 coin-equivalent,
a sixth of guild A's. Nominally alarming, in real terms modest.

**The real problem is that guild B has almost nothing to spend on**, and its
price list is *inverted* relative to earnings — the recurring, high-value
sinks that actually drain a float are near-free, while the small impulse buys
are several times too dear. Median balance is *lower* than guild A's (93 vs
199) while p90 is more than double: a few members bank everything and everyone
else has nothing, because there is no mid-priced thing to buy.

## Proposed prices (at the measured 8× line)

| Dial | A | B now | B/A now | **Proposed** | Why |
|---|---:|---:|---:|---:|---|
| `econ_price_voice_room` | 230 | 200 | **0.9×** | **1,840** | Never scaled. A recurring room lease at 200 is the single biggest missed sink. |
| `econ_price_text_room` | 200 | 200 | **1.0×** | **1,600** | Never scaled. |
| `econ_price_streak_shield` | 30 | 30 | **1.0×** | **240** | Never scaled. |
| `econ_price_role_icon` | 1,200 | 2,500 | 2.1× | **9,600** | The premium cosmetic; currently the cheapest thing in the shop relative to income. |
| `econ_price_role_holographic` | 500 | 1,500 | 3.0× | **4,000** | |
| `econ_price_role_gradient` | 150 | 500 | 3.3× | **1,200** | |
| `econ_price_raffle_ticket` | 25 | 50 | 2.0× | **200** | See the raffle note below. |
| `econ_price_role_color` | 65 | 1,000 | 15.4× | **520** | **Cut** — nearly 2× overpriced. |
| `econ_price_role_name` | 45 | 800 | 17.8× | **360** | **Cut.** |
| `econ_price_voice_style` | 40 | 900 | 22.5× | **320** | **Cut** — 2.8× overpriced. |
| `econ_price_qotd_sponsor` | 40 | 1,500 | 37.5× | **320** | **Cut** — 4.7× overpriced; a sink nobody can reach is not a sink. |
| `econ_price_emoji` | 60 | 2,500 | 41.7× | **480** | **Cut** — 5.2× overpriced. |
| `econ_price_emoji_animated` | 90 | 3,500 | 38.9× | **720** | **Cut.** |
| `econ_price_quest_reroll` | 10 | 400 | 40.0× | **80** | **Cut** — 5× overpriced. |

Note this is **not** a net price rise. Seven dials come *down*. The shape
changes from "cheap things cost a fortune, expensive things are free" to a
ladder members can climb, which is what makes a burn ratio move.

## Faucets: leave them alone for now

At the 8× line guild B's per-member faucet rates are broadly reasonable. Two
outliers are worth a look but neither is the main story:

| Dial | A | B | B/A |
|---|---:|---:|---:|
| `econ_streak_bonus_cap` | 10 | 1,000 | **100×** |
| `econ_reward_photo_post` | 5 | 500 | **100×** |

Both are ~12× above the 8× line. If the reprice alone doesn't move the burn
ratio, cut these to 80 and 40 respectively before touching anything else —
they are the likeliest driver of the p90/p50 spread, since a streak bonus
rewards exactly the members who are already there every day.

Also worth noting, not fixed here: `econ_bounty_rake_pct` and
`econ_wager_rake_pct` are both **0** in guild B (10 in guild A), so its two
PvP sinks burn nothing at all.

## The raffle will not work until the shop panel is re-posted

`econ_raffle_enabled = 1` in both guilds, prices set, **zero tickets ever
sold** — see the audit's L1. Cause: the shop panel is a posted Discord message
and its buttons are baked in at post time. Guild A's shop message was posted
2026-07-28 06:05 UTC; the raffle was enabled at the 07-30 retune, two days
later. The Tickets button is therefore not on the live message.

**Fix: re-post the shop panel** (Economy → Settings → the shop poster) in both
guilds. No code change. Repricing the raffle ticket without doing this changes
nothing, in either guild.

## If applied

Same procedure as the 07-30 retune, with the rollback file saved **outside the
repo root**. Baseline and checkpoint per guild:

```
python scripts/economy_tuning_report.py --all-guilds --days 4 \
    --save-baseline docs/reviews/economy-baseline-2026-08-06.json
# 4 days later
python scripts/economy_tuning_report.py --all-guilds --days 4 \
    --baseline docs/reviews/economy-baseline-2026-08-06.json
```

Success for guild B = burn ratio above 20% (from 3.1%) **and** median balance
rising from 93, which is the number that says the mid-tier members finally
have something to buy. A burn ratio that rises while the median keeps falling
means the reprice only taxed the whales and should be rolled back.
