# Clapback — functional spec

**Status: Reference** — matches current behavior as of 2026-09-04.

Head-to-head comedy party game. Everyone answers the same prompt, answers are
bracketed into one-on-one matchups, and the room votes the funnier one.

Cog: `src/bot_modules/cogs/games_clapback_cog.py` (thin — Discord glue only).
Decision logic: `src/bot_modules/games_clapback/logic.py`.
Embeds: `src/bot_modules/games_clapback/embeds.py`.
Tests: `tests/test_games_clapback_logic.py`.

Clapback is one of the `/games play <slug>` party games; see
[games_system_spec.md](games_system_spec.md) for the shared lobby, prompt-bank,
economy-quest, and failure-mode behavior that every game in the suite inherits.
This doc covers what is specific to Clapback — chiefly **how the bracket is
built and scored**, which is the part with real rules in it.

---

## 1. Launch and configuration

`/games play clapback [start_in:1-60]` — open to everyone. `start_in` posts a
lobby countdown, and **the game starts itself when it runs out** (clapback-8,
2026-09-04) provided at least `MIN_PLAYERS` have joined: the start-ping sweep
(`game_start_ping_service`, polling every 15 s) calls `ClapbackCog.auto_start`
— registered in `bot.lobby_auto_starters["clapback"]` — which re-reads the
roster, applies Start's own gates (the no-contact `playable_players` floor and
`MAX_PLAYERS`), stops the lobby view and greys its buttons as a press would,
runs the shared `_begin_game` (scoreboard seed, `joining` → `playing`) and
spawns `_play` (the game loop with its crash archive) as a background task.
The lobby's **⏰ Starting** field says so (`<t:…:R> — on its own, once 3 have
joined`). Short of three at the moment, the host is nudged once with what the
lobby is waiting on, and the game still starts itself the tick a third joins;
with fewer than three the idle-lobby close applies from the advertised start.
A refusal from the gates (a no-contact pair leaving fewer than three
playable) falls back to the old "time to start" nudge and the host's press gets
the ordinary refusal line. Without `start_in` nothing changes: the host
presses **Start**. A **scheduled** Clapback is stamped `start_in:10` when its
schedule names none, so it runs with nobody at the keyboard — before this a
scheduled Clapback could be started only by the schedule's creator or a mod
(clapback-8; 3 of 30 rows in 30 days were timed-out lobbies).

**The Game Night ping** (clapback-11 / discovery-2, decision D4). The lobby
message itself still carries no mention; the platform sweep posts one
`content=` line mentioning the guild's opt-in **Game Night** role
(`feature_roles.GAME_NIGHT_PING`, dial on Games Global Config, made on first
use) with a jump link to the lobby and its start time, the first tick the
lobby's `message_id` exists. Shared by every lobby game — see
[games_system_spec.md](games_system_spec.md), *The Game Night ping*.

Clapback is **bank-only**: it never falls back to AI prompt generation, so an
empty question bank means the run is skipped rather than improvised. Bank
lookups are NSFW-gated on `channel_allows_nsfw(channel)` — Discord's own
channel age-gate, never a bot-side toggle.

Config comes from the dashboard game options (with slash/scheduler overrides),
clamped by `logic.clamp_config_values`:

| Option | Default | Range |
|---|---|---|
| `rounds` | 5 | 1–15 |
| `timer` (submit window, seconds) | 120 | 15–180 |
| `vote_timer` (per matchup, seconds) | 40 | 10–60 |
| `anonymous` | false | hides author names on the reveal |
| `tags` | — | restricts the bank draw |

Player bounds are `MIN_PLAYERS = 3` / `MAX_PLAYERS = 16`. **Start** counts
the roster through `logic.playable_players` — anyone the no-contact list keeps
apart from every other player can never be seated, so they don't count — and
refuses with the ordinary "Need at least 3 players to start Clapback.
Currently: *n*." line (roster count) when that leaves fewer than three. See
§3.4.

**Three is a thin game, and the lobby says so** (clapback-7). With three
players and no spectators the only eligible voter in every matchup is the
third player, so each is decided 100/0 on one click and CLAPBACK / Best Single
Answer are unreachable unless someone watching votes. The lobby embed carries
`logic.THREE_PLAYER_NOTE` ("⚖️ 3 will play — each matchup is judged by the one
player not in it …") at exactly three joined, and Start posts the same line to
the channel when it starts three. No scoring change — the fuller option (a
3-way ballot per round) is in §6.

### 1.1 The lobby

Join / Leave / Start / ❓ Help / **Cancel**. Cancel is host-or-mod
(`is_host_or_mod`), behind the shared `ConfirmCloseView` popup like every
other Close/End path, and runs `_cancel_game(reason="cancelled")` then retires
the lobby message ("🛑 **Lobby cancelled** by the host …", buttons disabled)
the way the inactivity timeout does. The host's Leave reply points at it
("You're the host! Press **Cancel** to close the lobby instead.") — until
2026-09-04 that reply named a Cancel button that did not exist (clapback-12).

`_start_new_game(…, players=None)` takes an optional roster seed. The recap's
**🔁 Play Again** / **🔀 Play Again (Shuffled)** pass the finished game's
roster — leavers are already off it (§2.3) — so the rematch lobby opens with
everyone seated and the host still presses Start (clapback-10; 8 of 27 real
games began within three minutes of the previous one). The seed is
de-duplicated and capped at `MAX_PLAYERS`, and the finished game's
`start_epoch` is dropped from the carried config — a seeded roster plus a
spent countdown would otherwise have the sweep auto-start the rematch on its
next tick. The new lobby's `allow_nsfw` is
**re-read from the channel** (`channel_allows_nsfw`) rather than carried from
the finished game's config, so a rematch after the room's age-restriction
changed draws from the right bank (safety-sweep-10).

The recap view times out after **600 s** (matching the lobby's inactivity
window) and `on_timeout` disables its buttons and edits the message
(`view.message` is kept after send), so Play Again never sits looking live
over a dead view (clapback-13).

## 2. Round flow

1. Latecomers queued during the previous round are admitted — see §2.2.
2. The round's bye is picked **before** the prompt goes out — see §2.1.
3. Prompt is drawn from the bank and posted; players submit via an ephemeral
   modal (resubmitting before the timer overwrites the previous answer). The
   window closes early on `logic.submit_window_may_close`: a full house, or
   **one answer short with nothing changed for `SUBMIT_IDLE_CLOSE_SECONDS`
   (20 s)** — every real round that ran its whole window did so because one
   writer had stepped away, and paid them nothing anyway (clapback-2). Never
   below `MIN_ANSWERS` (2), which would only skip the round. The panel also
   carries **🔒 Close answers** for the host or a mod (same gate as Next
   Round), refused below two answers ("❌ Only *n* answer(s) in — at least 2
   are needed to run the round.").
   The phase is set to `bracketing` **before** the answers are read, and the
   modal's write goes through `logic.accept_answer(payload, round_num)` inside
   the write lock: an answer is stored only while the phase is `submitting`
   and `current_round` is the round the modal was opened for; otherwise
   nothing is written and the reply is "❌ Answers for round *N* are closed."
   Discord keeps a modal open indefinitely, and until 2026-09-04 a late one
   was written anyway — lost if the round had bracketed, or filed as the
   *next* round's entry if that prompt had posted (clapback-4).
4. Submitted answers are bracketed — see §3.
5. Each matchup is voted on **sequentially**, up to `vote_timer` seconds each.
   Contestants cannot vote on their own matchup. Each vote button carries the
   answer text (`logic.vote_button_label`), not a bare 🅰️/🅱️ emoji.
   **A matchup closes once every eligible player has voted** —
   `logic.all_eligible_voted`: the roster minus the two contestants minus a
   bye who has not voted (a bye may vote and is then counted, but is never
   waited for) — after a `VOTE_CLOSE_GRACE_SECONDS` (5 s) grace for a
   spectator mid-click. A vote from **outside the roster** reopens the
   electorate and the full timer runs, as it does when **nobody** is
   eligible (a 3-player game whose third player withdrew, or whose bye is a
   no-contact bench — §2.3): an empty electorate is not a finished one.
   > **Decision D1, 2026-09-04 — reverses ab27201b (June).** When voting was
   > opened to spectators the loop lost its early exit on purpose, since
   > "everyone eligible has voted" was no longer knowable. Prod data since:
   > spectators vote in 2–12% of matchups, and roughly half of a game was
   > fixed waiting — an all-eligible close would have ended 55–59% of
   > matchups early at 5–6 players (clapback-1). The June trade-off is now
   > the exception (a spectator vote keeps the timer) rather than the rule.
6. Each matchup's reveal shows the split; then the round scoreboard.

A round with fewer than 2 answers is skipped entirely ("Not enough answers this
round — moving on!"). Only players who actually submitted are in that round's
bracket; a missed submit window is not scored (but see §3.2 — it can still
force a second bye).

### 2.1 The bye is chosen up front (`logic.pick_round_bye`)

`pick_round_bye(player_ids, bye_history, rng, forbidden_pairs)` runs against
the **roster** before the prompt posts, and returns `None` for an even field or
exactly 3 players (§3.1) — unless the no-contact gate needs a bye (§3.4). The benched player is then left out of the round-start ping and
the `Answers In` denominator, is named on the submit embed and in the ping, and
their Submit button refuses with an explanation. `round_bye` is stored on the
payload so the button gate survives a reload.

Why: the bye used to fall out of `create_matchups`, i.e. *after* everyone had
written an answer, so the benched player composed something that was never used
and found out at the scoreboard ("It should really let you know when you're
sitting out" — game night 2026-08-21).

Both functions use the same fewest-byes-first rotation, so they agree when a
missing submitter forces a **second** bye on top of the pre-picked one. A round
can therefore hand out two byes; both are paid the round average, both are
appended to `bye_history`, and the round record carries `bye_players` (a list)
alongside the legacy singular `bye_player`.

### 2.2 Joining mid-game

The submit panel carries a **🙋 Join now** button for anyone not playing, and
`/games join` routes to the same rules. `logic.admit_player_now` decides which
of two things happens, from `payload["phase"]`:

- **Answers are open** (`submitting`) → the player is seated in *this* round
  and the answer modal opens on the same press. Nothing is fixed yet at that
  point: `create_matchups` is built from the answers dict after the window
  closes, so a latecomer with an answer in is paired like anyone else. They
  are announced in channel, because the panel's "Answers In *x*/*N*"
  denominator is re-read every tick and would otherwise jump for no visible
  reason.
- **Anything else** (voting, revealing) → queued into `pending_players` for
  the round boundary, where `logic.admit_pending_players` folds them in. There
  is nothing to write mid-vote, and the matchups on screen are already set.

A joiner is covered by the no-contact gate without any extra step: the
submitters' pairs are read after the window closes (§3.4).

**Parity** (clapback-5, 2026-09-04). The pre-picked bye (§2.1) exists so
nobody writes an answer that is never used, and a joiner who turned an even
writer count odd used to force exactly that on someone at the bracket. So
`admit_player_now` keeps the writer count even, deciding with
`pick_round_bye`'s own "needs a bye?" answer over the same `forbidden_pairs`
the round uses (the cog reads them over roster-plus-joiner before the write):

- seating the joiner leaves a field that needs no bye → `joined`;
- otherwise, when a bye was pre-picked and the whole roster plus the joiner
  pairs cleanly, the bye is **un-benched** — `round_bye` cleared so their
  Submit opens — and the channel post adds "🪑 @bye you're back in this round
  — that evens the numbers, so hit **Submit**!" (`joined-unbenched`). The
  submit loop reads `round_bye` off the payload each tick, drops the
  "Sitting out" field and re-counts; `_run_game` re-reads `round_bye` after
  the window so an un-benched player is never also paid a bye;
- otherwise → `queued-parity`, the next-round queue with a reply that says
  why ("Jumping in now would leave an odd number of writers and bench someone
  who's already written, so you're in from the **next** round …").

An un-benched bye is always pairable by construction: a player the no-contact
list keeps apart from everyone stays benched (the gate would only bench them
again after they wrote), and three players who include a pair are never read
as a clean round-robin.

Either way they start on **0 points**, seeded into `scores`, `clapbacks`,
`scores_checkpoint` *and* `clapbacks_checkpoint` — the checkpoints are what a
crash-resume rolls back to, and a joiner missing from them is rolled off the
scoreboard. Anyone over `MAX_PLAYERS` is turned away out loud rather than
silently dropped. Pressing Join during the **last** round would otherwise
queue someone for a boundary that never arrives, so the game end calls
`logic.drain_pending_players` and tells them the game is over instead of
leaving them waiting.

The button queued for the *next* round unconditionally until 2026-08-30. That
was the safe reading of a harder problem — a live round's matchups must not
shift — applied to a phase that has no matchups yet, and it made someone
watching a prompt they had a clapback for sit the round out.

### 2.3 Leaving mid-game

`/games leave` routes to `mid_game_leave`, which runs `logic.withdraw_player`:
the member comes off `players` and their id is appended to `left`. The score
is **kept but withdrawn** — `logic.board_scores` splits `scores` into
`(standing, withdrawn)`, the scoreboard and recap rank only the standing
players (the recap's `Winner` is the highest of them) and list the withdrawn
below, struck through, as "left the game" / "left mid-game; score
withdrawn". The reply says so ("… left Clapback — their score is withdrawn
from the board."). `end_game` gets the roster as it stands, so a leaver is
paid nothing, and `game_rewards._winners_clapback` skips withdrawn scores so
the game-win goes to the same player the recap crowns.

A leaver who presses **Join** again is playing again: both admission paths
(`admit_player_now` and `admit_pending_players`, which takes the payload's
`left` list and prunes it in place) take the id back off `left`, so their
old score ranks on the board once more rather than sitting struck through
below it while they play. A latecomer still queued in `pending_players` who
leaves is simply pulled from the queue — `withdraw_player` returns True,
nothing goes on `left` (they have no score), and they are not seated at the
next boundary.

A withdrawal can also empty a matchup's electorate — a 3-player game whose
third player leaves mid-vote, or a 3-player roster whose bye is a no-contact
bench. `all_eligible_voted` treats an empty electorate as *not* finished, so
the matchup runs its full timer rather than closing on zero votes after the
spectator grace (§2, step 5).

> **Changed 2026-09-04 (clapback-17, option a).** A leaver's score used to
> stay on the board, so someone who left in round 4 while leading was 🥇 on
> every later scoreboard and the recap's Winner while nobody was paid the win
> (the winner id was filtered out of the roster the faucet pays). Withdrawing
> the score matches the forfeit the payout already applied.

## 3. Bracketing (`logic.create_matchups`)

Signature: `create_matchups(answers, bye_history=None, rng=None,
forbidden_pairs=None)` → `(matchups, byes)`. `rng` is injected so tests pin the
shuffle order. `byes` is a list: at most one id in ordinary play (§3.2), more
only when the no-contact gate has to bench someone (§3.4), and empty alongside
an empty `matchups` when the round has nothing safe to vote on.

**Every submitter appears in the result exactly once** — either in one pair or
in `byes`. This is the invariant the rest of the rules work inside.

### 3.1 Three players → round-robin

With exactly 3 submitters the function returns the full round-robin (all 3
pairs, so each player competes twice). A 3-player game paired 1-vs-1 would leave
a permanent bye and one matchup per round, which isn't a game. Duplicate-answer
avoidance does not apply in this branch — the round-robin is the whole pairing.

### 3.2 Odd counts → one bye, fewest-byes-first

With an odd number of submitters (5, 7, …) one player sits the round out. In
normal play the roster's bye was already taken out before the prompt (§2.1) and
the submitters pair cleanly, so this branch only fires when someone misses the
submit window and leaves an odd count behind.

`bye_history` is every bye handed out this game, in order; the same id can
appear more than once across a long game. The bye goes to whoever among **this
round's submitters** has had the fewest so far, chosen at random within that
tied group.

Consequences, all deliberate:

- Nobody sits out twice until everyone has sat out once.
- Past that, the rule keeps cycling — round six starts a fresh lap among the
  players on one bye, rather than deadlocking or re-favouring whoever went first.
- It holds up when the submitter set changes between rounds, which happens
  whenever someone misses the submit window. This is why byes are **counted**
  rather than only remembering the previous one: a single `last_bye` pointer
  loses the rotation the moment the roster shifts, and could ping-pong the bye
  between two players in a 5-person game.

Superseded: games started before this rule carry only a `last_bye` key in their
payload. The cog seeds `bye_history` from it on crash-resume so an in-flight
game keeps rotating instead of restarting.

### 3.3 Duplicate answers

Identical answers are dull to vote between, so up to **10 shuffles** are tried
and the pairing with the fewest same-answer pairs wins. Comparison strips
whitespace and lowercases.

Every candidate pairing is **complete** — the loop never abandons a partial
bracket. When duplication is unavoidable (e.g. four identical answers among six
players, where no clean perfect matching exists) the cost is one repeated-answer
matchup, never a player dropped from the round.

> **Fixed 2026-07-27.** The previous implementation broke out of the pairing
> loop on the first duplicate and kept the *partial* list built so far. With six
> players and four identical answers this silently ran a single matchup and left
> four players out of the round — roughly half of all shuffles. Regression test:
> `test_create_matchups_never_drops_a_player_when_dupes_are_unavoidable`.

### 3.4 The no-contact gate

A matchup puts two answers side by side under two names on the vote and reveal
cards, so it is a contact surface under [no_contact_spec.md](no_contact_spec.md).
The cog reads the no-contact pairs among the people in play
(`no_contact_pairs_among`, in a thread) **at each use** — over the roster before
`pick_round_bye`, over the submitters before `create_matchups` — rather than once
per game: the service keeps no cache on purpose, a stale read fails toward
seating the pair, and reading at the second point is also what covers a **Join
now** joiner with no bookkeeping. Both functions take the set as
`forbidden_pairs` (any id type, either way round) and **never seat a forbidden
pair**. Everything the gate does is a bye, and a bye is paid the round average
and announced the same way whatever caused it, so nothing on screen says why.
In order:

- A submitter the list keeps apart from **every** other submitter is a bye (and
  is pre-benched at roster level, so they are never asked to write).
- **Three players who include a pair do not round-robin** — the round-robin is
  the one bracket shape that guarantees the pair meets. One of the two is the
  bye, fewest-byes-first *between them* (so it alternates across the game and
  the third player never sits), and the other two play one matchup. This is the
  known soft tell: a three-player game otherwise never has a bye.
- An odd field's rotation bye (§3.2) is the most overdue player whose absence
  still leaves a fully pairable field, so the gate never forces a second bye
  where one will do.
- The pairing is drawn by a randomised backtracking search over the same ten
  shuffles, still minimising duplicate answers (§3.3); the search is bounded
  (`MAX_PAIRING_NODES`). Only in the contrived case where no full safe pairing
  exists — a forbidden cluster dense enough that the leftovers can face nobody —
  is the largest safe pairing used and the rest benched as extra byes; that is
  the one way `byes` grows past one, and the one place the gate can force the
  same two players to meet again sooner than the shuffle otherwise would.
- A round with no safe matchup at all returns `([], [])` and the cog skips it
  with the ordinary "Not enough answers this round — moving on!" — nobody is
  paid a bye for a round that never ran.

With nothing forbidden the gate is inert and the draw is byte-for-byte the
pre-gate one (`test_create_matchups_without_forbidden_pairs_is_unchanged`).

## 4. Scoring

### 4.1 Matchups (`logic.calculate_matchup_score`)

Points are the **vote percentage**: 75% of the votes is 75 points. A unanimous
winner with **at least 2 votes** scores a **CLAPBACK** — `+25` bonus and a
tally in the recap. Both halves of that rule matter; a 1–0 result is not a
clapback.

A matchup with zero votes pays both sides 50 — the intentional "show up and
play" fallback. A tie has no winner and splits by percentage.

### 4.2 The bye (`logic.calculate_bye_award`)

The bye player is paid the **average of what everyone who actually competed
scored that round**, rounded — clapback bonuses included. Falls back to 50 if a
round somehow resolved no matchups.

Deliberately independent of the bye player's own history: a bye is a scheduling
accident, not a performance, so it should neither compound a lead nor deepen a
deficit. Pegging it to the round means it is always "a typical result for this
round" — in a round where everyone landed hard it is worth more than in a round
that bombed, which a flat number could not express.

Because the award depends on the round's results, it is **settled after the
vote loop**, not before it. The scoreboard's Bye field reports the real number;
`bye_award` is stored on the round-history record.

> **Changed 2026-07-27.** Previously a flat `+50` paid out *before* voting
> began. Fifty is a wash in an even round but arbitrary against a round where
> the field averaged 80, so a bye could quietly cost or gift a player a rank.

### 4.3 Recap

`find_best_answer_record` picks the highest vote-share matchup with **at least
3 total votes** (so a 1–0 doesn't win "best answer"), tiebroken by raw votes.
`find_closest_matchup_record` picks the smallest margin among matchups with any
votes, tiebroken by *larger* total (a 3–4 beats a 1–2). Both return the raw
record; the embed builder resolves names, keeping the logic layer Discord-free.

## 5. Persistence

Game payload keys specific to Clapback: `answers`, `matchups`, `scores`,
`scores_checkpoint`, `clapbacks`, `clapbacks_checkpoint`, `round_history`,
`used_prompts`, `bye_history`, `last_bye` (legacy, still written), `round_bye`
(this round's pre-picked bye; cleared by an un-bench, §2.2), `pending_players`
(queued latecomers), `left` (ids whose score is withdrawn, §2.3),
`current_round`, `phase` (`submitting` → `bracketing` → `voting` →
`revealing`).

`scores_checkpoint` and `clapbacks_checkpoint` snapshot scores and the
CLAPBACK tally as of the last fully-completed round; `_run_game` restores both
on resume so a crash mid-scoring can't double-count either. Until 2026-09-04
only the scores were checkpointed, so a resume in matchup 3 re-counted the
CLAPBACKs of whoever swept matchups 1–2 (clapback-14).

### 5.1 A hiccup is not a crash (2026-09-10)

Every phase card — lobby, round prompt, matchup, round summary, scoreboard,
recap — goes out through `retry_transient`
(`games/utils/send_retry.py`): three attempts, 1s then 2s apart, for a 5xx or
a dead connection, and no retry at all for anything under 500 (a 403 will not
come good, and sleeping on it parks the loop). This exists because discord.py
retries `{500, 502, 504, 524}` unconditionally but **not 503**
(`discord/http.py:765`), so a 503 came straight out of `channel.send`.

`_play` then classifies whatever survives the ladder, via the same
`is_transient`:

- **Transient** — the game is *never* archived. `_survive_hiccup` waits
  `REDRIVE_PAUSE_S` (60s) and re-enters `_play` once (`MAX_REDRIVES = 1`);
  `_run_game` resumes at `len(round_history) + 1` off the checkpoints above,
  so the interrupted round replays without double-counting. A second failure
  leaves the game **frozen but whole**: the row keeps its checkpoints, and the
  live view is dropped so a stale card stops taking votes for a round that is
  going to be replayed. Three things then finish it, all better than an
  unpaid archive — restart recovery resumes it, `/games end` archives and pays
  it (which is what the busy-channel refusal already tells the next host), and
  the hourly 24h sweep archives it *with* the roster and pays it. The re-drive
  count is per-process and deliberately not persisted: a restart re-drives once
  through recovery anyway.
- **Anything else** — archived with `reason="crash"` exactly as before. A
  genuinely broken game must not become an immortal row.

The bound matters in both directions. One re-drive, because a game that kept
re-driving through an outage would just re-post phase cards into the channel;
and a hard cancel for real bugs, because the alternative is a row nothing
clears. `_survive_hiccup` re-checks the row (`is_game_expired`, true for a
deleted row too) before resuming — a `/games end` landing during the pause
would otherwise have `_run_game` read an empty payload and replay from round 1.

Why this is written down: Clapback game `959cd749` was four rounds into a
five-round game with four players when the next matchup's send returned 503.
The blanket `except Exception` archived it as a crash, and `_cancel_game`
calls `end_game` — deleting the very row `recover_game` resumes from, without
the `bot=` / `player_ids=` that pay a roster. Four played rounds paid nothing,
and the checkpoint machinery that would have saved it was already there.

**The two notices catch every exception, not just `HTTPException` — on
purpose.** A connection reset before headers never reaches an HTTP status, so
it arrives as `ConnectionError` / `TimeoutError` / `aiohttp.ClientConnectionError`
and a narrower `except` lets it through. These are courtesy sends that fire
when Discord is *already* misbehaving, and each one guards cleanup that has to
run after it: the `HICCUP_NOTE` send sits directly in front of the re-drive, so
narrowing it back stalls the game silently — the exact outcome this section
exists to prevent. Hot Takes has the same three sites, including one *outside*
its handler's `try` that killed the whole callback. Pinned by
`test_a_dead_connection_on_the_pause_notice_still_re_drives` and its three
siblings, which fail if the excepts are narrowed.

**Known trade:** retrying a send can post a card twice if Discord accepted the
first attempt and lost the response. discord.py already takes that bet for its
four statuses, and a duplicated vote card beats a destroyed game.

## 6. Not yet built

- No seeding or bracket progression — each round's pairing is independent, so
  the same two players can meet in consecutive rounds. Opponent-repeat memory
  would be the natural next dial if that reads as unfair in play.
- No per-guild knob for the bye award or the clapback bonus; both are constants.
- No 3-way ballot for three-player games (each player votes for the better of
  the other two answers, scored as a share of 2, CLAPBACK reachable at 2–0).
  The floor is announced instead (§1); the ballot is the fuller fix if
  three-player games stop being rare (5 of ~45 all-time).
