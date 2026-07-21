# External game-bot economy sources (Gamebot CAH + Cat Bot)

Turn the existing raw external-message collector (`/games track`, migration
056, "stage 1") into economy payouts, and generalise it so more than one
external bot can be tracked per guild.

Locked with Billy (2026-07-21):

- **Multiple watches per guild.** The one-bot-per-guild `games_external_watch`
  becomes multi-row, each carrying a `kind` (`gamebot_cah` | `catbot`) that
  selects the parser. So Gamebot **and** Cat Bot can be tracked at once.
- **Reuse `party_game` + `game_win`.** External results fire the same economy
  triggers native party games do — participation pays `party_game`, a win pays
  `game_win` — so they feed existing quests with no new faucet config. Cat Bot
  catches use a new `cat_catch` trigger (tiered by rarity, amounts TBD).

## Stages

**Stage 1 — foundation (this stage).** Migration 097 rebuilds
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

**Stage 3 — Cat Bot parser + payout (#65).** Needs a real Cat Bot sample
(captured via Stage 1). Catch signal: a Cat Bot message that mentions the
catcher (`<@id>`) with the catch embed; rarity read from the embed (22 types:
Fine…eGirl). New `cat_catch` trigger, tiered by rarity (amounts TBD with
Billy). Keyed on the catch message id.

## Notes

- This worktree is behind main (main has migrations 091–096); 097 is safe and
  applies cleanly on merge. Reconcile with main before merging.
- Payout is best-effort and idempotent — same guarantees as the native game
  faucet (`fire_member_trigger`): economy-off / bot / unresolvable members are
  skipped, failures logged not raised.
