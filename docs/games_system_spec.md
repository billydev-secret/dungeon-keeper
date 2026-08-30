# Games System — Feature Spec

A 17-mode party-games suite. Every mode is one subcommand under a single top-level `/games` command group: players start a game with `/games play <slug>` (e.g. `/games play ffa`), which spins up a public embed with buttons; players join and play in-place. Games share a per-channel session window for the `/recap` highlight reel, a per-channel allowlist and per-guild enable/disable matrix (both managed from the web dashboard), an optional audit channel for anonymous content, a question bank with AI-generation fallback for prompts, and a dashboard for content authoring. Bank draws are round-robin (least-recently-served row first, so a small pool doesn't repeat until every row has been served once — see **Question-bank / AI-augmented games**). Games also feed the economy — completing quest-relevant actions credits economy quest triggers (see **Economy integration**).

The companion 1-v-1 nickname duel **Pressure Cooker** and the dice game **Risky Rolls** appear in the shared game registry (`GAME_ICONS`/`GAME_NAMES`) but are **separate features** with their own entry points and specs — they are not part of this suite. See [[pressure-cooker-spec]].

> **Command surface.** Only the `games` group is registered to the command tree (`command_groups.py`). Party games hang off the nested `games play` subgroup; meta/admin commands hang directly off `games` or its `config`/`track` subgroups. The one exception is `/recap`, which is a top-level command. There is no bare `/ffa`, `/wyr`, … — those forms are historical and no longer exist.

## Current Behavior

### Games

All party games launch with `/games play <slug>`. Params below are the actual slash options.

| Game | Command | Permission | Notes |
|---|---|---|---|
| Anonymous Truth or Dare | `/games play ffa kind:[truth\|dare\|random] tags:[csv] prompt:[str]` | Everyone | Drops a T-or-D prompt; players reply anonymously via modal, posted by the bot |
| Truth or Dare Card | `/games play ffa_banner kind:[truth\|dare\|random] tags:[csv] prompt:[str]` | Everyone | Card-only variant of FFA — just posts a prompt card for open chat, no interactive state |
| Photo Challenge | *(no command — standalone)* | — | **Moved out of the games menu + shared scheduler.** Now a standalone dashboard feature (**Photo Challenge** nav → Setup & Schedule): one dedicated channel, its own recurring schedule, a ping role, an enabled toggle. Auto-posts a photo-prompt card on schedule; members post their shots in the channel. Prompts come from the shared bank (`game_type='photo'`); a member's image post in the channel pays the economy `photo_post` faucet on the post itself (no reaction threshold). Config/schedule via `/api/photo-challenge` |
| Truth or Dare | `/games play traditional single_choice:[bool]` | Everyone | SFW/NSFW Truth & Dare opt-in pools; `single_choice:true` makes each player pick exactly one category (radio-style) |
| Spin the Compliment | `/games play compliment start_in:[1-60]` | Everyone | Derangement-paired giver → receiver |
| Marry, Fornicate, Kiss | `/games play mfk options:[csv] start_in:[1-60]` | Everyone | `options:` overrides the three default labels |
| Would You Rather | `/games play wyr question:[a\|b] tags:[csv]` | Everyone | Multi-round; `question` seeds an opening `a \| b`, else pulled from the bank |
| Never Have I Ever | `/games play nhie question:[str] lives:[0-10] tags:[csv]` | Everyone | Lives mode (default 3); `lives:0` disables elimination |
| Most Likely To | `/games play mlt question:[str] tags:[csv] start_in:[1-60]` | Everyone | 3-player minimum, 25-player max (the per-round vote select caps at 25 options); self-votes allowed |
| Two Truths & a Lie | `/games play twotruths prompt:[str]` | Everyone | Statements shuffled at display time; resubmit allowed until your round is revealed; optional per-round vote timer (dashboard `vote_timer`, 0 = host advances) |
| Hot Takes | `/games play hottakes` | Everyone | Anonymous submissions; 5-step temperature vote |
| Story Builder | `/games play story max_sentences:[≤30] visibility:[blind\|full] starter:[str] start_in:[1-60]` | Everyone | Default 10 sentences, default blind |
| Anonymous AMA | `/games play ama mode:[unfiltered\|screened] format:[hot_seat\|panel]` | Everyone | Two independent axes (see below); long-running; nightly 24h sweep cleans up |
| Fantasies & Dealbreakers | `/games play fantasies` | Everyone | Anonymous submit + Same / Not-for-me vote, multi-round |
| Name Your Price | `/games play price source:[host\|players\|ai\|bank\|both]` | Everyone | $ prices, reveal sorted; round/timer knobs live on the lobby buttons, not slash args |
| Mt. Rushmore Draft | `/games play rushmore topic:[str] source:[host\|ai\|bank] mode:[snake\|blitz] start_in:[1-60]` | Everyone | 4 rounds, no duplicates; snake (turn-by-turn) or blitz (everyone picks at once, first-come wins dupes); 60s post-draft backfill for skipped slots |
| Clapback | `/games play clapback start_in:[1-60]` | Everyone | Head-to-head matchups; unanimous winners get a CLAPBACK bonus |
| LegitLibs | `/games play legitlibs mode:[classic\|quiplash] tier:[1-4] template_id:[str] tag:[str]` | Everyone | Mad-Libs template fill; tiers 1 Flirty / 2 Spicy / 3 Filthy / 4 Unhinged, default tier 2 |

That is **17 `/games play` commands** (Anonymous AMA's two axes are one command; Photo Challenge is standalone with no command). `ffa_banner` is a card-only variant of FFA, so counting distinct games it is 16 plus the banner variant.

### Meta & admin commands

| Command | Permission | Purpose |
|---|---|---|
| `/recap` | Everyone | Highlights from the current channel's last 30 minutes of games |
| `/games help` | Everyone | Catalog embed built from the game name/icon/description registry |
| `/games end` | Host or Mod/Admin | End the active game in this channel (confirm popup). AMA additionally tears down its per-question views |
| `/games join [user]` · `/games leave [user]` | Self, or Host/Mod/Game-Host to move others | Add/remove yourself (or, with elevation, someone else) in a running game that has a roster. Open-submission games reply that there's nothing to join |
| `/games config game-status` | Mod/Admin (Manage Server or Administrator) | Inspect the active game in the current channel |
| `/games end force:true` | Mod/Admin | Force-close the active game without confirming and post a "Game Force-Closed" notice (absorbed `/games config game-end` 2026-07-28) |
| **Games → External Tracking** (web) | Mod | Watch a channel + bot and start banking its game-result messages. `kind` (`Gamebot (Cards Against Humanity, Connect 4, Anagrams)` \| `Cat Bot` \| `Wordle` \| `Co-ordle`) selects the parser/payout. Several bots can be tracked per guild, **and the same bot can be watched in several channels at once** — a watch is a `(bot, channel)` pair (migration 135) |
| **Games → External Tracking** (web) | Mod | Lists every tracked `(bot, channel)` pair — its kind, enabled/paused state, and messages banked in that channel |
| **Games → External Tracking** → Pause / Resume | Mod | Pause / resume banking. The row's own button toggles that one `(bot, channel)` pair; the API pauses every channel for a bot when no channel is named. Data is retained while paused. (The old `disable`/`enable` commands needed a `bot` argument and refused outright when several were tracked — a list with a toggle per row removes the ambiguity) |
| **Games → External Tracking** → Sample | Mod | Dump recent banked messages (raw content + embeds) to confirm the parser matches the bot's output |
| `/games dev fill` · `/games dev answer` | Dev/testing only | Populate a lobby with fake players / submit fake Clapback answers — a developer surface, not a player command |

External tracking is a format-agnostic collector (the `/games track *` commands that configured it were replaced by the dashboard panel on 2026-07-28): an `on_message`/`on_message_edit` listener banks every message from a watched channel+bot RAW (keyed on message id, de-duplicated across restarts/edits) into `games_external_messages`. Nothing is parsed at ingest — metrics are derived later, so a format change never loses history. Each watch carries a `kind` selecting a parser (`games_external/parser.py`); migration 097 generalised the one-bot-per-guild table to multiple `(channel, bot, kind)` rows, and **migration 135 widened its key to `UNIQUE(guild_id, bot_user_id, channel_id)`** so one bot can be watched in many channels. Before that a bot was capped at a single channel and every game it ran elsewhere was silently dropped; the collector cache is keyed on the `(bot, channel)` pair to match.

**Concurrency.** Games running at the same time in different channels each pay out independently, and need no coordination to do so: every payout is a pure function of its own channel's banked history (`recent_channel_messages` filters by channel), so N channels is just N independent backward scans. Nothing is tracked while a game is in flight — there is no game registry, and no state survives between messages.

**`gamebot` is live** (renamed 2026-07-25 from `gamebot_cah`), covering three sub-games that share one Discord bot account: Cards Against Humanity, Connect 4 and Anagrams. Every terminal message (`is_terminal`) goes through the single `_pay_gamebot_game` entry point, which reconstructs the game's window (`current_game_window`) and identifies the sub-game (`identify_game`) before dispatching:

- **Window bounding is anchored on the game's own lobby embed** (`<host> is starting a <Game> game!`), walking back from the terminal and stopping at that lobby, or at the previous game's terminal if the lobby has aged out of the banked slice. Anchoring on the lobby means a window holds exactly one game's messages.
- **The sub-game is read off that lobby, never off the terminal message.** CAH and Anagrams end with the *identical* `<@id> is the winner!` wording, so a terminal can't tell them apart — dispatching on it credited every Anagrams game as a one-player CAH game at score 0 (found and fixed 2026-07-26, with 3 such games in the banked history). A lobby naming a game we have no parser for (Chess, Poker, …) yields `None` and pays nobody, rather than falling through to a wrong guess.
- **Abandoned lobbies pay nobody.** `Not enough players joined the game!` still gets a *Game over!* from Gamebot; `is_abandoned` catches it. These used to be paid as Connect 4 games, because the roster reader picked up the **Joined Players** field off *any* embed — including an abandoned CAH lobby's. The join-phase reader is now gated on its embed titles and shared across sub-games (`players_from_join_phase`), the join phase being identical for all of them.

**Gamebot's 2026-08-15 format change.** Overnight Gamebot rewrote every CAH string, and because none of the new ones matched, DK stopped paying CAH entirely — the last `gamebot_cah` payout is 2026-08-14, while `catbot` and `coordle` kept paying daily. The parser now speaks both vocabularies, since a pre-update game can still be in flight:

| | through 2026-08-14 | from 2026-08-15 |
|---|---|---|
| standings | *Current Standings* embed, `<@id>: 3` in the description | a **Standings field** on *Round winner* / *Final scores*, `<@id>: **3**` |
| submissions | *Submission status*, `✅ <@id> Submitted!` | a **Submissions field** on *Play your card*, `✅ <@id>` / `⬜ <@id>` |
| finish | *Game over!* + `<@id> is the winner!` | *Final scores* — **no winner is declared at all** |
| lobby roster | **Joined Players** field | **Players (6/12)** field |

Two consequences beyond the string swap. **The winner is derived**, as the top of the last standings, since Gamebot no longer names one; CAH is first-to-N so a completed game has a unique leader, but a game Gamebot drops part-way can leave the lead tied and then *every* tied player wins (`extract_cah_game` returns a **list**, which `pay_cah_game_by_score` already accepts — Wordle ties the same way). An all-zero standings yields no winner at all rather than an N-way tie. **`Something went wrong` is terminal**, so a game Gamebot drops still pays out the rounds that were played (Billy's call, 2026-08-16). One posted 1.4s *after* a clean *Final scores* is not a double payment: each terminal claims on its own message id, so what protects the ledger is the window — bounding back from the crash stops at the *Final scores* before it, leaving a one-message window with no standings, which pays nobody. Replaying the whole banked history through the fixed parser reproduces all 43 existing payout rows unchanged.

Connect 4 and Anagrams **have not been verified against the new format**: the last banked Connect 4 lobby is 2026-08-08 (pre-change) and no Anagrams game has ever appeared in the archive at all, which is why `gamebot_anagrams` has no rows. The shared join-phase reader handles both roster field names, but Connect 4's new terminal wording is unknown until someone plays one.

Per sub-game: **CAH** reconstructs final scores (last standings post, folding in submission-only/winner-only players at 0) and calls `pay_cah_game_by_score` — the top scorer earns `EconSettings.reward_cah_win_max` (default 50, dashboard-configurable), everyone else a ratio of it, firing the same `party_game`/`game_win` triggers. Standings are accumulated and never pruned, so a player who leaves mid-game keeps the score they earned and is paid for the rounds they played, even though Gamebot cuts them from every later standings. **Anagrams** reads its *Scoreboard* embed, whose **field names** carry `"<username> - N POINTS"`; players are named by username rather than mention, so they're resolved with `resolve_named_scores` (the same `get_member_named` lookup Cat Bot uses) and then paid through `pay_cah_game_by_score` with `game_key="anagrams"`, sharing the same cap. **Connect 4** reconstructs roster + winner (`<@id> has won!`) and calls the flat `pay_game_rewards` — win/lose only, no score to scale by. All are paid once via a `games_external_payouts` ledger (migration 099) keyed on the game-over message id, independent of `parse_status` (which edit-recapture resets).

**`catbot` is also live**: Cat Bot catches are parsed from message content (`{username} cought <:raritycat:…>`, reverse cats un-reversed, "blessed…doubled" ×2), the catcher resolved by username→member (`get_member_named`), and `pay_cat_catch` credits rarity-tiered coins (common 1 → divine 300) plus the `cat_catch` quest trigger — paid once via the same ledger, keyed on the catch message id.

**`wordle` is live.** The Wordle bot posts one self-contained daily digest per group — no embeds, no lobby, so unlike every Gamebot game this needs *no backward scan at all* and is keyed on the digest's own message id:

```
**Your group is on a 9 day streak!** 🔥 Here are yesterday's results:
👑 3/6: <@490886726076727296> <@83699131368865792>
4/6: <@203866639005908992>
X/6: <@1069501326184153088>
```

Scoring is **inverted** relative to CAH/Anagrams — fewer guesses is better — so `parse_wordle_results` flips it (`1/6` → 6 … `6/6` → 1, `X/6` → 0, which keeps a failed player in the roster for `party_game` while earning no coins) and the shared `pay_cah_game_by_score` does the rest. Ties on the 👑 line are normal, so that function now accepts **several winners**. Wordle mentions only some players and prints the rest as a bare `@Name`; those are split on the `@` (not on whitespace — real display names contain spaces, e.g. `@communal potato`) and resolved with `resolve_named_scores`. In the observed history that recovers 21 of 22 such entries.

**`coordle` is live.** Co-ordle is a *co-operative* hourly word puzzle whose board embed (`Co-ordle for <t:…:f>`) lists each guess with its player and points. Two properties make it unlike everything else:

- **A new board message is posted per guess**, each showing the whole round. So the payout is keyed on the **round's own scheduled timestamp** (`coordle_game_key`), not a message id — keyed on the message it would pay the same round once per guess. Those values (~1.7e9) cannot collide with Discord snowflakes (~1.5e18) in the shared ledger.
- **There is no terminal message.** `This Co-ordle has ended` is only a rejection sent to a late guesser. Finality is read off the board: six greens in a row means solved, no unplayed rows left means exhausted. A round that simply times out with rows to spare stays open and never pays — 16 of 1,887 observed rounds, the accepted cost of having no end-of-round signal.

Points are the inline `**+N**`, or `**+N (+M)**` where a bonus applies, in which case the player earned **N+M**. That reading was verified against the bot's own cumulative leaderboard across consecutive snapshots: `N+M` reproduced the leaderboard delta for 79% of player-rounds versus 0.5% for `N` alone (the remainder being rounds clipped by a snapshot boundary or its top-10 cut). The leaderboard embed itself is cumulative all-time and is deliberately **not** used for payout — doing so would re-pay every prior round.

**Host bounty.** Gamebot names whoever started a game in its lobby title (`efficientpanic is starting a …`) — by username, so `host_from_lobby` reads it and the cog resolves it with `get_member_named`. Until 2026-07-26 no external game passed a host at all, so every tracked Gamebot game paid its players and nothing to its host, while native party games had always paid one. The bounty itself is unchanged and now lives in the shared `pay_host_bounty`, called by both the flat faucet and the score-proportional one: `joiners` counts players *other than* the host, so a game nobody else joined pays nothing (the anti-farm gate, which is also what makes an abandoned lobby pay its would-be host nothing). `scripts/backfill_gamebot_hosting.py` replays the backlog — dry run by default, idempotent via a `meta.game` stamp on each `game_host` ledger row (the payouts ledger can't be reused: it's keyed on the Game-over message id, and a game whose *participation* is already paid still owes its host). Booster status must be passed in with `--boosters`, since it lives only on the gateway (`member.premium_since`) and is recorded nowhere in the database.

**Replaying unpaid history.** `scripts/replay_gamebot_games.py` (dry run by default, `--apply` to write) replays every finished game with no ledger claim, feeding each through the *same* parser functions the cog calls — so backfill and live payout can't diverge. It claims before crediting, so a game already paid live is skipped structurally rather than by a cutoff, and fires quest triggers on each game's **own local day** so a backlog doesn't land on today's board at once. Same shape as `scripts/backfill_cat_catches.py`. **Wordle and Co-ordle have no equivalent replay**: their history predates tracking and sits in the general `messages` table rather than `games_external_messages`, and paying 1,581 historic Co-ordle rounds at once would be a large unplanned coin injection. Both start paying from the moment their watch is configured.

### Dashboard-managed configuration

These settings are **live and enforced** but are configured from the web dashboard (`/api/games/*`), **not** slash commands. There are no slash commands to manage them.

- **Channel allowlist** (`games_allowed_channels`, `guild_id`-scoped as of migration 115). Every *question-bank* game preflights `check_allowed_channel`; the six duel/group games in `dk_pvp_games_suite_spec.md` do **not** — their only channel rule is their own per-game `channel_allowlist`, and their panels say so. A channel that isn't on the allowlist refuses every question-bank game. The dashboard channels panel and the game-history/stats views are filtered to the active guild, so a host of one guild can't see or delete another guild's rows. (Legacy rows predating migration 115 carry `guild_id = 0`, treated as a wildcard by the in-Discord gate but invisible in the guild-scoped dashboard until reconciled.)
- **Per-guild per-game enable/disable** (`games_game_config`, default enabled —
  no row means on). Checked by `check_game_enabled` at the start of every
  game's command **and** by the scheduler before each auto-launch. Set on
  **Games Global Config → Available on This Server**, which lists every type in
  `routes/games.py:ALL_GAME_TYPES`; games with their own settings page repeat
  the same switch there, and Photo Challenge's lives on its own page instead
  (it is deliberately absent from the list, so the standalone panel stays the
  only writer of its row). The key is the game type string the bot reads, so
  each entry must be spelled exactly as its reader spells it or the switch is
  unreachable. Before 2026-08-29 seven games could not be switched off at all:
  `mfk`, `compliment`, `ttl`, `hottakes`, `story` and `fantasies` had no panel
  and never called the check, and `legitlibs` was missing from the list so its
  PUT 404'd; the list also carried a phantom `risky_roller` while the scheduler
  asked about `risky_roll`. The six duel/group games use the same store, keyed
  by each cog's `GAME_KEY`, checked at both creation entrypoints. **A panel
  labels the switch for everything that reads it.** `legitlibs` was labelled
  "Include in Scheduled Games" until 2026-08-29 and kept that label for one
  commit after its start command began gating on the switch, which made an
  admin unticking it to trim the schedule silently kill `/games play legitlibs`.
  `risky_roll` was the mirror-image bug: it sat in the Global Config list under
  "Available on This Server" while `/risky start` never consulted the switch, so
  the list promised a refusal that never came. Both now gate their start command
  and both panels say "Available on This Server", the same words the Global
  Config list uses for the same row.
- **Player limits are offered only where there is a lobby.** Most Likely To and
  Mt. Rushmore Draft create their game with `state="joining"`, so a floor and a
  ceiling have somewhere to apply; every other game goes straight to
  `state="playing"` and its panel offers neither. Both ceilings are clamped to
  **25** whatever the dashboard stored, because the per-round vote is a Discord
  `Select` and a larger lobby would 400 the message — the same reason
  `mfk` caps its pool. Rushmore had no join cap at all until 2026-08-27: the
  Join button appended unconditionally while the vote roster was sliced at 25,
  so the 26th player drafted a full board and then could not be voted for.
  **LegitLibs is the exception that proves the rule**: its ceiling isn't a dial
  but arithmetic on the chosen template's blank count (`ceil(n/10)` to
  `floor(n/5)`, each player filling 5–10 blanks), so the template editor shows
  the range instead of collecting it, and both modes turn away a joiner once
  the lobby reaches it (`validation.lobby_is_full`). Until 2026-08-29 the two
  numbers were editable fields whose values were discarded on save — create
  always derived them and update overwrote them whenever blanks rode along —
  and nothing enforced the ceiling anyway.
- **17 dials that no cog read were removed on 2026-08-27**, across WYR, AMA,
  NHIE, Price, Rushmore and Clapback. Three were worse than inert: WYR's "Hide
  Who Voted for What" could not have done what it said (naming is driven by a
  separate `revealed` flag set by a host/mod button, and `anonymous` only gated
  whether per-vote audit rows were written, so wiring it would have suppressed
  a mod trail while hiding nothing from members); AMA's key was `screened`
  while its cog reads `mode`; and Clapback's "Include NSFW Prompts" contradicted
  the house rule that NSFW gates on `channel.is_nsfw()` and never on a bot-side
  toggle.
- **Audit channel** (`games_audit_channel`). When set, anonymous submissions are mirrored there with the original author visible. **Only** anonymous submissions: nothing writes a game-lifecycle event there, and the panel hint no longer claims otherwise. This is now a *mirror*, not the record — every anonymous action is also written to `anon_audit_log` and surfaced on the admin dashboard regardless of whether this channel is configured (see `anon_audit_spec.md`).
- **Game Host / editor role** (`games_editor_role`). Holders pass the Game-Host check for content authoring on the dashboard and can add/remove other players via `/games join|leave`.
- **LegitLibs per-channel tier cap** (`legitlibs_channel_config.max_tier`, default 4), set per-row on the Games Config → Allowed Channels table, and LegitLibs template/vocabulary content.

### Dashboard

The dashboard mirrors the config surfaces above and adds full LegitLibs template authoring, question-bank import/export, and AI-prompt editing. Banks also share a **global pool** — bank rows stored under the reserved `global` game type, which gameplay never selects. Every bank manager has a per-question *Pool* button (copy to the pool; duplicate texts are skipped, and Traditional's category tags are translated to the generic `nsfw` tag or dropped) and a pool browser that imports selected pool questions into that game's bank (duplicates skipped; Traditional requires choosing the category the imports are filed under, other games carry the pool tags over). Two permission tiers gate it: `mod` (Administrator / Manage Server) for config writes, and a Game Host tier (Administrator OR the configured editor role) for content authoring (bank, templates, AI generate, history, stats). AI-prompt config is re-loaded per request so dashboard edits take effect mid-game without a restart. FFA's `truth`/`dare` tags are reserved the way `nsfw` is — a truth round draws only rows tagged `truth` — so its bank panel states that contract in its hint rather than enforcing a category the way Traditional does; an untagged FFA row is valid and simply only ever comes up in a random round.

## Behavior

Every interactive game follows the same skeleton: preflight (channel allowlisted? game enabled for this guild?), insert a "live game" row keyed by channel id, post the embed + view, and on close archive the game's final payload into a history table and free the live-game slot. Each channel can host at most one live game at a time. A 24-hour sweep closes orphaned games that nobody closed; closing copies the payload into the history archive and frees the slot. (Photo Challenge is fire-and-forget — it records a history row for stats but keeps no interactive state, since people just reply in the channel.)

The games cluster by shape; each cluster shares interaction patterns.

### Question-bank / AI-augmented games

**Would You Rather, Never Have I Ever, Most Likely To, Mt. Rushmore Draft, Name Your Price, Clapback** draw prompts from a pre-seeded bank first, falling back to AI generation when the bank is empty for the requested game (except Clapback, which is bank-only). **AMA has no bank** — every AMA question is typed by a member during the game, no draw function has ever read `game_type='ama'`, and the bank UI its panel used to offer (retired 2026-08-29) curated questions that could never be asked. The bank API still accepts the type so an old full-bank export re-imports. Bank draws are round-robin, not pure-random: each row tracks when it was last served and selection prefers the least-recently-served match (ties broken at random), so a small pool doesn't repeat a question until every row has been served once — including across separate game sessions. The multi-round ones rotate through rounds: each round opens with a fresh question (from bank, AI, or a host-supplied queue), collects votes or submissions, closes the previous round's view, then opens the next. `wyr` parses an optional `a | b` opening question; `nhie` clamps `lives` to 0–10 and disables elimination when set to 0; `rushmore`, `price`, and `clapback` show a live countdown timestamp the host can skip with a button. `rushmore` drafts in one of two modes (slash `mode:` arg or dashboard option; default snake): **snake** pings each player on their turn — the ping (and the 10-second nudge) carries its own **Make Your Pick** button so nobody scrolls back to the board — while **blitz** has everyone with an empty slot pick simultaneously each round, duplicates resolved first-come with an ephemeral "taken, try again". After the draft, skipped slots get a 60-second **backfill window** (own button, duplicates still blocked) before boards go final; boards that are still all-skip are hidden from the FINAL BOARDS embed and excluded from the vote. `clapback` pairs answers into head-to-head matchups with a special-case round-robin for 3-player games; a unanimous winner earns a "CLAPBACK!" bonus, and an odd submitter count gives one player a bye worth that round's average score (full bracketing and scoring rules in [clapback_spec.md](clapback_spec.md)). Bank lookups are NSFW-gated on the channel (`channel_allows_nsfw`), so NSFW prompts only surface in age-gated channels.

### Anonymous-submission games

**Anonymous Truth or Dare (FFA), Hot Takes, Fantasies & Dealbreakers, Anonymous AMA** post submissions to the play channel without the author's name attached. Every such submission is recorded in `anon_audit_log` — who posted it, when, and a pointer to the message — and is reviewable on the admin dashboard's **Anonymous Features** audit panel (`anon_audit_spec.md`). If an audit channel is also configured, the submission is mirrored there with the original author visible, so staff watching Discord see it without opening the dashboard. `hottakes` runs in two phases (submit, then a 5-step temperature vote per take with a live results bar). `fantasies` is multi-round; each round runs Submit → Reveal → Same/Not-for-me per entry, and the host can keep running rounds before the final recap.

`ama` is the largest and longest-running game, with **two independent axes**:

- **Content mode** — `unfiltered` (questions post immediately) or `screened` (host approves via DM before the question appears; rejected questions never post and never pay out quest credit).
- **Format** — `hot_seat` (one person answers at a time; the seat rotates) or `panel` (ask anyone who has opted into the panel, chosen from a dropdown).

AMA carries a per-question lifecycle (pending → answered / passed / rejected / expired), a screened-mode approval queue, DM notifications to the original asker when their question gets a reply (the DM names the channel and carries a jump link to the answered Q&A card, so the asker lands on their own question rather than at the bottom of the channel), a retention window for unanswered screened questions, and stale-target guarding: a modal submitted after the hot seat rotated (or after the target left the panel) is rejected with a "please try again" notice.

### Pool / pairing / draft games

**Spin the Compliment, Marry-Fornicate-Kiss, Truth or Dare (traditional), Two Truths & a Lie** open with a join-pool phase and a host-only "close pool" button that transitions to play. `compliment` requires 2 players and produces a giver → receiver pairing with no fixed points; the public ping is auto-deleted after 15 seconds. `mfk` requires 4 (and caps the pool at 25, since the assignments embed adds one field per player and a Discord embed holds at most 25 fields) and gives each player a deterministic 3-name slice from the shuffled pool (never themselves); `options:` lets the host override the three category labels. `traditional` toggles each player into any combination of four category pools (SFW Truth, SFW Dare, NSFW Truth, NSFW Dare) and weights target selection by least-asked count so one chatty player doesn't soak up turns. Its host-only **Bank Round** button deals every opted-in player one question from the web-managed bank; bank questions share the same per-(player, category) asked history as host-written ones, so each player is served at most once per opted-in category and re-pressing after new players join only serves the newcomers (the host summary reports how many already-asked players were skipped). Like the other bank-backed games, the draw itself is round-robin (least-recently-served row first) so a category's pool doesn't repeat a question across separate games until every row in it has been served. Its host-only **End Game** button posts the recap embed and pays the room; it was missing until 2026-07-29, which left the recap and the payout unreachable (see "Which end paths pay" below). `twotruths` collects three statements + the lie index per player via a components-v2 modal — the full prompt renders as static text inside it (the 45-char title used to truncate it), and the prompt is repeated on every round's guess embed for mid-game joiners. Statements are shuffled at display time and the room votes per player. Players can resubmit (the modal prefills their previous entry) via **Submit Statements** or the mid-game **Join / Edit** button until their own round is revealed; after that, statements are locked (`played` list in the payload). Only the lobby message's player roster is ever edited on submit — the modal never touches the active guess embed (2026-07-20 fix: a mid-game join used to overwrite statement 1's field). Rounds advance on the host's **Next** button, or automatically when the dashboard `vote_timer` option is set (>0 seconds; default 0 keeps host pacing). The recap's "fooled the fewest" award is **🪞 Open Book** (with fooled count); Best Liar and Best Guesser both earn the economy game-win bonus.

### Sequential storytelling

**Story Builder** builds a story sentence-by-sentence; the default "blind" visibility shows only the previous sentence in the modal while `full` shows everything so far. The host can skip a slow player. Max 30 sentences, default 10.

### LegitLibs

`legitlibs` runs a Mad-Libs-style template fill in one of **two** modes. **Classic** fills blanks one at a time round-robin, with a volunteer rescue path when a player times out. **Quiplash** has every player fill in parallel and reveals one filled version per player at the end. Templates are **per-guild** (`legitlibs_templates.guild_id`, migration 124): a guild draws its own templates plus the shared **global pool** (`guild_id = 0`), and an admin can promote a template to the pool — or claim it back to the server — from the dashboard (per-row *Make global* / *Make server-only*; the starter pack ships global). Templates are picked by tier (1 Flirty → 4 Unhinged, default 2) with optional tag filtering, and the picker avoids the five most-recently-used templates per guild. Each channel has a `max_tier` cap (dashboard-managed); requesting a higher tier silently downgrades and warns the user ephemerally. Per-blank fill prompts and example text are resolved through a fallback chain (most specific → bare part-of-speech) so even an under-specified template still renders a useful modal.

### Start countdown + host nudge

Six games open a join lobby and wait for a human to press the start button — **Clapback, Spin the Compliment, Marry-Fornicate-Kiss, Most Likely To, Mt. Rushmore Draft, Story Builder** (`LOBBY_GAME_TYPES` in `games/constants.py`). Only these take `start_in:[1-60]`. Three more games — Two Truths & a Lie, Hot Takes and LegitLibs — also open a lobby and wait on a host press, but were left out of this round and take no `start_in` (see `docs/plans/game-start-countdown.md`); every remaining party game posts its first prompt the instant the command runs, so it has nothing to count down to. `start_in` stamps a `start_epoch` in the game payload and renders it as a live Discord relative timestamp in an **⏰ Starting** field on the lobby embed, refreshed on every join/leave edit. It is **advertising, not automation** — the game does not auto-start, and the host still presses the button.

When the advertised moment arrives, `game_start_ping_service` posts a nudge in the game's channel mentioning the host by name and by button (`⏰ @host — time to start **Mt. Rushmore Draft**! Hit **Start Draft** when everyone's in.`), allow-listing only that one user. A **dashboard-scheduled** lobby game gets the same nudge the moment its lobby lands, aimed at whoever created the schedule — otherwise a scheduled lobby sits there with nobody aware a press is pending. Scheduled lobby-less games are never nudged; they self-run.

A schedule row with **Announce When It Starts** ticked adds one more message, and the order is **board → announcement → nudge**. The announcement fires *after* the launcher returns, not before it, because it carries a jump link to the board and there is no board to link at until the launcher has posted one (todo #97) — landing people in the channel to hunt for the game was the complaint. Every DB-backed launcher writes `message_id` before returning, so `get_active_game_by_id` reads it straight back; `risky_roll` keeps its round in memory and registers no row, so it announces the bare line with no link rather than a URL with a missing segment. The ordering also means a launch that fails announces **nothing** — the old order pinged a role about a game that then never appeared. That leans on `launch` returning `None` for *every* failure, which is a contract worth stating: `wyr` and `nhie` run their first round inside `launch`, and an empty question bank posts its notice, calls `end_game` and unwinds **normally** — so both check that their `games_active_games` row survived the round and return `None` when it didn't (`tests/cogs/test_games_launch_contract.py`). Without that they reported `launched` for a game that had already ended, putting the ping directly under the "bank is empty" notice. The configured role is allow-listed explicitly (`AllowedMentions(roles=[Object(id=…)])`, `none()` when unset), and the send suppresses embeds so the link doesn't render a preview of a board one message above it.

The nudge is driven by a 15-second poll over open lobbies rather than a per-lobby timer, so it survives a restart: a bot that was down across the start time nudges late on its next sweep instead of never. A game started early, cancelled, or timed out leaves the `joining` state and drops out of the sweep, so a running game never gets a "time to start". Each lobby is nudged at most once (`start_ping_sent`), and a lobby whose channel has become unreachable is marked rather than retried every tick.

### Meta surfaces

`/recap` reads the current channel's session window — the past 30 minutes of finished games — and renders highlights based on each game's final payload: most divisive WYR question, guiltiest NHIE player, best TTL liar, hottest hot take, and so on. `/games help` shows the full catalog from the game name/icon/description registry. `/games support` posts a static support-server invite.

### Close & archive

Every game's Close/End path opens a confirm popup ("Are you sure you want to end this game?" → Yes/No). On confirm: the view disables, the game's final payload is copied into the history archive, and the live-game row is freed. `/games end` (host or mod) and `/games config game-end` (mod only) both run this teardown; AMA runs its extra view/message cleanup first so nothing is orphaned. A 24-hour sweep (`games/utils/expiry_service.py`) closes orphans.

**Which end paths pay — all of them.** The economy faucet fires when `end_game` is given `bot=` and `player_ids=`. A game's own completion site builds that roster from locals it has in hand; the two paths that end a game from *outside* it have no such locals, so they rebuild the roster from the stored payload via `games/utils/game_roster.py` (`roster_from_payload`, one extractor per game type, each mirroring its cog's completion site).

- The **24-hour sweep** (`expiry_service.py`) pays. Hosts routinely never press End, so before 2026-07-29 every swept game paid nobody — all 18 Truth or Dare games in the history ended this way.
- **`/games end` and `/games config game-end`** (`force_end_active_game`) pay. This is the normal close method for several games, not only an abort, so it credits the room the same way.

A game type with no joined roster — ffa banner posts, Photo Challenge — resolves to an empty roster and pays nobody, which is correct: they are posts, not games players sign into (Photo Challenge is paid by the economy's own `photo_post` trigger instead). An abandoned lobby lands on that same empty roster, which is the whole anti-farm gate: aborting a game that never got going costs nothing, and leaving one open all day earns what it played. A malformed payload costs its own game a roster, never the sweep it was found in.

Because every path now routes through `end_game`, its `DELETE`-first claim is what keeps a game from paying twice when a sweep, a force-end, and a host pressing End race each other.

## Coin wagers (duel + group games)

All six duel/group games (Pressure Cooker, Quickdraw, Hot Potato, Hot Potato
Group, Chicken, Musical Chairs) accept an optional `wager:` amount on their
challenge/start command: equal ante from every player, winner takes the pot
minus an **optional house rake** (`wager_rake_pct`, default 0 — ships dark;
when priced, the cut is named on the payout announcement so the arithmetic
visibly adds up; refunds and single-stake pots are never raked). Escrow lives
in `econ_game_wagers` (economy side, migration 094)
and settles through the shared terminal-state seam — see
`docs/economy_spec.md` §6 and `docs/plans/economy-sinks-round-2.md` stage 4b.
The rule that inverts this module's usual one: `pay_game_rewards` swallows
every error because economy must never block game flow, but an escrow
**debit** raises and refuses the join/accept — you cannot enter a wagered
game you can't pay for.

A wager replaces the nickname stake: a wagered game with no custom `stakes:`
text records "Coins on the line — winner takes the pot." as its stakes and
resolves announce-only — no rename button, no nickname preflight. The rename
flow only runs when neither `stakes:` nor `wager:` is given (the default
"name" stake); see `docs/dk_pvp_games_suite_spec.md` §4.

## Economy integration

Games are wired into the economy quest system. Quest-relevant actions call `fire_member_trigger` (`bot_modules.economy.game_rewards`) to credit a member's economy quest progress — for example AMA credits an `ama_ask` trigger when a question actually becomes visible (on submit in unfiltered mode, on host approval in screened mode; AI-seeded idle questions and rejected questions never pay). Photo Challenge payout is handled by the economy directly (`EconomyCog._on_photo_post`): a member's image post in the configured photo channel pays the `photo_post` quest on the post itself — no reactions needed — the card itself just sets the prompt. Several other game cogs (`ffa`, `clapback`, `mlt`, `price`, `rushmore`, `traditional`, `nhie`, `wyr`) import the same game-rewards path. Credit is best-effort: an economy failure never unwinds a game.

## Permissions

**Bot needs:** Send Messages, Embed Links, Read Message History, Attach Files (Photo Challenge and card renders), Use External Emojis, Manage Messages (so `/games play compliment` can self-delete its public ping). The bot does **not** need Manage Nicknames — that's a Pressure Cooker requirement only.

**User needs:**
- Everyone can run every game command, subject to the channel allowlist and per-guild enable flag.
- Ending a game (`/games end`) is allowed for the game's host or a Mod/Admin. `/games config game-status` and `/games config game-end` require Mod/Admin (Manage Server or Administrator).
- `/games join`/`/games leave` are self-service; adding or removing *another* player requires the host, a Mod/Admin, or the configured Game-Host role.
- External tracking (web) requires Mod.
- Dashboard config writes need `mod`; dashboard content authoring needs Administrator OR the configured editor role.

## User-visible errors

| When | The user sees |
|---|---|
| Game runs in a non-allowlisted channel | Ephemeral: "This channel isn't set up for games. An admin can enable it from the web dashboard." |
| Game is disabled for this guild | Ephemeral: "{Game} is currently disabled on this server." |
| `/games play wyr` opening question doesn't have two options | Ephemeral: question must have two options separated by `|` |
| Question bank is empty AND AI generation failed | The game posts a "bank is empty" notice and closes (or, for scheduled/launch-only games like Photo and Clapback, the run is skipped) |
| Bot lacks send/attach/view permissions | Followup ephemeral perms hint listing the missing permissions |
| Non-host/non-mod clicks a host-only button, or tries `/games end` without rights | Ephemeral: only the host or a moderator can do that |
| Adding/removing another player without elevation | Ephemeral: only the host, a moderator, or a Game-Host-role holder can add or remove other players |
| LegitLibs tier exceeds the channel cap | Ephemeral warning that the channel's cap is lower; plays at the capped tier |
| LegitLibs has no templates matching tier / tag | Ephemeral: no published templates for that tier/tag; ask a mod to add some |
| AMA modal submitted after the hot seat rotated / target left the panel | Ephemeral: the seat/panel changed while you were typing — try again |
| AMA modal submitted after the game ended | Ephemeral: the game closed while you were typing — your question was not submitted |
| Audit channel was deleted | Silently swallowed; the game continues, and the `anon_audit_log` row is still written |
| AI generation API errors / times out | Falls back to bank-only or manual entry |
| Mod-only config command run without rights | Ephemeral: you need moderator or admin permissions |

## Non-goals

- **No cross-channel state.** Each channel runs one game at a time; the session window is also per-channel.
- **No cross-guild or in-app leaderboards yet.** History and external-game messages are collected per guild, but no leaderboard surface is rendered.
- **No per-user game settings.** Configuration is per-guild or per-channel only.
- **No pre-game RSVP.** Players join by clicking a button on the live embed.
- **No mid-game inactivity timeouts** except AMA's per-question lifecycle, the Clapback lobby's 10-minute quiet-timeout (the lobby message greys out to "Lobby timed out"), and the 24-hour orphan sweep. The host is trusted to close the game; the sweep is the safety net.
- **No matchmaking.** Pairings are random — no skill or history awareness.
- **No persistent seasons.** History is raw rows only.
- **No voice, music, or TTS integration.** Games are text + embed only.
- **Pressure Cooker and Risky Rolls are not part of this suite.** They share the game registry for naming/icons but have their own entry points and infrastructure; Pressure Cooker is documented in [[pressure-cooker-spec]].

> **Note — economy is no longer a non-goal.** Earlier versions of this spec claimed games have "no XP/economy integration." That is now false: games credit economy quest triggers and Photo Challenge registers reply-cards. See **Economy integration**.

## Configuration

### Per-guild (dashboard)

| Knob | Default | Purpose |
|---|---|---|
| Per-game `enabled` | on | Toggle a game on/off for the guild, from the availability list on Games Global Config. The settable set is `ALL_GAME_TYPES` in `web_server/routes/games.py`, and its spellings must match `games/constants.py` — the scheduler gates a run by calling `check_game_enabled` with the *constants* name, so a type spelled differently there is a toggle nobody can reach. `risky_roller` was exactly that (fixed 2026-08-30: it is `risky_roll`, and `legitlibs` was missing altogether); two tripwires in `tests/web/test_games_routes.py` hold the line. Risky Rolls and LegitLibs now also carry the switch on their own panels and honour it at slash-command entry, not just in the scheduler |
| Per-game `options` | empty | Per-game knob bag. **Every dial a panel offers must be a key its cog reads, and every key a cog reads must be a dial some panel offers** — `tests/web/test_game_dials_are_enforced.py` fails either way round. Read by clapback, price, rushmore, ttl, photo and mlt; wyr, ama and nhie read none, so their panels offer none. TTL's `vote_timer` fallback went on 2026-08-29 (no panel could ever write it; the per-schedule option is the only source), and Rushmore's `mode` gained the Draft Mode dial the same day. A save from a panel **replaces** the option bag rather than merging into it, so a key a panel stops offering is cleared by the next save; a payload with no `options` at all (the availability list) leaves the stored dials alone. |
| Question bank | per game | Only for games that actually draw from it. **AMA has no bank** — every question comes from members during the round and nothing in `games_ama_cog` reads `games_question_bank` — so its panel shipped a curation UI for rows the game could never serve. The bank UI was removed 2026-08-30 and the panel moved from the *Question Banks* nav group to *Live Games* (route id `games-ama` unchanged; ids are frozen). The API refuses new `ama` bank rows too (`BANK_READ_ONLY_GAME_TYPES` in `routes/games.py`), but still lists, exports and **imports** them, so a full-bank export taken before the closure round-trips instead of failing wholesale |
| Audit channel | unset | Mirror anonymous submissions here with original authors visible (the DB trail is always written either way) |
| Editor / Game Host role | unset | Role whose holders pass the Game Host check on the dashboard and can move other players |
| External tracking watches | unset | One or more (bot, channel, kind) pairs whose result messages are banked (set on Games → External Tracking); the same bot may appear in several channels |

### Per-channel (dashboard)

- **Channel allowlist** — only allowlisted channels accept any **party**-game command. It does not reach the duels and group elimination games, which gate on their own per-game `channel_allowlist` in `duel_config` and nothing else; their six panels claimed otherwise until 2026-08-30. See `dk_pvp_games_suite_spec.md`.
- **LegitLibs `max_tier`** — hard cap on tier (default 4); higher requests silently downgrade.

### Environment / files

- An Anthropic API key is required for AI question generation. Without it, AI fallback returns nothing and games default to bank-only or manual entry.
- AI-prompt text per game is stored in a config file editable via the dashboard; reads are not cached, so edits take effect on the next game without a restart.
- A LegitLibs starter pack of templates is loaded once per boot (idempotent — already-present templates are skipped).

### In-memory

- The external-tracking watch cache (`guild → {(bot, channel) → kind}`) is warmed on load and refreshed by the dashboard through `GamesExternalCog.refresh_watch_cache` after every write, so the `on_message` hot path never touches the DB. A write that skipped that refresh would be a silent no-op until the next restart. Keyed on the pair, not the bot, so a bot playing in several channels is matched in all of them.
- A per-game payload lock serialises mutations within one game; the lock is freed on game close.

## Stored data

Per-guild content (the seeded question bank, LegitLibs templates and their revision history, AI-prompt overrides) plus per-game runtime state (the live game's payload, the session window, audit channel and editor-role settings, allowlisted channels, the anonymous-features audit trail and its retention setting) and an archive of every finished game's final payload. External-tracking config and raw banked bot messages are stored per guild. User ids appear inside game payloads and the history archive. Photo Challenge registers card metadata for the economy `photo_post` payout.

LegitLibs additionally stores a small vocabulary table (parts of speech, domains, forms) and per-blank prompt text used to render fill modals, plus an anti-repeat window of the last few templates used per guild, channel tier caps, and user-submitted abuse reports on filled-in answers.

No DM data is stored. No filesystem cache outside the prompt-config file and the LegitLibs seed file.

## Not Yet Built / Roadmap

The following were described as current behavior in earlier versions of this spec but are **not implemented today**. The underlying *capabilities* they described mostly still exist — they're just reached a different way now (usually the dashboard), or were removed outright. They're recorded here so the design intent isn't lost.

### Never-built or removed command surfaces

- **`/games allow-channel | disallow-channel | list-channels`** — no such slash commands exist. Channel allowlisting is real and enforced, but is managed from the web dashboard. Only the *slash-command interface* was ever documented; it was never wired. (The embed/logic helpers for these strings still linger in `games_config/logic.py` and `embeds.py` but nothing calls them.) A future slash interface for allowlisting is a reasonable roadmap item.
- **`/games audit-channel [channel]`** — likewise unbuilt as a slash command. The audit channel is a live feature, set from the dashboard (`games_audit_channel`).
- **`/games portal-grant | portal-revoke | portal-list`** — removed. The `games_portal_access` table these wrote was dropped in migration `041_drop_games_portal_access.sql`. Dashboard/Games access is now governed solely by Discord admin/mod permissions plus the `games_editor_role` (Game Host role) — the editor role **supersedes** the old portal sign-in allowlist concept.
- **`/legitlibs-admin reload | cap-tier | preview | list | killswitch | enable`** — no such commands exist. LegitLibs content management, tier caps, and template preview live in the dashboard. The module-level **kill switch** described in older specs has been removed entirely (no `killswitch` code remains). LegitLibs tier caps are configured per channel from the dashboard, not via a slash command.

### Removed gameplay concepts

- **Consent system (`/consent`, `/consent-status`).** Fully deleted. The empty `games_consent/` directory went in `49a02867` and the orphan `games_consent` table — one row, zero code references — was dropped in migration 184. There is no per-user opt-in flag and no game blocks on consent. If per-user consent gating is ever revived, it would be a new build, not a re-enable of paused wiring. (Note: unrelated "consent" systems exist elsewhere — DM permissions and Rules Watch — and are *not* part of the games suite.)
- **LegitLibs Hot Seat mode.** Older specs listed a third "Hot Seat" LegitLibs mode as a stub. It has been removed; only `classic` and `quiplash` exist today. A one-at-a-time LegitLibs variant could be revisited as a roadmap item.

### Collection-without-surface

- **External game leaderboards.** External tracking and the `games_external_messages` collector bank raw messages from external bots. **Economy payout is now built** (Gamebot CAH → `party_game`/`game_win`; Cat Bot catches → tiered `cat_catch` — see `docs/plans/external-game-economy.md`), but our own *leaderboards/streaks* over those games (games-played, win-rate, catch counts by rarity) are still not surfaced — a roadmap item on top of the same banked data.
- **Hot Potato style points (metric tracked, never shown).** Every Hot Potato game already computes and accumulates **style points** — 10 per second a player spends holding the potato in the danger zone (last 30% of the timer) — into `hot_potato_style (guild_id, user_id, total_points)` via `compute_style_points` + `add_style_points`. The per-game figure appears on the result card, but the **cumulative total is written and never read** — no leaderboard, no command, no profile line. A "for fun" surface (a style-points leaderboard, or the running total on the result card) is the small next step; a fuller pass could track games played / wins / explosions (losses) / longest hold alongside it. This slots into the deferred participation-based cross-game standings roadmap (see memory: games progression roadmap) rather than being a one-off.
