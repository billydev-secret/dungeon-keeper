# Pin of the Day — paid, mod-approved daily pinned message

A member pays coins to pin a short message; a mod approves it; the bot posts a
"📌 Pinned by @X" card in a configured pin channel and pins it for 24h, then
auto-unpins. A public, expressive sink — the first perk whose payoff everyone
sees (cf. the theme-of-the-day idea). Modelled almost 1:1 on **sponsor-a-QOTD**
(`economy_qotd_sponsor_service` + `sponsor_views` + the loop expiry sweep).

## Locked decisions

- **Mod-approved** (user's call): submit → `pending` → a mod Approves/Declines on
  a card posted to the bank channel (persistent buttons, same as sponsor). Deny
  refunds.
- **Charged at submit** (a free queue invites spam). Deny and *pending*-expiry
  refund; a pin that actually went live does **not** refund (they got their time).
- **24h lifetime from go-live**, swept off on the hourly economy tick (unpin +
  delete the card). Not calendar-locked, so an 11pm approval still gets a full day.
- **One live pin per guild** (partial unique index). Approving a new one while one
  is live **replaces** it (old unpinned early) — mod-paced, so no member can bump
  another; a mod simply won't approve two the same day in a quiet server.
- **One in-flight submission per member** (`pending`|`live`), partial unique index —
  can't buy ten slots to spam the queue.
- **Enabled when** `price_pin_of_day > 0` **and** `pin_channel_id` is set (dark by
  default, like the other channel-gated sinks — announce before flipping on).
- Input via a `/bank pin` **modal** (paragraph, multiline). Length 1–280.
- Sponsor credited by name on the live card (buying a *public* thing = opting into
  being named, same carve-out as the raffle/theme).
- If the pin can't be posted/pinned (missing channel or perms), the approval
  **refunds** and tells the mod — the member is never charged for a pin nobody saw.

## Pieces

- Migration `108_econ_pin_of_day.sql`: `econ_pin_submissions` (+ state machine,
  one-open-per-member and one-live-per-guild partial indexes).
- `economy_pin_service.py`: submit / resolve / go_live (+ supersede prior live) /
  expire_live / expire_stale_pending / refund — pure DB, exactly-once refunds.
- `economy/pin_views.py`: approval card + persistent Approve/Decline buttons, the
  live pin card, `_handle_resolution` (approve → go live + pin; deny → refund),
  `post_review_card`, resolution DM.
- Cog `/bank pin` (modal) + persistent-view registration in `setup_hook`.
- `economy_loop.run_pin_expiry` on the hourly tick (unpin expired live after
  commit; refund + DM stale pending).
- Settings `price_pin_of_day`, `pin_channel_id`, `pin_expire_days`; register ledger
  kinds `pin_sponsor` / `pin_sponsor_refund`; dashboard price (Sinks) + channel
  picker (Economy config).
- Tests: service state machine + money matrix, loop sweep, cog guardrails.
- Docs: manual.html, README, economy_spec.
