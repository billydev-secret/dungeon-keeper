# Economy — live auctions (mod-run, ascending, coin sink)

**Date:** 2026-07-23
**Status:** Plan. Nothing built yet.
**Origin:** `economy-engagement-review.md` Stage 2 ranked a weekly auction the
best-fit demand mechanic for our wealth distribution — it burns from the top of
a Gini-0.62 curve, prices by revealed demand instead of a guessed shelf price,
and manufactures the recurring chat a dead economy lacks.

## Design (decided 2026-07-23)

- **Prize: mod-curated freeform.** A mod opens an auction with a title and a
  freeform description of what the winner gets ("name the next QOTD theme", a
  custom role, a pinned shoutout, whatever). The bot runs the bidding; the mod
  **fulfils the prize by hand** at close. Maximum flexibility, no new
  cosmetic-projection code, and it mirrors the bounty board's freeform tasks.
- **Bidding: ascending open, live.** The current high bid is public. Each new
  bid must beat it by at least the minimum increment. The moment a higher bid
  lands, the previous high bidder is **refunded in full**. A **soft close**
  (anti-snipe) extends the deadline when a bid arrives in the final window, so
  the auction isn't won by whoever happens to be awake at the timer.
- **Cadence: mod-started one-offs.** A mod opens an auction when they want one,
  with a duration; it closes on its timer. No queue, no auto-schedule. (Trade-
  off noted in the review: like the games programme, this only happens when a
  mod runs one. Acceptable for v1; a scheduled cadence can come later.)

## The money model (the part that must be exactly right)

**Escrow at bid time.** A bid immediately debits the bidder (`apply_debit`,
kind `auction_bid`). The coins leave the wallet the instant you bid — so a
member can never bid money they don't have, and the winner is *already charged*
by the time the auction closes (close has no debit that could fail).

**Outbid = instant full refund.** When a higher bid lands, the previous high
bidder is credited back their exact escrowed amount (`apply_credit`, kind
`auction_refund`). Net zero for everyone who gets outbid.

**Close = the winning bid is simply never refunded → burned.** The winner's
escrow is already gone from their wallet and is *not* returned; there is no
counterparty credit, so the coins are destroyed. That is the sink. A mod-curated
prize is granted out-of-band, so no currency flows back in. If nobody bid, there
is no winner and no burn.

**Cancel = refund the standing high bidder,** no burn. Mods can cancel an open
auction (mistake, prize fell through); the one escrowed bid is returned.

This makes the auction the bounty's sibling: bounty escrows *many* contributions
into a pot and refunds all on cancel; an auction escrows exactly *one* live bid
at a time and refunds the loser on every outbid. Both lean on `apply_debit` /
`apply_credit` and an exactly-once refund guard.

### The one hard concurrency case

Two bids racing. The high-bid transition must be atomic: a new bid wins the
"current high" slot **only if it still strictly beats the stored high** at
commit time. SQLite serialises writes, so the guard is a conditional update
inside one transaction:

1. `BEGIN IMMEDIATE`
2. Re-read `high_bid`, `high_bidder_id`, `state`, `ends_at` **inside the txn**.
3. Reject if closed, if bidder is already the high bidder, or if
   `new < max(min_bid, high_bid + min_increment)`.
4. `apply_debit(new_bidder, new_amount, 'auction_bid')` — escrow. Raises on
   insufficient balance → the whole txn rolls back, no state change.
5. If a previous high bidder exists:
   `apply_credit(prev, prev_amount, 'auction_refund')` and mark their bid row
   `refunded`.
6. Insert the new bid row (`escrowed`); set `high_bid` / `high_bidder_id`.
7. Soft close: if `ends_at - now < soft_close_seconds`, set
   `ends_at = now + soft_close_seconds`.
8. `COMMIT`.

A losing racer re-reads the now-higher `high_bid` in step 3 and is rejected
before any debit — it never escrows, so there is nothing to refund.

## Schema (one migration)

```
econ_auctions
  id, guild_id, channel_id, message_id,
  title, description, created_by,
  state          TEXT  -- 'open' | 'closed' | 'cancelled'
  min_bid, min_increment, soft_close_seconds,
  ends_at, created_at, closed_at,
  high_bid, high_bidder_id,     -- current standing bid (null until first)
  winner_id, winning_bid        -- set at close (= high_* frozen)

econ_auction_bids
  id, auction_id, user_id, amount, created_at,
  state          TEXT  -- 'escrowed' | 'refunded' | 'won'
```

Standing high bid lives on the auction row (one read, no aggregation). The bids
table is the audit trail and the refund ledger — exactly one row per auction is
`escrowed`/`won` at any time; the rest are `refunded`.

## Config (dashboard, EconSettings)

- `auction_min_bid` (default 10) — opening floor.
- `auction_min_increment` (default 5) — each bid must beat the high by this.
- `auction_soft_close_seconds` (default 300) — a bid inside this window of the
  end pushes the end out by this much.
- `auction_max_duration_hours` (guard-rail on what a mod can set).

Naturally dark: no auctions exist until a mod opens one, so nothing needs a
kill-switch flag — the feature is off by absence, like bounties.

## Surfaces

- **Open/cancel/close-now: mod action, in Discord.** `/bank auction start`,
  `/bank auction cancel`, `/bank auction end` — a nested subgroup under the
  existing `/bank` group (alongside `/bank quests`, `/bank shop`). A mod action,
  not admin config, so it lives in Discord (the `/qotd post` precedent) rather
  than the dashboard, and spontaneity was the whole reason for one-offs.
  Manager-role gated.
- **Bid: member self-service.** A sticky auction card in the channel with a
  **Bid** button → modal for the amount (persistent view, `custom_id` keyed by
  auction id). The card repaints live on each bid: current bid, bidder, time
  left, bid history count. Ping allow-listing per the embed style guide.
  **Concurrency requirement (from the Stage 0 code review):** the bid handler
  MUST run `place_bid` under a `BEGIN IMMEDIATE` transaction (not the default
  DEFERRED `open_db`), so concurrent bidders serialize on the write lock up
  front instead of colliding on `SQLITE_BUSY_SNAPSHOT`. Also wrap the bid in a
  `sqlite3.OperationalError` catch that maps to the same graceful "someone just
  outbid you — try again" retry the service's CAS branch intends. Money is
  already safe without this (the transaction rolls back), but without it a
  genuinely concurrent second bidder sees a raw error instead of the retry. See
  the concurrency caveat in `economy_auction_service.py`'s module docstring.
- **Close:** a background settle pass (the drops-loop / economy-loop pattern)
  finds auctions past `ends_at` and settles them exactly once — freeze
  `winner_id`/`winning_bid`, flip the winning bid row to `won`, repaint the card
  as ended, and post/DM a result that pings the mod (fulfil the prize) and the
  winner. No bid → closes with "no winner", no burn.
- **Dashboard:** an Auctions section under Economy showing live + past auctions
  (read-only history + the sink total they've burned), and the three config
  knobs on the Settings/Sinks page. Statistics gains "coins burned via auctions".

## Stages (each ships with tests in the same commit)

0. **Schema + service core — BUILT 2026-07-23.** Migration 117;
   `economy_auction_service.py` with `open_auction`, `place_bid`/`place_bid_now`,
   `cancel_auction`, `end_auction_now`, `settle_due_auctions`. Pure-DB. 29 tests
   hammer the money model: escrow, outbid refund, the CAS guard, insufficient-
   balance abort, winning-bid burn, cancel refund, exactly-once settle.
1. **Cog + panel — BUILT 2026-07-23.** `/bank auction start|cancel|end` under the
   `bank` group; `economy/auction_views.py` owns the sticky card, the Bid modal,
   the persistent `AuctionBidButton`, live repaint, and the settle→announce
   (winner + host ping, outbid/winner DMs). Bids run through `place_bid_now`
   (BEGIN IMMEDIATE + retry — the M1 concurrency fix). Card renderer unit-tested.
2. **Settle loop — BUILT 2026-07-23.** `EconomyCog._auction_settle_loop`
   (`@tasks.loop(seconds=30)`) closes auctions past `ends_at` per guild and
   announces each, so an auction ends on time with no mod present.
3. **Dashboard — TODO.** Auctions history panel + the config knobs
   (`auction_min_bid` / `min_increment` / `soft_close_seconds` /
   `max_duration_hours` aren't on the Sinks page yet) + the Statistics burn line.
4. **Docs — BUILT 2026-07-23** (this stage): economy_spec.md §, README slash
   reference, manual.html Help. INDEX.md classification + embed-style conformance
   pass still TODO.

## Open defaults (sensible unless you say otherwise)

- **Self-outbid blocked:** if you're already the high bidder you can't bid again
  (nothing to gain, and it would burn extra on a win). Raising your own bid is
  pointless in an ascending auction.
- **Duration:** mod sets it at `/auction start` (e.g. `duration:48h`), clamped
  to `auction_max_duration_hours`.
- **A "mark fulfilled" button** on the result card (mod taps it once they've
  granted the prize) — nice-to-have, deferred past v1 unless wanted.
- **One live auction per guild at a time** (DECIDED) — keeps the sticky card and
  the mental model simple; `open_auction` rejects while another is `open`.
  Multiple concurrent auctions can come later.
