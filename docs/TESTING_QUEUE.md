# Testing Queue

Changes that pass pytest + the fake-driven smoke checks but still need a
**live-server** pass before we fully trust them (Discord API behaviour that
can't be exercised offline). Move an item to the bottom "Done" section once
it's been verified in the dev guild, with a date.

---

## Pending

### Chat Revive (stages 0–4) — rhythm-aware lull questions  (this commit)

New feature ("Ember"): a monitor loop learns each enabled channel's per-band
message rhythm from `processed_messages` and posts a bank question into a
genuinely unusual lull — never over an active room, never twice in a row,
never overnight. Migration 073 adds five `revive_*` tables **and a new
channel-leading index on `processed_messages`** (builds once over the full
~516 MB table). `/revive` command group is mods-only.

- [ ] Restart the bot → boots clean; expect the restart to take noticeably
      longer than usual **once** while `idx_pm_channel_ts` builds.
- [ ] `/revive setup` (let it create the role) → confirms settings; the echoed
      **server-local time is correct** (tz sanity — main guild inherits the
      global −7 offset).
- [ ] `/revive channel #test-channel` → enabled; `/revive check` there explains
      the rhythm (or fallback mode for a young channel) in plain language.
- [ ] `/revive fire` in the test channel → plain-text question posts (🔥 +
      flourish), no embed; a second `/revive question list` shows its
      use-count bumped.
- [ ] `/revive optin-post` → button posts; tapping toggles the role on/off
      (works again **after a restart** — dynamic item persistence).
- [ ] With ping enabled (`/revive channel #test ping:True`), `/revive fire`
      pings the role once; firing again the same day posts **un-pinged**.
- [ ] Auto-fire soak (needs patience): enable a quiet-ish real channel, watch
      logs for `revived #…`; verify it never posts while a game is running in
      that channel, never inside quiet hours, and never chains after its own
      message with no human in between.
- [ ] ~35 min after any revive, `/revive stats` shows it measured
      (sparked or not) and the scoreboard renders.

### Economy — quest completion pings via DM under a game role  (this commit)

New `game_role_id` setting (Dashboard → Economy → Settings → **Game role**).
When set, auto-claimed quest completions (trigger-word, photo-reply, media-post)
DM the claimant their ✅/📝 card instead of replying in the trigger channel;
members without the role are paid silently. Unset (default) keeps the legacy
in-channel reply for everyone.

- [ ] With **no** Game role configured, a trigger-word quest still posts the
      ✅ reaction + in-channel reply embed (unchanged legacy behavior).
- [ ] Set a Game role in the dashboard. A **role-holder** who fires a
      trigger-word / photo-reply / media-post quest gets the ✅ reaction on
      their message **and a DM** with the completion card — **no** channel reply.
- [ ] A member **without** the role who fires the same quest is **paid** (wallet
      goes up) but gets **no reaction, no reply, no DM**.
- [ ] A role-holder with econ notifications **muted** (`/bank mute`) gets the
      payout but no DM. If their DMs are closed, the card falls back to the
      bank channel.
- [ ] A **sign-off** trigger quest still posts the bank-channel approval card
      regardless of the claimant's role; a role-holder additionally gets the
      📝 card by DM.

### Tickets — closed-embed status fix + 24h auto-delete  (this commit)

The ticket embed's **Status** field was never re-rendered on close/reopen
(both the button flow and the `/ticket` slash flow only swapped the buttons),
so a closed ticket kept showing "🟢 Open". Escalate never touched the embed at
all. All four paths (button close/reopen, slash close/reopen) now rewrite the
Status field, and `/ticket escalate` now flips it to "⚠️ Escalated". Separately,
a new hourly sweep (`ticket_autodelete_loop`) permanently deletes any ticket
left closed for 24 h, routing through the shared `_finalize_ticket_delete`
(transcript archived + DM'd before the channel is removed). Reopening resets
the countdown. Live checks:

- [ ] Open a ticket → **🟢 Open** in the embed. Close it (Close **button**) →
      embed field flips to **🔒 Closed**, buttons become Reopen/Delete.
- [ ] Reopen it → embed goes back to **🟢 Open**, button back to Close.
- [ ] Repeat close/reopen via the **slash** commands `/ticket close` and
      `/ticket reopen` → same Status-field updates (not just the buttons).
- [ ] `/ticket escalate` on an open ticket → embed Status shows **⚠️ Escalated**
      and admin roles are pinged/added; reopen after a close keeps ⚠️ if it was
      escalated.
- [ ] Delete a closed ticket manually (button and `/ticket delete`) → transcript
      still posts to the transcript channel + DMs the creator, channel deleted.
- [ ] Close a ticket, then confirm ~24 h later (or temporarily shorten the
      window to test) it is auto-deleted: transcript posted + DM'd, channel
      gone, audit embed reads "auto-deleted 24h after close". A ticket reopened
      inside the window is **not** deleted.

### Games — launch "did not respond" fix + Clapback lobby timeout  (this commit)

Every `/games play …` command deferred publicly and never resolved the
deferred response (the lobby/prompt posts via `channel.send`), so Discord
converted the dangling placeholder into a red "The application did not
respond" ~15 min after every launch. All 17 launch commands now call a shared
`finish_launch_response()` that deletes the placeholder (and, on a failed
launch, sends the permissions hint ephemerally). Separately, the Clapback
lobby view silently expired 5 quiet minutes after the last button press,
cancelling the game but leaving live-looking buttons that swallowed the
host's Start click ("This interaction failed"). Lobby inactivity window is
now 10 min, extended past a scheduled `start_in`, and on expiry the lobby
message is edited to a disabled "Lobby timed out" state. Live checks:

- [ ] `/games play clapback` → lobby posts; the invoking user's "Poppy is
      thinking…" placeholder disappears (no red "did not respond" 15 min
      later).
- [ ] Spot-check two other games (e.g. `/games play wyr`, `/games play ttl`)
      → same: prompt posts, no dangling placeholder.
- [ ] `/games play` in a non-games channel still gets the ephemeral "isn't
      set up for games" reply (pre-flight path untouched).
- [ ] Open a clapback lobby, leave it idle 10 min → message edits to
      "⌛ Lobby timed out" with disabled buttons; clicking them does nothing
      (no "interaction failed" spinner).
- [ ] `/games play clapback start_in:15` → lobby survives past 10 idle
      minutes (window extends to start time + 2 min).
- [ ] Start a clapback game normally → plays through; recap unaffected.

### Economy — per-user quest board + Gaussian bands + duel_lose  (e62f697)

- [ ] Restart the bot → boots clean (migration 072 adds `target_min`/`target_max`;
      no error on a second boot).
- [ ] With several active dailies, two different members run `/quests` (or open
      the wallet) → each sees **2 dailies**, and the two members' boards are
      **not identical**. Same for weeklies/monthlies once >2 are active.
- [ ] A member only earns a kind when its quest is on their board: pick a member
      whose board today does **not** include the message quest, have them send
      messages → **no** quest payout for it (ledger has no `quest` row); a member
      whose board **does** include it earns it.
- [ ] Board is stable within a period: re-running `/quests` the same day shows the
      **same** 2 dailies; after the guild-local day rolls, the daily set changes.
- [ ] Counted quest with a band shows a **per-member** target on the progress
      meter (two members can see different "/N" targets for the same quest), and
      the claim fires when that member hits *their* N.
- [ ] Lose a duel (chicken/hot potato/musical chairs/pressure/quickdraw) with a
      `duel_lose` quest active → the loser earns it; the winner does **not**.
- [ ] Quest editor trigger-kind dropdown lists **🥈 Lose a duel / PvP challenge**.

### Perk shop — customise via ephemeral buttons + modals  (this commit)

Role-perk customisation moved out of slash commands into `/bank shop`'s
ephemeral panel: rented rows show a green customise button that opens a modal
(name / colour / gradient / icon), a fresh rental's confirmation carries the
same button, and `/bank role name|color|gradient` are **removed**. Icon emojis
are now **this server's custom emojis only** (the bot stores the emoji's
image); `/bank role icon` survives image-upload-only. Live pass:

- [ ] After restart the command picker shows `/bank role icon` (image param
      required) and **no** `/bank role name|color|gradient` — global command
      sync may take a few minutes.
- [ ] `/bank shop` with nothing rented → all Rent buttons; rent a colour →
      confirmation shows a **Set colour** button that opens the hex modal and
      the colour applies; reopening `/bank shop` shows that row as ✅ rented
      with a green **Set colour** button.
- [ ] Bad hex / blocklisted name / clashing colour typed into a modal → the
      usual friendly ephemeral errors.
- [ ] Rent the icon perk → confirmation notes `/bank role icon` for images.
      Icon modal: a typed `:server_emoji:` works and the role shows the emoji
      image; a pasted emoji (`<:name:id>`) works; a unicode emoji ✨ and a
      foreign server's emoji are refused; an animated emoji is refused.
- [ ] `/bank role icon` with an uploaded PNG still sets the icon.
- [ ] `/bank gift` a colour to someone with no rentals → their `/bank shop`
      shows **Set gifted colour** (plus a Rent colour button), and their DM
      points at /bank shop.
- [ ] `/bank post-guide` panel's Spending section reads "style it right from
      the shop's customise buttons".
- [ ] Renting from the **persistent shop panel** (`/bank post-shop`) also ends
      with the customise button on its ephemeral confirmation.

### Traditional TOD — Bank Round tracks asked history  (this commit)

Bank Round questions are now recorded in the same per-(player, category)
`asked` history as host-written ones: each player gets at most one bank
question per opted-in category, and re-pressing **Bank Round** after new
players join serves only the newcomers. Covered by fakes offline; live pass:

- [ ] `/games play traditional`, two players opt into SFW Truth, press
      **Bank Round** → both get a card. Press it again immediately → no cards,
      ephemeral says everyone has already been asked.
- [ ] A third player then opts in; **Bank Round** again → only the newcomer
      gets a card, ephemeral says "Served **1**" + "Skipped 2 players".
- [ ] A player opted into two categories gets their *other* category on the
      second press (not a repeat of the first).
- [ ] **Ask Question** after a bank round skips (player, category) pairs the
      bank already covered, and the lobby's "Questions Asked X / Y" counter
      includes bank questions.
- [ ] Game-over recap: per-category counts include bank questions; the
      separate "Bank Round Questions" field still shows.

### Quote cards — border-colored header + centered announcement body  (this commit)

Follow-up to the restyle: the no-pfp **header now takes the border's dominant
color** (`dominant_border_color` — vividness-weighted, so Golden Poppy → gold,
not the dark leaves), and the **announcement/banner body text is centered**
(avatar `/quote` cards stay left-aligned in their column). Verified offline;
live look:

- [ ] QOTD card (`/qotd post`): "Question of the Day" header renders in the
      border's gold; the question body is **centered** under it (not left-hugging).
- [ ] `/banner`: header echoes the active border's dominant color; multi-line
      body is centered and stays clear of the corner flowers.
- [ ] Photo-challenge and FFA launch cards: prompt body centered, header in
      border gold.
- [ ] `/quote` on a message (avatar mode): body still **left-aligned** in the
      right column — unchanged.

### Quote cards — slim frame, smaller flowers, new typography  (this commit)

The Golden Poppy quote/banner cards were restyled: the thick baked gold frame is
replaced by a slim drawn rounded-rect (full-bleed), the floral cluster is shrunk
to ~72% and tucked into the corner, the no-pfp header is de-bolded, and the fonts
default to **Helvetica header over Times body** (Times = bundled Liberation
Serif; the corrupt `lora` option was removed). Rendering is pixel-verified
offline; the live surface needs a look:

- [ ] `/quote` on a message → generate with the default (Times) → card shows the
      slim gold frame, small corner flowers, serif quote body, avatar + double
      ring intact on the left.
- [ ] `/banner` with a title → header renders in **Helvetica** (clean sans, light
      weight — no heavy outline), body in **Times**, over the guild's uploaded
      border if set (custom/Midnight frames unchanged — only Golden Poppy is slim).
- [ ] Font picker: **Times** and **Helvetica** are the first two options; picking
      another body font still renders a Helvetica header in banner mode.
- [ ] No `lora` option in the picker; a guild that previously stored `lora`
      falls back to Times without a crash.
### Economy — persistent shop panel  (this commit)

**`/bank post-shop [channel]`** [manager/admin] posts the perk shop as a
channel panel with always-working rent buttons (DynamicItems — they survive
restarts; settings + feature gates are re-read on every click, replies are
ephemeral to the clicker). Same lifecycle as the guide panel: same-channel
re-run edits in place (embed + button labels), another channel moves it.
Button labels bake prices at post time — re-run after re-pricing. Live
checks:

- [ ] `/bank post-shop` in a test channel → embed lists the four perks +
      gift line; four "Rent … · price" buttons.
- [ ] Click a rent button with enough balance → ephemeral "Rented …", role
      perk applies, rental visible in `/bank wallet` and the dashboard
      Operations → Perk rentals.
- [ ] Click with too little balance → ephemeral "need X but only have Y";
      click again while rented → "already renting".
- [ ] A second member clicks the same panel → their own rental (panel is
      shared, unlike the ephemeral /bank shop).
- [ ] **Restart the bot**, then click a panel button → still works (no
      "interaction failed").
- [ ] If gradient/icon features are missing, those buttons are disabled and
      the row says so; with the feature present they rent fine.
- [ ] Change a price on Economy → Settings, re-run `/bank post-shop` in the
      same channel → panel edits in place with new prices on the buttons.

### Economy — auto-updating leaderboard panel  (this commit)

**`/bank post-leaderboard [channel]`** [manager/admin] posts a branded embed
that the economy loop then refreshes in place every hour: 🥇 top 5 earners
over a rolling 7 days (transfers excluded), community-goal progress bars,
the active quest board, and a "check your own progress with `/quests` /
`/bank wallet`" blurb. Repost in the same channel edits in place; another
channel moves it; **deleting the message retires the panel** (the loop
clears the stored ids on 404). Live checks:

- [ ] `/bank post-leaderboard` in a test channel → embed shows earners with
      display names + currency emoji, community goal ▰▱ bar, quest lines
      with `Daily`/`Weekly` tags (+⭐xp where set), and the /quests blurb.
- [ ] Re-run in the same channel → "Refreshed", panel edits in place (no
      duplicate). Run pointing at another channel → old panel deleted, new
      one posted there.
- [ ] Wait for the top of the hour (or restart-adjacent tick) → the embed
      timestamp advances on its own.
- [ ] Earn some coins (claim a quest / QOTD) → next hourly refresh moves the
      earner totals.
- [ ] Delete the panel message → after the next tick the loop stops trying
      (no error spam in the journal; `/bank post-leaderboard` posts fresh).
- [ ] Non-manager member gets the permission refusal; economy-disabled guild
      gets the disabled message.

### Economy — Claims page + waiting-claims on the Moderation tile  (this commit)

Claim sign-off moved off Operations onto its own **Claims** page (second item
in the Economy section): the pending queue with Approve/Deny, plus a filter
strip over the history — Pending / Paid / Denied / Expired / All (an approved
claim is stored as `paid`; the Paid view also includes instant non-sign-off
completions). The home dashboard's **Moderation tile** now shows a fourth
row: pending econ claims count + latest claimant · quest (moderator-visible,
like the rest of the tile). Live checks (service restart for the JS):

- [ ] Economy → Claims: pending claims render with Approve/Deny; approving
      pays and the row leaves the Pending view; Deny prompts for a reason.
- [ ] Filter strip: Paid shows resolved claims with "paid by <manager> ·
      age"; Denied shows the deny reason; All mixes states with buttons only
      on pending rows.
- [ ] Operations no longer has a claims card; its header links to Claims.
- [ ] Home Moderation tile shows "N claims" with the latest claimant and
      quest title; 0 claims shows "0 claims · none"; a moderator (non-admin,
      non-manager) sees the row too.
- [ ] Bank-channel card Approve still works and the Claims page reflects it
      on refresh (same claim, both surfaces).

### Economy — dashboard reorg: one Economy nav section  (this commit)

The economy dashboard pages are now one top-level **Economy** section:
**Operations** (claims, community goals, grants, rentals, ledger — the old
Bank Manager page minus authoring), **Quests** (library + authoring + AI
ideas, split out), **Income Sources** (trigger switches + the faucet rates,
now editable in place for admins), **Statistics**, and **Settings** (the old
Config → Economy page, minus the faucet fields, admin-only). Same page ids —
old `#/economy-*` links still resolve. Verify live (needs a service restart
for the JS):

- [ ] As **admin**: Economy section shows all five pages; Config section no
      longer lists Economy; `#/economy-config` opens Settings inside the
      Economy section.
- [ ] As a **manager-role holder who isn't admin**: Economy section shows
      Operations / Quests / Income Sources / Statistics but **not** Settings;
      Income Sources shows faucet rates read-only (no inputs, no Save).
- [ ] As admin on **Income Sources**: edit a faucet rate (e.g. QOTD reward),
      Save rates → reload → value persisted; Settings page no longer shows
      faucet fields but still saves branding/prices fine.
- [ ] On **Quests**: create + edit a quest end-to-end (the form moved panels —
      check the game-trigger/phrase conditional fields and the AI ideas
      button still work).
- [ ] On **Operations**: set community progress and check the community card
      refreshes without the quest library (it fetches quests itself now).
      (Claim sign-off has since moved to the Claims page — see the entry
      above.)
- [ ] Cross-links navigate: Operations header → Quests; Quests library hint →
      Income Sources + Operations; Income Sources → Quests + Settings;
      Settings subtitle → Income Sources.

### Economy — onboarding path + quest XP rewards  (this commit)

Quests can now pay **XP alongside coins** (`Bonus XP` field — flat, no booster
multiplier, ledgered as xp_events source `quest`), and quests flagged
**🧭 Onboarding path** are DMed to new members on join as a branded "starter
path" embed (once ever per member; rejoins never re-DM). Live checks:

- [ ] Quest editor: Bonus XP field + 🧭 Onboarding checkbox save and reload;
      library shows "+Nxp" next to the coin reward and 🧭 before the title.
- [ ] Create the starter set as event quests flagged onboarding (e.g. "Set
      your bio" bio_set +20/+100xp once-ever, "Play your first game"
      party_game, "Say hello" message_sent) — join with a test account →
      one DM listing all three with coins + XP + how-to lines.
- [ ] Rejoin with the same account → no second DM.
- [ ] Complete a flagged quest → wallet gets coins, `/xp` (level) reflects
      the XP, ledger kind `quest`, xp_events row with source `quest`.
- [ ] Sign-off quest with XP: XP arrives at manager approval, not at filing.
- [ ] /quests shows "+ ⭐ N XP" in the reward line; trigger-quest completion
      announcements show "(+⭐ N XP)".
- [ ] Member with economy DMs muted: the starter path lands in the bank
      channel fallback instead.

### Economy — counted quests, monthly cadence, message/reply/react/win sources  (this commit)

Quests can now be **counted** ("do it N times this period" — progress bar on
/quests, occurrence-deduped so replays never double-count), **monthly** joins
the cadences (calendar month, starts the 1st guild-local, up to 5 active), and
five new sources landed: **send a message, reply to someone, react to someone
(all channel-scopable), win a party game, win a duel**. Live checks:

- [ ] Quest editor shows Monthly in the Type select with its hint and 75–200
      band; slot line reads "monthly 0/5"; a monthly manual quest claims once,
      refuses a re-claim, and (if you want to wait) resets on the 1st.
- [ ] Create weekly "React to 5 messages" (reaction_given, target 5):
      /quests shows the 🗣️-style auto line + a ▰▱ progress bar; five distinct
      reacts complete it (silent, ledger kind `quest`); re-reacting the same
      message doesn't advance the count (XP dedup inherited).
- [ ] Counted "send 10 messages" scoped to a channel: only messages there
      advance it; the bar updates; payment at 10.
- [ ] Reply quest: replying to someone else counts; replying to yourself
      doesn't.
- [ ] game_win/duel_win: win a NHIE/TTL/Hot Takes party game and a quickdraw
      duel with win-kind quests active → winner paid, losers not.
- [ ] Income Sources page lists 18 sources; counted/monthly suggestions are
      gone (built), streak-milestones suggestion appears.
- [ ] Editing a counted quest re-loads its target into "How many times".

### Economy — 8 new income sources + Income Sources page  (this commit)

Trigger kinds now cover most member-facing modules: **voice session, QOTD
reply, starboard, invite, boost, bio set, media post (channel-scopable), Pen
Pal pairing** join the five game kinds. A new **Income Sources** page (Bank
Manager section) holds an enable switch per source (default on; disabling
stops firing instantly, quests wait in the library), shows which quests use
each source, the built-in faucet values, and the suggested-sources roadmap.
Live checks:

- [ ] Income Sources page loads under Bank Manager; all 13 sources listed as
      enabled; toggling one off/on sticks across reload.
- [ ] Disable a source that an active quest uses (e.g. duel) → playing a duel
      pays nothing; re-enable → next duel pays.
- [ ] Voice: sit unmuted in VC past the qualify window with a voice-session
      daily quest active → quest completes once that day (ledger `quest`),
      not again on later ticks.
- [ ] QOTD: `/qotd post`, first reply gets flat award + completes a
      qotd-reply quest; second member likewise; repeat replies don't.
- [ ] Starboard: a message crossing the threshold completes a starboard quest
      for its author (once, even with more stars added).
- [ ] Invite: a join attributed to an inviter completes their invite quest;
      the same invitee rejoining does not re-pay.
- [ ] Boost: starting a boost completes a boost quest (ledger check).
- [ ] Bio: finishing the bio wizard completes a bio-set quest; an event
      bio quest does NOT pay again on a later edit ("once ever"), a daily one
      does next period.
- [ ] Media post: quest scoped to #art — an image in #art completes it
      (✅ + embed on the message), an image elsewhere doesn't, a thread under
      #art does.
- [ ] Pen Pals: a pairing completes the quest for both members (occurrence =
      session).
- [ ] Bank Manager quest form: "Playing a game" kind list shows all 13 with
      labels; picking Media post reveals the trigger-channel picker.

### Economy — game-trigger quests + Bank Manager UX overhaul  (this commit)

Quest triggers now span the game modules, and any daily/weekly quest can be
auto-verified by one. New trigger kinds: **party game**, **duel/PvP**, **Risky
Roll dare**, **Guess Who round** (plus the existing photo reply). Daily/weekly +
kind = "do it once this period" (auto-claims the calendar period); event + kind
= pays every occurrence. Game-fired payouts are silent in-channel (ledger +
/quests carry the news); sign-off claims still post the bank card. The Bank
Manager page was reordered around the workflow (pending claims first, slot
summary, community goals only when present, quest editor below the library) and
gained **in-place quest editing**, a completion-mode radio (member claims /
phrase / game), member pickers for grant + ledger, and a ledger kind datalist.
Live checks:

- [ ] Bank Manager loads with the new order: Pending claims → Quest library
      (slot summary line + "Also earn from…" note) → quest editor → Grant →
      Rentals → Ledger; the Community goals card only appears when a community
      quest exists.
- [ ] Edit flow: Edit on a library row loads the quest into the editor
      ("Editing: <title>", Save changes / Cancel edit), saving updates the row
      without delete/recreate; switching completion mode on edit clears the
      other mode's fields (check a phrase quest edited into a game trigger).
- [ ] Create a **daily** quest with completion "Playing a game" → Risky Roll
      dare: pressing Roll in a Risky Rolls round completes it (once that day,
      silent — verify via wallet ledger kind `quest` and /quests showing ☑️).
      A second roll the same day pays nothing.
- [ ] Create an **event** quest with trigger "Finish a duel": each completed
      quickdraw/chicken/etc. pays every participant once per game; a rematch
      pays again (new occurrence).
- [ ] Party game (e.g. /games play wyr to completion) fires the party-game
      trigger for the roster alongside the usual participation payout.
- [ ] Guess Who: making a scored guess completes a guess-kind quest; a second
      guess on the same round does not double-pay an event quest.
- [ ] Slot rule: two active event quests with the *same* kind → 409; one
      photo-reply + one duel event quest active together → allowed.
- [ ] Sign-off + game trigger: completing the game files a pending claim and
      posts the bank-channel card (no channel spam).
- [ ] Grant + Ledger use the member picker; ledger kind datalist filters
      (e.g. kind `quest`).

### Economy — photo-reply event quest + Photo Challenge ping role  (this commit)

Photo Challenge now feeds the economy: every posted card is registered, and an
active **event quest** (new type, trigger kind "photo reply") pays a member who
**replies to a card with an image** — once per member per card, no time gate
(old cards still count). The Photo panel in the Games Studio gained a
**Ping role on post** option mentioned with every card (manual and scheduled).
Offline tests cover the claim dedup, pairing validation, slot rule, listener
guards, and registry; live checks:

- [ ] Bank Manager → quest editor: pick type "Event (every time it happens)" —
      completion locks to "Playing a game" with the photo-reply trigger
      selectable; quest saves and lists with the 📸 game-trigger tag;
      activating a second photo-reply event quest is refused (409 toast).
- [ ] `/games play photo` posts a card; reply to it **with a photo** → ✅
      reaction + "Quest complete!" embed, wallet credited once (ledger kind
      `quest`). A second photo reply to the same card stays silent; a reply
      to a *new* card pays again.
- [ ] A reply without an image, or a plain (non-reply) photo in the channel,
      pays nothing.
- [ ] `/bank quests` lists the event quest with the 📸 how-to line and no
      claim button.
- [ ] With sign-off ticked on the event quest: photo reply reacts 📝, files a
      pending claim, and the bank-channel card approves/denies it (photo
      review flow).
- [ ] Games Studio → Photo Challenge → set **Ping role on post** to a role ID:
      manual `/games play photo` and a scheduled photo run both mention the
      role above the card exactly once (don't also set the schedule's
      announce ping unless a double mention is wanted).
- [ ] A card posted while **no** event quest was active still pays once a
      quest is activated later (reply after activation).

### Economy — trigger-word quest verification  (this commit)

Daily/weekly quests can carry trigger phrases (+ optional channel scope); saying
one in chat auto-claims the quest — instant quests pay on the spot, sign-off
quests file the pending claim + bank-channel card. Offline tests cover matching,
claim wiring, channel/thread scoping, and the once-per-period silence; live checks:

- [ ] Bank Manager → New quest: trigger-words field + "(any channel)" picker
      show for daily/weekly, hide when Community is selected; library table
      shows the 🗣️ trigger badge (hover = the phrases).
- [ ] Create an instant daily with trigger words (e.g. `gm, good morning`),
      activate it, say "gm" in chat → ✅ reaction + "Quest complete!" reply,
      wallet credited once; saying it again the same day stays silent.
- [ ] Channel-scoped quest: phrase in the wrong channel does nothing; in the
      configured channel (and in a thread under it) it pays.
- [ ] Sign-off trigger quest: phrase → 📝 reaction + "sent for sign-off" reply,
      card lands in the bank channel, Approve pays the claimant.
- [ ] `/bank quests` lists the trigger quest with the 🗣️ "completes
      automatically" line and does NOT offer it in the claim select.
- [ ] Dashboard edit of trigger words takes effect within ~60 s (cache TTL)
      without a restart.

### Economy — `/bank post-guide` channel how-to panel  (uncommitted)

New staff command posts a branded "how the economy works" embed that sits in a
channel (earning streams, shop prices, command crib sheet — templated from the
guild's econ settings). Message/channel ids persist in config so re-running
refreshes in place instead of stacking panels. Offline tests cover the builder
and the post/refresh/move/permission matrix; live checks:

- [ ] `/bank post-guide` (no arg, as admin or manager-role holder) posts the
      panel in the current channel; text shows the guild's currency
      name/emoji and real shop prices.
- [ ] Re-running it in the same channel **edits the existing panel** (no
      duplicate message, panel keeps its position in history).
- [ ] Changing a price or the currency emoji on the dashboard, then re-running,
      shows the new values on the refreshed panel.
- [ ] Running it with a different `channel:` option deletes the old panel and
      posts in the new channel.
- [ ] Plain member gets "you don't have permission" ephemerally; with the
      economy disabled the command answers with the disabled notice.

### Economy — AI quest-idea generator on the Bank Manager  (uncommitted)

The New-quest form gained a "✨ Generate ideas" button that batches AI quest
suggestions for the selected type (Anthropic cloud path, same as the Games
Studio — needs `ANTHROPIC_API_KEY`). Ideas render as clickable cards; clicking
one loads title/description/criteria/reward into the form. Nothing is saved
until you create it. Offline parser/prompt tests pass; the live call + form
wiring need a pass:

- [ ] Bank Manager → New quest: pick a type, click **Generate ideas** →
      cards appear within a few seconds (rewards land in the type's band;
      community ideas show a target).
- [ ] Click an idea → its title/description/criteria/reward populate the form,
      the title field focuses, and a "Idea loaded" toast shows. Editing then
      **Create quest** saves it normally.
- [ ] A theme in the box steers the ideas; changing the type changes the flavor
      (daily = quick, weekly = bigger, community = a server goal).
- [ ] With no `ANTHROPIC_API_KEY` (or on an API error) → a clear inline error in
      the results area, no crash, form still usable manually.
### Economy — Bank Manager Statistics page  (uncommitted)

New live, on-demand tuning surface under Bank Manager (`GET /api/economy/stats`,
manager-role-or-admin gated): supply concentration (median / top-10% / Gini over
positive balances), balance histogram, 7d flow + burn rate, per-member income
velocity table, engagement (earner ratio, quest approval rate, hoard-weeks), perk
affordability in days of median income, and top transfer pairs.

- [ ] Restart clean (no startup errors in the log).
- [ ] Bank Manager section shows a **Statistics** nav item for an admin AND for a
      plain `economy_manager_role` holder; a member without that role does **not**
      see it (and the endpoint 403s for them).
- [ ] Page loads with real data: supply row, histogram, and member table render;
      the member table sorts on header click; the refresh button re-fetches.
- [ ] Numbers sanity: the displayed total supply equals
      `SELECT SUM(balance) FROM econ_wallets WHERE guild_id=<guild> AND balance>0;`.
- [ ] Make a transfer between two members, then refresh → the pair appears in the
      top-transfers list.
- [ ] Affordability card appears once some members have 7d income.

### DM Perms — `/dm_revoke` confirmation now ephemeral  (uncommitted)

The final "Done — your connection with @user has been removed" reply was
posted publicly in the channel; it's now ephemeral. Revoke DMs to both
parties, the audit log, and the in-place edit of the original request DM are
unchanged.

- [ ] `/dm_revoke` an existing connection → only you see the confirmation;
      nothing appears in the channel.
- [ ] Both parties still receive the revoke DM.

### Pen Pals — 24h sessions, round-only matching, monthly cooldown  (uncommitted)

Pen Pals reworked: sessions now live **24 hours** (was 72); `/penpals join` and
the signup-panel button **only queue** — pairing happens solely in a round (the
weekly auto-round or `/penpals round`); a member is skipped by a round unless
they've had **no pen pal for a month** (30 days from their most recent pairing).
The first question still posts immediately when a channel opens.
`/penpals pair` (admin) still bypasses the pool and the cooldown. Offline
logic tests pass; the live flow needs a pass:

- [ ] `/penpals join` on an empty pool → "You're in the pool! You'll get a
      private channel the next time matches are drawn." — and **no** channel is
      created yet.
- [ ] A second member joins → still no channel; both appear as waiting in the
      panel / `/penpals status`.
- [ ] `/penpals round` (Manage Guild) → eligible waiting members get private
      channels, each opening with the pinned intro embed **and** the first
      question posted immediately; "Session ends" reads ~24 h out.
- [ ] A member paired **less** than a month ago stays in the pool when a round
      runs (not re-paired); the round summary counts them among "still waiting".
- [ ] A member last paired **more** than a month ago is paired again.
- [ ] 1-hour close warning fires near the end and the channel deletes at ~24 h.
- [ ] `/penpals pair <a> <b>` still force-pairs two members regardless of the
      cooldown.

### Games — cross-game global question pool  (uncommitted)

Every bank manager gained a per-question **Pool** button (copies the question
into a reserved `global` bank slot; duplicate texts skipped, Traditional's
category tags collapsed to `nsfw`/dropped) and a **Browse pool** panel that
imports selected pool questions into that game's bank (duplicates skipped;
Traditional makes you pick the category the imports are filed under). New
routes `POST /api/games/bank/{id}/pool` and `POST /api/games/bank/pool/import`;
the `global` type is a valid bank slot so full-bank export/import round-trips.
Offline route + logic tests pass; the dashboard flow needs a live pass:

- [ ] On a game's bank manager, tap **Pool** on a question → status confirms it
      was copied; tapping it again reports the duplicate (not re-added).
- [ ] **Browse pool** → the pool list loads; search filters it.
- [ ] Tick pool questions and **Import selected** into a non-Traditional game →
      they land in the bank with their pool tags; duplicates already present are
      skipped and reported.
- [ ] Same import into Traditional → you must choose an "Import as" category;
      imported questions carry exactly that one category tag.
- [ ] Send a Traditional NSFW question to the pool → its four-way category tag
      is gone but a generic `nsfw` tag remains.

### Economy (stage 0) — wallets, ledger, settings, `/bank` + config panel  (uncommitted)

Foundation slice of the economy feature (`docs/plans/economy-and-perk-shop.md`):
migration 062 adds `econ_wallets`/`econ_ledger`/`econ_notify_prefs`, an
`EconSettings` KV loader (per-guild, no guild-0 legacy fallback), atomic
`apply_credit`/`apply_debit` with the booster ×1.5 ceil, the `/bank`
command group, and an admin-only Economy config panel + API. Service, cog,
and route tests cover the offline logic; the Discord + dashboard surfaces
need a live pass:

- [ ] Bot restarts clean with the new `economy` cog loaded (no boot error,
      `/bank` appears in the command list).
- [ ] `/bank wallet` on a fresh member → shows an empty branded wallet
      (0 balance, currency name/emoji from settings, accent color) with no
      ledger rows.
- [ ] `/bank grant` run by an **admin** → credits the target, confirmation
      shows the new balance, and the amount appears in that member's
      `/bank wallet` ledger.
- [ ] `/bank grant` run by a **plain member** (no manager/admin) → refused,
      no wallet change.
- [ ] Dashboard: an admin sees **Economy** under Config; branding + scaling
      settings save, and persist across a page reload (re-open shows the
      saved values, not defaults).
- [ ] A non-admin session cannot reach the Economy API
      (`GET/PUT /api/economy/config` → 403), and the nav item is hidden.

### Economy (stage 1) — faucets: logins, conversion, reactions, QOTD, game payouts  (uncommitted)

Faucet slice of the economy feature (`docs/plans/economy-and-perk-shop.md`):
migration 063 adds `econ_logins`/`econ_streaks`/`econ_conversions`/`econ_qotd`
(+ `econ_qotd_rewards`) and the `xp_reaction_awards` dedup table; a new hourly
`economy_loop` (day/week-roll conversion, streak eval, QOTD window close); the
`reaction_given` XP source; `/qotd post` and `/bank mute`; login hooks on the
message and voice-XP paths; and duel/party game payouts. Offline logic is
covered by service/loop/logic/cog/route tests; the Discord + scheduler surfaces
need a live pass:

- [ ] **Setup first:** set the dev guild's `tz_offset_hours` config row so
      "local midnight" is correct — it currently inherits global −7 (Pacific).
      Every day-roll check below depends on this.
- [ ] Bot restarts clean: the new economy loop registers (no boot error), and
      `/qotd post` + `/bank mute` appear in the command list.
- [ ] First counted message of the local day pays the text login → `/bank
      wallet` ledger shows a "login" row (5 base).
- [ ] Sit in a 2-human, non-AFK VC ≥5 min with **no** message earlier that day
      → voice login pays 15 base (ledger "login"); streak increments the next
      day.
- [ ] React to someone else's message → the reactor gains XP (`xp_events`
      source `reaction_given`); reacting again / unreact+react on the same
      message pays **nothing** (once per message ever). No self/bot payout.
- [ ] `/qotd post <question>` renders a banner card; a non-manager is refused;
      members who reply in-channel that day each get 10 **once**.
- [ ] Finish a duel game (e.g. quickdraw) → winner +25 total (20 win + 5
      participation) and loser +5 in their `/bank wallet` ledgers.
- [ ] Conversion lands after guild-local midnight: a "conversion" ledger entry
      appears, coins = floor(day XP / rate), and the fractional remainder
      carries onto the conversion row.
- [ ] Dashboard XP panel shows the new **Reaction Given XP** coefficient
      (default 0.34), and editing + Save persists it (reload shows the saved
      value).

### Economy (stage 2) — quests, Bank Manager panel, party roster payouts  (uncommitted)

Quest slice of the economy feature (`docs/plans/economy-and-perk-shop.md`):
migration 064 adds `econ_quests`/`econ_quest_claims`/`econ_community_progress`/
`econ_community_payouts` (period-keyed claims, partial-unique race anchors); a
Bank Manager dashboard section (gated on `economy_manager_role_id` or admin)
with quest CRUD + active-slot rule + sign-off queue + community progress/settle
+ grant + ledger audit; `/bank quests` with instant + sign-off claim flow;
persistent `DynamicItem` Approve/Deny cards in the bank channel; economy-loop
daily rotation / weekly activation / plain-community auto-settle / >7-day claim
expiry; and 11 party cogs enriched to pay participation. Offline logic is
covered by service/loop/logic/view/route tests; the Discord + dashboard +
scheduler surfaces need a live pass:

- [ ] Bot restarts clean — the `DynamicItem` claim buttons register with no boot
      error and `/bank quests` appears in the command list.
- [ ] In Bank Manager, create a **daily** and a **weekly** quest; a non-manager
      session can't see the section, a manager-role holder **without** admin can.
- [ ] Activate a second daily → the ≤1-daily slot error surfaces in the panel
      (not a silent failure).
- [ ] `/bank quests` lists the active quests with claim buttons.
- [ ] Claim an **instant** quest → pays immediately and a `quest` row appears in
      the `/bank wallet` ledger.
- [ ] Claim a **sign-off** quest → a card posts in the bank channel. **Approve**
      from the card → pays, DMs the claimant, card turns green. Then verify a
      claim **Approved from the DASHBOARD** panel also edits the card + DMs
      (the shared-event-loop path has no test coverage).
- [ ] **Deny** a sign-off claim with a reason → the reason is DM'd and the member
      can re-claim.
- [ ] Same quest is not claimable twice the same local day, but is claimable
      again the next local day.
- [ ] Community quest: set progress to target in the panel; a **sign-off** one
      waits for the manual **Settle**, a **plain** one settles on the next weekly
      roll, paying all 30-day-active members.
- [ ] Play a quick party game (e.g. MFK) with 2+ players → **all** participants
      get +5 (not just the host).
- [ ] Dashboard **grant** is refused (409) while the economy is disabled.

### Economy (stage 3) — transfers, rental billing, role perks, gifts  (uncommitted)

Sinks slice of the economy feature (`docs/plans/economy-and-perk-shop.md`):
migration 065 adds `econ_rentals` (billing state machine: no-drift
anniversaries, single-charge catch-up after downtime, 36h grace, suspension
freezes the clock) and `econ_personal_roles`; `/bank pay|shop|gift`; the
`/bank role name|color|gradient|icon` subgroup with an idempotent personal-role
projector (position above the "#### Cosmetics" band on create, `ENHANCED_ROLE_COLORS`
/`ROLE_ICONS` gates, ΔE ≥ 25 staff-colour guard, Voice Master name blocklist);
`transfer_currency` (no booster on `transfer_in`); a rental-billing pass in the
economy loop (feature-gate sweep → billing → post-commit effects, transition-only
DMs); dashboard Rentals table + force-cancel; and `on_member_remove` rental cleanup.
Offline logic is covered by service/loop/logic/projector/route tests; the Discord +
dashboard + scheduler surfaces need a live pass:

> **Superseded in part:** `/bank role name|color|gradient` were later replaced
> by `/bank shop`'s customise modals (see the "customise via ephemeral buttons
> + modals" entry above) — test the starred items through the shop's buttons
> instead of the old subcommands.

- [ ] Bot restarts clean — `/bank pay`, `/bank shop`, `/bank gift`, and
      `/bank role icon` all appear with no boot error.
- [ ] `/bank pay` a small amount → lands in **both** ledgers (payer `transfer_out`,
      recipient `transfer_in`) and DMs the recipient.
- [ ] `/bank pay` **>100** → shows the confirmation step before debiting.
- [ ] Disable transfers in config → `/bank pay` is refused with a branded notice.
- [ ] `/bank shop` shows branded prices; icon/gradient rows reflect the server's
      role features (gated when the guild lacks them).
- [ ] Rent a **colour**, then set it via the shop's colour modal → the personal
      role appears **above** the booster swatch band and shows the colour.
- [ ] Try a **staff-adjacent** colour → the ΔE refusal **names** the staff role it
      clashes with.
- [ ] Rent a **gradient** → the gradient renders and **supersedes** the solid colour.
- [ ] `/bank gift @friend` a colour → the friend gets the DM + role and the payer
      sees the gift rental in `/bank wallet`.
- [ ] A **blocklisted** role name is refused by the shop's name modal.
- [ ] Let a rental hit its anniversary with an **empty wallet** → grace DM, then
      after 36h the role reverts + a lapsed DM. *(Force it fast by editing the row:
      `UPDATE econ_rentals SET next_bill_at = strftime('%s','now') - 60 WHERE id = <rid>;`
      to trigger grace on the next tick, then
      `UPDATE econ_rentals SET grace_since = strftime('%s','now') - 130000 WHERE id = <rid>;`
      to push past the 36h window.)*
- [ ] Dashboard **Rentals** table lists the rental with the correct state.
- [ ] Dashboard **force-cancel** of a **grace** rental removes the role within a
      minute (best-effort de-projection).
- [ ] A member **leaves** → their rentals cancel and the personal role is deleted.

### Auto-delete: media-only mode  — committed 1c56e7c (2026-07-10)

New per-channel "only delete messages with attachments" toggle on the
dashboard config page. Queue-time filtering (the sweep is queue-driven), plus
a matching guard in the startup history scan. Unit + route tests cover the
logic; the Discord-side delete behaviour needs a live pass:

- [ ] On a test channel, add an auto-delete rule with **media-only ON**, post
      a text message and an image, wait for the sweep → only the image is
      deleted, the text stays.
- [ ] Toggle the same rule's media-only **OFF** and Save → confirm the tracked
      queue was cleared (no surprise mass-delete of already-queued text) and
      that new text messages start aging out again.
- [ ] Toggle media-only **ON** on a rule that already had a text backlog →
      confirm the backlog stops being deleted (queue cleared); the next bot
      restart's startup scan should re-queue only the media.
- [ ] Edit only the age/interval of a media-only rule (no toggle) → confirm the
      existing queue survives (messages still age out on schedule).
- [ ] Sanity: a message whose only "media" is a link-preview embed (no real
      attachment) is **not** deleted under a media-only rule.

### `/setup` DM-delivered config wizard  — committed 042c95e (2026-07-10)

The `/setup` wizard now DMs the admin who runs it instead of showing an
in-channel ephemeral wizard. Rebuilt on hand-populated StringSelects (native
role/channel selects don't populate in a DM). Unit tests + fake-driven smoke
cover the logic and both code paths, but the Discord-side delivery needs a
live pass:

- [ ] Run `/setup` in the dev guild → confirm the bot DMs you and the channel
      reply says "Check your DMs".
- [ ] Walk all six steps; confirm each writes to the **correct guild's**
      config (check per-guild, now that we're multi-guild).
- [ ] Verify the `Configuring: <guild>` footer shows the right server.
- [ ] Test on a guild with **>25 roles** → the ◀ ▶ pagination appears and
      multi-select picks accumulate across pages.
- [ ] Test the **DMs-closed** fallback (disable DMs from server members) →
      `/setup` should fall back to the in-channel wizard, not silently fail.
- [ ] Confirm skipping a step (picking nothing) leaves existing config intact.
- [ ] Sanity-check the 3s ACK: in a cold DM channel the `defer` should keep
      the interaction alive even when opening the DM is slow.

### Ops hardening — watchdog DMs, deploy tag, lockfiles (uncommitted)

- [ ] Install + start the watchdog:
      `sudo cp deploy/dungeon-keeper-watchdog.service /etc/systemd/system/ &&
      sudo systemctl daemon-reload && sudo systemctl enable --now dungeon-keeper-watchdog`
- [ ] `python3 scripts/watchdog.py --test` → you get a 🧪 DM.
- [ ] Live drill: `sudo systemctl stop dungeon-keeper`, wait ~40 s → 🔴 DM;
      `start` it again → 🟢 recovery DM.
- [ ] After the next bot restart: `git describe --always deployed` names the
      running commit, and the boot log shows "Booted at …" (warns if dirty).
- [ ] First push after committing: CI is green on Python 3.14 with
      `requirements-dev.lock` (watch the Actions run — first lockfile install
      is the risky one).

### Truth or Dare — `single_choice` (one category per player)  (uncommitted)

`/games play traditional` gained a `single_choice` boolean. When on, the four
category buttons act like radio buttons: a player's second pick swaps out the
first (`toggle_pref(..., single_choice=True)`). Also exposed as a scheduler
option and stored in the game payload so it survives a bot restart. Logic +
embed unit tests cover it; the Discord-button behaviour needs a live pass:

- [ ] `/games play traditional single_choice:true` → lobby says "Pick the one
      category you're up for" and the footer reads "One category each".
- [ ] Pick SFW Truth, then tap SFW Dare → ephemeral says "Switched to SFW
      Dare", and the embed shows you under only one category.
- [ ] Tap your single selected category again → it deselects and you drop out
      of the participant list.
- [ ] Default `/games play traditional` (no option) still lets you opt into
      multiple categories, unchanged.
- [ ] Schedule a traditional game from the dashboard with the "One category
      per player" box checked → the launched game runs in single-choice mode.
- [ ] Restart the bot mid-game → recovered game still enforces single-choice
      (flag read from the payload, not the view).

### Economy (stage 4) — weekly metrics rollup + pricing hints  (uncommitted)

Migration 066 adds `econ_metrics_weekly` (one immutable row per guild + closed
ISO week) and `econ_rentals.ended_at`. At the guild-local week roll the economy
loop computes a rollup for the week that just closed (median/p90 income over
earners, minted vs burned, faucet mix, rental holders/churn, streak health);
the admin home gains an Economy tile and the config panel shows suggested-price
lines. All idempotent/pure-math paths are unit-covered; the live surface needs a
pass:

- [ ] Restart the bot → boots clean (migration 066 applies once; no error on the
      second boot).
- [ ] After the first guild-local **Monday** rollover, a row exists for the week
      that closed:
      `SELECT iso_week, median_income, minted, burned FROM econ_metrics_weekly
      WHERE guild_id = <guild> ORDER BY iso_week DESC LIMIT 3;`
- [ ] Admin home page shows the **Economy** tile populated (median coins, p90,
      minted/burned with the net-mint arrow, faucet bar, rental-holder %). Before
      the first rollover it shows the "rollup pending" empty state instead.
- [ ] Log in as a **non-admin** → the Economy tile does **not** appear (route is
      admin-gated; `GET /api/economy/metrics` 403s for them).
- [ ] Economy **config panel** shows "suggested ≈ N" lines under each price field
      once a rollup exists (nothing shown while metrics are empty).
- [ ] Sanity-check one number against reality — `minted` should equal the week's
      minted ledger sum:
      `SELECT COALESCE(SUM(amount),0) FROM econ_ledger WHERE guild_id = <guild>
      AND amount > 0 AND kind != 'transfer_in'
      AND created_at >= <week_start_epoch> AND created_at < <week_end_epoch>;`

---

## Done

_(none yet)_
