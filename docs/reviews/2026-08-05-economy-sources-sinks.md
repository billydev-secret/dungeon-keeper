# Economy sources + sinks/shop — battery review, 2026-08-05

Bundle: drops, quests(+quest_ai), bounties, qotd_sponsor, pins, photo,
raffle, wager (sources); rentals, auction, emoji, icon_catalog,
stats/metrics (sinks); ~10 economy panels + economy_manager routes.
Game-balance ground covered by `2026-07-25-economy-casino-sources-sinks.md`
and loose-ends §1 — not re-reviewed.

## Architecture

- All sources/sinks mutate balances exclusively through
  `apply_credit`/`apply_debit` (proved by the funnel check in
  economy-core — no other module writes `econ_wallets`). ✓
- Manager routes uniformly gated by `require_economy_manager`
  (admin OR manager role), documented at the top of the file. ✓
- `econ_photo_cards` stores message ids + prompt only — no image bytes or
  URLs ✓. `econ_emoji_submissions` stores an `image_path` on disk but the
  table is empty in prod; the service docstring documents the
  deny-refund/orphan-delete lifecycle. Fine.

## GDPR — one significant discovery

- **G1 — Anthropic API is a live cloud processor** (`games/utils/
  ai_client.py`, `anthropic` in requirements). Callers:
  1. `economy_manager.py:317` — quest **idea generation** (admin-triggered;
     theme text only; explicitly "nothing is persisted") — low impact;
  2. `games.py:1074` — party-game AI prep (host-triggered game material)
     — low impact;
  3. **`advisor_service.py` — the Billy-bot assistant sends member/staff
     questions plus built guild context to Anthropic** (Haiku for members,
     Sonnet for staff, per-guild configurable). This is member-authored
     text leaving the box.
  None of this is wrong — it's the assistant feature working as designed —
  but the register's processor column has claimed "all local" until now.
  Actions: (a) register rows updated; (b) the advisor bundle (W4) must
  document exactly what `build_system(guild_context)` includes (config
  only, or member names/messages?); (c) the server's privacy notice should
  name Anthropic as a processor for advisor questions — same transparency
  class as health-analytics G1. **Priority: medium.**
- G2 — quest/bounty/raffle/wager tables: per-member state, all covered by
  the economy-core G1 `econ_purge_user` recommendation. No new decisions.

## UX / Docs

- No new findings; panels are manager-gated and prior UX review (07-22)
  covered the economy dashboard family. A2 (silent renewals) from
  economy-core remains the actionable UX item for this half.

## Verdict

Sources/sinks inherit the core's soundness. The finding that matters is
G1: the processor inventory now includes Anthropic via the advisor — carry
into the W4 advisor bundle and the synthesis privacy-notice recommendation.
