# External game-bot economy sources (Gamebot CAH + Cat Bot)

Turn the existing raw external-message collector (`/games track`, migration
056, "stage 1") into economy payouts, and generalise it so more than one
external bot can be tracked per guild.

Locked with Billy:

- **Multiple watches per guild.** The one-bot-per-guild `games_external_watch`
  becomes multi-row, each carrying a `kind` (`gamebot_cah` | `catbot`) that
  selects the parser. So Gamebot **and** Cat Bot can be tracked at once.
- **Reuse `party_game` + `game_win`.** External results fire the same economy
  triggers native party games do — participation pays `party_game`, a win pays
  `game_win` — so they feed existing quests with no new faucet config. Cat Bot
  catches use a new `cat_catch` trigger (tiered by rarity, amounts TBD).

## Stages

**Stage 1 — foundation. SHIPPED** (migration 097 merged). Migration 097 rebuilds
`games_external_watch` with an `id` PK, a `kind` column (default `gamebot_cah`
for the existing row), and `UNIQUE(guild_id, bot_user_id)`. `logic.py` gains
multi-watch helpers (`list_watches`, `get_watch_for_bot`, per-bot enable). The
cog's `/games track watch` gains a `kind` choice; `status` lists every watch;
`disable`/`enable`/`sample` take an optional bot to disambiguate. The collector
cache becomes `guild_id -> {bot_user_id: (channel_id, kind)}`. No parsing yet —
this is purely the shared plumbing, and it's what lets a mod run
`/games track watch #cat-bot @Cat Bot kind:catbot` and then `/games track
sample` to capture a **real Cat Bot catch** for Stage 3.

**Stage 2 — Gamebot CAH parser + payout (#70). SHIPPED.** A `parser.py` keyed on
`kind`. For `gamebot_cah`, from the confirmed sample:
- roster = union of member mentions in *Current Standings* (`<@id>: N`) and
  *Submission status* (`✅ <@id> Submitted!`) embeds of the game;
- winner = the *Game over!* embed's `<@id> is the winner!`.
A game is bounded by its *Game over!* message; the parser walks back to the
latest *Current Standings* for the roster. On an unparsed *Game over!* message,
fire `party_game` for every roster member and `game_win` for the winner, keyed
on the Game-over message id (via `parse_status` / trigger occurrence) so a
re-parse or restart never double-pays. `parse_status` marks each message
`ok`/`skip`/`error`.

**Stage 3 — Cat Bot parser + payout (#65). SHIPPED.** Real format (from 33
banked messages, not embeds): catches are message *content*
`{username} cought <:raritycat:id> {Rarity} cat`. The catcher is a plain
Discord **username** (not a mention) — resolved to a member via
`guild.get_member_named`; unresolved (left/renamed) pay nobody. Rarity from the
emoji name; reverse cats print the line reversed but keep the emoji intact, so
the catcher is the non-emoji token beside "cought". "blessed…got doubled" →
×2. Tiers (tapered 2026-07-21 — a 75%→0% linear cut from the bottom tier to the
top, off an earlier flat 3/8/20/50/120/300): common 1, uncommon 3, rare 11, epic
35, mythic 102, divine 300 (the 22 types grouped in `parser._RARITY_TIER`). `pay_cat_catch`
credits the tiered coins (`apply_credit` kind `cat_catch`, booster-multiplied)
and fires the new `cat_catch` trigger. Once per catch via the payout ledger.

## Notes

- This worktree is behind main (main has migrations 091–096); 097 is safe and
  applies cleanly on merge. Reconcile with main before merging.
- Payout is best-effort and idempotent — same guarantees as the native game
  faucet (`fire_member_trigger`): economy-off / bot / unresolvable members are
  skipped, failures logged not raised.
