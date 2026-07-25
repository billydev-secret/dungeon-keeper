# Casino ephemeral-play UX — plan

**Status: built 2026-07-24 (single stage).** Spec:
[../casino_spec.md](../casino_spec.md).

## Why

Live observation of TGM's casino channel (2026-07-24): one player grinding
"Spin again" produced a slots **result message every ~2 seconds** — each a
new public message with its own Play Again button. Every message armed the
hub restick, so the 7-button hub panel was perpetually deleted + reposted
at the bottom, and Discord's pinned-to-bottom auto-scroll dragged every
active UI element (live blackjack hands, open roulette/derby betting
windows) up-screen while members were mid-click. The root problem: per-play
results and per-player controls shared one scrolling surface with everyone
else's spam.

## Design: private play, public moments

1. **Instant games go ephemeral, editing in place.** Coinflip, slots and
   blackjack render in an ephemeral message per player (`_respond_private`:
   a press on your own ephemeral machine — Play Again, the coinflip picker
   — edits it in place via `response.edit_message`; any other origin opens
   a fresh ephemeral). Animation frames use `edit_original_response`
   (interaction webhook — not the channel edit bucket). Play UI physically
   cannot move, and plays add zero channel messages.
2. **The hub panel grows a floor ticker** (`📡 On the floor`) so the
   channel still feels alive: `casino_ticker` (migration 128) gets one row
   per resolved instant play inside `record_play`'s settlement transaction
   (communal games stay off — their recaps are already public), trimmed to
   `TICKER_KEEP` per guild. A per-guild 8s debounced repaint coalesces a
   burst of plays into one in-place hub edit.
3. **Broadcast moments**: jackpot celebrations (unchanged, always public)
   plus any instant-game win paying ≥ `broadcast_min_payout` (new setting,
   0 = off, dashboard **Economy → Casino**) — the result embed is reposted
   publicly with its Play Again button, keeping the "me too" invitation
   for wins worth advertising.
4. **Restick holds during communal rounds**: the 60s restick debounce
   re-checks while a roulette/derby round is open in the channel and only
   fires once no round is live (or a 5-minute cap expires) — the panel
   never jumps under members who are mid-bet.

## Consequences handled

- **Blackjack auto-stand** edits an ephemeral message → only its
  interaction webhook can do that, and tokens die at 15 min. The cog keeps
  `hand_id → (followup, message_id, stored_at)` in memory (swept past the
  TTL); the settle never depends on the edit; `blackjack_idle_seconds` is
  now dashboard-capped at 840s.
- **Boot refunds** can no longer edit the orphaned hand message (token died
  with the process) — the register feed's `casino_refund` entry is the
  notice; stale buttons answer "already finished".
- **Rejected alternative**: keeping results public but editing one anchored
  message per player session — preserves the spectacle but fights the
  shared per-channel edit rate bucket as soon as 2–3 members grind
  simultaneously.
