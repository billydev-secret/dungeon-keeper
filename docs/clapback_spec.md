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
lobby countdown; the host still clicks **Start**.

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

## 2. Round flow

1. Latecomers queued during the previous round are admitted — see §2.2.
2. The round's bye is picked **before** the prompt goes out — see §2.1.
3. Prompt is drawn from the bank and posted; players submit via an ephemeral
   modal (resubmitting before the timer overwrites the previous answer).
4. Submitted answers are bracketed — see §3.
5. Each matchup is voted on **sequentially**, `vote_timer` seconds each.
   Contestants cannot vote on their own matchup. Each vote button carries the
   answer text (`logic.vote_button_label`), not a bare 🅰️/🅱️ emoji.
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

Either way they start on **0 points**, seeded into `scores`, `clapbacks` *and*
`scores_checkpoint` — the checkpoint is what a crash-resume rolls back to, and
a joiner missing from it is rolled off the scoreboard. Anyone over
`MAX_PLAYERS` is turned away out loud rather than silently dropped. Pressing
Join during the **last** round would otherwise queue someone for a boundary
that never arrives, so the game end calls `logic.drain_pending_players` and
tells them the game is over instead of leaving them waiting.

The button queued for the *next* round unconditionally until 2026-08-30. That
was the safe reading of a harder problem — a live round's matchups must not
shift — applied to a phase that has no matchups yet, and it made someone
watching a prompt they had a clapback for sit the round out.

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
`scores_checkpoint`, `clapbacks`, `round_history`, `used_prompts`,
`bye_history`, `last_bye` (legacy, still written), `round_bye` (this round's
pre-picked bye), `pending_players` (queued latecomers), `current_round`,
`phase`.

`scores_checkpoint` snapshots scores as of the last fully-completed round so a
crash mid-scoring can't double-count on resume.

## 6. Not yet built

- No seeding or bracket progression — each round's pairing is independent, so
  the same two players can meet in consecutive rounds. Opponent-repeat memory
  would be the natural next dial if that reads as unfair in play.
- No per-guild knob for the bye award or the clapback bonus; both are constants.
