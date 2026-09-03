# Games deep review — findings register (2026-09-02)

Companion to `2026-09-02-games-deep-review.md`. Every finding from the fourteen review agents, with the
verifier's verdict. **Status** is the adversarial verifier's call (a second skeptic re-judged everything the
first rated high); **severity** and **size** are the verifier's corrected values (high = money / safety / data
lost / cannot be played; medium = a player or host will hit it; low = polish; S under an hour, M half a day,
L multi-day). Where the verifier corrected the statement, the corrected statement is what appears. Refuted
findings are kept, with the reason, so the next pass does not re-find them. Line numbers are as of
`games-deep-review` at `dfe908d2` (main at 2026-09-02).

Fix-queue package letters (P0–P9) refer to the main document's queue.

**Totals:** 211 findings, 207 survived verification (7 high / 107 medium / 93 low), 4 refuted. By lens: gameplay 73, backend 77, ux 57.

## Games platform: game_manager, end_game, history, scheduler, recap, help

### platform-18 — 16 of 17 /games play commands launch on top of a running game — no per-channel busy guard
*backend · medium · S · confirmed* · game: platform · queue: **P2**

**Where:** src/bot_modules/cogs/games_wyr_cog.py:288-317, games_clapback_cog.py:640-663, src/bot_modules/cogs/games_legitlibs/__init__.py:78-83

**What:** A host who double-taps, or a second member who starts a game while one is running, gets two live boards in one channel. From then on `/games end`, `/games join|leave`, `/games config game-status` and `/games dev fill` act on whichever row SQLite returns first, the session tracker merges both, and the scheduler thinks the channel is busy until both are swept. Players see two sets of buttons and an End that closes the wrong game.

**Fix:** In the shared slash preamble (common-lib A1 `guard_and_launch` is the natural home) call `get_active_game(self.db, interaction.channel_id)` and refuse with an ephemeral '❌ A {name} game is already running here — the host or a mod can `/games end` it first.' Do it in the preamble rather than in create_game so the scheduler's own guard stay…

### platform-19 — guild_id=0 is written by every bare end_game — ~45 call sites, the daily photo post being the bulk
*backend · medium · S · confirmed with correction* · game: platform · queue: **P2**

**Where:** src/bot_modules/games/utils/game_manager.py:386-393, games_photo_cog.py:150, games_ffa_cog.py:676

**What:** Every bare end_game (photo daily post at games_photo_cog.py:150, lobby timeouts, empty-bank and Forbidden/crash paths) archives guild_id=0 because game_manager.py:386-393 only resolves the guild when bot= is passed; 51 prod rows sit at guild 0 (34 in the last 30 days, 30 of them the daily photo post), invisible to the guild-filtered dashboard queries at routes/games.py:233-236 and :738. The rows remain attributable via channel_id, and genuine completions of the second guild's games are recorded correctly.

**Fix:** Stamp the guild at creation: add `guild_id` to games_active_games (migration), have create_game take it (every launcher already receives `guild_id` — e.g. games_wyr_cog.py:322-331), and have end_game copy `row['guild_id']` instead of re-deriving it.

### platform-20 — end_game archives player_count=0 and an empty payload even though it already holds the stored payload and a roster extractor
*backend · medium · S · confirmed* · game: platform · queue: **P2**

**Where:** src/bot_modules/games/utils/game_manager.py:347-356, game_roster.py:218, expiry_service.py:278

**What:** Any engagement metric on the history table lies — Play Statistics' Rounds Played and per-game player counts, `/recap` highlights (which read the archived payload), and the ability to ever audit who was in a game. Half the game types will keep writing zeros until each cog's every end path is touched.

**Fix:** Fix it once in end_game: `payload = payload if payload is not None else json.loads(row['payload'] or '{}')`, then when `player_count == 0 and player_ids is None` derive `players, rounds = roster_from_payload(row['game_type'], payload)` and record `len(players)` / `rounds` — recording only, never paying (payment stays gated on explicit `pl…

### platform-21 — Reactive games end by the 24h sweep, not by anyone — the recap and payout arrive a day later out of sight
*gameplay · medium · M · confirmed with correction* · game: platform · queue: **P2**

**Where:** src/bot_modules/games/utils/expiry_service.py:260, src/dungeonkeeper/__main__.py:431-437, games_wyr_cog.py:186-240

**What:** wyr, nhie and mlt boards have no End control (only /games end, host-or-mod with confirm), and the hourly 24 h sweep (expiry_service.py:29, __main__.py:431-437) archives and pays silently with no in-channel recap; in prod ~41 of ~119 rostered reactive games (traditional 18/19, wyr 11/16, ttl 6/11, story 2/2, ...) ended that way. AMA now ends at midnight by decision and ffa has no roster, so exclude both. Adding an inactivity close contradicts games_system_spec.md:288 and is a design call;

**Fix:** Platform-level inactivity close (this contradicts games_system_spec.md:288 'No mid-game inactivity timeouts', which is the design call to make): bump a `last_activity` stamp on every vote/pose/advance via modify_payload (or an `updated_at` column) and have the hourly sweep become a 5-minute sweep that closes a game idle > N minutes (dial,…

### platform-22 — /recap recaps sessions that are mostly one game and one player, and goes blank 30 minutes into a long game
*gameplay · medium · M · confirmed* · game: recap · queue: **P2**

**Where:** src/bot_modules/games/utils/game_manager.py:564-616, games_clapback_cog.py:765, games_wyr_cog.py:375

**What:** A member typing /recap during a 45-minute AMA or a 25-minute Clapback gets 'No active session found'; after a game they get 'Unique Players: 1' with the host as a bare mention that renders as a number to anyone whose client hasn't cached them. The feature is meant to be the memory of the night and it has nothing to remember.

**Fix:** Bump `last_game_at` on activity/end (or extend the window to cover a game still in games_active_games in that channel); merge joiners into the session on join (the `bot.game_joiners` handlers already know the roster); skip `NO_ROSTER_TYPES` (photo, ffa banner) so bot posts don't open sessions;

### platform-23 — A scheduled Would You Rather / NHIE / MLT stalls on round 1 when the schedule's creator isn't there
*gameplay · medium · M · confirmed with correction* · game: scheduled-games · queue: **P2**

**Where:** src/bot_modules/services/scheduled_games_service.py:432-438, games_wyr_cog.py:226-229, core/utils.py:48-56

**What:** A scheduled Would You Rather or Never Have I Ever launches with the schedule creator as host and nobody is nudged; each round's Next is host-or-admin/manage_guild only (core/utils.py:48-62), so without that person or a mod the board stalls on round 1 until the 24 h sweep. MLT is a lobby game and does get the start nudge, but its Next is equally host-gated. The Game Host role honoured by /games join does not unlock Next.

**Fix:** Mark scheduled launches in the payload (`scheduled: true`) and let any voter advance after a per-round timer (e.g. Next unlocks for everyone 90 s after the round opens), or auto-advance on a timer for scheduled rows; at minimum extend `is_host_or_mod` on Next to the configured Game Host role.

### platform-24 — A daily schedule that finds its channel busy loses the whole day, with no visible history — Risky Rolls 12:53 is being skipped
*backend · medium · S · confirmed with correction* · game: scheduled-games · queue: **P2**

**Where:** src/bot_modules/services/scheduled_games_service.py:402-412, games-scheduling.js:333

**What:** Recurring schedules roll to the next day the moment the channel is busy (scheduled_games_service.py:402-412) while once-rows retry until giveup_at, and only last_status is stored so the panel can never show how often a schedule actually launched. Prod row 6 (Risky Rolls 12:53 local) was skipped on 09-02 because a member-opened round was live at 19:53 UTC and closed at 20:14 UTC — not because the 05:13 round overran (it auto-closed at 18:04 UTC as configured).

**Fix:** Give recurring rows the same retry: stay due and re-poll until `next_run_at + GIVEUP_GRACE_SECONDS`, then roll with 'skipped_active'. Add a `last_launched_at` (or a small run counter) so the panel can show 'last actually launched 6 days ago'.

### platform-25 — /games help is at Discord's 25-field ceiling and advertises a `/games support` that does not exist
*ux · medium · S · confirmed* · game: games-help · queue: **P9**

**Where:** src/bot_modules/games_help/embeds.py:155-170, constants.py:14-48, games_help/logic.py:104-110

**What:** A member opening /games help gets a 24-row wall with no grouping, a dead command name, and no mention of the five other game-shaped features; the next game shipped silently kills the command.

**Fix:** Rebuild as three grouped fields (Party games / Duels & group / Rooms & tables) or a select-menu per group, each line `emoji Name — /command`; add a tripwire `assert len(embed.fields) <= 25` today; fix the copy to `/support` in logic.py, the test and manual.html.

### platform-26 — Play Statistics only knows 8 of 17 game types and its Unique Players number is fiction
*ux · medium · S · confirmed* · game: platform · queue: **P2**

**Where:** src/web_server/static/js/panels/games-logs.js:6-12, src/web_server/routes/games.py:246-264

**What:** The one page Billy has to judge which games are played shows Truth or Dare — the second-most-played game — nowhere in its chart, and headline numbers that don't mean what they say.

**Fix:** Serve GAME_NAMES/GAME_ICONS from the API (schedule/options already does) and iterate `games_by_type` keys; compute unique players server-side with `game_roster.roster_from_payload` over each row; add a 30-day toggle since all-time is dominated by the photo bot post once finding 2 is fixed.

### platform-27 — MLT is unplayable in prod: bank-only with zero bank rows, and only Clapback pre-checks its bank before launching
*gameplay · medium · S · confirmed with correction* · game: mlt · queue: **P2**

**Where:** src/bot_modules/games/utils/question_source.py:11-14, games_mlt_cog.py:497-505, games_clapback_cog.py:653-659

**What:** MLT's default (bank) path is dead in prod: a bare `/mlt` passes the slash preamble (the bank check at games_mlt_cog.py:379 only runs when tags are given), fills a 3+ lobby, and ends at Start with '❌ The prompt bank is empty!' (:497-505) because the mlt bank has 0 rows. The game is still playable when the host supplies `question:` and uses ✍️ Pose Prompt each round. Rushmore's bank is also empty but it defaults to host-supplied topics, so it is unaffected. WYR has 23 rows (not 18).

**Fix:** Generalise the clapback pre-check into the shared preamble: refuse at slash time with the dashboard link when `has_matching_questions(db, game_type, [])` is False for every bank-only type; seed MLT/Rushmore banks (templates_seed.json or the Global Pool import); show a 'bank is empty' warning on the game's dashboard panel header.

### platform-28 — Play Again / Run Again relaunch a game without the enabled, allowed-channel or busy checks
*backend · medium · S · confirmed* · game: platform · queue: **P1**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:523-546, games_rushmore_cog.py:569-594, games_price_cog.py:444-470

**What:** An admin unticks Clapback on the dashboard mid-evening; the host presses Play Again on the recap and a new game starts anyway. Small window, but it is exactly the 'toggle that isn't enforced' CLAUDE.md forbids, and it also bypasses the busy guard once finding 1 adds one.

**Fix:** Route the three re-launch buttons through the same guard helper the slash preamble uses (enabled + allowed + no active game) and reply ephemerally on refusal; one shared `guard_and_launch` (common-lib A1) makes this automatic.

**Already recorded / fixed:** Related to common-lib-round-2 A1 (shared preamble) but the re-entry buttons are not listed there.

### platform-29 — A Hot Takes lobby is not recovered after a restart — buttons die, row sits until the sweep
*backend · medium · S · confirmed with correction* · game: hottakes · queue: **P2**

**Where:** src/bot_modules/cogs/games_hottakes_cog.py:490-492, games_wyr_cog.py:573, recovery.py:425-429

**What:** recover_game (games_hottakes_cog.py:481-491) ignores the row's state. An empty `joining` lobby returns False and is left with dead buttons until the 24h created_at sweep — as described — but a `joining` lobby with one or more takes already submitted is worse: it is re-driven straight into _run_voting(resume=True), so a restart mid-collection starts voting without the host, cutting off members still writing takes.

**Fix:** In recover_game, when the game is in `joining`, re-attach a fresh HotTakesSubmitView to the anchor message (`bot.add_view(view, message_id=...)`, as wyr does) instead of returning False; tests/cogs/test_games_recovery.py gets a hottakes-lobby row.

### platform-30 — After a restart, already-expired games keep dead buttons for up to another hour
*backend · low · S · confirmed with correction* · game: platform · queue: **P2**

**Where:** src/bot_modules/games/utils/recovery.py:406-408, src/dungeonkeeper/__main__.py:434-437

**What:** Evidence should cite recovery.py:93 and :109-111 (not :406-408) plus __main__.py:428-439. Statement stands: recovery skips expired rows and the only sweep sleeps 3600 s before its first pass, so an expired game keeps a view-less message for up to an hour after a restart. The busy-check part is true generally (get_active_game has no age filter, game_manager.py:266-271), not only after restart — the sweep's hourly cadence means any channel with a >24 h game is 'busy' to the scheduler for up to 59 min anyway.

**Fix:** Run one `sweep_expired_games` pass at the end of `_game_recovery` (or sleep after the sweep, not before).

### platform-31 — Scheduled launches bypass the channel allowlist that every slash path enforces
*backend · low · S · confirmed with correction* · game: scheduled-games · queue: **P2**

**Where:** src/bot_modules/services/scheduled_games_service.py:357-418, routes/scheduled_games.py:148

**What:** Scheduled launches of question-bank games skip `check_allowed_channel` (scheduled_games_service.py:357-418 checks enabled/rotation/busy only; launchers like clapback_cog.py:673 don't check either; routes/scheduled_games.py:148 only requires the channel be in the guild), so a Game-Host-role holder can schedule a party game into any channel despite the 'can only be started in these channels' hint (games-config.js:41-42, manual.html:569).

**Fix:** Either gate non-photo scheduled types on the allowlist at create time (400 with a clear message) or reword the allowlist hint to say it governs slash commands only. Cheap either way; the copy is currently untrue.

## Cross-cutting: discovery, host effort, why the tail is dead

### discovery-1 — Per-round host presses are why the tail is dead; survivors need one press
*gameplay · medium · M · confirmed with correction* · game: wyr, nhie, mlt, ttl, hottakes, fantasies, traditional, price · queue: **P2**

**Where:** src/bot_modules/cogs/games_wyr_cog.py:227, games_nhie_cog.py:177, games_mlt_cog.py:328

**What:** WYR, NHIE, MLT, Hot Takes, Fantasies and TTL (vote_timer default 0) advance only on a host press and have no round cap; Price and Rushmore are already timer-paced with fixed rounds and only need the host for the scenario/topic because the 'source' option defaults to 'host'; Traditional already has a one-press Bank Round next to the per-question modal. The prod sweep numbers (WYR 11/16, Traditional 18/19, AMA 12/18, TTL 6/11, Story 2/2 vs Clapback 0/63 at 22 min avg) are confirmed.

**Fix:** Add an auto-advance round timer + fixed round count to WYR/NHIE/MLT/Hot Takes/Fantasies (reuse the TTL vote_timer pattern; dashboard default ~60s, schedule option already exists for TTL), keep Next as an early-skip.

### discovery-2 — Hosted games are never announced to anyone; the two pinged games are the two played daily
*gameplay · medium · M · confirmed with correction* · game: all /games play · queue: **P4**

**Where:** games_photo_cog.py:121, scheduled_games_service.py:63-87, event_echo_logic.py:56-64

**What:** A member-started /games play game posts nothing but the board — no role ping on any launch path (only games_photo_cog.py:121 and the scheduler's announce_role_id path in scheduled_games_service.py:459-482 ever mention a role). Event Echo is silent by design and rate-limited to one echo per type per hour. The scheduler already has a per-schedule announce role; the gap is member launches.

**Fix:** Add an opt-in 'Game Night' ping role in feature_roles (same _ping shape as RISKY_PING at feature_roles.py:110-114, dashboard-configured) and allow-list it on the lobby/first-prompt post of every /games play launch via the shared launch tail. Keep Event Echo as is. Optionally let a host suppress the ping with ping:false like /risky start.

### discovery-3 — WYR has no in-game end; NHIE ends only by elimination — the only exits are /games end or the 24h sweep
*gameplay · medium · M · confirmed with correction* · game: wyr, nhie · queue: **P2**

**Where:** constants.py:172-173, games_config_cog.py:66-68

**What:** WYR has no End button and no round cap on the board; its only exits are /games end, the 24h sweep, an empty bank or an error (end_game is called at games_wyr_cog.py:364/423/456/495/520). NHIE ends only through elimination, so lives:0 has no end. Both the sweep and /games end do pay the voter roster; what is missing is an in-game ending with a recap.

**Fix:** Give WYR/NHIE a round cap (schedule option 'rounds', default ~10) that posts a recap (most divisive question, guiltiest player — the /recap highlight builders already exist in games_session/logic.py) and pays via end_game with player_ids; add an End Game button for the host on the round view.

### discovery-4 — Scheduler offers 18 game types but only 3 of them can run without a human host
*gameplay · medium · S · confirmed* · game: scheduled games · queue: **P2**

**Where:** src/bot_modules/games/constants.py:99-103, scheduled_games_service.py:510-519

**What:** The dashboard implies scheduling is a way to run game night without a host. In practice a scheduled Clapback posts a lobby and nudges the admin; a scheduled WYR shows one question and waits for Next forever. The one self-running party game (FFA/ffa_banner) is the one nobody schedules.

**Fix:** Short term: tag each type in the scheduler UI as 'self-running' or 'needs a host' (from a constant next to LOBBY_GAME_TYPES) and put a daily FFA prompt on the schedule next to Photo. Longer term the auto-advance work makes the tag true for more games.

### discovery-5 — Risky Rolls, the most-played game, writes no history row — the live table and every report miss it
*backend · medium · M · confirmed* · game: risky_roll · queue: **P2**

**Where:** scheduled_games_service.py:78-81

**What:** The brief's live table (223 games, 17 types) omits the game the server actually plays daily. /recap, games_session_tracker, the game_host/party_game quests, player_count analytics and the Ping Response game-player join (ping_tracker_service.py:427) all key on games_game_history/active rows, so Risky Rolls contributes to none of them and any 'is games night working' number is wrong by the largest term.

**Fix:** At round close write a games_game_history row (game_type 'risky_roll', host_id=opener, player_count=len(rolls), round_count=1, payload with the roll summary) through end_game or a thin equivalent; keep state in memory otherwise. Then the dashboard game reports and quests see it.

### discovery-6 — A lobby opened without start_in is never nudged and sits for 24h
*ux · medium · S · confirmed with correction* · game: clapback, compliment, mfk, mlt, rushmore, story, hottakes, ttl, legitlibs · queue: **P2**

**Where:** src/bot_modules/services/game_start_ping_service.py:82-102, expiry_service.py:29-34

**What:** Compliment, MFK, MLT, Rushmore and Story lobbies (LOBBY_GAME_TYPES minus Clapback) and the Hot Takes/TTL submission phases have no inactivity timeout and are only nudged when a start_in countdown was given, so an abandoned one sits until the 24h sweep. Clapback already self-cancels after 10 idle minutes (games_clapback_cog.py:158-193) and should be the model; Hot Takes and TTL are outside the nudge service altogether.

**Fix:** In game_start_ping_loop treat a joining lobby with no start_epoch as due after N idle minutes (e.g. 20), nudge once, and auto-cancel joining lobbies with fewer than min_players after ~60 minutes instead of waiting for the 24h sweep.

### discovery-7 — /games help is a flat 24-entry list with no rules and no ceiling tripwire
*ux · medium · M · confirmed* · game: games help · queue: **P9**

**Where:** src/bot_modules/games_help/embeds.py:132-147, constants.py:14-48, constants.py:244-443

**What:** Builds on the known 25-field ceiling: the next GAME_ICONS entry makes /games help raise at send time and no test would catch it. Beyond that, the embed tells a member nothing they need to pick a game — how many people it needs, whether the host has to babysit it, how long it takes, or the rules — and the ❓ Help that has those rules is only shown after the game has already started.

**Fix:** Replace with one ephemeral panel (house rule): a select menu of games → HOW_TO_PLAY text, min players, 'self-running / host-paced', typical length, and a Start button that calls the same launch(). Fold ffa_banner into ffa's kind option in the listing. Add the <=25 tripwire now regardless.

### discovery-10 — Event Echo's 1h per-type cooldown hides back-to-back rounds of the same game
*ux · low · S · confirmed with correction* · game: clapback, echo · queue: **P2**

**Where:** src/bot_modules/services/event_echo_logic.py:56

**What:** Event Echo's per-type 1h cooldown suppresses the second and third Clapback round of an evening (prod: 2-3 rounds/day from the main host, e.g. echo 179 posted and 180 suppressed 20 min later on 09-01), even when the echoed round has already ended. This is the recorded 07-28 decision ('same type at most hourly'); a 'previous echoed game has ended' bypass would be a change to that decision, to raise with Ben rather than fix.

**Fix:** Keep the global 10-min cooldown but let a new game_id in the same channel bypass the per-type cooldown when the previous echoed game of that type has already left games_active_games (it ended, so 'still open' is true again). Small change in decide()/echo_event.

### discovery-12 — LegitLibs slash entry checks check_game_enabled twice
*backend · low · S · confirmed* · game: legitlibs · queue: **P9**

**Where:** src/bot_modules/cogs/games_legitlibs/__init__.py:63-73

**What:** Harmless duplicate guard (likely a merge artifact of the 08-29 enable-switch work). Two DB reads and two messages for one condition.

**Fix:** Delete the second block.

**Already recorded / fixed:** The enable switch itself was added on the config-review branch (7d19c458 lineage); the duplication is new.

### discovery-13 — /games play picker descriptions give no player-count or host-effort hint
*ux · low · S · confirmed* · game: all /games play · queue: **P9**

**Where:** src/bot_modules/cogs/games_wyr_cog.py:277, games_nhie_cog.py:203, games_mfk_cog.py:187

**What:** Minor: the count is 61 /games play presses (12 subcommands) vs 8 /games help; and the gap extends to /games help's GAME_DESCRIPTIONS, which also omit floors for every party game (only musical_chairs states '3+ players'), so a registry should feed both the picker descriptions and the help embed.

**Fix:** Put the floor and pacing in the ≤100-char description, e.g. 'Would You Rather — 3+ players, host presses Next each round' generated from a small registry next to LOBBY_GAME_TYPES.

### discovery-8 — Help copy contradictions: LegitLibs default mode, one-game-per-channel promise, dead photo text
*ux · low · S · confirmed* · game: legitlibs, help, manual · queue: **P9**

**Where:** src/bot_modules/games/constants.py:424-426, src/bot_modules/cogs/games_legitlibs/__init__.py:33-34, constants.py:231-233

**What:** Members reading the in-game Help are told a different default than the command uses; two help surfaces promise a per-channel lock that does not exist; the manual's /games help description still points at the dead support subcommand.

**Fix:** Fix the five strings in one docs commit; either drop HOW_TO_PLAY['photo'] or point it at the standalone feature; align games_system_spec.md:211 with the removed /games support (or restore the subcommand if wanted).

### discovery-9 — Traditional Truth or Dare makes the host write every question; Bank Round is opt-in
*gameplay · low · S · confirmed with correction* · game: traditional · queue: **P9**

**Where:** src/bot_modules/cogs/games_traditional_cog.py:192-220, constants.py:274-282

**What:** Traditional Truth or Dare's Ask Question button makes the host write each question via a modal while the bank-backed Bank Round is a peer button that the in-game Help presents as a tip; in prod the only game that reached play (08-31, 10 players) was run entirely host-written, and the other 18 games ended with zero opt-ins — they died before the question phase, so the opt-in lobby, not the host loop, is where most games stall.

**Fix:** Make Ask Question draw from the bank by default and offer 'Write my own' as the secondary button; keep the modal for custom questions. Consider a per-round timer so the game self-advances.

### discovery-11 — Party-game payouts are not a reason to return; quests carry the whole incentive
*gameplay · low · M · refuted* · game: all party games

**Where:** src/bot_modules/economy/game_rewards.py:59-90, quests.py:127

**What:** Three coins for playing is invisible next to a 50-coin weekly quest, and the quest pays once per week so the second game night has no economic pull. The 07-30 retune deliberately flattened faucets, so the fix is not 'pay more' but 'pay for showing up together'.

**Why refuted:** Per-game values confirmed (prod config: main guild econ_reward_game_participation=3, econ_reward_game_win=25; game_rewards.py:59-90 pays via end_game). But the central claim — no attendance bonus beyond a session_join trigger 'tied to scheduled sessions that do not exist' — is false.

## Cross-cutting: safety and house-rule sweep

### safety-sweep-1 — Bank NSFW filter is case-sensitive; 68 adult prod rows tagged "Nsfw" serve in SFW channels
*backend · high · S · confirmed* · game: price / nhie / clapback (question bank) · queue: **P1**

**Where:** src/bot_modules/games/utils/question_source.py:163, src/web_server/routes/games.py:137-146

**What:** NSFW is correctly gated on channel.is_nsfw() (CLAUDE.md), but the gate's second half — recognising which bank rows are NSFW — matches the literal string 'nsfw'. Rows tagged 'Nsfw' pass rule 1 and are served in age-unrestricted channels. Clapback is the most-played game (30 rounds/30d) and 5 of its adult prompts leak; every Price scenario is adult and untagged in the filter's eyes; more than half the NHIE bank leaks. tests/test_question_source_bank_only.py:91-95 only tests the lowercase tag.

**Fix:** (a) Normalise in _filter_bank_rows: `{t.lower() for t in row_tags}` and compare requested tags the same way; (b) lowercase in routes/games.py `_norm_tags` on write; (c) one-off UPDATE on prod rewriting '["Nsfw"]' → '["nsfw"]' (68 rows); (d) add a pytest.param row with tag 'Nsfw' / 'NSFW' to test_nsfw_row_excluded_without_channel_opt_in.

**Already recorded / fixed:** Another finder flagged 'bank rows tagged Nsfw bypassing the channel gate'. Confirmed; this adds the write-path cause (_norm_tags), the exact prod counts and content, that the live nsfw-gate-audit branch does not fix it,…

### safety-sweep-2 — Traditional Truth-or-Dare seats host→target for a written question without consulting the no-contact list
*backend · high · S · confirmed* · game: traditional · queue: **P1**

**Where:** src/bot_modules/cogs/games_traditional_cog.py:205-220, src/bot_modules/games_traditional/logic.py:122-150

**What:** This is exactly the Risky Rolls shape docs/no_contact_spec.md solved ('the dice are nudged, not the outcome'): a public round, the bot picks who answers, and the host writes a directed — possibly NSFW — question at them, pinging them in content. If the host (or a mod pressing Ask on the host's behalf) and the chosen player are a no-contact pair, the bot manufactures the contact.

**Fix:** Add an `excluded: set[str]` parameter to select_next_question_target (drop those uids from `available_targets` before the least-asked filter) and have the Ask Question handler pass `no_contact_partners_conn(conn, guild_id, host_id)` (the actor pressing the button, since mods can ask too).

**Already recorded / fixed:** Not in any prior review or the other finders' list (they covered compliment, MFK, MLT crown, duels/lobbies).

### safety-sweep-3 — Feature-rotation game launch never reads the enabled dial (the scheduler does)
*backend · medium · S · confirmed with correction* · game: platform (feature rotation — all 19 launchable games) · queue: **P1**

**Where:** src/bot_modules/services/feature_rotation_service.py:271-306, src/bot_modules/services/scheduled_games_service.py:364-369, risky_roll_cog.py:507

**What:** Feature rotation's `start_room_game` (feature_rotation_service.py:254-310, not `_launch`) never consults check_game_enabled before calling the registered launcher; game_manager.create_game does not check it either, so only the slash entry and the scheduler enforce the dial. Inert in prod until a room's launch game is set.

**Fix:** In `_launch`, after the launcher lookup: `if not await check_game_enabled(db, SCHEDULE_BASE_GAME_TYPE.get(game_key, game_key), guild.id): log + return None`. Better still (see the Play Again finding) put the check inside each cog's `launch` so slash, scheduler, rotation and replay all pass through one gate.

**Already recorded / fixed:** New. Rotation lifecycle decisions in the brief (midnight end, echo out of gated rooms) are untouched.

### safety-sweep-4 — Play Again / Run Again buttons relaunch without the enabled dial — clapback, price and rushmore
*backend · medium · S · confirmed* · game: clapback / price / rushmore · queue: **P1**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:523-546, src/bot_modules/cogs/games_price_cog.py:444-470, src/bot_modules/cogs/games_rushmore_cog.py:569-595

**What:** The dial is enforced at one door (the slash command) while three other doors — recap replay buttons, the scheduler (which re-checks itself) and rotation (which does not) — call the launcher directly. A host can keep a switched-off game alive indefinitely from the recap card.

**Fix:** Move the check into `cog.launch` (and clapback's `_start_new_game`) and have it return None with a log line; the slash entry keeps its user-facing refusal. Then one guard covers replay, scheduler and rotation.

**Already recorded / fixed:** Another finder reported Play Again bypassing the enabled check (clapback). Confirmed; adds price and rushmore Run Again and the shared-seam fix.

### safety-sweep-5 — No-contact is consulted nowhere in Compliment, MFK, MLT or any of the six duel/party games (confirmed) — and none has a test
*backend · medium · M · confirmed* · game: compliment / mfk / mlt / chicken / hot_potato / hot_potato_group / musical_chairs / pressure_cooker / quickdraw · queue: **P1**

**Where:** games_compliment_cog.py:114-115, duels/base_duel.py:78-90, duels/base_game.py:808-815

**What:** docs/no_contact_spec.md rule 1: 'a guarantee that holds in three features and quietly fails in the other three is not a guarantee.' A duel challenge is the most direct contact surface in the bot (A picks B by name); Compliment's spin seats a giver with a receiver; MFK/MLT put a member's name in another member's answer.

**Fix:** Duels: in base_duel challenge, if is_no_contact_conn(challenger, target) return the ordinary target-unavailable text (the same string a cooldown/nickname-sentence refusal uses) — never a new message. Group lobbies: refuse Join with the ordinary 'lobby is full/closed' text when the joiner pairs with anyone seated (mahjong_service.py:472-47…

**Already recorded / fixed:** Reported by other finders (compliment, MFK, MLT crown, duel/lobby surfaces). Confirmed by grep; this adds the zero-test state and the per-surface refusal shape to use.

### safety-sweep-6 — <@id> inside embeds confirmed for clapback, WYR, TTL, compliment, MFK — plus clapback's two bye fields the report missed
*ux · medium · M · confirmed with correction* · game: clapback / wyr / ttl / compliment / mfk · queue: **P1**

**Where:** src/bot_modules/games_clapback/embeds.py:331, src/bot_modules/games_wyr/embeds.py:69-70, src/bot_modules/cogs/games_ttl_cog.py:555

**What:** <@id> inside embeds confirmed at all cited sites. For clapback, only build_submit_embed ('Sitting out', :124) and build_scoreboard_embed (ranking :331 and Bye(s) :358) hardcode mentions — the lobby, reveal and recap builders already take a NameResolver that the cog feeds display names, so the clapback fix is to pass that existing resolver to the two remaining builders. WYR, TTL, compliment and MFK need a name_fn introduced (TTL's resolver exists but returns m.mention by design).

**Fix:** Give each builder a `name_fn: NameFn = mention` parameter (as risky_roll/formatters.py:34-60 and guess_embeds do), build one resolver per render with services/name_resolver.build_name_fn at the cog, and add a pytest.param row per builder to the existing AST render-site test rather than per-file tests.

**Already recorded / fixed:** Other finders reported the clapback scoreboard, compliment pairings, WYR Reveal Voters, TTL Final Results and the MFK results field name. Confirmed;

### safety-sweep-10 — Traditional 'Ask Question' does not re-filter NSFW preferences by the channel flag (Bank Round does)
*backend · low · S · confirmed* · game: traditional · queue: **P9**

**Where:** src/bot_modules/cogs/games_traditional_cog.py:198-212, games_clapback_cog.py:713

**What:** If the channel's age-restriction is removed mid-game, the host is still prompted to write an 'NSFW Dare for X' (modal title at :61) into a now-SFW channel. Host-written rather than bot-served, so lower stakes, but the code already recognises the case in the sibling button.

**Fix:** Apply filter_nsfw_prefs before select_next_question_target in ask_question; in clapback, recompute allow_nsfw from the channel in _start_new_game rather than trusting the carried config.

**Already recorded / fixed:** New.

### safety-sweep-11 — Risky Rolls tie-rolloff embed builder is dead code and still names players as <@id> in an embed
*backend · low · S · confirmed with correction* · game: risky_roll · queue: **P1**

**Where:** src/bot_modules/services/risky_roll/formatters.py:319-343, tests/test_risky_roll.py:947, tests/test_embed_accent_contract.py:952

**What:** build_rolloff_embed/post_rolloff_embed (formatters.py:319-370) are unreachable from the cog and still format members as `<@id>` inside an embed; however tie outcomes are not silent — the round embed already prints 'A, B → A' via format_lowest_rolloff_note with a name_fn (formatters.py:232-256). What never posts is the per-round rolloff detail. Delete the builder and its two tests plus the accent-contract row, or wire it with the resolver.

**Fix:** Decide: wire post_rolloff_embed into the close path with make_name_resolver (and the room gets to see the tiebreak — a small fairness/UX win), or delete the builder and its two tests.

**Already recorded / fixed:** New.

### safety-sweep-7 — Session recap and /games config game-status embeds render players/host as raw <@id>
*ux · low · S · confirmed* · game: session recap / games config · queue: **P1**

**Where:** src/bot_modules/games_session/embeds.py:29-32, src/bot_modules/cogs/games_session_cog.py:102-104, src/bot_modules/games_config/logic.py:74

**What:** Both are public-or-mod cards naming members inside an embed. The session recap is the one members screenshot; a departed player renders as digits for everyone.

**Fix:** build_session_recap_embed takes a name_fn (cog builds it once with build_name_fn over player_ids); game-status uses `Name (`id`)` per the mod-facing convention. Add both to the AST render-site test table.

**Already recorded / fixed:** New.

### safety-sweep-8 — Hot Takes voting ping names the anonymous submitters (confirmed)
*ux · low · S · confirmed with correction* · game: hottakes · queue: **P1**

**Where:** src/bot_modules/cogs/games_hottakes_cog.py:149-159

**What:** Hot Takes' 'voting is starting' ping (games_hottakes_cog.py:149-161) mentions exactly the set of members who submitted a take, in a game whose submissions are anonymous; with one take it names the sole author. There is no lobby roster or game role to ping instead (payload is takes/results only, :335) — the fix is to remove the ping.

**Fix:** Ping the lobby roster (payload players) or the game role instead of the derived submitter set; keep the roster the pay path uses at :466-470 unchanged.

**Already recorded / fixed:** Reported by another finder; confirmed at the cited lines.

### safety-sweep-9 — Fantasies has no age gate at all (confirmed) — launch should require an age-restricted channel
*backend · low · S · refuted* · game: fantasies

**Where:** games_fantasies_cog.py:287, games_help/logic.py:63, games_traditional_cog.py:166

**What:** The bot serves no bank content here, so the existing tag filter has nothing to catch; the gate has to be on the game itself. CLAUDE.md says NSFW gates on channel.is_nsfw() and nothing else — that is what to apply at launch.

**Why refuted:** The absence is real (grep -i nsfw over games_fantasies_cog.py and games_fantasies/*.py returns nothing; the launch guard at :287 is check_game_enabled only). But it is a recorded owner decision, not a gap: docs/reviews/2026-08-05-games-batch-bc.md:27-28 'Fantasies placement is mod-policed channels — same owner-decision class as Guess's 07-27 call;

**Already recorded / fixed:** Reported by another finder; confirmed by absence and the copy.

## Clapback

### clapback-1 — Vote phase always runs the full 40 s even when every eligible player has voted — roughly half of a game is fixed waiting
*gameplay · medium · M · confirmed with correction* · game: clapback · queue: **P4**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:1127-1161

**What:** The vote loop runs the full dashboard vote_timer (default 40 s) with no early exit, by a deliberate June decision (ab27201b) taken when voting opened to spectators; prod data shows spectators vote in only 2-12% of matchups, so that trade-off buys little. An all-eligible-voted close (votes >= roster - contestants - bye) would end about half of matchups early (55-59% at 5-6 players, 11-20% at 9-11); the rest still need the timer or a host 'close voting' control.

**Fix:** Close the matchup early once votes >= len(roster) - 2 (contestants) - (1 if a bye who has not voted... simply: number of players not in the pair), then give a 5 s grace so a spectator mid-click still lands, then reveal. Keep the full timer only when a spectator vote has already arrived (their presence means the electorate is open).

### clapback-2 — Submit window waits the full 120 s whenever one player goes quiet; no host 'close answers' control
*gameplay · medium · S · confirmed* · game: clapback · queue: **P4**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:1028-1063

**What:** Every one of those rounds ran the whole write window because one person had stepped away, and then paid that absent player nothing anyway. The Next Round button exists for the round summary (a 10 s wait) but the phase that actually stalls has no host control. Hosts compensate by talking; a third-time host will not know to.

**Fix:** Add a host/mod-only 'Close answers' button on the submit panel (same is_host_or_mod gate as Next Round, cog 504) that sets the submit event and breaks the loop; and auto-close when count == expected - 1 and nothing has changed for 20 s.

### clapback-3 — A restart during a lobby re-drives the game loop on a 'joining' row: empty roster plays five no-op rounds, a joined roster crashes on a missing scores key
*backend · medium · S · confirmed* · game: clapback · queue: **P2**

**Where:** src/bot_modules/games/utils/recovery.py:96-125, src/bot_modules/cogs/games_clapback_cog.py:602-624, games_rushmore_cog.py:650

**What:** Billy restarts the bot for deploys, and games cluster 00-04 UTC. A lobby open at that moment: with nobody joined, _run_game posts 'Not enough answers this round — moving on!' five times and a 'Final Results / Winner: Nobody' recap, then archives the game (players pinged: none). With players joined, they are pinged for round 1, write answers, the matchups post, and the first _vote_matchup raises KeyError('scores') inside the background task — the game is left in active_views with a dead vote panel until the 24 h exp…

**Fix:** In recover_game: `if row['state'] == 'joining': rebuild ClapbackJoinView with view.message = message, bot.add_view(view, message_id=message.id), return True` (mirror rushmore 650-664), or at minimum return False so the row falls to the expiry sweep with its buttons disabled.

### clapback-4 — An answer modal submitted after its window closed is accepted with 'Answer submitted!' and either discarded or written into the NEXT round
*backend · medium · S · confirmed* · game: clapback · queue: **P4**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:107-131

**What:** Discord keeps a modal open on the client indefinitely. A player who opens Submit in the last seconds of round N and sends after the window closes gets a success reply while their answer is not in the bracket (silent loss); if they send after round N+1's prompt has posted, their round-N answer becomes their round-N+1 entry — the wrong joke under the wrong prompt, and it counts toward Answers In and can close that window early. Resubmitting in-window overwrites it, but only if they notice.

**Fix:** In _store, read payload.get('phase') and payload.get('current_round'); if phase != 'submitting' or current_round != self.round_num, skip the write and reply '❌ Answers for round N are closed' (the modal reply is ephemeral, so this is cheap). One logic-layer test on a small `accept_answer(payload, round_num)` helper.

### clapback-5 — Join now during the write window on an even roster benches someone AFTER they wrote — the exact complaint the pre-picked bye fixed
*gameplay · medium · S · confirmed with correction* · game: clapback · queue: **P4**

**Where:** src/bot_modules/games_clapback/logic.py:86-126, src/bot_modules/cogs/games_clapback_cog.py:840-849

**What:** Join now during the write window has no parity check, so on an even answer count the latecomer forces create_matchups to bench a player who already wrote (and on an odd roster produces two byes paid the average). It is real but conditional: 11 of 30 recent games had an even roster and the seating path is three days old; the same bench-after-write already occurs by design whenever a submitter misses the window (spec §3.2).

**Fix:** In admit_player_now, take the current expected-writer count; if seating the joiner would make it odd, either (a) un-bench the pre-picked bye (clear round_bye, set the submit gate, post '🪑 <@bye> you're back in this round') so the count stays even, or (b) when there is no pre-picked bye, queue the joiner for the next round with the existin…

### clapback-6 — Round scoreboard, bye line and 'Sitting out' field render <@id> inside embeds — the third-time-fixed house defect, in the game's most-viewed card
*ux · medium · S · confirmed* · game: clapback · queue: **P1**

**Where:** src/bot_modules/games_clapback/embeds.py:124, docs/embed_style_guide.md:371-395, tests/test_games_clapback_logic.py:869-934

**What:** The between-round scoreboard posts five times a game and is what players scan to see if they are winning; on mobile or for anyone who has not loaded a member, the top three read as 🥇 1359533934387396638 — 425 pts. The lobby and recap in the same game show names, so the inconsistency is visible within one session. Not in any prior review (batch A found 'no UX findings').

**Fix:** Give build_submit_embed and build_scoreboard_embed a name_resolver parameter (the cog already builds `lambda uid: resolve_name(guild, uid)` at 315/747/1190/1269) and pass it from _submit_phase, _round_summary and _post_scoreboard; keep the ping in message content= at 1013 as is.

### clapback-7 — At the 3-player floor every matchup is decided by one judge and the recap loses two of its three flourishes
*gameplay · medium · M · confirmed with correction* · game: clapback · queue: **P4**

**Where:** src/bot_modules/games_clapback/logic.py:31

**What:** With three players and no spectators the only eligible voter per pair is the third player, so every matchup is 100/0 on one click, and each still runs the full 40 s vote timer (cog :1158-1161) — 15 matchups x 40 s of dead time. CLAPBACK and Best Single Answer are unreachable only while nobody outside the roster votes (a 3-player game with spectators, b76e1c46, did produce 2-vote matchups and 4 CLAPBACKs). 26 of 27 real 30-day games gathered 4+; 3-player games are 5 of ~45 all-time with a recorded count.

**Fix:** Keep MIN_PLAYERS at 3 but make 3 a real game: run one 3-way ballot per round where each player votes for the better of the other two answers (2 votes per answer, ties possible, CLAPBACK reachable at 2-0), scored as vote share of 2; or, cheaper, say it in the lobby embed and Start reply ('3 will play;

### clapback-8 — Hosting costs one press, but the host must be at the keyboard: only host/mod can Start, a start_in countdown ends in a dead lobby, and a scheduled Clapback can never start
*gameplay · medium · M · confirmed with correction* · game: clapback · queue: **P4**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:231-247, src/bot_modules/services/scheduled_games_service.py:510-519

**What:** Only the host (or an administrator/manage_guild holder) can press Start; a start_in countdown is advertising only and a lobby that sees no press for 10 minutes after the countdown (+2 min) is cancelled; a scheduled Clapback is launched with the scheduler as host, so it can be started by them or a mod but nobody else — it is not 'never'. 3 of 30 clapback rows in 30 d are timed-out lobbies; two hosts ran 26 of the 27 played games.

**Fix:** (1) Auto-start at start_epoch when len(players) >= MIN_PLAYERS (the lobby view already knows the epoch; poll it in the same loop the start-ping service uses, game_start_ping_service.py) and say so in the countdown field.

### clapback-10 — Play Again opens an empty lobby — every player re-joins by hand, on a night where rematches are the norm
*gameplay · low · S · confirmed with correction* · game: clapback · queue: **P4**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:523-547

**What:** Play Again opens an empty lobby (cog :729-735). 8 of 27 real 30-day games began within 3 minutes of the previous game ending in the same channel, and in 7 of those the roster size changed, so seeding from the last roster saves most but not all re-joins.

**Fix:** Give _start_new_game an optional `players` seed; ClapbackRecapView passes the finished game's roster (minus anyone who left mid-game) and the lobby embed lists them immediately. Keep the Start gate so the host still confirms.

### clapback-11 — Discovery: a Clapback lobby is silent outside its channel — no ping role, and the only nudge goes to the host
*ux · low · S · confirmed with correction* · game: clapback · queue: **P4**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:673-718, src/bot_modules/services/game_start_ping_service.py:1-30

**What:** A Clapback lobby posts with no role mention and no content line (cog :741-756); the only nudge is the host tap. Photo Challenge has a ping-role dial (photo-challenge.js:171-181) but prod has it empty, so there is no live ping-role precedent — the dashboard's shared optSchema already supports type 'role', making a per-game ping-role option an S fix.

**Fix:** Reuse the photo panel's ping-role dial as a per-game option on games-clapback.js (optSchema row `ping_role`, mount via games-panel-shared) and mention it in the lobby content= with AllowedMentions(roles=[…]) — content, not embed. Pair with the auto-start finding so the ping has a time attached.

### clapback-12 — Host 'Leave' reply tells the host to 'Use Cancel instead' — there is no Cancel button
*ux · low · S · confirmed* · game: clapback · queue: **P4**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:214-220, src/bot_modules/cogs/games_config_cog.py:66-120

**What:** A host who wants to abandon a lobby is pointed at a control that does not exist and left to discover /games end or wait 10 minutes for the timeout. Small, but it is the first dead end a new host can hit.

**Fix:** Change the copy to 'You're the host — run `/games end` to close the lobby', or add a host-only Cancel button that calls _cancel_game and edits the lobby message the way on_timeout does (181-192).

### clapback-13 — Recap Play Again buttons go dead after 2 minutes without being disabled
*ux · low · S · confirmed* · game: clapback · queue: **P4**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:513-521

**What:** After the recap sits for two minutes the primary 'Play Again' button still looks live and fails with Discord's 'This interaction failed' — right when a host who stepped away for a drink comes back to restart. The lobby view already handles this correctly.

**Fix:** Add on_timeout that disables the buttons and edits the message (store view.message after channel.send at 1279), and consider timeout=600 to match the lobby's inactivity window.

### clapback-14 — clapbacks tally is not checkpointed, so a crash-resume mid-round double-counts CLAPBACKs in the recap
*backend · low · S · confirmed* · game: clapback · queue: **P4**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:780-784, embeds.py:426-432

**What:** The score rollback is right; the per-player CLAPBACK count and the 'Total Clapbacks' field are the one piece of mid-round state that survives the rollback, so a game resumed after a crash in matchup 3 of round 2 shows one or two extra CLAPBACKs for whoever swept matchups 1-2. Cosmetic (points are correct), but it is the recap's bragging line.

**Fix:** Snapshot clapbacks alongside scores (`payload['clapbacks_checkpoint']`) at 924 and restore both at 782-784; one row in the recovery test.

### clapback-15 — Bank depth: 99 prompts against ~135 draws a month — regulars cycle the whole bank every ~20 games
*gameplay · low · M · confirmed* · game: clapback · queue: **P4**

**Where:** src/bot_modules/games/utils/question_source.py:185-197, logic.py:36-48

**What:** At two games a night the same core of ~10 regulars sees every prompt again roughly every ten nights, and the rotation guarantees it. The recap's 'Best Single Answer' is the memory the game leaves behind, and a repeated prompt invites a repeated joke. Bank-only is the right call for a comedy game (quality control); the bank just needs to be deeper than one month.

**Fix:** Grow the bank to 300+ via the ToD/question studio rather than runtime AI; or wire the unused AI prompt strings into a dashboard 'draft 20 prompts into review' action on the Clapback question tab (games-panel-shared has the bank UI) so an admin approves before they can be served.

### clapback-17 — Leaving mid-game keeps the score on the board but silently forfeits the payout, including a win nobody then receives
*backend · low · S · confirmed* · game: clapback · queue: **P4**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:1349-1364, src/bot_modules/economy/game_rewards.py:104-116

**What:** A player who leaves in round 4 while leading stays 🥇 on every later scoreboard and in the recap's 'Winner:' field, is paid no participation, and because the winner id is filtered out by `allowed`, no game_win is paid to anyone. The recap footer still advertises '+N to winners'. Probably intended as 'you left, you forfeit', but the copy and the recap say the opposite.

**Fix:** Pick one: (a) keep the forfeit and drop the leaver from `scores` (or mark them) so the recap crowns the highest remaining player and the copy says 'their score is withdrawn'; or (b) pass the union of players and leavers to end_game. (a) matches the anti-farm spirit of pay_game_rewards.

### clapback-9 — Timed-out or crashed lobbies archive as guild_id 0, player_count 0, payload '{}' — the row says nothing about why the lobby died
*backend · low · S · confirmed* · game: clapback · queue: **P2**

**Where:** src/bot_modules/cogs/games_clapback_cog.py:1295-1301, src/bot_modules/games/utils/game_manager.py:386-393

**What:** Three of the thirty '30-day clapback games' the brief counts are lobbies nobody started. Because the roster is dropped on cancel, it is impossible to tell whether they died with 2 joiners (a floor problem) or with 6 joiners and an absent host (finding above), and the guild-0 write is what makes the dashboard treat them as legacy. The brief already flags guild_id = 0 being written today; this is the clapback cause.

**Fix:** On both cancel paths pass the current payload and len(players) to end_game, and let end_game resolve guild_id from games_allowed_channels (keyed by channel_id, has guild_id) when no bot is supplied. Consider a `reason` key in the archived payload ('lobby_timeout' / 'crash') so the games dashboard can separate abandoned lobbies from played…

**Already recorded / fixed:** Brief: guild_id = 0 still being written (cause and empty payload not previously identified)

### clapback-16 — Points are pure vote-share regardless of electorate size: one voter's click swings 100 pts, and 18% of matchups tie at 50/50
*gameplay · low · S · refuted* · game: clapback

**Where:** src/bot_modules/games_clapback/logic.py:343-365

**What:** With 3-4 eligible voters the point scale is coarse: a 2-1 is 67/33, a 1-0 is 100/0, a 1-1 is 50/50. That is not a bug — the spec documents it — but it means a missing voter (asleep, on the bye, distracted) changes a round's standings more than any joke does, and the many ties read as 'nothing happened'. Raised for the design record, not as a defect; the >=2-vote CLAPBACK rule already shows the authors saw the problem.

**Why refuted:** The numbers are real: prod payloads hold 500 matchups; 62 are 1-0, 82 are contested ties plus 8 zero-vote (90 at 50/50 = 18%). logic.py:345-365 scores pure vote share (pct_a = round(votes_a/total*100)), zero votes → 50/50 (335-341), CLAPBACK needs total_votes >= 2 (349-352).

## AMA, Spin the Compliment, Mt. Rushmore

### social-prompt-32 — Spin the Compliment pairs and publicly pings members without consulting the no-contact list
*backend · high · S · confirmed with correction ×2* · game: compliment · queue: **P1**

**Where:** src/bot_modules/cogs/games_compliment_cog.py:283, src/bot_modules/games_compliment/logic.py:48-59, src/bot_modules/games/utils/derangement.py:4-23

**What:** Spin the Compliment (and MFK, same gap) pairs members without consulting the no-contact list: games_compliment_cog.py:106 derives the giver->receiver map from the raw pool and :110-133 posts it publicly and pings both halves; derangement.py:4-23 has no exclusion support and nothing on the platform path gates it. Already recorded as 'raised, not in scope' in docs/plans/rotation-rooms-round-2-build.md:76 and :501 (2026-09-02) but no todo row or fix exists yet.

**Fix:** Give `random_derangement` a forbidden-pairs argument (or a rejection-and-retry loop with a bound) fed from `no_contact_partners_conn` for the pool; if no valid derangement exists, refuse with the ordinary 'Need at least 2 players' style copy so the protected party cannot tell.

**Already recorded / fixed:** Not fixed; recorded as an open rule gap in docs/plans/rotation-rooms-round-2-build.md:76 (C10) and :501 on 2026-09-02, with no todo row created.

### social-prompt-33 — AMA cannot be ended by its host and its recap has never been posted in prod
*gameplay · medium · M · confirmed with correction* · game: ama · queue: **P9**

**Where:** src/bot_modules/cogs/games_ama_cog.py:939-1003, src/bot_modules/games/utils/expiry_service.py:47-60, src/bot_modules/cogs/games_config_cog.py:44-52

**What:** AMA has no host End control; the recap + payout-footer embed in _do_close is reachable only via close_now, which only the (unconfigured) feature rotation calls. The 24h sweep ends the game silently (bottom bar removed, nothing posted); /games end posts only the generic force-end embed. Payouts still happen on both paths - only the recap and the visible payout footer never reach the channel.

**Fix:** Add a host/mod '🏁 End AMA' button (row 1, confirm popup like the other games) that calls `_do_close`. Make the 24h sweep and `/games end` prefer a live view's `close_now` the way feature_rotation_service.py:241-246 already does, so the recap posts on every end path.

### social-prompt-34 — Two- and three-person AMAs pay a click tax and re-ping the @AMA role every seat change
*gameplay · medium · S · confirmed* · game: ama · queue: **P9**

**Where:** src/bot_modules/cogs/games_ama_cog.py:709-713

**What:** With two people the hot-seat format alternates 4-question turns and pings a whole role each time the seat flips, while the panel format makes every question three interactions through a dropdown that lists you and one other person. Neither format lets a small room just talk; the 'plant your own question' hole is a small anonymity oddity on top.

**Fix:** In `_begin_ask` (panel) drop `interaction.user.id` from candidates and open the modal directly when exactly one candidate remains; in hot-seat count *answered* questions toward the turn (or expose 'questions per turn' as a dashboard dial, default 4) so the seat is not emptied while questions are still open.

**Already recorded / fixed:** ama-hot-seat-ping-is-intentional memory covers the ping itself, not the small-room click cost

### social-prompt-35 — Screened AMA questions silently die if the host does not approve within 5 minutes
*ux · medium · S · confirmed* · game: ama · queue: **P9**

**Where:** src/bot_modules/cogs/games_ama_cog.py:443

**What:** Screened mode is advertised as the safe option, but the approval buttons in the host's DM stop working after five minutes ('This interaction failed'), the question stays pending forever, the asker is never told, and the host has no way to recover it. A host who steps away for a coffee loses the queue.

**Fix:** Give the approval view `timeout=None`, persist the pending question's DM message id in the payload and rebuild the views in `recover_game`; failing that, add `on_timeout` that marks the entry expired and DMs the asker 'not reviewed in time'.

### social-prompt-37 — Rushmore has had no real play since the 07-20 UX round; snake pacing still floods the channel
*gameplay · medium · S · confirmed* · game: rushmore · queue: **P9**

**Where:** src/bot_modules/cogs/games_rushmore_cog.py:903-908

**What:** The engine got the full 07-20 treatment (ping buttons, backfill, blitz, all-skip hiding) and then nobody launched it again in the main guild. Snake with 6 players is 24 timed turns of dead time for five people and ~70 self-deleting messages; blitz fixes that but is opt-in per launch. This is a discovery and default problem, not a code defect.

**Fix:** Set the dashboard Draft Mode dial to blitz for the main guild and schedule one weekly Rushmore via games_scheduled so it gets a real audience before further investment; collapse the snake turn's ping + 10s nudge into one message edited in place. Decide keep/retire from two or three scheduled runs.

**Already recorded / fixed:** docs/plans/game-ux-round-2026-07-20.md items 5-7 are shipped; this is what is left after them

### social-prompt-38 — Compliment pairings embed names members with <@id> mentions
*ux · medium · S · confirmed* · game: compliment · queue: **P1**

**Where:** src/bot_modules/cogs/games_compliment_cog.py:289-295, src/bot_modules/games_compliment/embeds.py:148-177, tests/test_embed_accent_contract.py:915

**What:** After the 15-second ping is gone the embed is the only record of who compliments whom, and an embed mention renders as a bare number for any reader whose client has not cached that member - exactly the case the embed-names rule exists for. A player scrolling back later, or a phone client, may see '<@1284869710847934544> → <@1382372076853399712>'.

**Fix:** Resolve names via `services/name_resolver.build_name_fn` for the embed lines (the no-contact degrade to 'User <id>' is fine) and keep the mentions in the content ping. Add the builder to the render-site guard test.

**Already recorded / fixed:** embed-names-rule-is-written-down memory states the rule; this site was not listed

### social-prompt-39 — Spin the Compliment has no closing beat - it ends and pays before any compliment is given
*gameplay · medium · M · confirmed with correction* · game: compliment · queue: **P9**

**Where:** src/bot_modules/cogs/games_compliment_cog.py:267-344

**What:** Participation is paid once at Close & Generate to everyone in the pool at that moment (end_game player_ids, games_compliment_cog.py:159-165), not on the Join press; the rest of the finding stands and the 30-minute follow-through numbers (5/6, 6/7, 6/6, 8/10) reproduce exactly.

**Fix:** Optional 10-minute wrap after generate: repost the pairings with a check for each giver whose reply-to or mention of their receiver was seen (`messages.reply_to_id`, `message_mentions` already exist), nudge the stragglers once, then a short recap with the payout footer; add a host 'Spin Again' button.

### social-prompt-36 — Rushmore's vote is a coin flip at its 3-player floor and degenerate at 2
*gameplay · low · S · confirmed with correction* · game: rushmore · queue: **P9**

**Where:** src/bot_modules/games_rushmore/logic.py:158-191, src/web_server/static/js/panels/games-rushmore.js:18, src/bot_modules/cogs/games_rushmore_cog.py:184-186

**What:** Rushmore has no tie-break (every tied uid wins and is paid), spectators can vote but the vote embed never says so, and the dashboard/clamp permit a 2-player draft that always ties 1-1. The 3-player case ties only on a vote cycle, and prod game 195's tie came from a dev-seeded fake seat (900000001) that could not vote, not from organic play.

**Fix:** Tie-break in `tally_votes` on fewest skips, then lowest total pick time (both already in `skipped`/`pick_times`), and state 'anyone in the channel can vote' on the vote embed. Raise the clamp floor to 3 so a 2-player draft cannot be started from the dashboard.

### social-prompt-40 — AMA records a different player_count depending on which path ended it
*backend · low · S · confirmed* · game: ama · queue: **P2**

**Where:** src/bot_modules/cogs/games_ama_cog.py:1059, src/bot_modules/games/utils/game_roster.py:99-117, expiry_service.py:47-54

**What:** The two end paths disagree on what a 'player' is, so the engagement column the brief warns about is unreliable for AMA specifically: the same session would record 6 via the recap path and 9 via the sweep. Payout rosters agree; only the history column drifts.

**Fix:** Have `_do_close` pass `player_count=len(participants)` (the roster it already builds), so both paths write the same number; or make the recap say 'asked by M people, answered by K'.

### social-prompt-41 — AMA finds its ping role by name instead of a dashboard dial
*ux · low · S · confirmed* · game: ama · queue: **P9**

**Where:** src/bot_modules/cogs/games_ama_cog.py:702-707, src/web_server/static/js/panels/games-ama.js:7-11

**What:** Configuration lives on the dashboard by house rule; here it lives in a role's spelling. Renaming or recreating the @AMA role silently stops the hot-seat announcements Billy said he wants, and a second guild cannot point the ping at its own role.

**Fix:** Add a 'Hot-seat ping role' dial to the AMA panel (read in `_ama_role_mention`), default none, and drop the by-name lookup. Small; keep the announcement block itself untouched.

**Already recorded / fixed:** ama-hot-seat-ping-is-intentional memory (the ping, not the lookup)

### social-prompt-42 — AMA hot-seat 1-hour timer is not re-armed after a restart
*backend · low · S · confirmed* · game: ama · queue: **P9**

**Where:** src/bot_modules/cogs/games_ama_cog.py:684-700

**What:** A seat recovered after a restart never times out: it sits until four more questions are asked or a mod skips, which in a slow room means the same person is 'in the hot seat' all day and the queue never advances.

**Fix:** Persist the seat's start time in the payload and call `_start_hot_seat_timer(channel)` with the remaining time in `recover_game`.

### social-prompt-43 — Open AMA question cards keep working after the game has ended
*ux · low · S · confirmed with correction* · game: ama · queue: **P9**

**Where:** src/bot_modules/cogs/games_ama_cog.py:1019-1062

**What:** Leftover Reply/Pass cards keep working only until the next bot restart (recover_game re-registers views solely for rows still in games_active_games; afterwards the buttons fail rather than act), and the asker DM links the answered card itself, not a dead game — the rest (no prune on any end path, no _closed guard in ReplyModal, payload write lost, quest/audit/DM still fire) is as stated.

**Fix:** On close (all paths) iterate unresolved questions with a message id and `_prune_question_message_view(channel, msg_id, footer_text='AMA ended')`, mirroring the 7-day prune.

### social-prompt-44 — AMA embeds print the raw mode enum
*ux · low · S · confirmed* · game: ama · queue: **P9**

**Where:** src/bot_modules/games_ama/embeds.py:47

**What:** Lobby, main and panel embeds show 'unfiltered' / 'screened' in lowercase enum form while the recap Title-Cases it; embed_style_guide asks for Title Case.

**Fix:** Use `mode.title()` (or a label map 'Unfiltered' / 'Screened') in the three builders.

### social-prompt-45 — Rushmore recap 'Hand Off' button hands nothing off
*ux · low · S · confirmed with correction* · game: rushmore · queue: **P9**

**Where:** src/bot_modules/cogs/games_rushmore_cog.py:607-623

**What:** Rushmore AND Price recap 'Hand Off' buttons (games_rushmore_cog.py:607-623, games_price_cog.py:482-496) are host/mod-gated, hand nothing off (ephemeral command hint only) and disable Run Again; already flagged as a dead-end in docs/reviews/2026-07-23-novel-hunt.md S3, where only the command-path copy was fixed. Remove the button or make it relaunch under the presser; relaxing Run Again's host gate is a separate design call.

**Fix:** Let any member press Run Again to relaunch as the new host (that is the hand-off), and remove the Hand Off button.

### social-prompt-46 — Compliment 'Join' is a hidden toggle
*ux · low · S · confirmed with correction* · game: compliment · queue: **P9**

**Where:** src/bot_modules/cogs/games_compliment_cog.py:237-265, games_rushmore_cog.py:221-271

**What:** Compliment's single 'Join' button is an unlabelled join/leave toggle (games_compliment_cog.py:60-88) while Clapback/Story/MLT/Rushmore use separate Join/Leave buttons; a double-tap removes the member, which IS visible (ephemeral says 'removed from the pool' and the Pool field updates) but nothing on the lobby warns beforehand. Either add Join/Leave like the siblings or add AMA-style 'tap again to leave' copy (games_ama_cog.py:770-773).

**Fix:** Split into Join / Leave like Rushmore, or relabel to 'Join / Leave' - consistent button shapes across the family.

### social-prompt-47 — AMA no-contact gate branches have no test at the logic or cog layer
*backend · low · S · confirmed* · game: ama · queue: **P9**

**Where:** src/bot_modules/cogs/games_ama_cog.py:190-213, tests/cogs/test_games_ama_panel_flow.py:314

**What:** CLAUDE.md treats a passing test as the enforcement of a safety gate. The AMA gate is the most branchy one in the repo (byte-identical impersonated replies per mode) and nothing asserts that a blocked pair is refused, that the reply text matches the real path, or that the picker drops the partner.

**Fix:** Add a parametrised test in tests/cogs/test_games_ama_panel_flow.py: blocked pair x {screened, unfiltered-panel, unfiltered-hot-seat} asserting the exact constant reply and no channel send; one for approval-time drop; one for the picker filter.

### social-prompt-48 — Dead AI idle-question code still shapes AMA payload and docs
*backend · low · S · confirmed* · game: ama · queue: **P9**

**Where:** src/bot_modules/games_ama/logic.py:108-132, src/bot_modules/cogs/games_ama_cog.py:100-104

**What:** The idle-question generator is gone but its helpers, the asker_id 0 sentinel handling and its docstrings remain, which is how a future reader ends up 'restoring' a feature that never should post under a real asker.

**Fix:** Delete `first_content_line` and the `source` parameter with their tests; keep the `asker_id > 0` filters (cheap defence) but reword the comments.

## Would You Rather, Most Likely To, Never Have I Ever, Two Truths and a Lie

### vote-games-49 — Every WYR bank row is malformed (no '|'), so a bank-fed /wyr ends in the same second
*gameplay · medium · S · confirmed with correction ×2* · game: wyr · queue: **P2**

**Where:** src/bot_modules/games/utils/question_source.py:34-47, src/bot_modules/cogs/games_wyr_cog.py:418-427, routes/games.py:379-395

**What:** WYR's bank source is dead in prod: all 23 wyr rows are 'Would you rather X, or Y?' prose with no '|' (added 08-28 and 09-03 via the dashboard, which gives no format hint and does no validation), and since 7ab7e562 (07-28) removed the AI fallback that used to mask that, a default /wyr marks one row served and ends in the same second with 'bank is empty'.

**Fix:** S: in get_wyr_question fall back to splitting on ', or ' / ' or ' when '|' is absent (and strip a leading 'Would you rather'); validate the wyr format in create_question/bulk_add on the dashboard (reject or auto-convert); repair the 18 prod rows. Add a logic test for the fallback.

### vote-games-50 — Empty bank ends the game instead of waiting for a posed prompt; MLT bank is empty in prod so MLT dies at Start
*gameplay · medium · M · confirmed with correction ×2* · game: mlt · queue: **P2**

**Where:** src/bot_modules/cogs/games_mlt_cog.py:490-505, games_wyr_cog.py:418-427, games_nhie_cog.py:320-327

**What:** When the bank has no prompt and the pose queue is empty, MLT/WYR/NHIE end the game while telling the host to use a Pose button that end_game has just removed (games_mlt_cog.py:490-505, games_wyr_cog.py:414-427, games_nhie_cog.py:313-327); with 0 mlt rows in prod a default /mlt discards its 3+-player lobby at round 1. MLT and WYR do post standings / pay the voter roster on that path; NHIE does not — games_nhie_cog.py:325 ends with no bot/player_ids, so an NHIE session that runs dry pays nobody and shows no recap.

**Fix:** M: when no prompt is available, post the round in a 'waiting for a prompt' state with only Pose enabled (or open the pose modal for the host directly) instead of ending; seed the MLT bank from the dashboard; correct spec line 174 to say the game waits/ends rather than falling back to AI.

### vote-games-51 — NHIE declares a winner after round 1 when exactly one person voted, and pays them
*gameplay · medium · S · confirmed with correction ×2* · game: nhie · queue: **P2**

**Where:** src/bot_modules/games_nhie/logic.py:82-123, src/bot_modules/cogs/games_nhie_cog.py:418-474

**What:** NHIE declares 'last one standing' after any round in which exactly one member has voted (no lobby/floor, players tracked only on vote; find_winner treats a one-entry tracker as a win) — reproduced. The payout detail is different from the finding: the win bonus goes to the guiltiest player (_winners_nhie), so a solo voter is paid game_win only if they voted Guilty; an Innocent solo vote pays participation only; the host bounty is not paid.

**Fix:** S: only resolve 'winner'/'all_eliminated' once at least two players have ever been registered and at least one elimination has happened (track a max-roster in the payload); until then treat the round as 'continue'. Add the one-voter and two-voter-no-elimination rows to test_games_nhie_logic.py.

### vote-games-52 — WYR/MLT/NHIE have no end condition and no End button; the only finish is a red 'Force-Closed' embed or the 24h sweep
*gameplay · medium · M · confirmed* · game: vote-games · queue: **P2**

**Where:** games_wyr_cog.py:188-266, games_mlt_cog.py:319-343, games_nhie_cog.py:134-192

**What:** A host who stops pressing Next leaves a live game that blocks the channel (one active game per channel) until someone runs /games end, and the ending they get reads as an abort. MLT's crowns never get a finale on the normal exit, WYR never gets any recap (its 'most divisive question' exists only inside /recap). Nothing marks 'we finished' for players, which is the payoff moment these formats live on.

**Fix:** M: add a host/mod '🏁 End Game' button (row 2) on the three round views that closes the round, posts a recap (WYR: most divisive question + total votes; MLT: build_final_standings_embed; NHIE lives-0: guilt board) and ends via the paying path; make /games end for these types call the same recap instead of the force-closed embed.

### vote-games-53 — No round timer or vote target in any of the four; every round waits on the host
*gameplay · medium · M · confirmed* · game: vote-games · queue: **P2**

**Where:** games_ttl_cog.py:525-532, docs/games_system_spec.md:303, games_wyr_cog.py:227-237

**What:** Pacing is entirely host attention: if the host looks away the room stalls; if the host is fast, late voters get cut off. WYR/NHIE are 5-10 second decisions, so a 45-90 s auto-advance (or 'advance when N votes are in') would remove most dead time and make the games survive an absent host.

**Fix:** M: add an optional per-launch 'seconds per round' (slash arg + scheduler option, 0 = host-paced) to wyr/nhie/mlt reusing TTL's asyncio.wait_for pattern; optionally auto-advance when votes >= last round's voter count.

### vote-games-54 — MLT vote select and crown embed never consult the no-contact list
*backend · medium · S · confirmed* · game: mlt · queue: **P1**

**Where:** games_mlt_cog.py:260-274, games_mlt/embeds.py:371-406, games_ama_cog.py:800-813

**What:** 'Most likely to <prompt>' is a directed pick of one member by another, published with a crown. A blocked member can be voted onto a spicy prompt by the person they have no-contact with, and vice versa. CLAUDE.md's rule is that any surface putting two members in contact consults the list and refuses indistinguishably.

**Fix:** S: in _vote_select_callback, if is_no_contact_conn(voter, target) send the normal '✅ Voted' ack but do not record the vote (the results only show counts, so it is invisible); add MLT to the no_contact_spec surface table. Billy's call whether TTL (undirected voting) needs the same.

### vote-games-55 — <@id> inside embeds: WYR Reveal Voters and TTL Final Results violate the embed naming rule
*ux · medium · S · confirmed* · game: wyr · queue: **P1**

**Where:** src/bot_modules/games_wyr/embeds.py:69-70, src/bot_modules/cogs/games_ttl_cog.py:548-555, games_ttl/embeds.py:436-467

**What:** Anyone whose client has not cached the voter sees a bare number in the reveal and the TTL winners list — the third recurrence of this defect class per memory. TTL already builds a resolver for the reveal embed (:256-274 uses display_name) and only the recap swapped to mentions.

**Fix:** S: pass resolved display names (resolve_names / build_name_fn) into both builders; keep the TTL winner ping in content= via mention_resolver as now. Add both builders to the embed-name contract test.

### vote-games-56 — TTL recovery re-drives guessing from a lobby, bypassing the 2-submission Start check
*backend · medium · S · confirmed with correction* · game: ttl · queue: **P2**

**Where:** src/bot_modules/cogs/games_ttl_cog.py:584-606, games/utils/recovery.py:88-135, tests/cogs/test_games_recovery.py:407

**What:** TTL recovery re-drives guessing for any row with submissions and cannot tell a lobby from a game in progress because the cog never advances state past 'joining' (no update_game_state call anywhere in games_ttl_cog.py; MLT :174 is the only game that does). The fix must first persist the phase on Start, then branch in recover_game; a state check alone would misfire on every existing row.

**Fix:** S: in recover_game, if row['state'] == 'joining' re-register a TTLSubmitView on the lobby message and return True; only re-drive when state is 'guessing'. Add a test row for the joining case.

### vote-games-57 — TTL at the 2-player floor is one guess per round, and pure guessers are never paid
*gameplay · medium · S · confirmed with correction* · game: ttl · queue: **P9**

**Where:** games_ttl_cog.py:176

**What:** At the floor of 2 each TTL round is decided by a single vote (subject cannot vote, :292). Voters who never submitted are paid on no end path: the cog's roster is played subjects (:544), game_roster._ttl (:80-90) mirrors that for the sweep and /games end, and update_scores only records a guesser at all when they guessed right. Prod game 0f7ca919 had one such unpaid non-subject guesser. Fix must widen both the cog roster and _ttl (e.g. persist every voter id per round), not just the cog.

**Fix:** S: set the floor to 3 in the Start check and HOW_TO_PLAY/manual copy; build player_ids from scores keys (subjects + correct guessers) or from every voter seen, mirroring _ttl in game_roster.py.

### vote-games-58 — WYR and NHIE persist the current round's votes only on Next, so a restart mid-round rebuilds an empty tally under a full bar
*backend · medium · S · confirmed* · game: wyr · queue: **P2**

**Where:** games_wyr_cog.py:430-431, games_nhie_cog.py:337, games_mlt_cog.py:288-294

**What:** After a restart the recovered message still shows the bars from before, but the view's lists are empty: the next Next records zero votes for that round (no guilt/lives applied in NHIE), and voters who tap again see '(changed)' semantics reset. MLT already does this right.

**Fix:** S: modify_payload the a/b (guilty/innocent) lists on each vote as MLT does; the LiveBarUpdater already rate-limits the message edit, and payload writes are cheap.

### vote-games-59 — Expired-at-Next path is a bare end_game in all three round games: no payout, player_count 0, guild_id 0, last round dropped
*backend · low · S · confirmed* · game: vote-games · queue: **P2**

**Where:** games_wyr_cog.py:494-498, games_mlt_cog.py:601-605, games_nhie_cog.py:406-410

**What:** A host who presses Next on a >24h game between sweeps archives it as a guild-0 empty row and nobody is paid; it is one of the writers of the guild_id=0 rows the brief flagged. Small window, but it is the same defect the roster module was built to close.

**Fix:** S: replace the three bare calls with force_end_active_game-style end_game(bot=self.bot, player_ids=roster_from_payload(...)) after writing the round's votes; same for nhie's empty-bank branch.

**Already recorded / fixed:** docs/games_system_spec.md:217 'Which end paths pay — all of them' documents the intent; these three sites still do not.

### vote-games-60 — MLT dashboard dials contradict the code's 25-player clamp and '0 = no limit'
*ux · low · S · confirmed* · game: mlt · queue: **P9**

**Where:** src/web_server/static/js/panels/games-mlt.js:5-9, src/bot_modules/games_mlt/logic.py:48-60, games_mlt_cog.py:412-416

**What:** An admin can save 'max 60' or '0 = unlimited' and the lobby silently turns the 26th joiner away with 'this game is set to take up to 25 players'. Low stakes given the game's usage, but it is a dial that does not do what its hint says.

**Fix:** S: cap the panel inputs at 25, reword the hint ('up to 25 — the vote menu holds 25 names'), and make the Discord copy read the configured floor.

### vote-games-61 — WYR Reveal Voters is shown to everyone but works only for host/mod — 5 refused presses in one game
*ux · low · S · confirmed* · game: wyr · queue: **P9**

**Where:** games_wyr_cog.py:239-247, core/utils.py:48-60

**What:** Players clearly want to see who picked what. The button is a permanent invitation they cannot use, and the 'Anonymous' footer badge is constant because payload['anonymous'] is always True (games_wyr_cog.py:349) — there is no way to run a named round.

**Fix:** S: either hide the button for non-hosts by moving reveal to the host's Next flow (reveal-then-advance), or let a voter self-reveal; drop the always-on badge or make anonymity a real per-launch choice.

### vote-games-62 — NHIE pose queue is still uncapped (known), and self-votes in MLT can crown everyone
*gameplay · low · S · confirmed with correction* · game: nhie · queue: **P9**

**Where:** games_nhie_cog.py:66-78, games_mlt_cog.py:281-286, games_mlt/logic.py:156-170

**What:** NHIE's pose queue is still uncapped (games_nhie_cog.py:66-78 vs the 15-cap in wyr/mlt) — already queued as docs/plans/common-lib-round-2.md item 3, unfixed on every live branch; nothing new to add. MLT self-votes and all-tied co-crowns are documented design (games_system_spec.md:25, constants.py:328, test_games_mlt_logic.py:200, games_mlt/logic.py:14), not a defect — at most a gameplay-taste suggestion to break top-score ties by excluding self-votes.

**Fix:** S: apply the 15 cap in PoseStatementModal; reject a self-vote in _vote_select_callback with an ephemeral, or keep it but break ties by excluding self-votes.

**Already recorded / fixed:** docs/plans/common-lib-round-2.md item 3 (NHIE queue) — still unfixed at games_nhie_cog.py:71; the MLT self-vote point is new.

## FFA, Hot Takes, Fantasies, Marry/F/Kill, Story

### anon-tail-67 — MFK assignments never consult the no-contact list
*backend · high · M · confirmed with correction ×2* · game: mfk · queue: **P1**

**Where:** src/bot_modules/games_mfk/logic.py:119-129, src/bot_modules/cogs/games_mfk_cog.py:112-145

**What:** Neither of the two party games that assign members to each other consults the no-contact list: MFK's assign_targets (games_mfk/logic.py:119-129, posted publicly by games_mfk_cog.py:112-145) and Spin the Compliment's generate_pairings (games_compliment/logic.py:48-59, posted publicly by games_compliment_cog.py:106-141). Two members on the list who both Join can be paired and prompted, in public, to address each other.

**Fix:** Build blocked = {uid: no_contact_partners_conn(conn, guild_id, uid)} at Close & Assign and pass it into assign_targets so a blocked pair is excluded from each other's sample (fall back to fewer than three names only if the pool is too small, silently). Add a logic test with a blocked pair.

### anon-tail-63 — Hot Takes 'voting is starting' ping publicly names every anonymous submitter
*backend · medium · S · confirmed ×2* · game: hottakes · queue: **P1**

**Where:** src/bot_modules/cogs/games_hottakes_cog.py:149-161, src/bot_modules/games_hottakes/embeds.py:42

**What:** At Start Voting the cog builds the set of take authors and @-mentions them all in a public message ('get ready to vote!'). With one submission the take is fully attributed; with two it is a coin flip; in every case the room learns who wrote something. delete_after=15 does not unsend the mention notification. The prior review verified 'name withheld in-channel' at the vote and recap embeds and missed this ping.

**Fix:** Drop the per-submitter mentions: ping the games role / use game_start_ping_service like the lobby games, or post the same line with no mentions. Add a logic-layer test asserting the start message carries no author ids. Contradicts no house rule; it restores the promise the lobby embed makes.

### anon-tail-64 — Fantasies voters are never paid: roster extractor reads keys the results do not contain
*backend · medium · S · confirmed ×2* · game: fantasies · queue: **P2**

**Where:** src/bot_modules/games/utils/game_roster.py:147-151, src/bot_modules/games_fantasies/logic.py:127-135, tests/test_game_roster.py:47-50

**What:** Fantasies has no paying completion site of its own (see next finding), so the sweep and /games end rebuild the roster from payload['results']. The extractor looks for same_votes/nope_votes lists that build_result_entry never stores, so only entry authors are ever credited; everyone who voted gets nothing and player_count undercounts. The unit test passes because it hand-writes the wrong key names.

**Fix:** Read r.get('voters') (already the concatenated list) in _fantasies; change the test row to the real build_result_entry shape (or build it via build_result_entry so the two cannot drift). One-line fix plus test.

### anon-tail-65 — Fantasies has no ending: recap is dead code and end_game is only reachable via the 24h sweep or /games end
*gameplay · medium · M · confirmed with correction ×2* · game: fantasies · queue: **P2**

**Where:** src/bot_modules/cogs/games_fantasies_cog.py:441-448

**What:** Fantasies has no host-facing ending: _post_recap (games_fantasies_cog.py:441) is dead code, FantasiesMainView offers only Start Round and Help, and end_game is reached only via /games end or the 24 h sweep (prod game d891397e, 08-24 → 08-25, second guild, zero entries ever submitted). Per-entry results are still shown live and on close, so the game is playable; what is missing is the recap, the payout footer, an immediate payout, and the promised 'ending' in HOW_TO_PLAY.

**Fix:** Add a host-only 'End Game' button to FantasiesMainView that posts _post_recap and calls end_game(player_count, round_count, payload, bot, player_ids=roster) — mirror Hot Takes' completion at games_hottakes_cog.py:461-480. Update HOW_TO_PLAY/manual. Consider a 'Skip round' path when a round gets zero entries.

### anon-tail-66 — Hot Takes lobby cannot survive a restart (dead buttons) and a restart with takes present auto-starts voting
*backend · medium · M · confirmed with correction* · game: hottakes · queue: **P2**

**Where:** src/bot_modules/cogs/games_hottakes_cog.py:490-492

**What:** After a restart a Hot Takes lobby with zero takes is left with dead buttons (recover_game returns False → skipped), and a lobby with any takes has voting force-started without the host pressing Start Voting, because the cog never records the phase (no update_game_state) and recover_game keys on takes/results counts alone. The stale prod lobby can be cleared today with /games end by the host or a mod and does not stop a new hosted game in that channel;

**Fix:** In recover_game branch on the phase: if voting has not started, re-register HotTakesSubmitView on the anchor message (as MFK/Story do) instead of returning False; only redrive when voting was underway. Record the phase by calling update_game_state(game_id,'playing') in start_voting. Add a Cancel button (host/mod) to the submit view.

### anon-tail-68 — MFK results embed puts the player mention in the field NAME, which Discord renders as raw <@id>
*ux · medium · S · confirmed* · game: mfk · queue: **P1**

**Where:** src/bot_modules/cogs/games_mfk_cog.py:120, src/bot_modules/games_mfk/embeds.py:115-119, tests/test_games_mfk_logic.py:338-341

**What:** Embed field names (like titles and footers) do not resolve mentions, so every row header of the 'Your Three Names' embed shows a literal <@123456789012345678>. The content line already pings everyone (:140-145), so the embed gains nothing from the mention. This is the embed-names rule from docs/embed_style_guide.md; memory notes this is at least the fourth instance.

**Fix:** Pass the resolved display_name (name_fn) as the field name; keep the mentions in message content. Flip the test to assert the name is the display name and add the builder to tests/test_embed_accent_contract.py-style guard for name_fn.

### anon-tail-69 — FFA's persistent-alias map (confession_emoji_assignments) has no TTL and survives erasure
*backend · medium · S · confirmed with correction* · game: ffa · queue: **P9**

**Where:** src/bot_modules/cogs/games_ffa_cog.py:198-204, src/bot_modules/services/confessions_service.py:148-151, privacy_service.py:603-619

**What:** confession_emoji_assignments has no TTL and is not cleared by purge_user_data, while the register row (data_register.md:57) presents the confessions family as TTL'd; an erasure request misses it (an access export does see it, since the column is `user_id`). Most of the 445 thread-less rows are simply assignments that outlived the 7-day confession_threads sweep, but FFA embed replies (games_ffa_cog.py:198-204) also land here with the game message as root, and for those the row is a real user→alias map.

**Fix:** Add the table to the confessions 7-day sweep (delete rows whose root_message_id has no thread AND are older than the TTL, or key FFA rows on game end) and to purge_user_data's confessions block; correct the register row. Small, but it is a data-register correctness fix that the 09-02 GDPR sweep did not reach.

**Already recorded / fixed:** GDPR review cc5e2a0b touched the confessions purge block but not this table

### anon-tail-70 — FFA has no ending, no payoff, and half its bank is dares an anonymous text box cannot perform
*gameplay · medium · M · confirmed with correction* · game: ffa · queue: **P9**

**Where:** src/bot_modules/cogs/games_ffa_cog.py:147-150, game_roster.py:214, prompts.py:55-81

**What:** FFA embed mode never resolves: no close button, no recap, no payout (ffa is in NO_ROSTER_TYPES so the 24h sweep pays nobody, and the payload records only reply counts, not who replied). Prod draws from games_question_bank, not prompts.py; of its 61 dare rows (of 133), 34 ask for a voice note, photo, video, nickname or status change that the anonymous text modal cannot deliver, so roughly a quarter of random-mode draws are dead on arrival.

**Fix:** Either (a) make embed-mode FFA truth-only by default (dares only via ffa_banner), track replier ids in the prompts entry, and add a host Close that posts a recap (replies per prompt, busiest prompt) and pays repliers; or (b) retire /games play ffa and keep ffa_banner as a card poster, folding anonymous truths into AMA.

### anon-tail-71 — Hot Takes and Fantasies are paced entirely by host clicks with no timer and no cancel
*gameplay · medium · M · confirmed with correction* · game: hottakes · queue: **P9**

**Where:** games_hottakes_cog.py:265-272, games_fantasies_cog.py:166-175, docs/games_system_spec.md:189

**What:** Hot Takes and Fantasies advance only on host/mod presses with an unbounded wait per take/entry and no per-take clock or vote-complete auto-advance; the lobbies have no Cancel button (the host can still `/games end`, and an open lobby blocks scheduled/rotation launches but not a hand-started game). Both games have an enable switch on the dashboard but no tunable option of any kind.

**Fix:** Add a per-take/entry auto-advance timer (default ~45s, dial in Global Config or a small shared panel) with Next as skip-ahead, auto-advance when every active voter has voted, and a Cancel button on the submit phase. Reuse games/utils/timer.py as TTL does.

### anon-tail-72 — Anonymous-submission lobbies fizzle at zero entries with nothing nudging the room
*gameplay · medium · M · confirmed with correction* · game: hottakes · queue: **P9**

**Where:** constants.py:110-112, manual.html:607, games_hottakes_cog.py:135-137

**What:** Hot Takes and Fantasies are not in LOBBY_GAME_TYPES so neither gets the start_in countdown or the host nudge; Hot Takes at zero takes has no exit but the 24h sweep or /games end, while a Fantasies round at zero entries simply returns to the lobby. Hot Takes' omission is already recorded as a follow-up in docs/plans/game-start-countdown.md:150-161 awaiting Ben's call; Fantasies is not on that list. Prod: the one 09-02 hottakes lobby is still open with zero takes.

**Fix:** Add hottakes and fantasies to LOBBY_GAME_TYPES so start_in and the host nudge apply; require at least 2 takes before voting (say why: 'need 2+ so nobody can tell whose is whose'); show a 'how to play' line in the lobby embed itself rather than behind the Help button.

**Already recorded / fixed:** docs/plans/game-start-countdown.md:150-161 records Hot Takes (not Fantasies) as a pending LOBBY_GAME_TYPES addition

### anon-tail-73 — Story: a missing writer costs 5 minutes per lap and only the host can skip
*gameplay · medium · M · confirmed* · game: story · queue: **P9**

**Where:** src/bot_modules/cogs/games_story_cog.py:58

**What:** Turn order is a shuffled fixed rotation (logic.py:123-145). The game does not stall forever — a turn times out at 5 min and an all-skip lap ends the story — but one AFK writer in a 4-writer 10-sentence game costs two 5-minute holes in a ~15-minute game, and if the host is also the AFK one nobody can skip. Writers cannot leave mid-game. Nothing here is a bug; it is dead time.

**Fix:** Drop a writer from the rotation after two consecutive timeouts (announce it), let any joined writer press Skip once 2 minutes have elapsed, and shorten the default turn to 120s with max_sentences-aware pacing. Also keep Leave usable mid-game (remove from turn_order on next lap).

### anon-tail-74 — Hot Takes authors can vote on their own take, skewing the 'hottest take' winner bonus
*gameplay · low · S · confirmed* · game: hottakes · queue: **P9**

**Where:** games_hottakes_cog.py:243-254, games_fantasies_cog.py:230-234, economy/game_rewards.py:716-723

**What:** With rooms this small (2 voters in the only real prod game), one 🔥 self-vote decides the winner and the game-win coins. Fantasies already blocks self-votes; Hot Takes does not.

**Fix:** Carry the take's author id into HotTakeVoteView and refuse the author's vote with the same ephemeral line Fantasies uses. Logic test on tally/roster unaffected.

### anon-tail-75 — Hot Takes and Fantasies copy promises anonymity without the mod-visibility disclosure FFA gives
*ux · low · S · confirmed* · game: hottakes · queue: **P9**

**Where:** games_hottakes/embeds.py:42, games_fantasies/embeds.py:25, games_ffa_cog.py:69-77

**What:** Both games record the author in anon_audit_log (and mirror to a staff channel when set) but tell members their name is 'never attached'. FFA and the manual disclose the mod view; these two embeds and their HOW_TO_PLAY text do not.

**Fix:** Add the FFA one-liner ('mods can still see who sent it') to both lobby embeds and HOW_TO_PLAY entries; update manual.html rows 585/587 to match.

### anon-tail-76 — FFA kind+tags miss reports a permissions error instead of 'no prompts match'
*ux · low · S · confirmed* · game: ffa · queue: **P9**

**Where:** games_ffa_cog.py:506-513, question_source.py:294-295

**What:** /games play ffa kind:dare tags:lily where every lily-tagged row is a truth passes the pre-check, then get_ffa_prompt returns None and the host is told the bot lacks channel permissions.

**Fix:** Pass kind into has_matching_questions for ffa (or run _resolve_prompt before deferring and answer 'No dare prompts match tags: …'), so the None from launch only ever means Forbidden.

### anon-tail-77 — Story: whoever presses Start becomes the host for Skip purposes
*backend · low · S · confirmed* · game: story · queue: **P9**

**Where:** games_story_cog.py:213

**What:** A mod who starts a lobby on the host's behalf silently takes the Skip permission away from the original host for the rest of the story (mods keep it via is_host_or_mod). Minor but surprising in the one game where Skip is the only pacing control.

**Fix:** Keep row['host_id'] as the host; do not overwrite it at Start.

### anon-tail-78 — Story: dismissed sentence modal leaks a never-completing coroutine per dismissal
*backend · low · S · confirmed* · game: story · queue: **P9**

**Where:** games_story_cog.py:74-81

**What:** Closing the modal without submitting leaves the button callback awaiting forever; play is unaffected because the outer 5-minute wait times the turn out, but each dismissal leaks a task for the process lifetime.

**Fix:** Give the modal timeout=_TURN_TIMEOUT, or drop modal.wait() and have on_submit set the view's event directly.

### anon-tail-79 — Fantasies has no NSFW gate of any kind
*backend · low · S · confirmed with correction* · game: fantasies · queue: **P9**

**Where:** —

**What:** Fantasies has no channel.is_nsfw() gate (games_fantasies_cog.py:279-293) and this was recorded as an owner decision on 08-05 (batch-bc review :27-28); it is a decision to re-confirm, not a defect. Only act if Billy wants the house NSFW rule applied here.

**Fix:** If Billy wants the house rule applied: refuse /games play fantasies unless channel_allows_nsfw(interaction.channel) (Traditional's pattern). Otherwise leave as decided.

**Already recorded / fixed:** docs/reviews/2026-08-05-games-batch-bc.md:27-28 (owner decision, mod-policed channels)

### anon-tail-80 — MFK spec describes a deterministic slice; code samples independently per player
*backend · low · S · confirmed* · game: mfk · queue: **P9**

**Where:** docs/games_system_spec.md:189, src/bot_modules/games_mfk/logic.py:124-129

**What:** Reference spec and code disagree on the assignment rule (code wins). Independent sampling means the popular member can appear on everyone's card and someone can appear on nobody's, which is also a small fairness gap for an icebreaker.

**Fix:** Fix the spec line; optionally move to a derangement-style balanced assignment (games/utils/derangement.py exists) so every member appears roughly 3 times.

## Traditional Truth or Dare, Name Your Price, LegitLibs

### trivia-tail-81 — Bank rows tagged "Nsfw" bypass the NSFW channel gate (44 price, 19 nhie, 5 clapback rows)
*backend · high · S · confirmed ×2* · game: price · queue: **P1**

**Where:** src/bot_modules/games/utils/question_source.py:106-111, src/web_server/routes/games.py:137-146

**What:** The one safety rule for bank draws is 'rows tagged nsfw are excluded unless the channel is age-restricted'. The 08-28 import wrote the tag as "Nsfw", so every Name Your Price scenario — and 19 NHIE + 5 Clapback prompts — is treated as SFW and can be served in a non-age-gated games channel. Clapback is the most-played game (30 rounds in 30 days), so this is live exposure, not theoretical.

**Fix:** Lowercase tags at both ends: `_parse_tags` returns `{t.lower() ...}` and `_norm_tags` lowercases before dedupe; add a pytest.param row to the bank-filter test with tag "Nsfw"/"NSFW". One-off UPDATE on prod to normalise the 68 rows (read-only here; needs Billy).

### trivia-tail-82 — LegitLibs starter pack never loads: seed path resolves to a file that does not exist
*backend · medium · S · confirmed with correction* · game: legitlibs · queue: **P9**

**Where:** src/bot_modules/cogs/games_legitlibs/__init__.py:16, src/bot_modules/cogs/games_legitlibs/data.py:169-186, data.py:122-126

**What:** The starter pack never loads for three stacked reasons: (1) `_SEED_PATH` resolves to <repo>/templates_seed.json, which does not exist (real file: src/bot_modules/games/templates_seed.json); (2) even with the path fixed, every seed row would fail with sqlite 'datatype mismatch' because the seed uses string template_ids ("seed-t1-001") against an INTEGER PRIMARY KEY column — caught and logged per row by data.py:206, importing 0;

**Fix:** Point `_SEED_PATH` at `bot_modules/games/templates_seed.json` (or import it via importlib.resources), and drop the 'skip if any published template exists' guard in favour of INSERT OR IGNORE per template_id so the pack backfills existing guilds. Add a logic test that opens the resolved path.

### trivia-tail-83 — Derived player range makes every 5–9-blank template a one-player lobby and caps all templates at 5 players
*gameplay · medium · M · confirmed* · game: legitlibs · queue: **P9**

**Where:** src/web_server/routes/games.py:875-892, src/web_server/static/js/panels/games-legitlibs.js:144-160, src/bot_modules/cogs/games_legitlibs/validation.py:8

**What:** The cap is derived from a Classic-mode assumption (each player fills 5–10 blanks) but is enforced in both modes. In Quiplash everyone fills every blank, so the ideal template is short — yet a 5-blank template saves as player_max 1, the host is auto-joined, and nobody else can press Join. The ceiling is 25//5 = 5 players for any template, which is below a normal Golden Meadow game night (Clapback averages 5.7).

**Fix:** Make the range mode-aware: Quiplash ignores the derived cap (or uses a generous fixed ceiling like 12) and Classic keeps it; on the dashboard, show the range per mode and stop re-deriving on update when blanks did not change. Add pytest.param rows for 5-blank and 25-blank templates in both modes.

### trivia-tail-84 — Traditional has no ending in practice: 0 of 19 games closed by End Game, recap and payout footer never seen
*gameplay · medium · M · confirmed with correction* · game: traditional · queue: **P9**

**Where:** src/bot_modules/cogs/games_traditional_cog.py:301-357, src/bot_modules/games/utils/expiry_service.py:1-16

**What:** 18 of 19 traditional games were reaped by the 24 h sweep (24.1–25.9 h after start); the 19th (history 134) ended 8.6 h in via a bare end_game with an empty payload. None of the 19 went through the End Game button, so the recap embed and the '+N to everyone who played' footer have never been posted; since 07-29 the sweep pays the roster silently (history 215 shows 10/10 recorded) and posts nothing in the channel. Everything else in the finding stands.

**Fix:** Give the game a natural end: auto-close when select_next_question_target returns None (every pair asked) or after N minutes idle, posting the same recap; and make the recap the loud moment (per-category counts already exist in build_recap_embed).

**Already recorded / fixed:** expiry_service.py docstring records the 18-of-18 sweep history; the button itself landed 07-29 (spec :189). New here: it is still unused and the sweep posts no recap.

### trivia-tail-85 — Name Your Price starts with no lobby, no ping and a 2-minute silent hold on the host
*gameplay · medium · M · confirmed* · game: price · queue: **P9**

**Where:** src/bot_modules/cogs/games_price_cog.py:648-689, constants.py:100-102

**What:** A member who runs /games play price sees a placeholder embed and, as host, a ping to write a scenario; the room sees nothing that invites them in, and there is no start_in countdown or Join button. If the host takes the full 2 minutes the channel is dead for that long each round (default source is 'host', so five modals per game). Compare Clapback, which runs 30 rounds a month with a lobby and start_in. Nothing tells the host that the bank already holds 44 scenarios.

**Fix:** Add price to the lobby family (LOBBY_GAME_TYPES + start_in) with Join/Start, default `source` to 'bank' when the bank is non-empty, and post the scenario prompt as an ephemeral button to the host instead of a public ping. Once a lobby exists, pass its roster as expected_players (next finding).

### trivia-tail-86 — Price auto-advance is dead code: expected_players is never passed, so every round waits the full timer
*backend · medium · S · confirmed* · game: price · queue: **P9**

**Where:** src/bot_modules/cogs/games_price_cog.py:111

**What:** Even when everyone in the room has submitted, the submission phase runs its full 30s (dashboard 'Seconds to Name a Price'), then 5s reveal pause, then the 20s vote (which does early-exit via all_voted at :422-427). Over 5 rounds that is ~2.5 minutes of pure waiting on top of the host-modal holds. It is the kind of dead time that makes a Discord game feel broken rather than paced.

**Fix:** Once a lobby exists, construct PriceGameView with expected_players=len(roster); without a lobby, at minimum let the host's Skip button be the documented way to advance (it exists at :352-359 but the embed copy never mentions it). Test: a view with expected_players=2 and two prices calls skip_timer.

### trivia-tail-87 — Price voting degenerates at 2 players and has no in-game end button
*gameplay · medium · S · confirmed with correction* · game: price · queue: **P9**

**Where:** src/bot_modules/cogs/games_price_cog.py:199-201, games_config_cog.py:66-68

**What:** Price has no player floor at all (launch() creates the game in 'playing' with no min check) and any channel member can vote (the selects never check submitter membership). With two submitters and only them voting, self-vote refusal forces a 1-1 cross-vote, so `tally_winners` crowns both players in both categories every round and the recap ties as well — the payoff is a guaranteed double tie, not 'the same person crowned'.

**Fix:** Require 3 submitters for the vote (fall back to 'reveal only' at 2, like the 1-submission path), and add an End Game button (host/mod) to PriceGameView that calls the existing end path with the roster from payload['rounds'] so it pays. Delete `_end_game` or wire it. Logic test: tally_winners with two voters who must cross-vote.

### trivia-tail-88 — Traditional allows duplicate lobbies in one channel — hosts have double-started four times
*ux · medium · S · confirmed with correction* · game: traditional · queue: **P2**

**Where:** src/bot_modules/cogs/games_traditional_cog.py:372-395, src/bot_modules/cogs/games_legitlibs/__init__.py:78-83

**What:** Traditional's slash entry (games_traditional_cog.py:372-395) has no active-game check and the platform (create_game, no unique channel_id) does not enforce one lobby per channel, so a host can double-start in one room; prod shows two same-channel double-starts by the same host (99/100 on 07-14, 128/129 on 07-24) — 86/87 and 106/107 are in two different channels. Only LegitLibs (games_legitlibs/__init__.py:78-83) guards this;

**Fix:** Add the same get_active_game(channel) guard as LegitLibs, replying ephemerally with a jump link to the live lobby; a one-line wiring assertion in the cog test.

### trivia-tail-89 — Traditional is a host-labour game: every question is a modal, and Bank Round is not the default path
*gameplay · medium · M · confirmed* · game: traditional · queue: **P9**

**Where:** src/bot_modules/cogs/games_traditional_cog.py:192-220

**What:** For a 10-player room the host wrote 10 questions by hand in one sitting while 82 curated bank questions sat unused. The How-to-Play copy (constants.py:269-283) presents Ask Question first and Bank Round as a tip. The game is one-shot per (player, category) — after each player has been asked once per opted-in category, select_next_question_target returns None (logic.py:122-150) and the host is told 'All player/category combinations have been asked!' with nothing to do next.

**Fix:** Make Bank Round the primary button (or auto-serve a bank question when the host presses Ask and let them edit/replace it in the modal via `default=`), and allow a second pass per category once every pair has been asked (track a round counter instead of a single asked key). Logic tests exist for the selection helpers;

### trivia-tail-90 — LegitLibs is one story per command, up to ~8 minutes, with no Run Again
*gameplay · medium · M · confirmed* · game: legitlibs · queue: **P9**

**Where:** src/bot_modules/cogs/games_legitlibs/modes/classic.py:55-57, modes/quiplash.py:44, games_price_cog.py:434-499

**What:** Coded floor is player_min from the template (2 today). A round is join → up to 5 minutes of filling → optional 45s rescue claim → 2 minutes rescue fill → one reveal embed → game over. The payoff is a single filled story; to play another the host re-types /games play legitlibs with tier and mode again. There is nothing that brings the room back for a second story, which is where mad-libs actually gets funny.

**Fix:** Add a recap view with 'Another one' (same tier/mode, next template from the pool) and shrink FILL_TIMEOUT to ~120s with early exit (already present when everyone submits). Consider Quiplash-style voting on the revealed versions so there is a winner to pay the game-win bonus to (resolve_winners has no legitlibs resolver).

### trivia-tail-91 — Duplicate check_game_enabled guard in the LegitLibs slash entry (merge artifact)
*backend · low · S · confirmed* · game: legitlibs · queue: **P9**

**Where:** src/bot_modules/cogs/games_legitlibs/__init__.py:65-70

**What:** Harmless but confusing; the second copy can never fire. The recovered-branch commit and the IA-branch commit both added the guard and the merge kept both.

**Fix:** Delete lines 72-76.

**Already recorded / fixed:** Guard itself: main 7d19c458 (IA appendix #82). The duplicate is unfixed.

### trivia-tail-92 — LegitLibs panel mounts the status section twice; the labelled hint from 8bb90ff3 is overwritten
*ux · low · S · confirmed* · game: legitlibs · queue: **P9**

**Where:** src/web_server/static/js/panels/games-legitlibs.js:81-90, games-panel-shared.js:28

**What:** The commit that explained what the switch does ('scheduled one is skipped… templates below stay editable') is silently replaced by the generic hint, and the config endpoint is fetched twice on every panel open. The first mount's async loadConfig then fills the second mount's checkbox, so it works by accident.

**Fix:** Delete the second mount at :570-576 (keep the labelled one). Browser panel-load check will confirm a single status section.

### trivia-tail-93 — Tier clamp only logs; spec and edge-case table promise an ephemeral warning
*ux · low · S · confirmed* · game: legitlibs · queue: **P9**

**Where:** src/bot_modules/cogs/games_legitlibs/modes/classic.py:72-78, modes/quiplash.py:60-66, docs/games_system_spec.md:197

**What:** A host who asks for tier 4 in a capped channel gets a tier-2 story with no explanation. Inert in prod until someone sets a cap on Games Config → Allowed Channels, at which point the spec is wrong.

**Fix:** Have the cog compute the clamp before defer (get_channel_max_tier is one query) and send the ephemeral note from the slash entry, or fix the spec row to say 'silently'. Reference spec: code wins, so update the spec in the same commit either way.

### trivia-tail-94 — Dead 'AI generated' scenario choice survives in the schedule schema and spec; in-game AI generation no longer exists
*backend · low · S · confirmed* · game: price · queue: **P9**

**Where:** src/bot_modules/games/constants.py:389-396, src/bot_modules/cogs/games_price_cog.py:704-708, docs/games_system_spec.md:31

**What:** An admin scheduling Price can pick 'AI generated' and get bank scenarios; the Reference spec describes a slash signature and a fallback that do not exist. The AI Prep failure UX on the dashboard is fine (502 'check ANTHROPIC_API_KEY' → toast at games-legitlibs.js:337-338); the problem is documentation and a dead dial, which CLAUDE.md forbids.

**Fix:** Remove 'ai' from both SCHEDULE_OPTION_SCHEMA entries, correct spec :31 and :174, and let test_game_dials_are_enforced cover schedule choices too.

### trivia-tail-95 — Quiplash reveal pays every joined player even when nobody submitted
*backend · low · S · confirmed* · game: legitlibs · queue: **P9**

**Where:** src/bot_modules/cogs/games_legitlibs/modes/quiplash.py:305-352, classic.py:606-631

**What:** Two members can join, press nothing for 5 minutes, and collect the participation reward (econ_reward_game_participation is set in all three guilds). The anti-farm rule elsewhere is 'a game earns what it played' (expiry_service.py:9-13). Low because the reward is small and the game is unplayed, but it is a farm loop that costs 5 minutes.

**Fix:** Pay `list(complete.keys())` in Quiplash and `unique_contributors(fills)` in Classic; record player_count as joined players but player_ids as contributors. One param row in the quiplash/classic logic tests.

### trivia-tail-96 — Host-written Ask Question ignores a channel that lost its age-restriction mid-game
*backend · low · S · confirmed* · game: traditional · queue: **P9**

**Where:** src/bot_modules/cogs/games_traditional_cog.py:198-205, logic.py:219-234

**What:** The toggle is gated correctly (:166-171), but if a mod un-flags the channel after players opted into NSFW categories, the host is still prompted to write an NSFW Truth for someone in a now-SFW channel. The unlocked read-modify-write can also drop a concurrent toggle. Both are edge cases; listed because the NSFW rule is a house rule.

**Fix:** Apply filter_nsfw_prefs in ask_question before select_next_question_target, and route the modal's record_asked through modify_payload. Add a param row to the existing NSFW-gate logic tests.

### trivia-tail-97 — Price host prompt pings with <@id> in a public message every round and 'Hand Off' just tells you to retype the command
*ux · low · S · confirmed* · game: price · queue: **P9**

**Where:** src/bot_modules/cogs/games_price_cog.py:717-720

**What:** Five public pings per game for a modal only the host can open, and a hand-off button that hands nothing off. Content mentions are allowed by the style guide, so this is polish: an ephemeral prompt to the host (interaction is available on Run Again, and the scheduler path can keep the public fallback) would remove the noise, and Hand Off should relaunch with the presser as host the way Run Again does.

**Fix:** Send the scenario prompt ephemerally when an interaction is in hand; make Hand Off call cog.launch with host_id=interaction.user.id; drop the dead edit_original_response.

### trivia-tail-98 — Spec claims Traditional has a join-pool/close-pool phase it does not have
*ux · low · S · confirmed* · game: traditional · queue: **P9**

**Where:** docs/games_system_spec.md:189, src/bot_modules/cogs/games_traditional_cog.py:408-415

**What:** Reference spec, wrong on the game's shape; a maintainer reading the spec would look for a phase transition that is not there. Code wins, so the spec line needs correcting — or, if a pool phase is wanted, it pairs naturally with the auto-close finding above.

**Fix:** Correct spec :189 to drop traditional from that sentence (or implement the phase alongside the ending fix).

## Photo Challenge and external game bots

### photo-external-100 — External tracking has no health signal — silent parser breakage is invisible on the panel
*ux · medium · M · confirmed* · game: external · queue: **P9**

**Where:** src/web_server/routes/games_external.py:82-100, src/web_server/static/js/panels/games-external.js:179-185

**What:** The panel tells an admin how many raw messages were banked, which keeps growing whether or not a single coin is paid. Both the 08-15 CAH outage and the still-open Anagrams gap were only discoverable by someone noticing in Discord. A format change by any of the four watched bots will repeat this.

**Fix:** Per watch row show 'last payout: <date>' (max paid_at from games_external_payouts by kind/channel) and 'unpaid finishes, 30d' (run parser.is_terminal + identify_game over the buffer — 60 lobbies/30d, cheap) with a warning badge when a known game's terminal has no claim;

### photo-external-101 — Survey Says and Wisecracks games pay nothing though their Final scores carry per-player points
*backend · medium · M · confirmed* · game: gamebot-other · queue: **P9**

**Where:** src/bot_modules/games_external/parser.py:115-120

**What:** Three real 3-player games in the last month ended with a full scoreboard and paid nobody, and nothing tells the admin the lobby was for an unpaid game (the kind label lists only CAH/Connect 4/Anagrams). The one_terminal-for-all 'Final scores' also means the parser's fallback classifies any lobby-less Survey Says window as CAH (harmless today because scores come back empty, but the docstring is wrong).

**Fix:** Add 'survey says' and 'wisecracks' to _START_GAMES with a Final-scores reader for '**Name**: N points' lines resolved via resolve_named_scores and paid through pay_cah_game_by_score(game_key=...); fix the is_game_over/_infer_game comments; update the kind label and manual.html:747.

### photo-external-102 — Photo history row records nothing: guild 0, empty payload, player_count 0, invisible on the Games pages
*backend · medium · M · confirmed* · game: photo · queue: **P2**

**Where:** src/bot_modules/cogs/games_photo_cog.py:135-150, src/bot_modules/games/utils/game_manager.py:386-393, src/web_server/routes/games.py:235

**What:** Every daily card writes a games_game_history row that says nothing (no prompt, no guild, no count), inflates the guild_id=0 bucket, and is filtered out of the Games stats/history pages. Photo also drops a host-only games_session_tracker row per card (47 in the photo channel). Meanwhile the one number Billy asks for — how many people answered the prompt — exists in two other tables and never reaches a dashboard.

**Fix:** Keep a history row but make it true: pass bot=self.bot (guild resolves) and payload={'prompt','tags'} to end_game; then at the next launch (or a once-a-day task) update the previous card's row with player_count = distinct image posters and round_count = photos in the 24h after it (from messages media_kind='media' in the photo channel, or…

**Already recorded / fixed:** review-brief notes guild_id=0 is still written by photo/clapback/nhie; the generic end_game fix belongs to the platform family — this is the photo-specific part (payload dropped, counts derivable).

### photo-external-103 — Daily photo role ping comes from a hidden legacy announce row the panel shows as 'no ping'
*backend · medium · S · confirmed* · game: photo · queue: **P9**

**Where:** src/web_server/routes/photo_challenge.py:248-249, src/web_server/static/js/panels/photo-challenge.js:68-72, src/bot_modules/services/scheduled_games_service.py:63-88

**What:** Members get pinged every day at 05:13 guild-local, but an admin opening the Photo Challenge panel sees no ping role and has no control to stop or change it; setting the panel's Ping Role would ping twice (card content + announcement). The announcement copy is game-lobby wording for a photo prompt, and the {"prompt": ""} option is a leftover from the old shared scheduler (harmless — launch() falls through to the bank).

**Fix:** One-off data fix: copy row 7's announce_role_id into the photo config ping_role_id, set announce=0 and options='{}'; make PUT /schedule/{id} force announce=0/announce_role_id NULL so no photo row can announce again. Confirm with Billy that a 05:13 local ping is intended (46% of photos land 12–14 UTC, so the timing does work).

### photo-external-104 — Ping Response report says 'Played: 0' for every daily photo ping
*backend · medium · S · confirmed* · game: photo · queue: **P2**

**Where:** src/bot_modules/services/scheduled_games_service.py:485-500, src/bot_modules/services/ping_tracker_service.py:427-469, src/web_server/static/js/panels/ping-response.js:266-273

**What:** The one recurring ping in the guild reports zero roster to the admin while 10–24 people post photos after it — the report's own 'blank means no game attached, 0 means nobody joined' contract makes this read as a failed ping.

**Fix:** Fixed for free by recording real counts (finding on history rows); otherwise exclude game_type='photo' from query_game_player_counts so the cell renders '—'.

### photo-external-105 — Photo Challenge has no ending — nothing recaps, showcases or ranks the day's photos
*gameplay · medium · M · confirmed* · game: photo · queue: **P9**

**Where:** src/bot_modules/cogs/games_photo_cog.py:123-151, src/bot_modules/cogs/economy_cog.py:4664-4672

**What:** Player floor 0, host effort 0, pacing = one card a day; payoff is 5 coins and a tick. Members post into a stream and never hear back — no 'yesterday's gallery', no most-loved photo, no streak, no name-check. Given this is the guild's most-participated activity, the missing payoff is the cheapest engagement win in the family.

**Fix:** When the next card fires, post a one-line recap under it: 'Yesterday: 17 photos from 15 people — most loved: <jump link>' using messages + message_reactions for the previous 24h (name_fn for names, no <@id> in embeds). Optionally a monthly 'most days posted' shout-out from econ_photo_rewards.

### photo-external-99 — Anagrams payouts silently dead since Gamebot's 08-15 rewrite (4 games unpaid)
*backend · medium · S · confirmed with correction ×2* · game: gamebot-anagrams · queue: **P9**

**Where:** src/bot_modules/games_external/parser.py:85, src/bot_modules/cogs/games_external_cog.py:308-318

**What:** Anagrams payouts are dead since Gamebot's 08-15 rewrite: the Scoreboard now carries '**DisplayName** — N points' in the description (fields empty) and the finish reads '<@id> wins!', so scores_from_scoreboard/_WINNER see nothing and _pay_anagrams_game marks 'skip'. Three payable games (08-17, 3 players; 08-19, 4 players; 08-22 18:51, 3 players) went unpaid — the fourth (08-22 18:40) is a solo Survival run with 'Nobody won this one.' and no Scoreboard, payable under no parser.

**Fix:** Teach scores_from_scoreboard to also read description lines matching r'^\*\*(.+?)\*\* — (\d+) points' (names are display names — resolve_named_scores already uses get_member_named), add r'<@!?(\d+)>\s+wins!' to the winner reader, add a regression test built from the real 08-22 window, then replay the four games with scripts/replay_gamebot…

### photo-external-106 — 33-prompt bank, one duplicate, no tags — every prompt recurs monthly
*gameplay · low · S · confirmed* · game: photo · queue: **P9**

**Where:** src/bot_modules/games/utils/question_source.py:119-135

**What:** Round-robin over 33 means the same prompt lands roughly every 33 days, and the bank is dominated by colour/object prompts with no seasonal or weekend variants; the duplicate means 'purple' comes twice a cycle.

**Fix:** Grow the bank to ~90 (the ToD MCP photo_prompt_brief/roll_photo_seeds tools exist for this), delete the duplicate, and tag a few 'weekend' prompts so a weekly schedule row can pass tags.

### photo-external-107 — Panel and manual copy say a photo_post quest is required; a skipped_late run shows a raw status
*ux · low · S · confirmed with correction* · game: photo · queue: **P9**

**Where:** src/web_server/static/js/panels/photo-challenge.js:74, src/web_server/static/manual.html:3019, src/bot_modules/services/economy_photo_service.py:290-306

**What:** photo-challenge.js:74 wrongly implies a photo_post quest is required to pay (the flat Income Sources 'Photo Challenge post' rate pays on its own — economy_photo_service.py:130-145, prod rate 5) and STATUS_LABEL (:22-28) lacks skipped_late, which the scheduler writes for any recurring slot missed by >2h (scheduled_games_service.py:54,354) and :283 then renders raw. In manual.html only the summary-table row (:3023) still says 'reward set as a photo_post quest'; §4 body (:790,:798) is already correct.

**Fix:** Reword the hint to name the Income Sources 'Photo Challenge post' rate and make the quest optional; add skipped_late ('⏰ Skipped — bot was offline at post time') to STATUS_LABEL; fix manual.html:3019.

### photo-external-108 — reward_cah_win_max=15: a CAH winner earns less than the host bounty pays for one joiner
*gameplay · low · S · confirmed with correction* · game: gamebot-cah · queue: **P9**

**Where:** src/bot_modules/economy/game_rewards.py:481-500

**What:** The dials are as stated (15 vs 25/+3 vs 30×8 host bounty) but the 50→15 cut is a documented economy decision (08-06 ledger audit) and the 08-28 affordability review proposes 10, so a raise contradicts standing policy; and 0-scorers already fire the party_game quest trigger (game_rewards.py:508-510), so participation registers on quests even with no coins. Design call only.

**Fix:** Design call for Billy: raise reward_cah_win_max toward the native game_win (25–30) and/or pay a 1-coin floor to 0-scorers so participation registers; leave the host bounty alone.

### photo-external-109 — External payouts claim before crediting and never release on a no-op
*backend · low · S · confirmed* · game: external · queue: **P9**

**Where:** src/bot_modules/games_external/logic.py:207-220, src/bot_modules/cogs/mention_awards_cog.py:152, games_external_cog.py:257-263

**What:** Any transient no-op (bot cache miss on the guild, economy briefly off, cap set to 0 while tuning) burns the game's once-ever claim and those players are silently unpaid forever; the helper written to prevent this is unused on the four game paths.

**Fix:** Have pay_cah_game_by_score / pay_game_rewards / pay_cat_catch return whether anything was credited and call release_payout(kind) when they return False (the mention-awards pattern), except for the deliberate all-zero case.

### photo-external-110 — 300-message window vs observed 158-message games — a long game loses its lobby and host bounty
*backend · low · S · confirmed with correction* · game: gamebot-cah · queue: **P9**

**Where:** src/bot_modules/games_external/logic.py:232-243, src/bot_modules/cogs/games_external_cog.py:174-188, parser.py:300-327

**What:** The 300-row slice is unpaged and would drop the lobby of any single game with >300 Gamebot messages, but the largest prod game is 214 (not 158) and CAH always ends at 5 points, so it is a latent edge; the real lobby-less case seen in prod (08-31, 44-message window, no host paid) came from a lobby that was never captured into games_external_messages, which paging would not fix.

**Fix:** Raise the limit to 1000 or page backwards until a lobby/terminal is found; add a test with a 400-message synthetic window.

### photo-external-111 — A1 buffer sweep is shipped and biting; data register text is stale
*backend · low · S · confirmed* · game: external · queue: **P9**

**Where:** src/bot_modules/games_external/logic.py:277-297, src/bot_modules/cogs/games_external_cog.py:80-95, docs/data_register.md:88

**What:** The table 'still growing' in the brief is steady-state churn (about 30 days × ~600/day, 13 MB), not unbounded retention. The register row is the record of processing and now misdescribes it.

**Fix:** Update the register row: retention 30d enforced daily, current size, correct line reference. No code change.

**Already recorded / fixed:** docs/reviews/2026-08-05-games-batch-bc.md A1 — fixed in 49a02867; this is the doc residue.

## Duels and party games

### duels-party-112 — No duel or lobby surface consults the no-contact list; a blocked member can be challenged, pinged and renamed
*backend · high · M · confirmed ×2* · game: duels-party · queue: **P1**

**Where:** src/bot_modules/duels/base_duel.py:50-210, src/bot_modules/duels/base_game.py:929-990, docs/no_contact_spec.md:68-82

**What:** CLAUDE.md: any surface that puts two members in contact consults the no-contact list. /games pressure|quickdraw|hotpotato challenge @user publicly pings the target, forces them to Accept/Decline in-channel, and on a win lets the challenger set the target's nickname for 24h — the strongest contact surface in the bot after DMs. Lobby games let a blocked pair land in one roster and one can rename the other. Risky Rolls and Mahjong got this gate in August; the duels were skipped.

**Fix:** In _base_challenge, after the self/bot checks, refuse when is_no_contact_conn(guild, challenger, target) using the existing 'You two already have a game in progress.' wording (indistinguishable from an ordinary outcome, per the spec) and record an attempt event.

### duels-party-113 — Chicken's crash point is fixed and public, so the game almost never produces a loser
*gameplay · medium · M · confirmed with correction* · game: chicken · queue: **P7**

**Where:** src/bot_modules/cogs/chicken/cog.py:236-248, src/bot_modules/cogs/chicken/game.py:500-505, db.py:74-87

**What:** Chicken's crash is deterministic at start+climb_duration and the meter is broadcast (chicken/cog.py:236-248, :105-127) — by design per dk_pvp_games_suite_spec.md §9.5. In prod the stake fired in only 4 of 12 games (5,6,15,21); 4 were everyone-bailed at 94-100% and 4 were total wipeouts (nobody bailed, pot refunded with an announced '↩️ Stakes refunded'). A hidden/random crash point is a gameplay design change to the spec, not a bug fix.

**Fix:** Give Chicken a hidden crash: roll crash_at uniformly in [min_climb, max_climb] (dashboard dials replacing climb_duration) and show the meter as elapsed/max_climb so the bar can crash at 60%. Treat a total wipeout as everyone losing (no rename, but the pot goes to the house or is split) rather than a silent refund, or at least say so.

### duels-party-114 — Musical Chairs final round with no sitter makes one player both winner and loser and pays them the pot
*backend · medium · S · confirmed* · game: musical_chairs · queue: **P7**

**Where:** src/bot_modules/cogs/musical_chairs/cog.py:199-229, src/bot_modules/cogs/musical_chairs/game.py:595-607, src/bot_modules/duels/base_game.py:1135-1187

**What:** Two players left, one chair, both miss the 8-second scramble (AFK, lag, or a stale panel): the later-listed player is declared winner AND runner-up, gets the 'Name the Loser' button for themselves, and in a wagered game takes the whole pot (prod game 6 was a 9-player wager game). The result copy says 'X takes the last chair!' about someone who never sat.

**Fix:** When survivors is empty in the final round, re-run the round (fresh music timer, same two players) instead of resolving; if it happens twice, refund/void. Add a pytest.param row for alive=[a,b], seated=[] asserting no terminal write and winner != loser.

### duels-party-115 — Lobbies expire 90 seconds after the last join with no visible deadline; a full 10-player lobby died and was rebuilt
*gameplay · medium · S · confirmed* · game: duels-party · queue: **P7**

**Where:** src/bot_modules/cogs/chicken/db.py:60-71, hot_potato_group/db.py:62-73, musical_chairs/db.py:60-71

**What:** A host opens a lobby, pings the room, and unless someone presses Join within 90 seconds the lobby is gone. Once full, the host has 90 seconds to press Start before it expires under them — the 10-player Musical Chairs lobby on 08-17 expired while people were reading the How to play field, and everyone re-joined a second one. Nothing on the card says any of this; the expiry message blames the players.

**Fix:** Raise the window to 5 minutes from the last action, render '⏱️ Closes <t:...:R>' on the lobby embed (refresh on join/leave), and ping the host 60s before expiry. Make the dead lobby's copy honest ('Nobody pressed Start in time'). Consider auto-start when the lobby reaches max_players.

### duels-party-116 — 'Wait Before a Rematch' on the three duel panels is read by nothing; the same dial locks group-game rosters out for 48h
*backend · medium · S · confirmed* · game: duels-party · queue: **P7**

**Where:** src/bot_modules/cogs/pressure_cooker/db.py:161-170, src/bot_modules/duels/base_duel.py:50-210, src/web_server/static/js/panels/config-games-pressure.js:61-63

**What:** An admin who sets a rematch cooldown on Pressure Cooker/Quickdraw/Hot Potato changes nothing (CLAUDE.md: never ship a toggle that isn't enforced) — and this survived both 4dd92ab9's dead-dial pass and the recovered 7e059620. Meanwhile group games enforce the same dial with a 48-hour default: after one nickname-stake Chicken, every player in the roster (bailers included) is refused from any nickname-stake Chicken for two days, which is hostile to a game night; only wager/custom-stakes games skip it.

**Fix:** Either wire duels_db.check_cooldown/set_cooldown into _base_challenge and _finalize_result (and cover in test_duels_terminal_seam) or remove the field from the three duel panels and the spec. Lower the group default to 0 (the panel hint already says 0 lets people play back to back) and apply the cooldown only to the renamed loser if it is…

### duels-party-119 — Hot Potato duel has no minimum hold, so it is a click-spam coin flip; cumulative style points are written but never shown
*gameplay · medium · S · confirmed with correction* · game: hot_potato · queue: **P7**

**Where:** src/bot_modules/cogs/hot_potato/cog.py:377-409, src/bot_modules/cogs/hot_potato_group/cog.py:356-366, src/bot_modules/cogs/hot_potato/game.py:493-511

**What:** The duel has no minimum hold (unlike the group cog's 2 s min_hold dial), so passes are instant and the loser is whoever holds at a random tick — that part stands. Per-game style points are already displayed on the result card (cog.py:317-341); only the cumulative hot_potato_style total is write-only, which the spec already acknowledges. Fix = add a min_hold dial to hot_potato_config mirroring hp_group_config, and either surface or drop the cumulative table.

**Fix:** Adopt the group cog's min_hold (dashboard dial, default 2s) in the duel so a pass is a decision, show the per-game style points already computed at cog.py:317-341 as the tie-flavour, and either surface the cumulative hot_potato_style total (a line on the result: 'X now has 174 style points') or drop the table.

**Already recorded / fixed:** docs/dk_pvp_games_suite_spec.md §9.3 records that style totals are write-only; the min_hold gap is new

### duels-party-122 — Duel and group games are invisible to every games report: no games_game_history row, no dashboard reader
*backend · medium · M · confirmed* · game: duels-party · queue: **P2**

**Where:** src/bot_modules/duels/base_game.py:1340-1381

**What:** 135 duel/group games (68 pressure, 34 hot potato, 22 chicken, 9 quickdraw, 2 musical chairs) never appear in the games logs, the player-count/engagement metrics, or the DAU-style activity views, while the economy does pay them (econ_ledger game_participation/game_win). Any 'which games do people play' decision made from the dashboard undercounts the most-played 1v1 format in the server.

**Fix:** In _on_terminal_state for settling states, write a games_game_history row (game_type=GAME_KEY, host=challenger/host, player_count=len(participants), started_at=created_at, guild_id) — or expose a Duels tab in games-logs.js reading the six tables. Add a terminal-seam test asserting the row.

### duels-party-117 — No rematch path: every game needs a fresh command and a fresh lobby
*gameplay · low · M · confirmed with correction* · game: duels-party · queue: **P7**

**Where:** src/bot_modules/duels/views.py:181-216, src/bot_modules/duels/base_game.py:1135-1187, base_duel.py:417-466

**What:** There is no rematch control on the result view (duels/views.py; base_game.py:1160-1170; base_duel.py:445-466) and prod shows the same rosters rebuilding lobbies within minutes (chicken 16/17/18 at 1786604831/934/992, 21→22, mc 4→5→6, pressure 64→65 same pair reversed). This is the '🔁 Run Again' item already listed as designed-but-unbuilt in dk_pvp_games_suite_spec.md §13.4 — a roadmap feature, not a defect; the friction it causes is the lobby-expiry issue in duels-party-115.

**Fix:** Add a '🔁 Run it back' button to the result view (host/either duelist only, 5-minute life) that re-creates the game with the same roster, stakes and wager, skipping the lobby (re-take antes at press; refuse anyone who can't cover).

**Already recorded / fixed:** Recorded as unbuilt roadmap in docs/dk_pvp_games_suite_spec.md §13.4 (line 738)

### duels-party-118 — Half of Hot Potato winners never get their payoff: the 5-minute 'Name the Loser' window lapses, and NO_NICK_SET hides why
*gameplay · low · S · confirmed with correction* · game: hot_potato · queue: **P7**

**Where:** src/bot_modules/cogs/hot_potato/db.py:78-89, src/bot_modules/duels/base_game.py:251-263

**What:** NO_NICK_SET conflates four causes (winner timeout, loser left, loser outranks the bot, loser already serving) and the 5-minute window has no reminder ping — but in prod the lapse is historical: most NO_NICK_SET rows are pre-07-20 custom-stakes games that were wrongly offered a rename button (fixed in 6de0ee07), and no nick-stake Hot Potato or Pressure Cooker game has lapsed since August (hot_potato 6 NICKED / 0 lapsed, pressure 16 / 0).

**Fix:** Extend the naming window to 30 minutes (or until the next game between the pair), ping the winner in-channel at 2 minutes, and write a reason column (winner_timeout / loser_outranks / loser_left / already_serving) so the state stops lying.

### duels-party-120 — Hot Potato (Group) has never completed a game and shares its display name with the duel
*gameplay · low · S · confirmed with correction* · game: hot_potato_group · queue: **P7**

**Where:** src/bot_modules/cogs/hot_potato_group/cog.py:39-40, hot_potato/cog.py:35, base_game.py:461-464

**What:** Hot Potato (Group) has never completed a game (3 lobbies, all expired) and shares the 'Hot Potato' display name with the duel so refusals/DMs/audit reasons are ambiguous — but the lobbies died to the 90-second lobby-inactivity sweep shared by every N-player game (chicken has 10 expired lobbies of 22), not to discoverability (it is in /games help and the manual). The panel is already flagged 'possibly-dead' in dashboard-config-ia.md:204. Fix the name collision (S); folding the cogs is a design call for Billy.

**Fix:** Fold them: keep one /games hotpotato with a lobby of 2..N (the challenge form pre-fills a 2-player lobby and pings the target), migrate hot_potato_config into hp_group_config, and retire the group cog and its panel (route id can stay as an alias). Short of that, rename the group cog's display name so refusals and DMs are unambiguous.

### duels-party-121 — manual.html describes a Pressure Cooker that does not exist
*ux · low · S · confirmed* · game: pressure · queue: **P7**

**Where:** src/web_server/static/manual.html:630, src/bot_modules/cogs/pressure_cooker/game.py:341-403, cog.py:219-241

**What:** A member reading the guide expects a submission-and-rating game and gets a dice button; the in-Discord /games help copy and the dashboard panel subtitle (config-games-pressure.js:31) are right, so the dashboard's own manual is the one surface that lies about the family's most-played game.

**Fix:** Rewrite the row to match constants.py: 'take turns pressing Pump; each press adds a random 1–15 to a shared gauge; whoever pushes it past 100 loses and the winner picks their nickname for 24h'.

### duels-party-123 — Chicken's tie-break loser is the lowest user id: the oldest account always eats the nickname
*gameplay · low · S · confirmed* · game: chicken · queue: **P7**

**Where:** src/bot_modules/cogs/chicken/game.py:515-529, tests/cogs/test_chicken_game.py:49-53, src/bot_modules/cogs/chicken/cog.py:334-351

**What:** When two or more players ride to the crash, the same person loses every single time — Discord snowflakes are monotonic, so whoever made their account first is the permanent scapegoat in that group. Nobody at the table can tell why; the card just names them.

**Fix:** random.choice(crashers) with the seed logged, or make all crashers lose (each renamed by the bravest bailer, or nobody renamed and the pot refunded — but say which). Update the test row and the spec line.

**Already recorded / fixed:** docs/dk_pvp_games_suite_spec.md §9.5 states the rule; no review has questioned it

### duels-party-124 — Pressure Cooker has no decision — the only input is Pump on your turn
*gameplay · low · M · confirmed with correction* · game: pressure · queue: **P7**

**Where:** src/bot_modules/cogs/pressure_cooker/cog.py:216-241, game.py:345-403

**What:** Pressure Cooker's only input is Pump on your turn (single button, fixed roll range, no hold/vent), so play is pure chance plus the stakes text. That is an accurate design description, not a defect: prod shows repeat pairs and a growing month, so 'caps how often a pair comes back' is unsupported. Keep as a low design note if a decision layer is ever wanted; the first-pump invariant at game.py:95-101 must survive any change.

**Fix:** Cheap decision layer: on your turn choose Pump (1–15) or Vent (−5 to the gauge, but your opponent gets two pumps) — or let the challenger set the bust ceiling and roll range as stakes. Keep the first-pump-cannot-bust invariant (game.py:363-367).

### duels-party-125 — '24 hours' and '5 minutes' are hard-coded in every embed and slash description while sentence_hours and the sweeps are dials
*ux · low · S · confirmed* · game: duels-party · queue: **P7**

**Where:** src/bot_modules/cogs/pressure_cooker/cog.py:171, quickdraw/cog.py:405, hot_potato/cog.py:314

**What:** An admin who sets Nickname Lasts to 48 hours gets every card, DM and stakes line still promising 24. The abandonment card is wrong by 5 minutes for five of six games.

**Fix:** Thread cfg['sentence_hours'] through render_result_state/_render_lobby (one helper in base_game that formats 'for N hours'), build NICK_STAKES_LINE at creation from the dial, and derive the abandonment copy from each game's ACTIVE sweep constant.

### duels-party-126 — Pending challenges are not re-attached after a restart, so their buttons fail until the sweep expires them
*backend · low · S · confirmed* · game: duels-party · queue: **P2**

**Where:** src/bot_modules/duels/base_game.py:121-152, pressure_cooker/db.py:104-106, src/bot_modules/duels/views.py:106-120

**What:** A challenge posted in the minutes before a restart (Billy restarts after merges, often in the evening play window) shows Accept/Decline that return 'interaction failed' for up to five minutes, then flips to 'Challenge Expired'. With the window now 5 minutes this is more visible than it was at 60s.

**Fix:** On cog_load fetch PENDING rows too and either re-add a ChallengeView with the remaining timeout (persist by custom_id, it already encodes game_id) or expire them immediately with a 'restarted — send it again' card.

### duels-party-127 — Manual promises a stake refund when you leave the server mid-game; no listener exists
*ux · low · S · confirmed with correction* · game: duels-party · queue: **P7**

**Where:** src/web_server/static/manual.html:690-691, src/bot_modules/duels/base_game.py:1516-1528, base_game.py:237-249

**What:** The stake refund on leaving IS implemented (economy_cog.py:4017-4058 on_member_remove refunds every live non-mahjong escrow row); the manual is accurate. The residual is that no duel/group cog drops a leaver from the lobby roster or `alive` list, so a lobby or active round that included them can stall until the 600 s ABANDONED sweep or the game plays out around them. Low polish item: on_member_remove in BaseGame to remove the member from roster/alive and resolve if one player remains.

**Fix:** Add on_member_remove to BaseGame: refund and drop the member from roster/alive for LOBBY and ACTIVE games (resolving if one player remains), or change the manual and docstring to say the refund arrives when the game is abandoned.

### duels-party-128 — Refusal copy is off the house shape and one refusal is wrong
*ux · low · S · confirmed with correction* · game: duels-party · queue: **P7**

**Where:** src/bot_modules/duels/base_game.py:461-464, base_duel.py:137-146, base_duel.py:73-82

**What:** Duel refusals in base_game.py/base_duel.py lack the ❌ prefix and the fix hint the style guide requires; the sentence refusal (base_game.py:440-443) wrongly says the loser 'can't play again until it expires' when only nickname-stake games are blocked (base_duel.py:138-146) — prod shows that loser playing 7 wagered Pressure games during sentence 23. Quickdraw AND Musical Chairs already use ❌; the other four and the shared base do not. Lobby cooldown refusal (:963-966) omits the remaining time that :871-879 prints.

**Fix:** One _refuse(interaction, text) helper in BaseGame prefixing ❌; reword the sentence refusal to 'X is wearing a nickname sentence — challenge them with nickname:False and a wager or stakes instead'; include the remaining time in the lobby cooldown refusal.

### duels-party-129 — Six near-identical config panels where one would do
*ux · low · M · confirmed with correction* · game: duels-party · queue: **P7**

**Where:** src/web_server/routes/config.py:706-770

**What:** The six-panel duplication is real but already queued as common-lib-round-2.md §D.1 (shared config-games factory driven by the routes' spec) — cite that instead of re-raising. Drop the 'one nav entry' recommendation: games-nav-split (unmerged, todo #165) intentionally lists every game by name in one flat list; a single duels entry is a nav-IA decision for Billy, not a defect.

**Fix:** One 'Duels & Group Games' panel driven by the routes' _DUEL_GAMES spec: a game selector at the top, the shared Forfeit/Availability cards rendered once, and the game-tier card generated from the field spec. Keep the six route ids as deep-link aliases (ids are frozen) but show one nav entry.

**Already recorded / fixed:** docs/plans/common-lib-round-2.md §D.1 (planned, not built); nav shape decided on branch games-nav-split

## Casino

### casino-130 — Big-win broadcast bar is an absolute payout, so 1,000-coin even-money wins flood the channel (~100 public cards/day)
*ux · medium · S · confirmed* · game: casino · queue: **P5**

**Where:** src/bot_modules/cogs/casino/cog.py:1257-1262, src/bot_modules/services/casino_logic.py:312-323, cog.py:510

**What:** The ladder was sized on 08-15 against 'avg stake 36, largest win 3,000'. Since then the top players moved to 1,000-coin stakes (495 stakes of 1,000 in 30d), so a routine 2x blackjack win (2,000) clears the 500 bar four times over and headlines as 🔥 Huge Win, and a Mines cash-out at 1.06x on 1,000 (1,060) is a 💰 Big Win.

**Fix:** Gate on the multiple as well as the amount: broadcast only when payout >= bar AND payout >= 3x stake (or net win >= bar), keeping Legendary on the percentile. Alternatively express the bar as a multiple of max_bet on the dashboard.

### casino-131 — Doubled blackjack push is broadcast (and banked) as a win because the button path passes half the stake
*backend · medium · S · confirmed* · game: blackjack · queue: **P5**

**Where:** src/bot_modules/cogs/casino/cog.py:1725, casino_logic.py:312, cog.py:1817-1822

**What:** Commit 81507b34 fixed 'a push is not a big win' for the auto-stand path, but the player-pressed path reuses base_stake (correct for the Play Again button) as the broadcast stake. A doubled push returns 2x base, so big_win_tier sees payout > stake and announces 💰/🔥 for a hand that won nothing, then banks it into the Legendary percentile population. Two live instances on 09-01.

**Fix:** Pass stake=step.stake (the total) to _after_instant at cog.py:1756 while keeping base_stake for play_again_view; add a failing-first test: doubled push with base 1,000 against a 500 bar posts nothing and banks nothing.

### casino-132 — Private-round boards still read as communal countdowns, so a third of roulette/derby rounds are resolved by the idle sweep
*gameplay · medium · S · confirmed with correction* · game: roulette · queue: **P5**

**Where:** src/bot_modules/cogs/casino/embeds.py:757, cog.py:2361-2366, casino_service.py:98

**What:** Private-round embeds still carry the communal-countdown copy ('The wheel spins <t:R>', 'bets open!', 'be first') with no instruction to press Spin/Race/Deal, so about a third of roulette rounds (17 of 49, 14 of 30 in the main guild; 15 of the late ones had bets from 5 distinct players) and 4 of 23 derby rounds sat until the 600s abandonment sweep resolved them frame-less; player-resolved rounds finish in ~20-40s.

**Fix:** Rewrite the five round embeds for private play: 'Your own wheel. Stack bets, then press 🎡 Spin. Left alone it spins itself <t:R>.' Replace 'be first' with 'no bets yet', drop 'bets open!' titles, and consider a shorter TTL (180s like blackjack) since 96% of player-resolved rounds finish inside a minute.

### casino-133 — 07-25 R1/R2 still live five weeks on: max bet 1,000 + cap off in both real guilds, and per-play liability is capped on Mines only
*gameplay · medium · S · confirmed with correction ×2* · game: casino · queue: **P5**

**Where:** casino_service.py:56-59, casino_logic.py:835

**What:** 07-25 R1/R2 remain the live state by owner choice (max bet 1,000 / daily cap 0 in the main guild, 10,000 / 0 in the second; casino_daily untouched since 07-24) and the progressive jackpot has since paid once, 18,228 on 2026-08-22, to the member who wagered 384,320 in W36 (Aug 31–Sep 2; week handle 557,016 vs float 226,469). Both dials are correctly enforced; the jackpot is escrowed skim, not mint;

**Fix:** Dashboard now: set a daily cap (e.g. 2,000–5,000 at this denomination) in the main guild. Code: a per-play payout ceiling applied at pay_out (e.g. 30x max_bet, or a casino_max_payout dial) so keno/slots share Mines' bound; document the cap in How It Works. Re-run scripts/economy_tuning_report.py after.

**Already recorded / fixed:** Not fixed; recorded as docs/reviews/2026-07-25-economy-casino-sources-sinks.md R1/R2 and docs/reviews/2026-07-30-economy-health.md:197,309 (open question 1).

### casino-135 — Hub floor ticker shows one member's slots spins — every row of the last 25 is the same player
*ux · medium · S · confirmed* · game: slots · queue: **P5**

**Where:** src/bot_modules/services/casino_service.py:507-555, cog.py:98

**What:** The ticker is the casino's stated 'social texture' (casino_service.py:500-506) but at prod volume it is one grinder's last six spins, repainted every 8 seconds. Every other player's action is pushed off the panel within a minute. It reads as one person's private game on a public panel.

**Fix:** Make recent_ticker return the latest play per distinct player (window function over casino_ticker, limit 6) and raise TICKER_KEEP so six distinct players survive a grind burst; add a service test with two players and twenty spins.

### casino-136 — Pools voids one market in five because 2–4 bettors all pick the same side
*gameplay · medium · S · confirmed with correction* · game: pools · queue: **P5**

**Where:** src/bot_modules/cogs/casino/embeds.py:880-909, pools_panel.py:85-109

**What:** 9 of 37 completed markets (24%) voided, all because one side was empty; 5 of those had 2–7 bettors who all backed the same side, the other 4 had 0–1 bets. The panel (embeds.py:1346-1414) prints the odds bar and both pools but never says an empty side voids the market (void copy lives at embeds.py:1479-1509). Pools bets also bypass the amount ladder (pools_panel.py:106-109, PoolsBetModal has no default_amount).

**Fix:** Either seed both sides with a small house stake (e.g. 50 each, funded from the takeout, forfeited if unmatched) so every market settles, or add an explicit panel line when one side is empty and a reminder edit an hour before close.

### casino-134 — The casino is a four-player grind with no return hook for the other 43
*gameplay · low · M · confirmed with correction* · game: casino · queue: **P5**

**Where:** casino_service.py:541, embeds.py:1277

**What:** The casino's 30-day volume is ~78% four players (16,193 plays / 47 players) with weekly actives flat at 18-27 since launch, and there is no comp or free play; but the hub already has a daily-resetting Up most / Down most standing (casino_daily_net) and casino_weekly already feeds weekly table highlights into the economy leaderboard and wallet — so the gap is a missing casual hook on the hub itself, a design proposal rather than a defect.

**Fix:** Design one cheap return hook: a once-a-day free 5-coin spin from the hub (a 'comp', house-funded, no wager cap impact) and a weekly per-table 'luckiest hit' board on My Stats/hub. Both are S–M and reuse casino_weekly. Pair with the cap so the grinders' volume is bounded while the casual loop widens.

### casino-137 — Hub names the day's biggest loser publicly, now at −17,495 with the cap off
*gameplay · low · S · confirmed with correction* · game: casino · queue: **P5**

**Where:** src/bot_modules/cogs/casino/embeds.py:216-231, casino_service.py:745-784, docs/plans/casino-mines.md:456-475

**What:** The hub publicly names the day's biggest net loser (embeds.py:221-224), now at −18,317 with the daily wager cap set to 0 in prod. This is deliberate — the classics plan (l.44) and the Mines plan (l.473) both count hub standings as a harm-reduction control and the manual documents it — so raise it as a design question (keep 'Up most', move 'Down most' to My Stats?) rather than a bug.

**Fix:** Keep '📈 Up most', drop '📉 Down most' from the hub (or show it only to the member on My Stats). One line in build_hub_embed plus the standings test row.

### casino-138 — Derby, baccarat and dice are dead tables occupying a quarter of the hub
*gameplay · low · S · confirmed with correction* · game: derby · queue: **P5**

**Where:** src/bot_modules/cogs/casino/views.py:594-618, docs/plans/casino-mines.md:19-36

**What:** Baccarat is dead in the main guild (16 bets from one member in 30 days, none in the last week, last 08-24) and dice is thin (31 bets / 7 bettors in 30 days, 6 in the last week). Derby is not dead: 56 bets over 24 rounds in 30 days and 46 bets over 16 rounds in the last 7 days, albeit from two members. Unticking baccarat (and maybe dice) on Economy → Casino repacks the hub with no code (views.py:585-618).

**Fix:** No code: untick Derby, Baccarat and Dice on Economy → Casino in the main guild (build_hub_view repacks the rows). If they stay, the only thing that would revive derby is a scheduled communal race once a day, which is a different game.

### casino-139 — Stale copy after the private-round and no-button-broadcast decisions
*ux · low · S · confirmed* · game: casino · queue: **P5**

**Where:** src/bot_modules/cogs/casino/embeds.py:60, src/web_server/static/js/panels/config-casino.js:160-163, cog.py:1176-1180

**What:** The hub's Tables list tells members roulette is communal, and the dashboard tells admins the broadcast carries a Play Again button; both were reversed in August.

**Fix:** Fix the two strings; add both to the copy assertions in tests/test_casino_embeds.py / a panel text test.

### casino-140 — Ticker emoji map was never extended to the six games added to it, so Mines/roulette/derby/baccarat/keno all render as 🎲 (the Dice emoji)
*ux · low · S · confirmed* · game: casino · queue: **P5**

**Where:** src/bot_modules/cogs/casino/embeds.py:145, casino_service.py:507-511

**What:** A Mines cash-out and a dice roll look identical on the floor ticker, and both look like Dice.

**Fix:** Fill _TICKER_EMOJI from the same table _GAME_LINES uses (🎡 🏇 🎴 🎲 🔢 💣); one test row.

### casino-141 — Bet-step views time out silently into 'This interaction failed'
*ux · low · S · confirmed* · game: coinflip · queue: **P5**

**Where:** src/bot_modules/cogs/casino/views.py:389, cog.py:1093-1110

**What:** A member who opens Heads/Tails or the amount ladder from the hub and comes back a few minutes later presses a dead button and gets Discord's generic failure, with no hint to reopen from the hub.

**Fix:** On timeout, disable the items and edit the content to 'This step expired — press the table button on the panel again.' for the hub-game ladders and the coinflip picker.

## Meadow Mahjong

### mahjong-142 — Quick Duel on prod deals a 17-tile wall: Duel wall trim (60) is never clamped against the short deck
*gameplay · medium · S · confirmed with correction ×2* · game: mahjong · queue: **P0**

**Where:** src/bot_modules/games/mahjong/game_logic.py:519-522, src/bot_modules/games/mahjong/mahjong_service.py:386-392

**What:** Real, but the immediate defect is a stale prod dial: mahjong_duel_wall_trim is still the old checklist value 60 despite the plan and the dashboard hint saying 0, and create_table applies it on top of the short deck, so a Quick Duel on prod deals 17 live tiles (verified on prod table 11: wall 0 after 17 discards). Wall games refund escrow, so nothing is lost — the mode just cannot produce a win. Fix: set the dial to 0 now;

**Fix:** Two parts: (1) set mahjong_duel_wall_trim to 0 on the dashboard now; (2) in create_table, force wall_trim=0 when max_rank < FULL_RANK (or clamp trim to a fraction of the post-deal wall, e.g. ≤ 25%), with a service test 'a quick duel never trims'.

### mahjong-143 — Fill bots are ON in prod and a lone human can stake real coins against the house bot (no 2-human minimum)
*backend · medium · S · confirmed with correction ×2* · game: mahjong · queue: **P0**

**Where:** src/bot_modules/games/mahjong/mahjong_service.py:518-554, mahjong.js:150-152

**What:** Real gap between code and plan: add_bot never checks how many humans are seated, so with the fill dial on a lone host can open a Duel, seat a house-funded bot and play the house at real stakes with coach assist still available. But the dial is the designed gate and is on only because the owner enabled it for testing (every prod table is the owner's; the one fill hand netted the house +50), and requiring 2+ humans raises the farming cost rather than removing house-negative EV. Immediate action is the dial;

**Fix:** Refuse add_bot unless at least two human seats remain after the bot sits (i.e. never in a Duel; at most two bots on a 4-seat table), with a service test; and until that lands, switch House Bots in Real Games to Off on the dashboard. Optionally cap house exposure per day per member.

### mahjong-144 — Nothing tells a player it is their turn; a member who tabs away folds in 3 x turn timer and, in Duel, pays
*gameplay · medium · M · confirmed* · game: mahjong · queue: **P8**

**Where:** src/bot_modules/cogs/mahjong_cog.py:240-284, src/bot_modules/core/sticky.py:178-197, src/bot_modules/games/mahjong/game_logic.py:1417-1434

**What:** A 4-seat hand is 55-75 minutes long and a seat acts roughly one turn in four; between turns the only signal is a line in a sticky embed at the bottom of the channel. Discord users tab away, and a missed turn costs the table two minutes (prod timer) each, three times, then the seat folds — and in a Duel that ends the hand with the absent player paying. The author's own second real game ended this way. The rack panel does self-refresh for 13 minutes, but only if it was opened and the member is looking at it.

**Fix:** On every tile_drawn / turn start for a human seat, post a short plain message with content '<@id> — your draw' (allowed_mentions restricted to that user; mentions in content are house-legal) and delete it on the next transition, or DM the member with a jump link. Add the same nudge at the second strike ('one more and your seat folds').

### mahjong-145 — Auto-pass (08-25) leaks holdings: instant ✅ on the public card reveals which seats can call or Mahjong the discard
*gameplay · medium · S · confirmed* · game: mahjong · queue: **P8**

**Where:** src/bot_modules/games/mahjong/game_logic.py:857-876, src/bot_modules/games/mahjong/embeds.py:201-204

**What:** Before the change a ✅ meant 'this seat has responded'; now a seat that shows '…' the instant a tile lands is a seat holding two of that tile (or jokers) or one tile from Mahjong on it, and a seat that shows ✅ instantly holds neither. Every player reads it for free, every discard, and the bot brain is unaffected (it never reads the ticks), so it also tilts the human-vs-house games. This is a new leak from a post-review commit; the review rounds never saw it.

**Fix:** Render auto-passed seats as '…' (or omit per-seat ticks and show 'Responses: 2 of 3') while the window is open; alternatively tick every seat only when the window closes. One-line change in build_table_panel plus an embed test asserting an auto-passed seat is indistinguishable from an undecided one.

### mahjong-146 — Rack panel prints the raw enum 'You auto_pass — waiting on the window.' for every auto-passed seat
*ux · medium · S · confirmed* · game: mahjong · queue: **P8**

**Where:** src/bot_modules/cogs/mahjong_cog.py:822-827, src/bot_modules/games/mahjong/game_logic.py:72, tests/test_mahjong_cog_wiring.py:260-294

**What:** Since ~two-thirds of responder slots have no legal route, this is the Now line most seats see on most claim windows — and it's a code identifier, not copy. The manual (manual.html:1047) promises 'the table doesn't wait on you… your pass is entered for you'; the panel should say that.

**Fix:** Map AUTO_PASS to 'Nothing to claim here — the window moves on without you.' and add a pytest.param row to the existing rack-context test.

### mahjong-148 — Unanimous Rematch must land inside 60 s or the whole table closes after an hour of play
*gameplay · medium · S · confirmed with correction* · game: mahjong · queue: **P8**

**Where:** src/bot_modules/games/mahjong/mahjong_service.py:755-756, game_logic.py:1590-1606

**What:** SETTLE reuses phase_timer (prod 60 s) for a unanimous rematch, contradicting the LOBBY_LIFETIME docstring and spec §6.2's 10-minute inactivity rule; no human-vs-human table has expired yet in prod (table 10 was closed by a player; the 'expired' rows are practice/bot tables where the lone human never pressed Rematch).

**Fix:** Give SETTLE its own deadline (reuse LOBBY_LIFETIME=600 or a fixed 5 min) rather than phase_timer; no new dial needed. Optionally let non-voting seats be dropped after the window so the rest can rematch as a smaller table (would need D11 revisited).

### mahjong-149 — A table opening is silent and the lobby dies in 10 minutes — a 4-seat table cannot fill organically
*gameplay · medium · M · confirmed with correction* · game: mahjong · queue: **P8**

**Where:** src/bot_modules/games/mahjong/mahjong_service.py:81, src/bot_modules/cogs/mahjong_cog.py:453-461, games/constants.py:58

**What:** Opening a table posts only an unpinged sticky card; mahjong is outside the start-ping set (which covers just six lobby games, not 'every other game'), outside /games-help's GAME_ICONS registry and has no Event Echo source; the 10-minute lobby lifetime is per spec §6.2. Adding a host-consented ping and a /games-help entry is a feature ask, not a bug.

**Fix:** Reuse the games ping-role pattern (or Event Echo, which the brief says is decided for Risky Rolls/Guess) on table creation with the host's consent, make LOBBY_LIFETIME a longer default (20-30 min), and list /mahjong in /games-help. Consider a scheduled 'Mahjong night' via games_scheduled so the 4-seat table has a time.

### mahjong-151 — A closed table shows 'Closed' with no reason — including when one seat's empty wallet closed everyone's rematch
*ux · medium · S · confirmed with correction* · game: mahjong · queue: **P8**

**Where:** src/bot_modules/games/mahjong/embeds.py:251, src/bot_modules/cogs/mahjong_cog.py:263-264, src/bot_modules/games/mahjong/mahjong_service.py:878-882

**What:** A closed table never shows 'Closed' at all: the cog strips the buttons but leaves the last live embed (settle card still asking for Rematch, or the lobby card) in place, and posts no reason to the channel for expired/dissolved/cancelled/rematch_unfunded alike.

**Fix:** Render closed_reason into the final card ('Table closed — the lobby never filled / nobody rematched in time / a seat couldn't cover the next hand's escrow') and, for rematch_unfunded, keep the table in SETTLE for the remaining seats rather than closing it.

### mahjong-154 — Chat during a claim window moves the Mahjong/Call/Pass buttons: the sticky deletes and re-posts the card on every member message
*gameplay · medium · S · confirmed with correction* · game: mahjong · queue: **P8**

**Where:** src/bot_modules/cogs/mahjong_cog.py:161-170, src/bot_modules/core/sticky.py:693-719, src/bot_modules/games/mahjong/views.py:67-71

**What:** Meadow Mahjong's table sticky is built without a `hold` callback and its rack panel has no claim buttons, so a member message posted in the first ~2 s of the 8 s four-seat claim window makes the card (with the only 🀄/✋/Pass buttons) delete-and-repost to the bottom 6 s later, mid-window. It is one debounced repost per quiet gap, not one per message, and a delivered click still routes via the persistent custom_id — the confirmed harm is the buttons moving during the game's tightest decision, not a guaranteed 'interac…

**Fix:** Either suppress resticks while the table is in CLAIM_WINDOW (arm them after resolution) or add the three claim buttons to the ephemeral rack panel so a claim never depends on the moving public card.

### mahjong-147 — Practice tables are locked to the full 152-tile deck: the beginner's mode is the hour-long form, and Quick is staked-only
*gameplay · low · S · confirmed with correction* · game: mahjong · queue: **P8**

**Where:** src/bot_modules/cogs/mahjong_cog.py:463-481, mahjong_service.py:345, src/bot_modules/games/mahjong/views.py:397-407

**What:** Practice tables always deal the full 152-tile deck: handle_create_practice (mahjong_cog.py:476-479) never passes max_rank and CreateTableView's practice buttons (views.py:397-407) have no Quick variant, so the mode the How to Play footer points beginners at is the ~72-turn form while the measured half-length deck is reachable only through a staked Quick table.

**Fix:** Add Quick Practice Duel/Table buttons when short_deck_rank is set (pass max_rank through handle_create_practice), and consider making the quick deck the practice default. One cog wiring assertion + one service test.

### mahjong-150 — Escrow hold (450 Duel / 300 table at the lowest stake) locks ~45% of members out, and the panel never says so until three clicks in
*gameplay · low · S · confirmed with correction* · game: mahjong · queue: **P8**

**Where:** src/bot_modules/games/mahjong/mahjong_service.py:64, src/bot_modules/games/mahjong/embeds.py:71-86, views.py:409-416

**What:** The escrow size (300 4-seat / 450 Duel at stake 1) is a recorded go-ahead decision in docs/plans/meadow-mahjong.md, and it is shown on the stake picker, the confirm line and the table card; the only gap is that the /mahjong member panel lists stakes and balance without the per-size escrow. Polish, not a lockout surprise.

**Fix:** Show the escrow per size on the /mahjong panel itself; and revisit the cap — escrow at the card's *typical* line (e.g. 40 pts) with the loser's rare shortfall clamped to escrow, or allow a house-funded 'stake 0' real table for humans (the practice path already handles zero escrow) so the social game is free and only the coin game locks mo…

### mahjong-152 — Hand timing recorded since 08-25 is read by nothing, and practice hands still record no result — the pacing plan's 45 s/discard figure has n=1
*backend · low · S · confirmed with correction* · game: mahjong · queue: **P8**

**Where:** src/bot_modules/games/mahjong/mahjong_service.py:784-801, src/web_server/routes/mahjong.py:273-293, static/js/panels/mahjong.js:363-377

**What:** started_at/discards are recorded but never shown on the dashboard (real, S). Practice hands recording no result is a documented decision in commit 2673d90e and B5 that the card-generator plan contradicts — an open decision, not a bug — and the raw '-111' winner was explicitly accepted as intended in the bots review.

**Fix:** Add duration and discards columns (and a seconds-per-discard summary line) to Recent Hands; write a result row for practice hands flagged practice=1 (no seats/stats/coins — B5 is about money, not telemetry, and the register row can say so); render negative ids as '🌱 house bot'.

### mahjong-153 — Nothing brings a player back: no quest, XP, games_game_history or leaderboard hook for a settled hand
*gameplay · low · M · confirmed with correction* · game: mahjong · queue: **P8**

**Where:** economy_cog.py:4032-4035, src/bot_modules/games/mahjong/mahjong_service.py:761-834, embeds.py:598-616

**What:** Mahjong writes no games_game_history row, pays no quest/XP and has no echo hook — true, because it is off the games platform. But the settlement is posted publicly in the channel and the dashboard already shows per-player aggregates per spec §8; the ask is a new retention feature (history row + quest + seasonal board), not a bug.

**Fix:** Write a games_game_history row per settled hand (player_count included — the brief flags that column as unreliable elsewhere), add a 'hands played / first Mahjong' quest, and put a seasonal top-winners board on the dashboard report and optionally on the settlement embed.

## Survivor

### survivor-172 — Week 1's slate ping and last-call DM already fired two weeks early and cannot fire again
*backend · medium · S · confirmed* · game: survivor · queue: **P0**

**Where:** src/bot_modules/survivor/tasks.py:76-99, tasks.py:55-73, tests/test_survivor_reckoning.py:33-47

**What:** pick_week() returned 1 the moment the 2026 schedule was ingested, so the first Wednesday after creation posted "@Survivor @Ghost Week 1 is open — pick a team to win" and the first Saturday DM'd every pickless member "You haven't picked for Week 1 … or I'll pick for you" — 11 to 14 days before any game. The once-per-week keys now hold, so on Wed Sep 9 (the real week-open, 8 hours before the opener locks) there is no ping, and on Sat Sep 12 the two still-pickless members get no nudge;

**Fix:** Code (S): gate slate_due/lastcall_due on the pick week being imminent — e.g. the week's first kickoff is within 7 days of now — with a regression test whose season is created three weeks before Week 1. Prod, no restart needed: reset last_slate_week and last_lastcall_week to 0 for season 3 on Mon Sep 7 or Tue Sep 8 (frames 6/0, when neithe…

### survivor-173 — Reckoning marks the week reckoned and pays prizes before it posts — one failed send loses the week's post forever
*backend · medium · S · confirmed with correction* · game: survivor · queue: **P3**

**Where:** src/bot_modules/survivor/tasks.py:308-330, src/bot_modules/survivor/reckoning.py:45-51, tasks.py:390-398

**What:** Reckoning marks the week reckoned and pays prizes before it posts; a Forbidden (permission change) or a send that exhausts discord.py's own retries loses that week's public post, condolence DMs and panel refresh forever, while roles self-heal via reconcile_roles on the next pass; resetting last_reckoned_week to retry would double-pay the weekly prize.

**Fix:** Build the data and send first; then, in a second transaction, eliminate leavers, pay the prize and mark the week (or wrap send in try and roll the config marks back on failure). Return False from post_reckoning on send failure so the run report says 'blocked'.

### survivor-175 — The endgame is a promise nothing can keep: no season end, no Sole Survivor grant, no pot payout, and the field will collapse by December
*gameplay · medium · L · confirmed* · game: survivor · queue: **P3**

**Where:** src/bot_modules/survivor/embeds.py:259-260, src/bot_modules/survivor/logic.py:511-536, survivor.js:434-436

**What:** With 15 players, one strike and survivor-style picks winning ~78% of the time, roughly 3 players are alive by Week 12 (early December) and ~2 by Week 15; the plan's January deadline for the endgame assumes a much bigger field. When one player remains, nothing happens: the Reckoning keeps asking them to pick, the 8,000/2,000 pots stay on the panel, the Sole Survivor role sits unused, and ghosts keep streaking toward a side pot with no payout code. The whole return-for-months arc has no ending in code.

**Fix:** Build 6e before December, in this order: (1) season-over detection at the Reckoning (alive <= 1 after grading) that posts a ceremony, grants the Sole Survivor role and pays the main pot as a new survivor_payout credit kind registered in economy/kinds.py; (2) ghost side-pot payout by longest streak with tie split;

**Already recorded / fixed:** docs/plans/survivor.md lists 6e as Tier 2; the December timing and the never-granted role are new

### survivor-176 — A wipeout leaves zero alive and the season simply continues — annul/split (6b) is the shortest-leash gap
*gameplay · medium · M · confirmed with correction* · game: survivor · queue: **P3**

**Where:** src/bot_modules/survivor/settle.py:79-133, src/bot_modules/survivor/reckoning.py:264-268, docs/survivor_spec.md:41-43

**What:** Wipeout/annul (plan Tier 2 6b) is unbuilt as the plan already records; the additions are that a wipeout today prints a toll to zero and the season keeps running with no living player, and that commit 2394be08 removed the wipeout_annul_through_week dial so 6b must reintroduce it together with its reader.

**Fix:** Implement the week-level rule inside run_settle after grading: if the set of alive-before players all carry a losing result this week, either void that week's losing results as 'annul' (teams stay burned) through the configured week, or mark the season complete with an equal split among that week's players.

**Already recorded / fixed:** docs/plans/survivor.md Tier 2 6b

### survivor-177 — manual.html still tells members Survivor is an unclickable preview while 15 are enrolled, and describes unbuilt endgame rules as live
*ux · medium · S · confirmed* · game: survivor · queue: **P3**

**Where:** src/web_server/static/manual.html:983-990

**What:** The Help panel's own Survivor section contradicts the live server: a member who reads it before kickoff is told nothing is clickable yet, and the endgame paragraph promises the Accord, wipeout annul and split as rules of the season they just joined, none of which exist in code (#4, #5). The dashboard's own banner (survivor.js:513-520) is honest about the same list, so the two surfaces disagree.

**Fix:** Replace the callout with a 'live for the 2026 season' note listing what is still to come (wipeout, double-pick, Accord, payouts, notification toggles) and move the endgame paragraph under that list. Same commit as any code change per the docs rule.

### survivor-178 — Real seasons have no operator view of the weekly clock — the Week 1 problem is invisible from the dashboard
*ux · medium · M · confirmed* · game: survivor · queue: **P3**

**Where:** src/web_server/static/js/panels/survivor.js:84-86, routes/survivor.py:134-158

**What:** An admin looking at the panel today sees a healthy enrolling season and has no way to learn that Week 1's slate and last call are already spent (#1), when the next Reckoning will fire, or in which guild-local hour. Moving the force button to the rig (first-look #4) was right, but it left production with zero affordance: not even a read-only 'next due' line.

**Fix:** Add a 'Weekly clock' block to the Season card: for slate/last call/Reckoning show last fired week and next due time (guild-local, from the same past_weekly_moment logic), plus a per-task 'reset this week' control gated by confirmDialog. Keep the force button on the Simulator card.

### survivor-174 — Leaver elimination trusts guild.members with no chunked guard — a partial cache kills the roster on the record
*backend · low · S · confirmed with correction* · game: survivor · queue: **P3**

**Where:** src/bot_modules/survivor/tasks.py:306, src/bot_modules/survivor/reckoning.py:64-77, src/bot_modules/survivor/settle.py:109-114

**What:** eliminate_leavers reads guild.members with no guild.chunked guard; during the few seconds between a re-IDENTIFY's GUILD_CREATE and chunk completion a Reckoning tick would eliminate every uncached alive player as source='left', which settle will not undo. bot.is_ready() would not catch it (it is never cleared on re-IDENTIFY); gate on guild.chunked and confirm with fetch_member.

**Fix:** Skip eliminate_leavers unless bot.is_ready() and guild.chunked, and confirm each suspected leaver with guild.fetch_member (NotFound => gone; anything else => keep) before marking. Test: an empty present set with the guard false eliminates nobody.

### survivor-179 — double_pick_start_week=14 is enforced only by the Gauntlet — December late joiners are graded on a rule nobody else plays
*backend · low · S · confirmed* · game: survivor · queue: **P3**

**Where:** src/bot_modules/services/survivor_service.py:38, src/bot_modules/survivor/gauntlet.py:62, src/bot_modules/survivor/views.py:89-94

**What:** Live members can only ever place slot 1 (6c is unbuilt), but compute_fate replays two chalk picks per elapsed week from Week 14, so a late joiner in mid-December inherits two burned teams and a doubled chance of a fatal week compared with everyone who played it live. The dashboard's Escalation card presents the dial as a rule in force — the exact 'dial nothing reads' shape 2394be08 removed for its three siblings.

**Fix:** Until 6c ships, make compute_fate ignore double_pick_start_week (or set it to 0 for season 3 via PUT config) and hide the dial with the same 'returns with the code' note. Test: an elapsed week >= 14 replays one slot.

### survivor-180 — Last-call early-games list truncates before filtering, and its copy contradicts the groundskeeper
*backend · low · S · confirmed with correction* · game: survivor · queue: **P3**

**Where:** src/bot_modules/survivor/tasks.py:429-434, tasks.py:41-44, src/bot_modules/survivor/settle.py:276-288

**What:** The early-games query truncates (LIMIT 3) before the `now < ts` filter (tasks.py:429-441), but settled games become 'final' (settle.py:385) and leave the query, so the slots are only stolen by a kicked-but-unsettled game (an in-progress Saturday game late season, or an ESPN outage), not by Thursday's game on a normal Saturday. Moving `kickoff_utc > now` into SQL is still the right one-line fix. The 'terrible taste' copy is the spec's decided line (survivor_spec.md:65);

**Fix:** Filter future kickoffs in SQL (kickoff_utc > now) before LIMIT 3; reword to 'or the groundskeeper picks the favorite for you (📎 on the reveal)'.

### survivor-181 — No cancel/refund path in the product — refunds for the two test seasons were a hand-run script
*backend · low · M · confirmed* · game: survivor · queue: **P3**

**Where:** src/web_server/routes/survivor.py:295-322, src/bot_modules/services/survivor_service.py:225-227, scripts/refund_survivor_test_seasons.py:1-12

**What:** Season 3 is free entry, so the only money at risk this season is the gauntlet fee late joiners pay (50 coins per elapsed week — 850 by Week 18). If the season has to be scrapped (ESPN breaks, a rules dispute, the wipeout hole in #5), End Season leaves those debits in place and the refund is again an ad-hoc script. Not a Week 1 issue.

**Fix:** Add a 'cancel season and refund' variant of End Season that credits every survivor_buyin/survivor_gauntlet_fee debit for the season as survivor_refund with the script's idempotent refund_of meta, audited and mod-log mirrored; keep the plain archive for a season that ended normally.

### survivor-182 — Ghosts are invisible: their picks never appear in the Reckoning and they get no nudge, so the dead have little reason to keep playing
*gameplay · low · S · confirmed with correction* · game: survivor · queue: **P3**

**Where:** src/bot_modules/survivor/reckoning.py:143-148, src/bot_modules/survivor/tasks.py:417-427, docs/survivor_spec.md:49

**What:** A ghost's weekly pick is public nowhere (earlier ghosts are skipped from the ledger at reckoning.py:143-148; only top-3 live streaks show at :202-206) and ghosts get no Saturday nudge (tasks.py:421 is alive-only, a code choice — spec §1.7 bars only auto-assign for the dead). They are not entirely invisible: the weekly-win prize pays ghosts (reckoning.py:80-117, 25 coins by default) and their own ephemeral status card shows current/best streak (embeds.py:91-94).

**Fix:** Add one ghost line to the Reckoning ('👻 Ghosts: 8 picked, 6 correct') under the streak strip, and reveal ghost picks in a compact second ledger clipped to the field cap; consider a ghost last-call DM (opt-out later with the notification toggles).

### survivor-183 — The opener is Wednesday Sep 9 evening, not Thursday Sep 10 — docs and plan are a day off, and three picks lock Wednesday
*ux · low · S · confirmed* · game: survivor · queue: **P3**

**Where:** manual.html:988, tasks.py:34

**What:** The code is fine — per-game locks and a 9am Wednesday slate both precede the 5:20pm PT kick — but every human-facing reference says Thursday, and a tester or member who trusts them will find NE/SEA picks locked a day earlier than expected. The status card's relative lock time is the only place the truth shows.

**Fix:** Correct the manual and plan to Wednesday Sep 9 (kickoff at 5:20pm PT); have the Wednesday slate ping name the first kickoff with a <t:…:R> so a short week is obvious.

### survivor-184 — Pinning is fought by the sticky machinery: the panel is unpinned after the first chat message and re-pinned every Wednesday
*ux · low · S · confirmed with correction* · game: survivor · queue: **P3**

**Where:** src/bot_modules/survivor/views.py:773-789, src/bot_modules/core/sticky.py:475-500

**What:** repost_panel pins each Wednesday copy, but the sticky machinery replaces that exact message (set_panel_ids writes announcement_message_id) on the next message in the channel, sending unpinned and deleting the pinned copy — so the pin lasts only until someone chats. The pin is dead weight, not a member-facing broken promise: no embed copy tells members to look for a pinned panel; only the dashboard status line and the manual's admin paragraph say 'pinned'.

**Fix:** Drop the pin from repost_panel (and the 'pinned' wording in copy/manual) or pin in the sticky send path; pick one.

### survivor-185 — reconcile_roles runs every 60s and logs a full traceback per failing member per minute
*backend · low · S · confirmed* · game: survivor · queue: **P3**

**Where:** src/bot_modules/services/survivor_loop.py:241-246, src/bot_modules/survivor/tasks.py:244, src/bot_modules/survivor/views.py:565-571

**What:** With 15 players the no-drift path costs nothing, but one member whose role the bot cannot set (role above the bot, Manage Roles lost) produces ~1,440 tracebacks a day and a Discord API call per minute until someone notices; the log is wiped each boot so the pattern hides.

**Fix:** Reconcile on the hourly boundary (or only when a decision fired) and remember failures per member with a backoff; log at warning once per member per hour.

### survivor-186 — Pick menu and autocomplete show guild-local (UTC-7) clock times with no zone label
*ux · low · S · confirmed* · game: survivor · queue: **P3**

**Where:** src/bot_modules/survivor/views.py:36-47, src/bot_modules/cogs/survivor_cog.py:183-188

**What:** 'CIN (vs TB · Sun 10:00 AM)' is Pacific time; a member in Eastern reads it as 10am and has until 1pm. Select labels cannot carry Discord timestamps, so this is a copy fix, not a design one — but it is the surface where every pick is made.

**Fix:** Append the server zone ('Sun 10:00 AM PT' or '… server time') to kickoff_label, and say 'times are server time' once in the pick panel's message content.

### survivor-187 — Survivor is not discoverable outside its channel — absent from /games help and the games menu
*gameplay · low · S · confirmed with correction* · game: survivor · queue: **P3**

**Where:** src/bot_modules/cogs/survivor_cog.py:53-57, docs/survivor_spec.md:61

**What:** Survivor is absent from /games help — but so are Mahjong, Casino, Guess Who and Whisper: GAME_COMMANDS/GAME_DESCRIPTIONS only cover the games platform, duels and Risky Rolls, so the actionable item is a one-line entry (or a 'standalone games' footer) in games_help for every channel-native game, Survivor included. Drop the main-chat announcement recommendation or phrase it as a question for Billy — he removed Survivor's main-chat echo on 2026-08-20 (86e710de, 'the panel is the advertisement, permanently').

**Fix:** One line in /games help and the games menu pointing at the Survivor channel while enrollment is open; optionally a one-time main-chat announcement at the Week 1 slate (a dial, default off).

## Rotation rooms: Guess Who, Whisper, Risky Rolls, feature rotation

### rotation-rooms-155 — Feature Rotation 'Save Settings' throws before the PUT — the rotation cannot be enabled from its only admin surface
*backend · high · S · confirmed ×2* · game: feature_rotation · queue: **P1**

**Where:** src/web_server/static/js/panels/feature-rotation.js:196-233

**What:** Every click on Save Settings dies with an uncaught TypeError in the async listener — no toast, no request, no config row. An admin ticks Rotation: On, presses Save, sees nothing, and the feature stays dark forever. This is almost certainly why prod has a 4-room pool and no config row (docs/plans/rotation-rooms-round-2-build.md records 'rotation never enabled' as a fact but not the cause).

**Fix:** Delete the `tz_offset_hours` line from the body at :243 (the route's ConfigBody has no such field and the tz is read from Server Settings). Add a browser interaction scenario for the panel that clicks Save and asserts the PUT fired — the panel-load suite mounts the panel but never exercises the button, which is how this shipped.

**Already recorded / fixed:** docs/plans/rotation-rooms-round-2-build.md §1 records 'rotation never enabled' as prod reality; the broken Save is new.

### rotation-rooms-156 — Risky Rolls' payoff — the winner's question — is dropped in most rounds and nothing chases it
*gameplay · medium · M · confirmed with correction* · game: risky_roll · queue: **P6**

**Where:** src/bot_modules/services/risky_roll/views.py:233-262, formatters.py:138-166, store.py:398-414

**What:** In the last 7 days at least 12 Risky Rolls rounds resolved with a winner who never pressed Ask Question (the pending table only records failures — rounds whose question was asked and answered leave no row, so the true drop rate cannot be computed), and 4 of the 6 currently-posted questions have sat unanswered for over a day. Nothing re-pings the winner or answerer, no deadline is shown, and the 7-day sweep deletes the prompt silently.

**Fix:** (1) Nudge: re-ping the winner (and the answerer once a question is posted) after a configurable N hours, once. (2) Fallback so the round still pays off: if the winner has not asked after X hours, draw a question from the guild's question bank (the ToD/AMA banks already exist) and post it as the winner's question — the loser still answers,…

### rotation-rooms-157 — Risky Rolls records no play history — rolls are cascade-deleted with the round and no games_game_history row is written
*backend · medium · M · confirmed with correction* · game: risky_roll · queue: **P2**

**Where:** src/bot_modules/services/risky_roll/store.py:175-180, views.py:121-123

**What:** Risky Rolls writes no games_game_history row and its rolls are cascade-deleted with the round on every close path, so it is invisible to games-history-based metrics and the games report; the 'run a game' chore sign-off and the per-roll quest trigger do still fire, so it is not invisible to the todo board or the economy. The spec records state deletion as a non-goal; the missing history row is the new gap.

**Fix:** On resolve (views.py auto_close_round and close_button, before the delete) write a games_game_history row (game_type='risky_roll', player_count=len(state.rolls), guild_id set — not 0) and copy the rolls into a small `risky_round_history` table (game_id, user_id, roll, resolved_at) with a docs/data_register.md row;

**Already recorded / fixed:** docs/risky_roll_spec.md:141 records 'No leaderboards … closed rounds delete their state' as a non-goal; the missing games_game_history row (which every other game writes) is new.

### rotation-rooms-158 — Guess Who rounds never end unless solved — 21 rounds older than 30 days sit open, and the submitter's reveal never comes
*gameplay · medium · M · confirmed* · game: guess · queue: **P6**

**Where:** src/bot_modules/cogs/guess_cog.py:665-772, guess_nudge_service.py:49-53

**What:** A too-hard crop is a dead end for everyone: guessers burn their 5-guess cap (63 cap hits), the submitter never gets the reveal moment that is the point of posting, and the channel accumulates cards with a live Guess button that lead nowhere. The label 'Guess late' (guess_cog.py:819) is dead code — the solve path sets view=None (:748-754) — so the spec's 'button stays live' claim at docs/guess_spec.md:61 is also stale.

**Fix:** Add a round lifetime dial (default ~7 days, 0 = never) and an auto-resolve at expiry: edit the card to 'Nobody got it — it was {name}' with the spoilered original, mark solved_at with solver_id NULL, delete the original then. Offer the submitter a 'Reveal now' button on their own card. Close the 21 stale prod rounds by hand once.

### rotation-rooms-159 — Whisper sender gets no signal when they are guessed at, solved, or the guesses run out
*gameplay · medium · S · confirmed* · game: whisper · queue: **P6**

**Where:** src/bot_modules/cogs/whisper_cog.py:1143-1210

**What:** The cat-and-mouse that makes an anonymous-note game replayable is the sender watching the target guess wrong ('they think it was Alice!') and either being caught or getting away with it. Today the sender sends into a void: no notification on any guess, no notification on being unmasked, and the 'you got away with it' moment (guesses exhausted) is celebrated to the target with 'The sender stays anonymous forever' while the sender hears nothing.

**Fix:** DM the sender (branded, same send_branded_dm) on each guess: 'Whisper #N — they guessed {name}. Wrong, 2 left.' / 'They got you.' / 'They're out of guesses — you're safe.' Names in DM content are fine (mentions resolve). Keep it opt-out-able via the existing forget-me/optout if anyone objects.

### rotation-rooms-162 — Whisper Log Channel hint claims '(disabled) means nobody, including moderators, can find out who sent a whisper' — the admin audit panel shows every sender regardless
*ux · medium · S · confirmed* · game: whisper · queue: **P6**

**Where:** src/web_server/static/js/panels/config-whisper.js:45-47, src/web_server/static/js/panels/mod-whisper-audit.js:40, src/web_server/routes/moderation.py:1333-1348

**What:** The hint is a privacy statement and it is wrong in the dangerous direction: an admin reads it as 'we chose not to be able to deanonymise', while the audit panel deanonymises every whisper. The spec, manual and the member-facing consent copy all say identities are logged; the panel is the odd one out.

**Fix:** Rewrite the hint: 'A moderator-only Discord channel that mirrors each send/reply/report. Sender identity is always visible on the Whisper Audit page regardless of this setting.' Consider also whether the log channel should default on for a guild that enables Whisper, since reports (1 whisper + 2 reply reports in prod) otherwise surface on…

**Already recorded / fixed:** docs/reviews/2026-08-05-whisper.md G2 asked to verify the log channel target; it did not notice the hint contradicts the audit route.

### rotation-rooms-163 — Whisper Audit panel filters on states that do not exist — 'Shared' (140 prod rows) is unfilterable, three options always return nothing
*ux · medium · S · confirmed* · game: whisper · queue: **P6**

**Where:** src/web_server/static/js/panels/mod-whisper-audit.js:4-16, src/bot_modules/services/whisper_models.py:7-10, routes/moderation.py:1320-1322

**What:** A mod picking Expired/Rejected/Accepted gets an empty table and reads it as 'no such whispers'; the one state they might actually want (Shared) is not offered and renders through the raw fallback. The labels look like a copy-paste from another audit panel.

**Fix:** Replace STATE_LABELS with pending → 'In inbox', shared → 'Shared to feed' (drop hidden unless the Hide flow returns). One-line change; add a row to the audit-panel test if one enumerates filter options.

### rotation-rooms-164 — Guess Who: a member who leaves the server leaves guessable-but-unsolvable rounds behind, and the nudge can still ping about them
*backend · medium · S · confirmed* · game: guess · queue: **P6**

**Where:** src/bot_modules/cogs/guess_cog.py:1952-1984, guess_nudge_service.py:107-127

**What:** Guessers spend their cap and cooldown on a round nobody can win; the round then joins the permanent-open pile (finding above); the departed member's cached intimate original stays on disk for up to 90 days with no consent holder left in the guild. GDPR-wise the 08-05 review's G2 age-out helps but a departure should be the trigger, not a calendar.

**Fix:** Add an `on_member_remove` listener that calls the same `_do_flag_user_open_rounds_optout` + `_do_withdraw_consent` path and deletes the round's original immediately (the crop already posted stays). One test at the repo layer with a departed answer.

**Already recorded / fixed:** docs/reviews/2026-08-05-image-guard-guess.md G2 covered stale originals by age; departure is a different trigger.

### rotation-rooms-160 — Second daily scheduled Risky Rolls round is skipped whenever a member-started round is open — and members start 3-4 a day
*backend · low · S · confirmed with correction* · game: risky_roll · queue: **P0**

**Where:** src/bot_modules/services/scheduled_games_service.py:393-411, risky_roll_cog.py:324-334, store.py:12

**What:** Row 6 (12:53 local daily) was skipped_active on 09-02 because a member-started round was open; a daily row that skips is advanced a full day, so it can only ever fire if the channel happens to be idle at that minute. The dashboard row does show 'Skipped — channel was busy', but only for the last run, so a streak of skips is not visible. /risky start has no role gate; the per-channel cap is a dashboard dial (default 10) that prod has never set.

**Fix:** Either retire row 6 (the room self-serves in the evening) or make a recurring skip re-try within a bounded window (e.g. every poll for 60 min) before advancing the day, the way `once` rows already stay due. Separately consider a mod-only or role-gated `/risky start` if the intent was a hosted cadence;

### rotation-rooms-161 — Both social panels promise '(none) lets anyone submit/send' — the code fails closed instead
*ux · low · S · confirmed* · game: guess · queue: **P6**

**Where:** src/web_server/static/js/panels/config-guess.js:63-64, src/bot_modules/cogs/guess_cog.py:137-141, config-whisper.js:56-57

**What:** An admin who reads the hint and picks '(none)' to open the game to everyone has switched the game off, and members get a 'not configured' error with no clue why. It is the exact 'preference that isn't enforced' CLAUDE.md forbids, in reverse: the dial promises openness the code refuses.

**Fix:** Change both hints to '"(none)" turns submitting/sending off' (the manual at manual.html:823 already says the guess commands refuse without a role), or implement the promise. Copy-only fix; same wording in manual.html if changed.

### rotation-rooms-165 — Whisper Expose (and Hide) are dead code the spec and manual still describe
*backend · low · S · confirmed with correction* · game: whisper · queue: **P6**

**Where:** src/bot_modules/cogs/whisper_cog.py:554-632, whisper_service.py:172-175, docs/whisper_spec.md:63

**What:** Whisper Expose is dead code the Reference spec still describes (never attached to any view since 1396fb5e; the audit 'Exposed' column is frozen at 31 pre-May-27 rows), and validate_hide/STATE_HIDDEN have no writer. The manual's 'Hide it from your main inbox' is the existing Delete (soft-delete) button under the wrong name, not a missing button — fix by relabelling, alongside either restoring Expose on the solved-feed line or deleting the button, validator, column and spec lines.

**Fix:** Decide: restore Expose on the 'solved the whisper' feed line (it was the public payoff — see the sender-feedback finding) or delete WhisperExposeButton, validate_hide, the Exposed column, and the spec/manual sentences in one commit.

**Already recorded / fixed:** docs/reviews/2026-08-05-whisper.md A2 flagged a different dead function (decrement_guesses_left); Expose/Hide were not noticed.

### rotation-rooms-166 — Whisper launcher bump does a DB read for every message in every guild — the fast path Guess got was not applied here
*backend · low · M · confirmed with correction* · game: whisper · queue: **P6**

**Where:** src/bot_modules/cogs/whisper_cog.py:2334-2348, guess_cog.py:1813-1857, guess_cog.py:1754-1766

**What:** The whisper launcher's per-message DB read and delete-then-post shape are real and unfixed, but they are already the documented open item in docs/plans/sticky-panel-extraction.md (:3-5, :67, :74) — record as 'still open, no new information', not as a new finding.

**Fix:** Migrate the whisper launcher to `core.sticky.StickyPanel` (load_ids/save_ids/build callbacks, TextChannel target) exactly as the Guess prompt was in 11601f41; that brings the known-guilds fast path, post-before-delete and the shielded placement for free.

**Already recorded / fixed:** docs/plans/common-lib-round-2.md lists sticky-panel consolidation generally; this names the remaining hand-rolled instance.

### rotation-rooms-167 — Whisper guild-side guess picker offers every member, not the opt-in pool, and a stray pick burns one of three guesses
*ux · low · S · confirmed* · game: whisper · queue: **P6**

**Where:** src/bot_modules/cogs/whisper_cog.py:1216-1259, whisper_service.py:126-152

**What:** From the inbox a target can guess a member who cannot have sent the whisper (no Whisper role) and lose a third of their attempts; the DM route quietly avoids this. Two entry points to the same game play by different rules.

**Fix:** After the pick, check `cfg.role_id in guessed.roles`; if not, edit_message '❌ They aren't in the Whisper pool — that one's free.' without consuming. Or drop the native picker and use the pool list everywhere for consistency (it also leaks nothing extra: the pool is the same list the send picker shows).

### rotation-rooms-169 — Risky Rolls copy drift: panel says 'Risky Roller', manual says 'dare ladder', how-to-play promises a reply nothing enforces
*ux · low · S · confirmed with correction* · game: risky_roll · queue: **P6**

**Where:** src/web_server/static/js/panels/config-risky-rolls.js:25, manual.html:704, risky_roll_spec.md:3

**What:** Three of the four copy drifts are real: (1) src/web_server/static/js/panels/config-risky-rolls.js:25 renders '<h2>Risky Roller</h2>' (and :121 'Couldn’t load the Risky Roller settings.') while mountGamePanel at :75 says gameName 'Risky Rolls' and the nav/manual call it Risky Rolls; (2) manual.html:704 'a dice-based dare ladder' describes no mechanic in the game (risky_roll_spec.md:3 — roll 1–100, highest asks, lowest answers; no dares, no ladder);

**Fix:** Rename the panel heading to Risky Rolls; rewrite the manual line as 'a dice game — highest roll asks a question, lowest roll answers it'; soften the how-to-play to 'the loser is asked to reply' or add the deadline from the payoff finding above.

### rotation-rooms-170 — Guess Who picker includes the guesser — a self-pick burns a guess and starts the cooldown
*ux · low · S · confirmed* · game: guess · queue: **P6**

**Where:** src/bot_modules/cogs/guess_cog.py:872

**What:** Trivial, but with a 5-guess cap (63 cap hits in prod) and a 60s cooldown every wasted option costs something, and 'me' is the first name a fat thumb lands on in a filtered list.

**Fix:** Exclude `interaction.user.id` from guess_members alongside the no-contact filter (candidate_members_for already takes an exclusion set).

### rotation-rooms-171 — Guess Who consent evidence covers 4 members out of ~50 active submitters — grandfathered role holders have no consent row
*backend · low · S · confirmed with correction* · game: guess · queue: **P6**

**Where:** src/bot_modules/cogs/guess_cog.py:1686-1722, guess_repo.py:457-479

**What:** Core stands, with the numbers corrected and one recommendation fixed. Prod guess_consents has 4 rows (1 withdrawn), all from 2026-08-08 onward, against 57 distinct submitters, 55 solvers and 81 guessers in guess_rounds/guess_guesses; and since the consent record shipped (2026-08-06), 62 of 65 new rounds came from 15 grandfathered submitters with no consent row — so the unevidenced processing is ongoing, not just historical.

**Fix:** One-off: post the disclosure to existing role holders (ephemeral /info panel already has Join/Leave) or write a version-0 'legacy role holder, disclosure not shown' row per current holder so the gap is recorded rather than invisible; note it in docs/data_register.md.

**Already recorded / fixed:** docs/reviews/2026-08-05-image-guard-guess.md U1/G1 asked for the consent package; the backfill gap is the follow-on.

### rotation-rooms-168 — Whisper conversations end after exactly one reply — the hook that would bring both parties back is cut off by design
*gameplay · low · M · refuted* · game: whisper · queue: **P6**

**Where:** src/bot_modules/services/whisper_service.py:15, whisper_cog.py:896-906

**What:** The reply is the most-used follow-through (41% of sends get one) and it is a dead end: the sender cannot answer the reply, so every thread is one message each way. Whisper is an anonymity game, but a bounded anonymous back-and-forth (say 3 each) is what makes members send another one tomorrow.

**Why refuted:** The code is as cited (whisper_service.py:15 REPLY_LIMIT_PER_WHISPER = 1, validate_reply :191-198; WhisperReplyDmView :901-911 carries only Report) and the prod numbers roughly hold (848 whispers / 424 solved / 409 replies; 181 solved whispers with a reply; last 30d 289 sends / 116 replies).
