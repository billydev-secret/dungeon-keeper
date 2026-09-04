# Games deep review — gameplay, UX, backend (2026-09-02)

Scope, by Billy's call: the 17 `/games play` prompt games and their platform, the
rotation rooms (Guess Who, Whisper, Risky Rolls, feature rotation), duels and the six
party games, Meadow Mahjong, Survivor, the casino, Photo Challenge and the external
game-bot payouts. Three lenses in priority order — **gameplay** (is it a good game as
played in Discord), **UX** (Discord surface and dashboard panel), **backend**
(correctness, data, safety gates, restart resilience, tests).

The full list of findings, with each verifier's verdict, is in
`2026-09-02-games-deep-review-findings.md`.

Method: twelve family reviewers plus two cross-cutting sweeps (discovery / why the
tail is dead; safety and house-rule checklist), every finding then handed to an
independent verifier in a fresh worktree with instructions to refute it, a second
skeptic on anything still rated high, and a completeness critic at the end. Every
surviving finding cites `path:line` that a verifier re-opened. Refuted findings are
listed in the appendix so the next pass does not re-find them.

## How this relates to the August reviews

`docs/reviews/2026-08-05-games-*.md`, `-duels-party-games.md`, `-casino.md` and the
07-25 economy review were architecture and GDPR passes. They were right about what
they looked at — the platform extraction is real, the money funnel is the best code in
the repo, anonymity surfaces hold — and each closed with "UX: no findings". The
gameplay lens was never applied, and that is where nearly everything below lives.
Nothing in those documents is re-reported here; where a finding touches one of their
items it says so.

Three premises of the session brief did not survive contact with the repo:

- **Commit `7e059620` was not stranded.** It is patch-identical to `7d19c458`, which
  is on main with two follow-ups on top (`46df5360`, `4c4051b8`). The four
  unenforced dials it fixed — Risky Rolls / LegitLibs enable switch, the six duel
  panels' channel copy, the AMA bank UI, the Guess Who nudge — are live code. The
  August small-fixes branch (`/bank pay` no-contact, duel challenge window, pinned
  sweep, Clapback "Join now") is also on main under new hashes. Several memory notes
  calling both unmerged are stale.
- **Photo's 30 "plays" are a bot post.** `games_scheduled` row 7 posts the daily
  prompt at 12:13 UTC and the platform archives it as a game. Hosted play in the last
  30 days is about 48 rounds, 30 of them Clapback, run by two people.
- **The collision surface is four branches, not five sessions.** Only
  `game-start-echo`, `nsfw-gate-audit`, `review-fix-queue-round-2` and
  `games-nav-split` touch games code; the casino and mahjong sessions have merged.

## What is actually played (30 days to 2026-09-02, prod, read-only)

The `/games play` menu is not where the guild plays. The always-on rooms and the
scheduled prompt are.

| surface | 30-day activity | distinct people | host effort |
|---|---|---|---|
| Photo Challenge | 443 paid post-days, 10–24 photos/day | 59 posters | none (scheduled) |
| Casino | 15,095 plays (top four members = 76%) | 47 | none |
| Guess Who | 63 rounds, 429 guesses | 38 guessers | none |
| Whisper | 287 sends, 118 replies, 395 guesses | 62 senders | none |
| CAH via Gamebot | 50 lobbies, 45 paid finishes | — | a host, but not ours |
| Clapback | 27 real games, 500 matchups | 33 players, 5 hosts | one command + Start |
| Risky Rolls | 8–10 rollers a round, 3–4 member rounds a night | — | one button |
| Pressure Cooker | 37 games in August | 30 all-time | one challenge |
| AMA | 6 sessions, 17–35 questions each | 3 hosts | host at the keyboard |
| Compliment / Traditional / TTL / WYR / Rushmore / NHIE / MLT / Fantasies | 1–4 each | — | host at the keyboard |
| FFA, LegitLibs, MFK, Story, Hot Takes, Price, Hot Potato (Group), derby, baccarat | 0 | — | — |

Reading: the games that work share three properties — **no host at the keyboard**,
**a loop that closes on its own** (a solve, a settle, a reveal, a payout), and **a
public moment** (the vote button, the Reckoning, the reveal). The dead tail fails at
least two of the three, and the per-game findings below say exactly which timers,
floors and missing end paths do it. The one hosted game that thrives, Clapback, is
the one whose loop is timer-driven after a single Start press.

The two hosted-play numbers that matter most:

| | |
|---|---|
| games ended by a human (recap + payout shown) | Clapback, TTL, Story, MFK only |
| games ended by the 24-hour sweep or `/games end` | 62 of the 155 non-photo, non-clapback rows |

The recap embed and its payout footer — the designed ending of every prompt game —
have never been shown in-channel for AMA, Traditional, WYR, Fantasies or FFA. Coins
land a day later in silence. That is the single biggest gameplay defect in the
family and it is a platform property, not seventeen separate bugs.

## Corrections to the live-data table in the brief

- `player_count` is missing for two different reasons. Half the game types pass no
  roster to `end_game` (WYR, MLT, Fantasies, FFA, and every expired-at-Next path in the
  round games); the other half are historical — rows archived by the pre-07-29 bare
  sweep with an empty payload (Traditional's 18 of 19). `end_game` already holds the
  stored payload and a roster extractor and discards both.
- `guild_id = 0` is written by every bare `end_game` call without a `bot` — about 45
  call sites — and the daily photo post is the bulk of the recent rows. This is
  current behaviour, not legacy data.
- War in the casino is not dead: the brief's "10 hands" counts only tie standoffs;
  the table has 246 plays all-time and 189 in 30 days.
- Survivor's "26 players" is all three seasons; the real season has 15 alive and 13
  Week-1 picks in.

## The pattern the data shows

Every surface with a pulse shares four properties; every dead or dying game lacks at
least two. The fix queue below is ranked by how many of these a change adds.

| | one-press hosting | runs without a host | ends in-channel with a payoff | pinged or scheduled |
|---|---|---|---|---|
| Clapback | ✅ Start | ✅ timers | ✅ recap + coins | ❌ silent lobby |
| Photo Challenge | ✅ none | ✅ | ❌ no recap or showcase | ✅ daily ping |
| Guess Who | ✅ none | ✅ | ✅ solve + reveal | ✅ nudge |
| Whisper | ✅ none | ✅ | ✅ reveal | ✅ (DM) |
| Risky Rolls | ✅ one button | ✅ | ❌ winner's question dropped in most rounds | ✅ daily ping |
| Casino | ✅ | ✅ | ✅ | ✅ hub |
| Pressure Cooker | ✅ challenge | ✅ | ✅ rename | ❌ |
| AMA | ✅ | ✅ | ❌ no Close, recap never shown | ❌ |
| Compliment | ✅ | ✅ | ❌ ends before a compliment exists | ❌ |
| WYR / NHIE / MLT | ❌ Next every round | ❌ | ❌ no End button | ❌ |
| TTL | ❌ | ❌ (timer exists, default 0) | ✅ | ❌ |
| Traditional | ❌ writes every question | ❌ | ❌ 0 of 19 ended by a host | ❌ |
| Hot Takes / Fantasies | ❌ | ❌ | ❌ | ❌ |
| Price / Rushmore | ❌ writes the content | partly | ❌ | ❌ |
| FFA | ✅ none | ✅ | ❌ no ending, no payout | ❌ unscheduled |
| LegitLibs | ✅ | ✅ | ✅ | ❌ (and two bugs stop it starting) |
| MFK / Story | ✅ | ❌ turn-based | ✅ | ❌ |

Hosting is concentrated: 139 of 223 rows are one account, and eight people started
anything in thirty days. Discovery is one slash command nobody runs (`/games help`:
eight uses by two people since telemetry began; `/help` Games page: three). A hosted
`/games play` never pings anyone; the only two games with a daily role ping, Risky
Rolls and Photo, are the only two played every day.

## What the review found, by family

Numbers in brackets are finding ids in the register.

### Safety cells that fail (the seven highs are all here)

- **The bank NSFW filter is case-sensitive**, the dashboard never lowercases a tag,
  and prod holds 68 adult rows tagged `Nsfw` (all 44 Price scenarios, 19 of 36 NHIE,
  5 Clapback) that serve in any channel [trivia-tail-81, safety-sweep-1]. The
  `nsfw-gate-audit` branch touches the channel half of that gate, not the tag half.
- **No-contact is consulted nowhere in the party games.** Spin the Compliment pairs
  and pings giver→receiver from the raw pool [social-prompt-32]; MFK assigns three
  targets per player and posts the trios [anon-tail-67]; MLT's vote select and crown
  [vote-games-54]; Traditional's Ask Question seats host→random target
  [safety-sweep-2]; and none of the six duel and lobby games gate the challenge, the
  join, the result ping or the rename [duels-party-112]. Members holding a pair were
  in three of the last four Compliment pools. `docs/no_contact_spec.md`'s gated-surfaces
  table lists none of these, so they are omissions, not decisions.
- **Hot Takes de-anonymises itself**: the "voting is starting" ping mentions every
  submitter of a game whose lobby promises anonymity [anon-tail-63].
- **The enabled dial is skipped** by Play Again / Run Again in Clapback, Price and
  Rushmore [platform-28, safety-sweep-4], by feature rotation's launcher
  [safety-sweep-3], and by the casino's bet pickers for coinflip, slots, blackjack,
  war and mines (the per-table checkboxes only hide hub buttons, and stale hubs are
  plentiful) [critic]. Every slash entry, the scheduler and the duels do enforce it.
- **Feature rotation cannot be enabled at all**: its Save button reads an input a
  review commit deleted and throws before the PUT [rotation-rooms-155]. The rotation
  has never run in prod.
- Raw `<@id>` inside embeds survives in the Clapback scoreboard and bye fields, WYR
  Reveal Voters, TTL Final Results, Compliment pairings, MFK results, the session
  recap and the `/games config game-status` embed [clapback-6, vote-games-55,
  social-prompt-38, anon-tail-68, safety-sweep-6/7]. Some Clapback tests assert the
  mention, so the tests pin the defect.

Cells checked and clean, recorded so the next review does not redo them: no game keeps
admin config in Discord (`/risky reset_state` is the one admin command and it is an
ops action); mahjong, guess, whisper and survivor have their own off switch (dial,
role+channel, or season status); embed names outside the party games go through
`name_fn` or land in message content; `is_nsfw()` is absent from the rotation rooms,
photo, mahjong, casino, duels and survivor by design or because nothing adult flows
there; Fantasies' missing age gate is a recorded 07-27 owner decision.

### Platform (the fixes with the most reach)

- `end_game` stamps `guild_id = 0` on every call made without a `bot`, about 45
  sites, and archives `player_count 0` with an empty payload even though it already
  holds the stored payload and a roster extractor that could count it [platform-19,
  platform-20]. Fifty-one prod rows sit at guild 0 and are invisible to the
  guild-filtered dashboard queries. The same bare paths never fire the party-game
  quest triggers, so "Play a Party Game", "Win a Party Game" and "Join a Game
  Session" miss every sweep-closed game.
- 16 of 17 `/games play` commands launch on top of a running game. Prod shows 32
  overlapping pairs and four TTL games open at once; `/games end` then closes
  whichever row SQLite returns first [platform-18, trivia-tail-88].
- WYR, NHIE and MLT boards have no End control; the hourly 24-hour sweep archives
  and pays silently with no in-channel recap. About 41 of 119 rostered reactive games
  ended that way [platform-21, vote-games-52]. An inactivity close would contradict
  `games_system_spec.md:288`, so that half is a decision; the End button is not.
- When the bank and the pose queue are empty, MLT, WYR and NHIE end the game while
  telling the host to use a Pose button that `end_game` has just removed. MLT's prod
  bank has 0 rows, so a default `/mlt` discards its 3-player lobby at round 1
  [vote-games-50]. All 23 WYR bank rows lack the `|` separator the parser requires,
  because the panel gives no format hint and does no validation, so a default `/wyr`
  burns one row and ends in the same second [vote-games-49].
- NHIE declares "last one standing" after any round in which exactly one member
  voted, no lobby, no floor [vote-games-51].
- A scheduled WYR or NHIE launches with the schedule's creator as host and each
  round's Next is host-or-admin only, so it stalls on round 1 [platform-23]. The
  scheduler offers 18 types but only three can run without a human [discovery-4].
- Restart resilience is good except: a Hot Takes lobby is never recovered
  [platform-29, anon-tail-66]; a Clapback lobby is re-driven as if playing, and a
  joined roster crashes on a missing scores key [clapback-3]; TTL recovery
  auto-starts guessing from a lobby [vote-games-56]; WYR and NHIE lose the current
  round's votes [vote-games-58]; pending duel challenges lose their buttons
  [duels-party-126].
- `/games help` sits at Discord's 25-field ceiling, names a dead `/games support`,
  omits Mahjong, Guess, Whisper, Survivor and the casino, and contradicts itself
  (LegitLibs default mode, "one game per channel") [platform-25, discovery-7/8].
  Play Statistics knows 8 of 17 types and its Unique Players figure is fiction
  [platform-26]. `/recap` recaps one game and one player in 139 of 178 sessions
  because launch writes only the host [platform-22].
- Risky Rolls, the most-played game by presses, and all 135 duel games write no
  history row and appear in no report [rotation-rooms-157, duels-party-122,
  discovery-5].

### Clapback (keep, and invest here first)

The only hosted game with a self-running loop, and the code explains it: one command,
one Start, then timers; public one-matchup-at-a-time votes with the answer on the
button; a recap with winner, best answer and closest matchup. Bracketing, bye
rotation, checkpoint resume and the Answers In fix all held under reading and under
500 live matchups. What is wrong is pacing and edges:

- The vote loop runs the full timer with no early close, by a June decision taken when
  spectators were let in; spectators vote in 2–12% of matchups, so the trade-off buys
  little, and roughly half of a 23-minute game is fixed waiting [clapback-1]. The
  submit window has no host "close answers" and 36% of rounds waited out a missing
  writer [clapback-2].
- A late modal submit is accepted into the wrong round [clapback-4]; Join now during
  the write window on an even roster benches someone who already wrote [clapback-5];
  leaving mid-game silently forfeits a payout [clapback-17]; CLAPBACK tallies are
  not checkpointed [clapback-14].
- Only host or mod can press Start and a countdown lobby dies two minutes later, so a
  scheduled, auto-starting Clapback is impossible today [clapback-8]. Combined with
  the 00–04 UTC peak and the absence of any games ping role, a scheduled,
  role-pinged Clapback at the peak hour is the single biggest lever in the review.
- Play Again opens an empty lobby on rematch nights [clapback-10]; recap buttons die
  undisabled [clapback-13]; the bank is 99 prompts against ~135 draws a month
  [clapback-15].

### AMA, Compliment, Rushmore

- AMA has no Close button; its close path is reachable only through the feature
  rotation, which has never run, so 12 of 18 sessions were swept and the recap has
  never been shown [social-prompt-33]. Small rooms pay a click tax and re-ping the role
  on every seat change [34]; screened questions die after five minutes [35]; the
  hot-seat timer is not re-armed after a restart [42]; the ping role is found by name
  instead of a dial [41].
- Compliment lands socially (most of the pool posts within thirty minutes) but the bot
  ends and pays before any compliment exists [39], and the pairings embed is the
  no-contact and `<@id>` case above.
- Rushmore has had no real play since the 07-20 UX round; its 3-player vote floor is a
  coin flip and snake pacing floods the channel [36, 37].

### WYR, MLT, NHIE, TTL

All four are host-driven with no timer; a round ends only when the host presses Next.
TTL is the one that ends on its own with a recap and paid winners (its `vote_timer`
auto-advance exists but defaults to 0) [discovery table]. Beyond the platform items
above: MLT's dashboard dials contradict the code's 25-player clamp [vote-games-60];
WYR's Reveal Voters is shown to everyone but works only for the host (five refused
presses in one game) [61]; pure guessers in TTL are never paid at the 2-player floor
[57]; self-votes in MLT can crown everyone [62].

### FFA, Hot Takes, Fantasies, MFK, Story

Five games, 26 hosted rows all-time, four dead since June.

- Fantasies' recap and `_post_recap` are dead code; the only ends are the sweep and
  `/games end`, neither posts results [anon-tail-65], and its roster extractor reads
  keys the results never contain, so voters are never paid [64]. Both shipped with the
  07-29 roster work and the test hand-writes the wrong shape.
- Hot Takes: the de-anonymising ping above, an unrecoverable lobby, a restart that
  auto-starts voting [66], no timer or cancel [71], authors can vote for their own
  take [74], copy promises anonymity without FFA's mod-visibility disclosure [75].
- FFA never ends, never recaps, pays nobody (it is in `NO_ROSTER_TYPES`), and half its
  bank is dares an anonymous text box cannot perform [70]. 15 of its 18 plays were one
  admin's experiment. Its pseudonym map has no TTL and survives erasure [69]. It is a
  zero-effort prompt drop that belongs on the daily schedule beside Photo, not in the
  picker.
- Story works end to end but a missing writer costs five minutes per lap and only the
  host can skip [73]; a dismissed modal leaks a coroutine [78]. MFK is a 4-player
  icebreaker that ends at assignment [verdict].

### Traditional, Price, LegitLibs

- Traditional is the only one members play (10 participants on 08-31) but it is a host
  tool: every question is a modal, Bank Round is opt-in, and in 19 games nobody has
  ever pressed End Game [trivia-tail-84, 89, discovery-9]. Hosts double-start lobbies
  [88]. Ask Question ignores a channel that lost its age flag mid-game [96].
- Name Your Price: one dev-channel play ever. No lobby or ping, a two-minute silent
  host hold per round, auto-advance that is dead code because `expected_players` is
  never passed, voting that collapses at two players, no end button [85–87].
- LegitLibs' silence is three stacked bugs, not the design: the starter-pack seed path
  points at a file that does not exist, the seed's string ids would fail against an
  integer primary key even if it did, and the guard skips the main guild anyway [82];
  the dashboard's derived player range makes every 5–9-blank template a one-player
  lobby and caps everything at five [83]; one story per command with no Run Again
  [90]. The slash entry checks the enabled dial twice [91].

### Photo Challenge and external bots

Photo is the most-participated activity in the guild with zero host effort, and the
platform records none of it: the daily row is guild 0, empty payload, player count 0
[photo-external-102], the Ping Response report says "Played: 0" for every daily ping
[104], and the ping itself comes from a legacy announce row the panel shows as "no
ping" [103]. Nothing recaps, showcases or ranks the day's photos [105]; the 33-prompt
bank recurs monthly [106].

Externals: CAH survived Gamebot's 08-15 rewrite and is the guild's most-played hosted
game, but Anagrams did not: three payable games since 08-17 went unpaid because the
scoreboard moved into the embed description [99]. Survey Says and Wisecracks pay
nothing by design [101]. The panel has no health signal, so a silent parser break is
invisible [100]. The A1 buffer sweep from the August review is shipped and biting;
the data-register text is stale [111].

### Duels and party games

Pressure Cooker is the family's only success and has no decision at all; the stakes
text is the game [duels-party-124]. Chicken's crash point is fixed and public, so
nine of twelve completed games ended with everyone bailing at 94–99% and only three
ever produced a loser [113]; ties go to the oldest account [123]. Hot Potato (Group)
has never completed a game and shares its display name with the duel [120]. Lobbies
die 90 seconds after the last join with no visible deadline; a full 10-player Musical
Chairs lobby expired and was rebuilt [115]. A Musical Chairs final round with no
sitter pays one player as both winner and loser [114]. The rematch-cooldown dial on
the three duel panels is read by nothing while the same dial locks group rosters out
for 48 hours [116]. Half of Hot Potato winners never claim their rename [118].
`manual.html` describes a Pressure Cooker that does not exist [121].

### Casino

Alive but narrow: 15,095 plays in thirty days from 47 members, the top four making
76%, weekly actives flat at 18–27 since launch. Slots, blackjack, coinflip and Mines
are the tables; derby (12 plays, 5 players), dice and baccarat (one player) are dead
since their private conversion removed the spectacle [casino-138]. Two things the
August review could not have seen: the absolute 500-coin big-win bar now fires about
100 public cards a day because 1,000-coin stakes are routine, so a 1.06× Mines
cash-out is a "Big Win" [130]; and the five private-round boards still carry
communal copy ("the wheel spins in 10 minutes") and never say to press Spin, so a
third of roulette and derby rounds are resolved by the idle sweep [132]. One money-copy
bug: a doubled blackjack push is broadcast and banked as a win [131]. The 07-25
cap-off / max-bet-1,000 state remains by owner choice; the jackpot has since paid once
(18,228 on 08-22) [133]. The hub ticker shows one member's slots spins [135] and
renders six games as 🎲 [140]; the hub names the day's biggest loser publicly [137].

### Meadow Mahjong

Four review rounds and ~500 tests, almost no play: 12 tables, 2 settled hands. As
built, a 4-seat hand is 55–75 minutes of which a player is idle three-quarters of the
time, and nothing tells a player it is their turn, so tabbing away means folding in
three turn timers and, in a Duel, paying [mahjong-144]. Two prod-state items: the
Duel wall trim is still the old checklist value 60 on top of the short deck, so a
Quick Duel deals 17 live tiles (verified on prod table 11) [142]; and fill bots are on
with no two-human minimum, so a lone host can play the house at real stakes with
coach assist on [143]. Auto-pass leaks holdings through the instant ✅ pattern [145]
and prints a raw enum [146]. A table opening is silent and the lobby dies in ten
minutes [149]; the rematch window is 60 seconds after an hour of play [148]; the
escrow hold locks roughly 45% of members out and the panel says so only after three
clicks [150].

### Survivor (real season, opener Wednesday 09-09 evening)

Every item in the 08-18 first-look document is fixed on main, with commit evidence in
the register. The Week-1 path traces end to end. Two things will go wrong in the
first week unless acted on: the season's slate ping and last-call marks were set on
the first Wednesday and Saturday after creation (`last_slate_week 1`,
`last_lastcall_week 1`), so neither fires again for Week 1 [survivor-172]; and
`manual.html` still tells members Survivor is an unclickable preview while 15 are
enrolled [177]. The Reckoning marks the week and pays before it posts, so a Forbidden
loses the public post forever and a retry would double-pay [173]. The opener is
Wednesday the 9th, not Thursday the 10th, in the docs and plan [183]. The endgame
(season end, Sole Survivor, pot) and wipeout handling are todo #166's unbuilt Tier 2
[175, 176]; a cancel/refund path still does not exist [181].

### Rotation rooms

Guess Who and Whisper are the two healthiest loops in the bot. Their gaps: a Guess
round never ends unless solved, so 21 rounds older than thirty days sit open and the
submitter's reveal never comes [rotation-rooms-158]; a member who leaves the server
leaves unsolvable rounds behind that the nudge still pings about [164]; the Whisper
sender never learns they were guessed at, solved or exhausted [159]; the Whisper Audit
panel filters on states that do not exist [163]; both panels' "(none) lets anyone"
hints are false, the code fails closed [161, 162]. Risky Rolls is genuinely played
(133 `/risky start` presses by 10 members since telemetry began) but records nothing
[157] and its payoff, the winner's question, is dropped in most rounds with nothing
chasing it [156]; the second daily schedule is skipped whenever a member round holds
the channel [160]. Feature rotation is the Save-button bug above.

## Fix queue

Ranked. Each package is one branch. Sizes are the verifiers' corrected values.
Collision notes name the live branches that touch the same files.

**P0 — prod state, no code (needs Billy's hand or explicit OK for a DB write)**
1. Mahjong dashboard: set Duel wall trim to 0; turn fill bots OFF until P8 lands the
   two-human floor [mahjong-142, 143].
2. Survivor season 3: reset `last_slate_week` / `last_lastcall_week` so the Week-1
   ping and last call fire on 09-09 [survivor-172]. A one-line script; it is a prod
   write.
3. Games bank: lowercase the 68 `Nsfw` tags and reshape the 23 WYR rows to `A | B`
   [trivia-tail-81, vote-games-49]. Done by script alongside P1's code fix.
4. Retire `games_scheduled` row 6 if one Risky Rolls round a day is the intent
   [rotation-rooms-160].

**P1 — safety and enforcement (S/M; branch `games-safety`)**
- NSFW tag case: lowercase on every bank write, compare case-insensitively on read,
  a `pytest.param` row each side [trivia-tail-81, safety-sweep-1]. Coordinate with
  `nsfw-gate-audit`, which edits the channel half of the same function.
- No-contact: one shared exclusion helper built from `no_contact_partners_conn`, a
  derangement that accepts forbidden pairs, then Compliment, MFK, MLT, Traditional
  Ask Question, and the duels' challenge / join / result / rename paths
  [social-prompt-32, anon-tail-67, vote-games-54, safety-sweep-2, duels-party-112].
  Add the rows to `no_contact_spec.md`'s table. Logic-layer tests for each.
- Hot Takes voting ping without mentions [anon-tail-63].
- Enabled dial on Play Again / Run Again, feature rotation's launcher, and the casino
  bet pickers; extend `test_game_dials_are_enforced.py` beyond the seven party panels
  [platform-28, safety-sweep-3/4, critic].
- Feature rotation Save button [rotation-rooms-155].
- `<@id>` in embeds: the eight builders above, and flip the Clapback tests that pin it
  [clapback-6, vote-games-55, social-prompt-38, anon-tail-68, safety-sweep-6/7].

**P2 — platform endings and recording (branch `games-platform-endings`)**
- `end_game`: resolve the guild from the channel when no bot is passed, count the
  roster from the stored payload, keep the payload [platform-19, 20, vote-games-59,
  clapback-9, social-prompt-40]. One test that a sweep-closed roster game fires the
  party-game quest once.
- Per-channel busy guard in the shared slash preamble [platform-18, trivia-tail-88].
  This is common-lib round 2's A1 `guard_and_launch`; land the guard first, the
  refactor can follow.
- Fantasies: roster extractor keys, wire `_post_recap` and an End button
  [anon-tail-64, 65].
- WYR / NHIE / MLT: End button on the board; empty bank waits for a pose instead of
  ending; NHIE needs two tracked players before a winner [vote-games-50, 51, 52].
- Recovery: Hot Takes lobby, Clapback lobby state, TTL lobby, WYR/NHIE mid-round
  votes, pending duel challenges [platform-29, anon-tail-66, clapback-3,
  vote-games-56, 58, duels-party-126].
- History for Risky Rolls and duels [rotation-rooms-157, duels-party-122]; Photo's
  daily row records posts and paid post-days or stops archiving as a game
  [photo-external-102, 104].
- `/recap` roster on join and activity-bumped window [platform-22]; Play Statistics
  for all 17 types [platform-26].
- Scheduled WYR/NHIE: Game Host role unlocks Next, or the scheduler refuses to
  schedule a host-paced game without a nudge target [platform-23, discovery-4].
- Collisions: `game-start-echo` (risky_roll_cog), `review-fix-queue-round-2`
  (guess files only, not touched here).

**P3 — Survivor before 09-09 (branch `survivor-week-1`)**
- Guard the slate/last-call marks against firing before ingest or more than N days
  before kickoff; the P0 reset covers this season [survivor-172]. Post-then-mark, or
  a retry flag, for the Reckoning [173]. Manual copy and the opener date [177, 183].
  Zone label on pick menus [186]; reconcile_roles log volume [185]; leaver cache guard
  [174]. Operator weekly-clock view can follow Week 1 [178].

**P4 — Clapback (branch `clapback-pacing`)**
- All-eligible-voted early close (a recorded decision to reverse, see D1), host
  "close answers", late-modal guard, Join-now parity, leave-forfeit, tally checkpoint,
  Play Again keeps the roster, dead buttons, Cancel copy [clapback-1, 2, 4, 5, 10, 12,
  13, 14, 17].
- Auto-start on countdown and a scheduled Clapback with a games ping role dial (new
  dashboard control) [clapback-8, discovery-2]. This is the retention lever.

**P5 — Casino UX (branch `casino-broadcast-and-boards`)**
- Big-win bar as a multiple of stake or net win [casino-130]; doubled-push
  classification [131]; private-board copy and a Spin nudge [132]; ticker dedupe and
  emoji map [135, 140]; stale copy [139]; bet-step timeouts [141]; Pools one-sided
  void [136]. Retirements and the biggest-loser line are decisions D6.

**P6 — Rotation rooms (branch `rotation-rooms-round-3`)**
- Guess rounds end after N days with the reveal [158]; leaver rounds [164]; Whisper
  sender feedback [159]; audit filters [163]; both "(none)" hints and the log-channel
  hint [161, 162]; picker excludes self / offers the opt-in pool [167, 170]; Risky
  winner-question chase [156]; Risky copy [169].
- Collisions: `review-fix-queue-round-2` and `game-start-echo` own guess and risky
  files today; sequence this after they ship.

**P7 — Duels and party gameplay (branch `party-games-round-2`)**
- Chicken hidden, randomised crash and a fair tie-break [113, 123]; Musical Chairs
  no-sitter round [114]; visible lobby deadline [115]; rematch dial [116]; Hot Potato
  minimum hold and a longer naming window [118, 119]; a rematch button [117]; manual
  and embed copy [121, 125, 127, 128]. Merging Hot Potato (Group) into the duel is D4.

**P8 — Mahjong (branch `mahjong-pacing`)**
- Turn ping [144]; two-human floor for fill bots and a trim clamp against the short
  deck [143, 142]; auto-pass leak and enum [145, 146]; lobby announce and a longer
  lobby [149]; rematch window [148]; closed-table reason [151]; escrow copy [150];
  practice on the short deck [147].

**P9 — The tail: cheap fixes, then decisions (branch `games-tail`)**
- LegitLibs seed path + id type + guard, derived player range, Run Again, duplicate
  guard, panel double mount [trivia-tail-82, 83, 90, 91, 92].
- Traditional: Bank Round as the default path, End reachable, NSFW re-filter on Ask
  [84, 89, 96]. Story: anyone can skip after N minutes, Start host, coroutine leak
  [73, 77, 78]. Hot Takes timers, cancel, self-vote, disclosure copy [71, 72, 74, 75].
  AMA Close button and the small-room click tax [social-prompt-33, 34, 35, 41, 42].
  Compliment closing beat [39]. Rushmore tie-break and pacing [36, 37].
- `/games help` as one ephemeral panel with a select, rules, floor and "self-running
  vs host-paced", plus a 25-field tripwire meanwhile [platform-25, discovery-7, 8, 13].
- Photo ending / daily showcase [105]; ping row surfaced on the panel [103]; bank
  tags [106]. Anagrams parser [photo-external-99]; external health signal [100].
- Price, FFA, MFK: decisions D3 before any code.

## Decisions for Billy

- **D1 Clapback vote early-close.** Reverses the June decision (`ab27201b`) that let
  spectators vote. Prod shows spectators vote in 2–12% of matchups. Recommend: close
  when every eligible player has voted, record it in `clapback_spec.md`.
- **D2 Sweep-closed games.** An End button is a defect fix. An inactivity close or an
  in-channel recap from the sweep contradicts `games_system_spec.md:288`. Recommend:
  End buttons now; the sweep posts a one-line "archived, N coins paid" in-channel.
- **D3 Retire candidates.** Price (one dev play, needs the lobby work Clapback has),
  FFA as a picker game (keep it as a scheduled daily prompt drop), MFK (4-player hard
  floor, three plays), Story (two plays, both swept), Hot Potato (Group) (never
  completed; fold its mechanics into the duel), casino derby / baccarat / dice.
  Recommend: retire Price and Hot Potato (Group); schedule FFA; keep MFK and Story
  once P1/P2 make them safe and endable; retire derby, baccarat and dice from the hub.
- **D4 Games ping role.** A new opt-in "Game Night" role dial, allow-listed on every
  lobby post, and a scheduled auto-starting Clapback at the 00–04 UTC peak. Recommend
  yes; it is the one change that adds all four properties to the game that already
  works.
- **D5 XP for play.** No game path grants XP today; XP reaches players only through
  quests. Should finishing a party game, duel or mahjong hand grant XP the way voice
  minutes do? One hook in `_pay_party_rewards` if yes.
- **D6 Casino.** Redefine the big-win bar (multiple of stake, or net win) — recommend
  net win ≥ 500 or ≥ 5× stake, whichever is stricter. Keep or drop the public
  biggest-loser line. Cap-off / max-bet-1,000 stays by your earlier call.
- **D7 Survivor Week-1 reset** is a prod DB write. OK to run the script?
- **D8 Mahjong fill bots**: off now, and a two-human floor when they return?
- **D9 `/risky reset_state`** stays as an emergency command, or moves to the Risky
  Rolls panel? It is the last admin-gated slash command in the games.
- **D10 Survey Says / Wisecracks** payouts: track them (small parser addition) or not.
- **D11 Second guild.** It is a casino + Guess Who guild (11 casino members, 9 guess
  rounds, four party games ever, all one test evening). The casino per-table dial gap
  and the Guess Who off-switch matter there more than any party-game fix; nothing in
  this queue is specific to it.

## Not re-reported, and why

Fantasies' missing age gate (owner decision 07-27); Whisper's one-reply limit (spec
non-goal); Clapback's pure vote-share scoring (documented design); "payouts are not a
reason to return" (session-join quests exist and fire). The 07-25 casino R1/R2 state,
the 08-05 A1/A2 items, the Survivor first-look list, the four mahjong review rounds,
todo #156's ten config-audit items and todo #166's Survivor Tier 2 are all recorded
elsewhere and only cross-referenced here. See the register's refuted section for the
verifier's reasons.
