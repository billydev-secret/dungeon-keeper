# External game-bot economy sources (Gamebot CAH + Connect 4, + Cat Bot)

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
`guild.get_member_named`; unresolved (left/renamed) pay nobody. The printed name
is markdown-**escaped** (`tryingnewthingz\_0504`), in both word orders, so the
parser unescapes before resolving — left in, every username containing an
underscore silently paid nobody (fixed 2026-07-25; 213 catches missed). Rarity from the
emoji name; reverse cats print the line reversed but keep the emoji intact, so
the catcher is the non-emoji token beside "cought". "blessed…got doubled" →
×2. Tiers (tapered 2026-07-21 — a 75%→0% linear cut from the bottom tier to the
top, off an earlier flat 3/8/20/50/120/300): common 1, uncommon 3, rare 11, epic
35, mythic 102, divine 300 (the 22 types grouped in `parser._RARITY_TIER`). `pay_cat_catch`
credits the tiered coins (`apply_credit` kind `cat_catch`, booster-multiplied)
and fires the new `cat_catch` trigger. Once per catch via the payout ledger.

**Stage 4 — CAH score-proportional payout (2026-07-24). SHIPPED.** Replaced the
flat participation/win payout for `gamebot_cah` with `pay_cah_game_by_score`:
the *Game over!* winner (top scorer) earns `EconSettings.reward_cah_win_max`
(default 50, dashboard-configurable on Income Sources) and everyone else earns
that cap scaled by their score's ratio to the winner's, rounded to the nearest
coin (a share that rounds to 0 pays nothing). `party_game`/`game_win` quest
triggers still fire for the same roster/winner — only the direct coin amount
changed. Required teaching the parser real scores: `extract_cah_game` now
returns `{member_id: score}` instead of a bare roster set, reading the *last*
Current Standings embed in the window (each is a full cumulative snapshot, so
later ones supersede earlier ones) and folding in submission-only/winner-only
players at 0.

**Stage 5 — Connect 4 alongside CAH, `gamebot_cah` renamed to `gamebot`
(2026-07-25). SHIPPED.** Discovered the watched CAH channel *also* hosts
Gamebot's Connect 4 (same bot account, same channel — `&play c4`). Both games
end in a title-only-identical *Game over!* embed, and `UNIQUE(guild_id,
bot_user_id)` means the same bot can't hold two separate watch rows, so the
`kind` value had to broaden rather than add a sibling: `gamebot_cah` →
`gamebot` (safe rename — no production row carried the old value). The cog's
dispatch now checks `parser.is_game_over` (CAH's specific "is the winner"
phrasing) first, and treats any other *Game over!* as Connect 4 — the only
other sub-game tracked so far; a third Gamebot game needs its own check
instead of that fallback. Window-bounding (`current_game_window`) moved from
CAH-only `is_game_over` to the new title-only `is_terminal`, so back-to-back
games of *either* type don't bleed rosters into each other.

Connect 4 has no per-round score (win/lose only), so `_pay_connect4_game`
reuses the flat `pay_game_rewards` faucet exactly like the original
(pre-score-payout) CAH design — participation to the roster (from the start
embed's **Joined Players** field / *Time's up!* recap), a win bonus to the
`<@id> has won!` winner. A draw's exact wording is unconfirmed (no real sample
yet in the banked history) — an unrecognised finish just pays participation,
no winner, same safe default as CAH's own no-winner case.

**Stage 6 — Anagrams, per-channel watches, and the history replay
(2026-07-26). SHIPPED.** Auditing the 22 unpaid games in the banked history
turned up two live mis-classification bugs and one hard cap:

- **Anagrams was being paid as CAH.** Gamebot's third sub-game ends with the
  *identical* `<@id> is the winner!` wording CAH uses, so `is_game_over`
  claimed it and `extract_cah_game` — which looks for *Current Standings* —
  found nothing, paying the winner alone at score 0 and the other players
  nothing. 3 games in the history. Anagrams keeps its scores in a *Scoreboard*
  embed's **field names** (`"efficientpanic - 900 POINTS"`), by **username**
  rather than mention, so `extract_anagrams_game` + `resolve_named_scores`
  (the `get_member_named` lookup Cat Bot already uses) feed
  `pay_cah_game_by_score` with `game_key="anagrams"`.
- **Abandoned lobbies were being paid as Connect 4.** `Not enough players
  joined the game!` still gets a *Game over!*, and the roster reader picked up
  the **Joined Players** field off *any* embed — so an abandoned CAH lobby fed
  the Connect 4 payout. 2 games in the history. Both reads are now gated on
  their embed title (`players_from_join_phase`, shared by all sub-games since
  the join phase is identical) and `is_abandoned` skips the game outright.
- **The root cause of both:** identifying a sub-game from its *terminal*
  message. Fixed by anchoring `current_game_window` on the game's own **lobby
  embed** and reading the type off it (`identify_game`). A lobby for a game we
  don't parse (Chess, Poker, …) returns `None` and pays nobody rather than
  falling through to a wrong guess. Verified against all 23 terminals in the
  banked history: every game now classifies correctly, and all 16 real CAH
  games — including the one paid live on 2026-07-26 — reproduce byte-identical
  scores and winners to the previous parser.

Separately, **migration 135** widens the watch key from `UNIQUE(guild_id,
bot_user_id)` to `UNIQUE(guild_id, bot_user_id, channel_id)` and the collector
cache from `{bot: (channel, kind)}` to `{(bot, channel): kind}`. A bot was
previously capped at one channel per guild and every game it ran elsewhere was
silently dropped. Nothing else was needed for concurrency: each payout is
already a pure function of its own channel's banked history, so games running
simultaneously in different channels are just independent backward scans, with
no in-flight state anywhere.

`scripts/replay_gamebot_games.py` replays the backlog through those same
parser functions — dry run by default, `--apply` to write, claim-before-credit
so the already-paid game is skipped structurally, and quest triggers on each
game's own local day (the `backfill_cat_catches.py` shape). On the 2026-07-26
history: 15 CAH + 3 Anagrams + 2 Connect 4 paid, 2 abandoned lobbies claimed
but paying nobody, 2,526 coins across 24 members.

## Notes

- This worktree is behind main (main has migrations 091–096); 097 is safe and
  applies cleanly on merge. Reconcile with main before merging.
- Payout is best-effort and idempotent — same guarantees as the native game
  faucet (`fire_member_trigger`): economy-off / bot / unresolvable members are
  skipped, failures logged not raised.
