# User Testing Checklist

For testers with **no special server permissions** — a regular member account. Everything here should be doable without needing Manage Server, a mod role, or Administrator.

Companion lists: [mod_testing_checklist.md](mod_testing_checklist.md), [admin_testing_checklist.md](admin_testing_checklist.md). Full specs for any item live in `docs/<name>_spec.md`.

Check items off as you go. If something behaves differently than described, note it rather than just unchecking it — that's a bug report, not a failed checkbox.

---

## Onboarding & Roles

### Role Grant

- [ ] **`/grant`** — If you're on a grant's allowlist, run `/grant role:<key> member:<@member>`; confirm the role is added and (if configured) an announcement/audit-log post appears.

### Role Menus

- [ ] **Role Menu — Toggle mode** — Click a Toggle-mode option on a published role menu; confirm the role grants with an ephemeral confirmation. Click again and confirm it's removed.
- [ ] **Role Menu — Unique mode** — Click one Unique-mode option, then another; confirm the first role auto-drops when the second is granted.
- [ ] **Role Menu — Verify mode** — Click a Verify-mode option twice; confirm it grants once and the second click doesn't remove it.
- [ ] **Role Menu — Drop mode** — Click a Drop-mode option while holding the role (confirm removal), then while not holding it (confirm no grant).
- [ ] **Role Menu — Binding mode** — Click a Binding-mode option, then try to change your pick; confirm "Your choice here is permanent."
- [ ] **Role Menu — Dropdown style** — Open a dropdown-style menu, check/uncheck options, submit; confirm your role set matches exactly.
- [ ] **Role Menu — max-roles cap** — Try acquiring one more role than a menu's cap allows; confirm the "remove one first" message.
- [ ] **Role Menu — required-role gate** — Without the required role, click any option; confirm the "requires the @X role" message.
- [ ] **Role Menu — cooldown** — Click two options in quick succession on a cooldown-enabled menu; confirm the "slow down" message.

### Inactive Panel

- [ ] **"Open Ticket" panel button (Inactive)** — If you're marked inactive, click "Open Ticket" on the inactive panel; confirm a ticket opens.

## Moderation-Adjacent (things any member can trigger)

### Tickets

- [ ] **`/ticket open [description]`** — Open a ticket with a description; confirm a private ticket channel is created and you get a DM confirmation.
- [ ] **Open Ticket About This Message (context menu)** — Right-click a message → Apps → Open Ticket About This Message; confirm the ticket embeds a jump link to that message.

### DM Permissions

- [ ] **`/dm_help`** — Run it; confirm it explains the three DM modes (open/ask/closed) and the request flow.
- [ ] **`/dm_set_mode`** — Set your mode to Ask, then Closed; confirm only the current mode's role is held.
- [ ] **`/dm_revoke user:<member>`** — Revoke an existing DM connection; confirm both sides get a revoke DM. Try with no existing connection and confirm the "no connection" message.
- [ ] **`/dm_status user:<member>`** — Check status against a connected member (✅) and an unconnected one (❌).
- [ ] **Open DM Request Form (panel button)** — Pick a recipient in Ask mode, submit an optional reason, confirm the target gets a DM with Accept/Deny. Send 6 in a row and confirm the "max 5 pending" message.
- [ ] **Accept / Deny DM request buttons** — As the target, click Accept (confirm both sides get a confirmation DM) and separately Deny (confirm the denial DM). Have a non-target click and confirm "This request isn't for you."

### Data Deletion

- [ ] **`/delete_me`** — Confirm the Yes/Cancel prompt, then that it fully deletes your data across Discord + DB (archive kept by default) and posts a summary.
- [ ] **Concurrent-deletion lock** — Start `/delete_me`, then immediately run it again; confirm "A deletion is already running…".

### Post Monitoring

- [ ] **Post monitoring — non-spoilered image** — Without a bypass role, post a non-spoilered image in a spoiler-required channel; confirm it's deleted with a self-destructing reminder.
- [ ] **Post monitoring — spoilered image** — Post an already-spoilered image in the same channel; confirm no action.
- [ ] **Post monitoring — bypass role** — With a bypass role, post a non-spoilered image; confirm it's left alone.
- [ ] **Post monitoring — non-image content** — Post a non-image attachment or plain text; confirm no action.

## Economy & XP

### XP

- [ ] **`/xp_leaderboards`** — Run with default timescale; confirm four top-5 boards (Text/Replies/Voice/Image Reacts) plus your own rank.
- [ ] **Text message XP** — Post a normal message; confirm XP is added. Reply to another human's message and confirm the reply bonus.
- [ ] **XP cooldown / duplicate dampening** — Send two messages within 10 seconds; confirm the second gets reduced XP. Repeat identical content and confirm further reduction.
- [ ] **Image-reaction XP** — Post a non-spoilered image and have someone react; confirm you (the poster) get XP.
- [ ] **Reaction-given XP** — React to someone else's message; confirm you get a small stipend once (not repeated on remove/re-add).
- [ ] **Voice-tick XP** — Sit in a voice channel with ≥2 non-bot humans past one interval; confirm XP lands.
- [ ] **Level-5 role grant + level-up announcement** — Cross level 5 (or any level) and confirm the role grant / level-up embed posts.

### Daily Income

- [ ] **Daily login credit** — Send your first qualifying message (or 5 min of voice) of the day; confirm a silent daily-login currency credit.
- [ ] **Login streak / grace / milestones** — Log in several days running; confirm the streak bonus grows and day-7/30/100 milestones pay extra. Miss a day within a rolling week and confirm the grace period preserves your streak.
- [ ] **XP→currency daily conversion** — Earn XP during the day; confirm currency appears after local midnight as a "Daily activity" ledger entry.

### Quests

- [ ] **`/bank quests`** — Confirm your personal daily/weekly/monthly quests plus any active event/community quests show with progress and claim state.
- [ ] **Claim an instant quest** — Claim one; confirm immediate payout. Try re-claiming the same quest in the same period and confirm it's blocked.
- [ ] **Claim a sign-off quest** — Claim one; confirm a bank-channel card posts and your wallet shows "pending" until a mod approves/denies.
- [ ] **Trigger-word quest** — Post a message containing a configured quest's exact trigger phrase; confirm instant payout or a sign-off card files.
- [ ] **Onboarding quest DMs** — On a fresh join (or with a test alt), confirm a starter-path DM lists onboarding quests once.
- [ ] **Trigger-kind quests (spot-check a few)** — Pick 3–4 of: photo-reply, party-game/duel completion, Risky Roll, Guess submission, voice session, starboard, invite, boost, bio save, media post, Pen Pals pairing, message/reply sent, confession, AMA question, whisper delivered, quote-someone — confirm each fires its matching quest exactly once when a matching quest is configured.

### Bank

- [ ] **`/bank pay @member amount`** — Pay someone ≤100; confirm both wallets update instantly. Pay >100 and confirm the extra confirm step.
- [ ] **`/bank wallet`** — Confirm it shows balance, today's XP, streak+grace, active quests, and rentals.
- [ ] **`/bank mute`** — Toggle notification mute; confirm subsequent economy DMs fall back to the bank channel.

### Perk Shop

- [ ] **`/bank shop`** — Open it; confirm unrented perks show Rent buttons and rented ones show a customise button.
- [ ] **Rent a perk (Shop → Rent)** — Rent an affordable perk; confirm balance debits, your personal role updates, and weekly billing is scheduled.
- [ ] **Customise a rented perk** — Submit a new name/color/gradient/emoji; confirm your role updates. Try a color too close to a staff role's and confirm the refusal names that role.
- [ ] **`/bank role icon image:`** — Upload a ≤256KB image while holding the icon perk; confirm your role's icon updates.
- [ ] **`/bank gift @member <perk>`** — Gift a solid color to a friend; confirm their role updates and your balance debits.

### QOTD

- [ ] **QOTD reward** — Reply to a posted Question of the Day before day-end; confirm the once-per-QOTD reward.

### Game Rewards

- [ ] **Game participation / win reward** — Complete a covered party/duel game; confirm a participation credit, plus a win bonus if you won.

## Party & PvP Games

### Party Games

- [ ] **`/games play ffa`** — Submit a truth/dare anonymously; confirm no name attached.
- [ ] **`/games play ffa_banner`** — Confirm it posts a static prompt card with no buttons.
- [ ] **`/games play photo`** — Reply to the card with a photo; confirm it's recorded.
- [ ] **`/games play traditional`** — Join the pool, opt into a category, have the host Bank a round; confirm targeting avoids repeats.
- [ ] **`/games play compliment`** — With 2+ players, close the pool; confirm a giver→receiver pairing posts.
- [ ] **`/games play mfk`** — With 4+ players, confirm each gets a distinct 3-name slice excluding themselves.
- [ ] **`/games play wyr`** — Vote across a couple of rounds; submit a malformed (non `A|B`) question and confirm rejection.
- [ ] **`/games play nhie`** — Run with `lives:3`; confirm elimination after 3 "I have" hits. Run with `lives:0` and confirm elimination is disabled.
- [ ] **`/games play mlt`** — With 3+ players including a self-vote; confirm self-votes tally correctly.
- [ ] **`/games play twotruths`** — Submit 3 statements + lie via modal; confirm statements shuffle and voting works.
- [ ] **`/games play hottakes`** — Submit an anonymous take, run the temperature vote; confirm the live results bar updates.
- [ ] **`/games play story`** — Run with `visibility:blind`; confirm each writer sees only the previous sentence.
- [ ] **`/games play ama`** — Run `mode:screened format:hot_seat`, submit a question, have host approve; confirm it posts only post-approval.
- [ ] **`/games play fantasies`** — Submit anonymously, run Submit→Reveal→Same/Not-for-me; confirm multi-round continuation.
- [ ] **`/games play price`** — Run `source:bank`; confirm prices reveal sorted.
- [ ] **`/games play rushmore`** — Run a topic/source; confirm a 4-round snake draft with no duplicate picks.
- [ ] **`/games play clapback`** — With 3 players, confirm a unanimous winner earns the bonus.
- [ ] **`/games play legitlibs`** — Run `mode:classic`, fill blanks round-robin (let one player time out to trigger rescue); then run `mode:quiplash` and confirm parallel fill + simultaneous reveal.

### Games Commands

- [ ] **`/recap`** — After a couple of games, run it within 30 minutes; confirm highlights render.
- [ ] **`/games help`** — Confirm the full game catalog embed renders.
- [ ] **`/games support`** — Confirm the support-server invite posts.
- [ ] **`/games end` (as host)** — Confirm the confirm-popup then archive. As a non-host, non-mod, confirm it's rejected.
- [ ] **`/games join` / `/games leave` (self)** — Join/leave a roster-based game yourself; on an open-submission game like FFA, confirm "nothing to join."

### Quickdraw

- [ ] **Quickdraw — `/games quickdraw challenge`** — Challenge, accept; test one player firing before DRAW (instant loss) and a clean win.
- [ ] **Quickdraw — `cancel` / `stats`** — Cancel a pending challenge as challenger; check stats after a completed game.
- [ ] **Quickdraw — draw-window expiry** — Let neither player press FIRE; confirm it resolves VOID with no penalty.
- [ ] **Quickdraw — `revert` (as loser)** — If early revert is enabled, confirm your nickname restores.

### Hot Potato

- [ ] **Hot Potato (duel) — challenge/cancel/stats** — Challenge, accept, pass with 🤲 until detonation; confirm the holder loses and danger-zone passes earn style points.
- [ ] **Hot Potato Group — start/stats** — Open a lobby with 3+, Start; confirm clockwise-only passing and the last-eliminated player is renamed.

### Chicken

- [ ] **Chicken — start/stats** — Lobby with 3+, have players BAIL at varying meter %; confirm the bravest bailer wins and the crasher is renamed.
- [ ] **Chicken — total wipeout** — Have everyone bail before the crash; confirm the last bailer wins cosmetically with no rename.

### Musical Chairs

- [ ] **Musical Chairs — start/stats** — Lobby with 4+, race SIT during SCRAMBLE; confirm only the first valid presses seat and the runner-up is renamed.
- [ ] **Musical Chairs — false start** — Press SIT during MUSIC; confirm elimination.

### Nickname Stakes

- [ ] **Nickname stake — "Name the loser"** — As winner, click 📝 Name the loser and submit a nickname; confirm it applies and auto-reverts after the sentence period.
- [ ] **Custom-stakes mode** — Start with `stakes:"text"`; confirm no rename button, announce-only result.
- [ ] **No-nick-set timeout** — Win a nickname-mode game and wait 5 minutes without naming; confirm nobody gets renamed.
- [ ] **Challenge rate limit (3/hr)** — Issue 4 challenges/starts within an hour; confirm the 4th is rejected.
- [ ] **Cooldown / concurrent-sentence guard** — While serving a nickname sentence, try starting another nickname-mode game; confirm it's blocked.

### Pressure Cooker

- [ ] **Pressure Cooker — `/pressure challenge`** — Challenge with no stakes (nickname mode); confirm Accept/Decline responds only to the target.
- [ ] **Pressure Cooker — `cancel` / `stats`** — Cancel a pending challenge; check stats after a completed game.
- [ ] **Pressure Cooker — `revert` (as loser)** — Revert early if enabled; confirm refusal when disabled.
- [ ] **Pressure Cooker — pump/bust gameplay** — Alternate PUMP presses until the gauge busts; confirm the busting player loses and an out-of-turn press is rejected.
- [ ] **Pressure Cooker — nickname validation** — As winner, try `@name`, `everyone`, and a denylisted word; confirm each rejection reason, then submit a valid nickname.

### Guess

- [ ] **Guess — `/guess submit`** — Submit a valid image; walk the crop editor (Auto cycle, move/zoom, Post); confirm it posts publicly with a Guess button.
- [ ] **Guess — submit without the role** — Without the Guess role, run `/guess submit`; confirm the "need the Guess role" message.
- [ ] **Guess — `/guess optin`** — Confirm immediate role grant with no confirmation step.
- [ ] **Guess — `/guess leaderboard`** — Confirm both Top Posters and Top Guessers post.
- [ ] **Guess — `/guess delete` (own round)** — Soft-delete your own round; confirm re-running says "already deleted."
- [ ] **Guess — `/guess confess`** — Submit confession text; confirm it renders an anonymous card with a Post/Cancel preview.
- [ ] **Guess — Guess button** — On someone else's round, use the member picker + 🔍 Filter, guess wrong (confirm "Not it"), then guess right (confirm reveal + solved embed).
- [ ] **Guess — self-guess block** — On your own round, click Guess; confirm "can't guess on your own round."
- [ ] **Guess — cap / cooldown** — Submit 5 wrong guesses on one round (confirm the 6th is capped); guess again inside the cooldown and confirm the relative-timestamp message.
- [ ] **Guess — late correct guess** — After a round is solved, guess correctly anyway; confirm the generic "already solved" message.
- [ ] **Guess — sticky prompt buttons** — Click 🎭 Submit Guess (same pipeline as `/guess submit`) and ❓ Help.
- [ ] **Guess — no-detections fallback** — Submit an image with no detectable region; confirm the crop editor opens with a default box rather than rejecting.

### Risky Roll

- [ ] **Risky Roll — `/risky start`** — Start a round; confirm the embed with Roll/How to Play/Close Round buttons.
- [ ] **Risky Roll — `/risky start_no_ping`** — Confirm no role ping posts.
- [ ] **Risky Roll — Roll button** — Roll once (confirm you can't roll again); trigger a tie and confirm only tied players can reroll.
- [ ] **Risky Roll — How to Play button** — Confirm the ephemeral rules text.
- [ ] **Risky Roll — Close Round button** — As opener, close after the min-game-time floor with ≥2 rollers; confirm resolution. Try before the floor and confirm the wait message.
- [ ] **Risky Roll — special rolls (69/100/1)** — Get each and confirm the corresponding side-game (room question, target-the-bottom-two, two-questioner sub-game).
- [ ] **Risky Roll — Ask Question / Reply buttons** — Submit a question as the eligible questioner; try asking twice (confirm blocked). Reply once as recipient; try a second reply (confirm blocked).

## Voice

### Voice Master

- [ ] **Hub join → channel creation** — Join the configured Hub; confirm a new channel is created, your saved profile applies, and you're owner.
- [ ] **`/voice access <state>`** — Cycle open→nsfw→locked→spectate; confirm the NSFW flag, status line, and permission shape change each time.
- [ ] **`/voice rename`** — Rename twice within 10 minutes, then try a third; confirm the rate-limit message.
- [ ] **`/voice limit`** — Set a user cap, then reset to 0; confirm it applies then clears.
- [ ] **`/voice invite`** — Invite without `remember` (confirm View+Connect + DM link); repeat with `remember:true` (confirm trust list gets it too).
- [ ] **`/voice kick`** — Kick a member in your channel; confirm disconnect. Try kicking yourself and confirm it's rejected.
- [ ] **`/voice transfer`** — Transfer ownership to someone in the channel; confirm it moves.
- [ ] **`/voice claim`** — After the owner leaves past the grace period, claim the channel; try claiming before the grace period and confirm the remaining-time message.
- [ ] **`/voice reset`** — Reset a locked/nsfw channel; confirm overwrites/status return to open (but the Discord NSFW badge persists). Try `also_profile:true`.
- [ ] **`/voice owner`** — Confirm it reports the correct owner.
- [ ] **`/voice sleepkick`** — Set a short timer; confirm disconnect after it elapses. Set `0` and confirm cancellation.
- [ ] **`/voice knock`** — Knock on a locked channel you don't own; confirm the Accept/Deny embed and that Accept grants access.
- [ ] **`/voice trusted` list/add/remove** — Add a member (confirm idempotent re-add), then remove; confirm the cap evicts the oldest entry when exceeded.
- [ ] **`/voice blocked` list/add/remove** — Add/remove; confirm you can't block yourself or a bot.
- [ ] **`/voice profile show`** — Confirm it displays your saved settings.
- [ ] **`/voice profile reset`** — Reset one field, confirm only that clears; reset `all` and confirm everything clears.
- [ ] **Panel dropdowns (Access/Settings/Permissions)** — As owner, use the persistent panel; confirm parity with slash commands.
- [ ] **Claim button (panel)** — After the owner-gone grace period, confirm the "up for grabs" prompt appears and clicking re-validates.

### Needle

- [ ] **Needle — `/close`** — Inside your thread, run `/close`; confirm it archives/unlocks. As a non-owner without Manage Threads, confirm the permission error.
- [ ] **Needle — `/title`** — Rename your thread; confirm the update. Submit an empty title and confirm rejection.
- [ ] **Needle — welcome-message buttons** — Click Archive/Edit-title on the pinned welcome message; confirm parity with the slash commands.
- [ ] **Needle — thread auto-creation** — Post in a Needle-configured channel; confirm a thread spawns with the configured title/slowmode/auto-archive.
- [ ] **Needle — status reactions** — Post, then have someone else reply; confirm the unanswered/archived reactions behave as configured.

### Voice Transcription

- [ ] **Voice transcription** — Post a native Discord voice message in an enabled channel; confirm the bot replies with the transcript. Post outside any configured channel allowlist and confirm no reply.

## Music

### Music

- [ ] **`/play`** — With a search query, a YouTube URL, and a Spotify track URL; confirm playback starts and the now-playing card shows correct metadata.
- [ ] **`/play` (Spotify playlist)** — With a playlist over 500 tracks; confirm only the first 500 queue with a size warning.
- [ ] **`/skip`** — Confirm the next track starts and the card updates in place.
- [ ] **`/queue`** — With several tracks queued; confirm paginated display.
- [ ] **`/shuffle`** — Confirm upcoming order changes without touching the current track.
- [ ] **`/loop`** — Cycle off/track/queue; confirm correct repeat behavior each time.
- [ ] **`/pause` / `/resume`** — Confirm both work from the bot's channel.
- [ ] **`/stop`** — With 24/7 off, confirm queue clears + disconnect. (24/7-on behavior is a mod-tier check.)
- [ ] **`/nowplaying`** — Confirm the card reposts.
- [ ] **`/disconnect`** — Confirm a force-disconnect.
- [ ] **Now-playing card buttons** — Click Pause/Resume/Skip/Stop/Shuffle/Loop; confirm parity with slash commands. Click from outside the voice channel and confirm the rejection.
- [ ] **`/play` from a different voice channel** — While the bot's in channel A, run `/play` from channel B; confirm the "join me there or wait" rejection.

## Social & Content

### Confessions

- [ ] **`/confess`** — Submit a body under the character cap; confirm it posts to the destination channel/thread and mirrors to the mod log.
- [ ] **Confess launcher button** — Confirm the same modal/flow as `/confess`.
- [ ] **🎭 Reply Anonymously** — Reply in a confession thread twice; confirm your identity (name+color) stays stable across replies.
- [ ] **🎲 Reply as Someone New** — Use it twice; confirm each reply gets a different identity.
- [ ] **❓ What's this? (confessions)** — Confirm the ephemeral help text.
- [ ] **Confession cooldown / per-day limit** — Submit two confessions inside the cooldown window and confirm the slow-down message; hit the daily cap and confirm the limit message.

### Whispers

- [ ] **`/whisper send`** — As a Whisper-role member, send a message ≤1000 chars; confirm the target gets a DM with Guess/Share/Reply/Delete and a no-content feed announcement posts.
- [ ] **`/whisper sent`** — Confirm it lists your active sent whispers.
- [ ] **`/whisper optin` / `optout`** — Confirm the consent embed grants the role, and optout removes it while preserving existing whispers.
- [ ] **`/whisper forget-me`** — Run the two-step confirm; confirm every whisper/reply/guess/report you're party to is deleted.
- [ ] **Send Whisper / My Inbox / My Sent (launcher buttons)** — Confirm each matches its slash-command equivalent.
- [ ] **Whisper guessing** — As target, use all 3 guesses: one wrong ("Wrong! N left"), then correct ("You solved it!" + feed announcement with Expose).
- [ ] **Whisper Share / Reply / Delete / Expose** — Share reveals full content; a second Reply attempt is blocked; Delete removes it from your inbox only; Expose (after a correct guess) appends the sender's name.
- [ ] **Whisper Report button** — Report once; confirm a second report attempt is blocked.
- [ ] **Whisper cooldown / hourly cap** — Send two whispers within 30 seconds (confirm cooldown); send a 6th to the same target within an hour (confirm the cap).

### Bios

- [ ] **`/bio`** — As a new user, confirm a private wizard channel is created and the walkthrough begins.
- [ ] **Create/Update Bio button** — Confirm the same wizard flow, with edit-mode pre-fill if you already have a bio.
- [ ] **Bio wizard field steps** — Answer normally, then Skip an optional field (confirm it's omitted); exceed a field's max length (confirm re-prompt).
- [ ] **Bio wizard question steps + re-roll** — Answer a drawn question, then 🎲 re-roll one and confirm a fresh, non-duplicate question.
- [ ] **Bio wizard completion** — Complete it; confirm the embed posts to the bios channel.
- [ ] **Re-trigger `/bio` mid-session** — Start `/bio`, run it again before finishing; confirm a resume/restart prompt with no second channel.

### Birthdays

- [ ] **`/birthday set`** — Set a valid date (and optional request); confirm confirmation. Try Feb 30 and confirm the day-count rejection.
- [ ] **`/birthday remove`** — Remove a stored birthday; confirm success. Run with nothing stored and confirm the "no birthday" message.

### Pen Pals

- [ ] **`/penpals join` / `leave`** — Join the pool (confirm "you're in"); try joining again while paired (confirm "already have a pen pal"). Leave before a round runs.
- [ ] **`/penpals status`** — Confirm it shows your current pairing/time-remaining or pool position.
- [ ] **`/penpals new-question`** — Swap the question in an active session; confirm the swap counter decrements. Use all 3 and confirm the cap message.
- [ ] **`/penpals end`** — Start the 15-second confirm; confirm the channel deletes and the other member gets a DM.

### Emoji Stealer

- [ ] **Steal Emoji (context menu)** — Right-click a message with one custom emoji (confirm immediate upload); right-click one with multiple (confirm the Steal/Steal All/Cancel picker).
- [ ] **`/steal_emoji`** — Upload from a valid HTTPS URL; try a non-HTTPS URL and confirm the rejection.
- [ ] **GIF compression (emoji stealer)** — Steal an animated GIF over 256KB; confirm it downscales until it fits.

### Todo

- [ ] **`/todo`** — Add a task; confirm an ephemeral reply with the new id. Submit an empty task and confirm rejection.
- [ ] **Add to Todo (context menu)** — Right-click a message, add optional notes; confirm the todo captures the source jump-link.

### Quote Cards

- [ ] **`Quote` (context menu)** — Right-click a message, pick a theme/font, Generate, review the preview, Post; confirm it posts publicly and auto-reacts.
- [ ] **Quote on system/empty message** — Attempt it; confirm the ephemeral rejection.

### Banner Cards

- [ ] **`/banner`** — Run with free text; confirm a centered banner card renders.

### Starboard

- [ ] **Starboard threshold repost** — React with the configured emoji from enough distinct members; confirm the repost appears in the starboard channel.
- [ ] **Starboard self-star exclusion** — Have the message author react; confirm it doesn't count toward the threshold.
- [ ] **Starboard reaction removal** — Remove a counted reaction; confirm the footer count decrements via edit, not a new post.

### Support & Invite

- [ ] **`/support`** — Run it in a guild and in a DM; confirm both return the support-server invite.
- [ ] **`/invite`** — Confirm it returns the bot's OAuth install URL.

### Chat Revive

- [ ] **Chat Revive — opt-in button** — Click the persistent opt-in join/leave button; confirm your role membership toggles.
- [ ] **Chat Revive — passive posting** — Let a channel sit quiet with an eligible configuration; confirm a revive question posts (banner card or text fallback).
- [ ] **Chat Revive — adult-only questions** — Confirm adult-tagged questions only ever appear in an NSFW-flagged channel.

### Auto React

- [ ] **Auto React (passive)** — Post an image in a channel with an auto-react rule; confirm the configured emoji(s) are added. Post a bare image *link* (no attachment) and confirm no reaction.

### Bump Tracker

- [ ] **Bump Tracker — auto-detection** — Have the configured listing bot post its confirmation message; confirm the bump auto-logs.

## Wellness Guardian

### Wellness Guardian

> **Prerequisite:** This whole feature is dormant until an admin manually seeds a `wellness_config` row with `role_id`/`channel_id` — nothing in the bot provisions this automatically. Confirm with whoever's running the admin checklist that this has been done before testing anything below.

- [ ] **`/wellness setup`** — With the guild seeded, run it; confirm the 2-step wizard (disclaimer+timezone, then enforcement level) completes and assigns the Wellness Guardian role.
- [ ] **`/wellness away on [message]`** — Confirm the ephemeral preview embed and that away mode activates.
- [ ] **`/wellness away off`** — Confirm away mode deactivates.
- [ ] **Away-mention auto-reply** — Have someone @-mention you while away; confirm an in-channel auto-reply posts (rate-limited) and your message itself isn't deleted.
- [ ] **Slow-mode friction** — With active per-user slow mode, post again inside the interval; confirm it's deleted and you're DMed the held content + countdown.
- [ ] **Cap evaluation + escalation** — Exceed a configured cap repeatedly; confirm nudge → cooldown → slow-mode escalation.
- [ ] **Blackout enforcement** — Post during an active blackout window; confirm your enforcement level applies.
- [ ] **Streak decay** — Trigger an overage; confirm your streak decays (never to zero) and your personal best doesn't decay.
- [ ] **Active-list embed + milestones** — Opt into public commitment and reach a milestone; confirm the hourly-refreshed embed and celebration post.
- [ ] **Weekly summary DM** — Wait for Sunday ≥09:00 your local time; confirm one summary DM arrives.
- [ ] **Member dashboard — caps/blackouts/away/partners/settings/pause** — Via your member dashboard, create a cap, apply a blackout preset, set away text, send/accept a partner request, change settings, and pause/resume your own tracking; confirm each persists and takes effect.
