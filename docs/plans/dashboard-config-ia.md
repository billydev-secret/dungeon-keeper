# Dashboard configuration IA — audit and staged plan

**Status: COMPLETE on this branch — stages 1–4 (2026-08-29) plus the whole
defect queue (2026-08-30). Full gate green: 15,227 passed, 63 skipped.**
Billy's decisions: D1 keep Policy Tickets and Pen Pals merged; D2 all four
grouping bundles; D3 was stage-4-only until he asked for "all of it including
the defects" — so the remaining 85 findings were worked too.

**Defect queue outcome: 82 fixed, 3 deferred**, by thirteen agents each in an
isolated worktree, one per feature area, then merged here. The three deferrals
are product calls, not oversights:

* **#91 LegitLibs blank axes/prompts** — read-only from the dashboard, but no
  copy claims otherwise. Closing it means a new CRUD surface with cascading
  pos/domain/form and min_tier rules: a feature, and a decision about who may
  reword the in-game prompts.
* **#89 XP `role_grant_level`** — the milestone level is hard-coded at 5.
  Partly addressed (the report stopped hard-coding it and now reads the
  setting); making it a dial strands three member-facing surfaces that bake
  "Level 5" into their wording.
* **#53 `music_channel_settings`** — an unread table still holding one live
  prod row. The only honest fix is `DROP TABLE`, which needs a migration.

**Migrations deliberately not written** (thirteen parallel agents picking
migration numbers is a guaranteed collision, and every one of these touches
live prod data, so they need an explicit go-ahead): drop `dm_request_channels`,
`give_role_permissions`, `music_channel_settings`; drop the now-unread columns
behind findings 45–48 and `confession_config.max_attachments`; and delete the
config rows that are now formally dead (`ticket_panel_*`, the legacy grant
block, `ai_mod_model` / `ai_wellness_model` / `ai_model_%`, `econ_price_*_room`,
`econ_quest_board_monthly`, the legacy `greeting_watch_notify_user_id`). None
of it is load-bearing — the code no longer reads any of it.

**Four collisions the parallel branches could not see** were reconciled at
merge time and are worth knowing about: two agents each fixed the voice
transcription gate (kept the rule that cannot trap an admin with a wiped
cache), two disagreed on `risky_roller` (kept the rename to `risky_roll`, the
spelling the scheduler actually reads), two wrote the same policy-sweep fix
(composed: a whole-sweep entry point over a per-guild pass), and two chose
different auction-duration ceilings (kept 720h — the dial is itself a
guard-rail, and 168h remains its default).
Written 2026-08-29 on branch `dashboard-config-ia-review`. Produced by a
110-agent audit workflow plus a 14-agent gap-close pass; every defect claim was
independently re-derived by an adversarial verifier before landing here.

Billy's ask: (1) scan the web config surface for duplicated controls like the
recent economy channel fix, (2) check findability and brevity, (3) reabsorb the
controls that were moved onto report pages, (4) step back and make the
groupings logical. On (3) he was shown the merge rationale (evidence next to
the dial, moderators get a read-only view) and decided: **split them back
apart.** That decision is settled; this plan executes it.

Sibling work already shipped and excluded here: the economy channel-field audit
(migration 191, commit f5925642 on main) — its three dropped keys were checked
against every finding below; no collisions.

---

## Part 1 — the splits (Billy's decision, executed)

Seven pages carry the merged report+settings pattern, not five. The five in
scope split; two are recommended exemptions (open decision D1).

Every settings half had its own nav id before its merge, and every one of those
ids was **deleted without a MOVED_PAGES redirect** — those deep links have
rendered "Page Not Available" since their merges. The split therefore
**revives the original ids** rather than minting new ones: old bookmarks start
working again and each id's pre-merge telemetry series resumes. (The lens
agents independently proposed new ids before the archaeology surfaced the old
ones; the revival supersedes those suggestions.)

| Merged page (frozen id, keeps report half) | Report half returns to | Settings half revives id | Settings half files under |
|---|---|---|---|
| `voice-activity` → "Voice Activity" | Reports → Engagement (unchanged) | `config-voice-master` "Voice Control" | Config → Voice (beside Voice Transcription — cures the one-item heading) |
| `xp-leaderboard` → "XP Leaderboard" | Reports → Engagement (unchanged) | `config-xp` "XP & Leveling" | Config → Members |
| `intake-report` → "Intake" | Reports → Greeter (unchanged) | `config-intake` "Intake Procedure" | Config → Members, after Welcome & Leave (or the New Members heading if D2 adopts it) |
| `birthday-calendar` → "Birthday Calendar" | Reports → Member Lists (unchanged) | `config-birthday` "Birthdays" | Config → Members |
| `rules-watch` → "Rules Watch" (queue + ledger) | **Moderation** main items (its pre-merge home — it is a work queue, not a report; deliberate deviation from the literal "reports go to Reports" framing) | `config-rules-watch` "Rules Watch" | Config → Moderation & Safety, beside Greeting Watch |

Mechanics common to all five (full per-page step lists in the audit journal):

- Each half regains standalone `mount()` chrome (the wrapper module owns the
  panel header today); wrapper modules (`voice.js`, `xp.js`, `intake.js`,
  `birthday.js`, `rules-watch-page.js`) are deleted; every half must return
  `{unmount}` — the XP leaderboard's Chart.js teardown and the policy/rules
  45s poll handles are the known leak shapes.
- Settings entries are `adminOnly: true` (pre-merge state). Keep the
  `lockUnlessAdmin` calls as defense in depth. **Moderators lose the read-only
  settings view** — the accepted price of the split; server-side gates don't
  change (writes were always admin-only, moderator GETs stay).
- `related:` chips link each pair both ways (the `inactive-report ↔
  config-prune` pattern).
- `panel_registry.py`: the `voice-control` PanelSpec's `host_page` repoints
  from `voice-activity` to `config-voice-master` — the Discord-side Channel
  Panels link otherwise lands on a page without the poster. Sweep the registry
  for equivalent `host_page` pointers on the other four.
- Known test landmines: `tests/web/test_error_states_surface.py` hard-codes
  the merged-world intake copy (flip its assertions with the split);
  `test_frontend_wiring.py` fails on the deleted wrapper files if their
  hash-param forwarding tests aren't removed in the same commit;
  `test_merged_page_gating.py` keeps its `/api/config/xp` server-boundary
  assertions (they pin the split's permission model too — do not delete).
- The intake queue's "intake disabled" message points mods at settings "below"
  — rewrite it to name Config → Intake Procedure.
- Same-commit doc updates each page: `docs/dashboard_ia.md` merged-features
  table row, `manual.html` §Where a Setting Lives + Configuration Reference
  row + any deep links (`#/voice-activity` setup-checklist sentence etc.).

### The two exemptions (decision D1)

**Policy Tickets (`mod-policy-tickets`) — recommend KEEP MERGED.** Its settings
half is one integer (voting deadline). The old page (`config-policy-tickets`)
was an 88-line frame around a single input — the species this dashboard has
twice retired (`channel-panels`, `economy-qotd`). "Settings live with the data
they produce" survives as a documented rule with this page as its exemplar.
Ship three cheap fixes instead: add `"config-policy-tickets":
"mod-policy-tickets"` to MOVED_PAGES (the only retirement that never got a
redirect); add `/api/config/policy` rows to `test_merged_page_gating.py`; fix
`policy_vote_timeout_loop` to iterate `bot.guilds` (finding #q-policy in the
appendix — the dial is a silent no-op off the home guild).

**Pen Pals (`pen-pals`) — recommend EXEMPT.** Structurally merged but
categorically different: it has no in-page lock because commit ede311ee
(2026-07-28) deliberately **removed the permission seam server-side** —
moderators own the whole feature (channel, timers, question bank), with the
rationale in the commit, the route comments, and two regression tests built to
fail on reversion. Splitting it either reverts a seven-week-old tested
permission decision or produces two same-audience pages — nav noise.

---

## Part 2 — duplicated controls (the ask-1 sweep)

9 confirmed duplicated-control findings. The dominant pattern — **6 of 9 —
is the AI advisor's `settings_registry` disagreeing with the owning panel**:
different bounds, different units, missing side effects, or describing
features that don't exist. The registry is a second, unsynced schema for the
same keys. The three worst:

- `bios_archive_grace`: panel writes seconds (0–3600), registry writes "days"
  (0–3650) to the same key — an advisor Apply is an 86,400× error.
- `inactive_channel_id` / `inactive_role_id`: the panel routes through a setup
  flow that does role/permission plumbing; the registry Apply writes the bare
  key and leaves the old channel visible to inactive members forever.
- QA Tracker: all five `qa_*` keys writable from both surfaces, and the
  registry describes a **fictional** "Q&A rewards" feature with 10–100×
  wider bounds; gap detection actively nudges admins to configure it.

Proposed fix shape (stage 4): make the registry derive from or defer to the
owning panel's schema per key — bounds, choices, and a "route through
endpoint X" marker for keys with side-effect writes — rather than patching the
six divergences one by one and drifting again. `booster_swatch_dir` (global
key rendered as per-guild) and the Photo Challenge double write-path round out
the list; full claims in the appendix.

---

## Part 3 — findability (measured, not tasted)

Telemetry window 2026-07-28 → 08-29: 1,249 panel views over 178 routable ids.
68 healthy, 43 set-and-forget (zero/low views but demonstrably live features —
success, not death), 45 never-opened (43 are static manual sections), 9
possibly-dead, 0 unreachable (every section gate passes for some real viewer).

**Possibly-dead** (empty/stale feature tables behind them — candidates to
discuss, not delete unilaterally): `config-needle` (auto-thread watches 0
channels), `config-auto-react` (all four tables empty), `games-mlt` and
`games-rushmore` (0 bank rows), `games-legitlibs` (3 templates, placed in no
channel), `config-games-hotpotatogroup` (3 games ever, last 07-21),
`photo-challenge` (no card since 07-18), `wellness-history`, `system-stats`.

**Structural fixes (stage 3):**

- **Search can't see ids.** The nav search haystack is
  section+heading+label+keywords — route ids are excluded, so `shop-approvals`
  is unfindable by "shop" and every id remembered from a deep link or doc is
  unsearchable. Add the id to the haystack.
- **Keyword enrichment** for the nine worst gaps, led by `config-branding`
  (holds accent color / avatar / nickname / casino name; haystack is "Config
  Server Branding" — "color", "avatar", "nickname" all miss), `config-global`
  (timezone/bypass/allowlist unfindable), `config-casino` (roulette, keno,
  dice, mines, baccarat, war, race — all live tables, none searchable),
  `config-auto-role` ("autorole", "join role"), `economy-income-sources`
  ("daily", "streak" — the panel speaks economist), `games-legitlibs` ("mad
  libs"), `config-bios` ("profile", "introduction").
- **MOVED_PAGES entries** for the retired ids still taking hits:
  `health-composite-score` (13 views in-window), `config-booster-roles` (2;
  successor `economy-sinks`), plus `config-policy-tickets` per D1.
- `help-start` ("Getting Started", pinned first in Help) has zero views ever —
  worth knowing, no action proposed.

---

## Part 4 — groupings (decision D2, four bundles)

All three lens agents endorsed the recent IA1/IA2 regroups (Games subgroups,
Social extraction, Economy four-way split) — nothing there reopens. The
proposals that survived cross-lens agreement, bundled for sign-off:

**Bundle A — Reports partition.** Rename "General" → "Activity" and move
DAU/MAU into it (Heatmap, Activity, Channels, DAU/MAU); "Engagement" keeps
Gini, Activity Drops, Quality Score + the two returning report halves. Move
`nsfw-gender` to Reports → Moderation beside Sentiment & Tone (it's content
analytics, not engagement). "General" was the one Reports heading naming
nothing.

**Bundle B — Config "New Members" heading.** Gather `config-welcome`,
`config-auto-role`, `onboarding`, `config-greeting-watch`, and the revived
`config-intake` between Roles and Members. The set-up-the-newcomer-experience
job currently spans three headings. (If declined, `config-intake` files under
Members and everything stays put.)

**Bundle C — Moderation section shape.** Give the eight bare items a "Queues &
Workflows" heading (the IA doc already calls them Queues; Moderation is the
only section mixing bare items with groups). Pull `nsfw-blocks` + `nsfw-tags`
out of Audit Logs into a two-item "Image Guard" heading (they're classifier
diagnostics, not audit trails, buried 8th/9th of ten). Reorder `grant-audit`
first under Audit Logs (the only moderator-openable entry currently sits last
under nine locked rows). Make audit↔config `related:` links bidirectional
across all ~8 sibling pairs.

**Bundle D — section moves + labels.** `usage-telemetry` moves to Dev
(retiring Reports' one-item adminOnly "Bot Usage" heading); `config-event-echo`
moves to Games → Operations (the one game-night page outside Games — cost: it
vanishes for non-host moderators, acceptable for an adminOnly page). Label
fixes: "Auto React"→"Auto-React"; Home item "Dashboard"→"Home";
`config-prune`/`config-inactive` relabel to the parallel pair "Inactive Role
Removal"/"Inactive Kick Sweep"; `nsfw-blocks`/`nsfw-tags` →
"Image Guard Blocks"/"Image Guard Tags"; `config-moderation` → "Moderation &
Privacy" (it holds `message_storage_level`, the biggest privacy dial, invisible
from the nav). Old labels stay as search keywords.

Documentation owed regardless of bundles: a **label-vs-id drift table** in
`dashboard_ia.md` §Naming (ids are frozen; the drift should be a documented
fact, not a per-session rediscovery), plus catching the doc up (Survivor and
Mahjong missing from Live Games; `help-qa` missing from Dev).

---

## Stages

Each stage is one or more commits with tests and same-commit doc updates;
stages land in order but are independently shippable. Gate per CLAUDE.md.

1. **Stage 1 — the five splits.** One commit per page (small, revertable),
   voice first (it has the panel_registry repoint), then xp, intake, birthday,
   rules-watch. Plus the Policy Tickets cheap fixes and MOVED_PAGES additions
   (redirects + the two other retired ids) as a sixth commit.
2. **Stage 2 — groupings.** The approved D2 bundles + the drift table + doc
   catch-up. Pure app.js/docs/manual churn, one commit per bundle.
3. **Stage 3 — findability plumbing.** Id-in-haystack search fix + keyword
   enrichment + (if approved) the possibly-dead discussion outcomes.
4. **Stage 4 — duplicated-control fixes.** The 9 findings, led by the
   settings_registry-derives-from-panel-schema change. Includes its tests.
5. **Queue for later sessions:** the remaining 85 findings (appendix) —
   unenforced controls, dead keys, unwired readers, missing controls. These
   are feature-code fixes, not IA work; several are one-liners but each needs
   its failing-test-first treatment. High-severity ones are marked.

## Open decisions

- **D1** — Policy Tickets & Pen Pals: keep both merged (recommended), or split
  one/both for consistency.
- **D2** — which of grouping bundles A–D to adopt.
- **D3** — how much of the defect queue this branch takes on: stage 4 only
  (recommended), stage 4 + the 14 high-severity findings, or queue everything.

## QA checklist for the defect fixes

Gathered from the thirteen fix branches; `/dk-ship` folds these into the
branch's single QA card along with the stage 1-4 Testing sections.

- [ ] Open Config → AI Models and confirm there are no model dropdowns anywhere on the page — just where the model comes from, and one instructions box per job.
- [ ] Scroll that page to the "Rules Watch — automatic guard" card, press Try It Out, type a message and confirm an answer comes back below it.
- [ ] Edit any job's instructions, press Save Instructions, reload the page, and confirm your text is still there with an "Edited" tag on the card.
- [ ] Press Restore Original on that same card, confirm the warning, reload the page, and confirm the original wording is back and the tag reads "Original".
- [ ] Open Config → AI Assistant, untick "Let Billy-bot look up settings when an admin asks", press Save, reload, and confirm the box is still unticked.
- [ ] In a channel where Auto React tipping is switched on, remove that channel's rule on the Auto React panel, then tap one of the emoji the bot had already added to an older post — your coin balance does not change.
- [ ] Uncheck the Tips box for a channel's Auto React rule and save, then tap a placed emoji on one of that channel's older posts — no coins move, and re-checking Tips shows the per-emoji prices still filled in.
- [ ] Open Economy → Income Sources as an admin and check "Hosting a game, per attendee" and "Host bounty attendee cap" show your server's real numbers instead of 0
- [ ] Change one faucet rate on Income Sources, save, reload the page, and confirm the host bounty boxes still hold their old values
- [ ] On Economy → Pricing, set a Longest Auction of 24 hours and save, then start an auction and confirm it refuses a duration longer than 24 hours
- [ ] On Economy → Pricing, confirm a Custom Shop Items card shows an order window in days and that a change to it saves
- [ ] On the Wellness admin page, pick an Opt-In Role and a Wellness Channel, save, reload and confirm both are still selected
- [ ] With the Opt-In Role set, run /wellness setup as a member and confirm the enrollment wizard opens instead of turning you away
- [ ] On Economy → Statistics, confirm the affordability card no longer lists "Text room" or "Voice room"
- [ ] Open Economy → Pricing, set an opening bid and a minimum raise under Live Auctions, save, then reload the page — the numbers you typed are still there.
- [ ] On the same card, type 0 into the opening bid and press Save — it refuses and names the box instead of accepting it.
- [ ] Start an auction with /bank auction start and try to bid below the opening bid you set — the bot turns the bid down.
- [ ] On Economy → Pricing, set the Order Review Window under Custom Shop Items, save, and reload — the value sticks.
- [ ] On Economy → Shop & Perks, give a custom item an On Sale From date in the future and save — the item is gone from the shop until that time.
- [ ] Type delivery instructions into a custom item's Details box, save, then buy that item — the note appears on the job that lands on the mod todo board.
- [ ] Edit any custom item's name or price and press Save — it saves instead of showing an error.
- [ ] Change the Order box on two role icons so they swap places, save both, then reload the page — they are listed in the new order.
- [ ] Open Economy → Statistics — the affordability card no longer lists "Text room" or "Voice room".
- [ ] On the Pressure Cooker settings page, untick "Available on This Server", press Save in that Status box, then try to start a Pressure Cooker challenge in Discord — the bot refuses and says the game is switched off.
- [ ] Tick "Available on This Server" back on, Save, and start the same challenge again — it starts normally.
- [ ] Do the same untick-and-try on any one of Quickdraw, Hot Potato, Hot Potato (Group), Chicken or Musical Chairs — that game refuses too, and the others still start.
- [ ] Type a word into "Extra Banned Words" on the Pressure Cooker page, press Save, then start a challenge with that word in the stakes text — the stakes are refused.
- [ ] Reload the Pressure Cooker page — the word you saved is still in the "Extra Banned Words" box.
- [ ] Open any duel game's settings page — there is no longer a notice claiming the game cannot be played anywhere, and the Allowed Channels hint says leaving the list empty allows the game anywhere.
- [ ] On the Risky Rolls page, untick "Include in Scheduled Games", press Save in that box, then reload the page — it comes back unticked.
- [ ] On the LegitLibs Templates page, tick and untick "Available on This Server" and Save — it saves and survives a reload, and the template list below still loads and edits normally.
- [ ] Read the Audit Channel description on Games → Global Config — it says anonymous submissions are copied there with the author attached, and no longer claims every game that starts or finishes is recorded.
- [ ] On the Todo List page, read the Discord Board card's description: it names all four board buttons and says Sign-Offs and Approvals need admin or the economy manager role.
- [ ] Open the QA Tracker page, set "Passed Cards Linger (minutes)" to 0, save, and confirm a green passed card stays in the testing channel instead of vanishing.
- [ ] Turn the QA Tracker off, save, and confirm a card that has already passed stays in the testing channel.
- [ ] On the Voice Transcription page, pick a model whose Model Files row says "Not downloaded", tick the feature on and save — it refuses and tells you to download the model first.
- [ ] On the Anonymous Features Audit page, hover an Actor to read that member's id, paste it into the Actor ID box, and confirm the table narrows to that member's entries.
- [ ] On Reports → Member Lists → Birthdays, set Announcement Time to a different hour, save, reload the page, and see the new hour still selected.
- [ ] On Config → Members → Welcome & Leave, change the Arrival Line wording, save, and have a member join — greeter chat shows your new wording.
- [ ] Delete the @here from the Arrival Line, save, and have a member join — the line appears in greeter chat with no notification ping.
- [ ] Empty the Arrival Line box entirely, save, and have a member join — greeter chat gets no arrival line at all.
- [ ] On Config → DM Permissions, confirm there is no "Request Channel" picker and the page still saves.
- [ ] Set "Requests expire after" to 1 hour, save, reload the page, and see 1 still there.
- [ ] Send a DM request from a test account and check the DM it receives quotes the window you set instead of 24 hours.
- [ ] Set "Requests one member may have waiting" to 1, then try to send a second request from that same account and see it refused, naming that number.
- [ ] Turn off "Notify moderators when a ticket is opened" on Config → Moderation, open a ticket, and confirm no moderator is DM'd.
- [ ] On Config → Greeting Watch, remove every member from the notify list, save, reload, and confirm the list is still empty.
- [ ] On Config → Image Guard, clear the spoiler-required channel list, save, then post an unspoilered image in a channel that used to be on that list and see it stay up.
- [ ] Ask the bot's setup advisor what still needs setting up and confirm Rules Watch is described as screening messages for a review queue, not as pointing members at the server guide.
- [ ] On Games -> Operations -> Global Config, untick Story Builder under "Available on This Server", then run /games play story — the bot refuses and says the game is disabled; tick it back on and it starts.
- [ ] Untick LegitLibs in that same list and run /games play legitlibs — it refuses; tick it back on and it starts.
- [ ] Open Games -> Live Games -> AMA — the page shows only the availability switch and a line saying AMA questions come from the room, with no question bank.
- [ ] Open the LegitLibs template editor and add or remove blanks on a template — the Players line updates itself (for example "2-4 players") and there is nothing to type in.
- [ ] Start a LegitLibs round on a template with only a few blanks and have more people press Join than it fits — the extra joiner is told the round is full instead of being let in.
- [ ] On the Rushmore page, set Draft Mode to Blitz, press Save and reload the page — Blitz is still selected.
- [ ] Open the FFA / Truth or Dare question bank — the hint above it explains that tagging a prompt truth or dare is what makes it eligible for a truth or dare round.
- [ ] On the Role Grants page, set "Role Required First" on a grant to a role your test account does not have, put that grant's role on a Role Menu, and click the button as the test account — the reply names the role you need first and no role is given.
- [ ] Give the test account that required role and click the same menu button again — the role is handed out normally.
- [ ] Read the Log Channel hint on any grant on the Role Grants page — it now says only handing the role out is recorded there, not removals.
- [ ] With a Promotion Review Grant Role chosen on the XP & Leveling page, add or remove that role from a member who already has a Level 5 card — the card's "Spicy access" line updates to match.
- [ ] On the Bump Tracker panel, untick "Send Bump Reminders" and save, then let a listing bot bump the server in the reminder channel — the site's "Last Bumped" time on the panel updates and nothing new appears in the channel.
- [ ] With reminders still unticked, run /bump log for a site — you get the "Timer reset!" reply and the bump tracker status message in the channel does not change or reappear.
- [ ] Re-tick "Send Bump Reminders", save, and run /bump log — the live status message in the reminder channel updates again.
- [ ] On the Pen Pals settings page, choose Scheduled pairing, pick a day and an hour, save, then reload the page — your choices are still selected.
- [ ] With Pen Pals set to Scheduled, press Join Pool on the signup panel — the reply names the day and hour you picked, in server time.
- [ ] Set Question Swaps Allowed to something other than 3, then open a fresh pen pal chat — the pinned message's command list quotes that number.
- [ ] On the Whisper settings page set Guesses Per Whisper to 5 and send a whisper — the recipient's DM says 5 guesses, and the Guess button lets them try five times.
- [ ] Open the Survivor season settings — the Escalation & Endgame card no longer offers the wipeout, Accord or double-pick-minimum boxes, and saving the remaining settings still works.
- [ ] On the Voice page, tick "Room access" under Saveable Fields and press Save — it saves instead of showing an error.
- [ ] Untick every Saveable Fields box, save, then reload the page — every box is still empty.
- [ ] Untick "Room name", save, then have a member who had saved a room name join the hub — their new room gets the default name, not the saved one.
- [ ] Add a voice channel to "Channels That Earn No XP", sit in it with one other person for a few minutes, then check the XP leaderboard — no voice XP was added for that time.
- [ ] On Voice Transcription, choose a model that reads "Not downloaded" and press Save — it refuses and names the model; press Download beside it first and the same Save succeeds.
- [ ] Open Reports → Engagement → XP & Leveling → Time to Level 5 — the "XP required" figure matches your server's Level Curve Factor (320 at a factor of 20, not 249.6).

## Appendix: the verified defect queue (94 findings)

Every finding below was made by one audit agent and independently re-derived
by a second (adversarial) agent with file:line and, where prod state matters,
read-only SQL evidence. Full evidence trails live in the session workflow
journal; the claims here are self-contained enough to work from. Severity is
the audit's call: high = a live lie to an admin or a real prod misbehavior,
medium = divergence that will bite on the next touch, low = latent debris.

### Duplicated controls — the same setting writable from two surfaces (9)

1. **[high]** bios_archive_grace is writable from two surfaces that disagree
   on its unit and meaning: the Bios panel treats it as seconds the wizard
   room stays open (0-3600), while the advisor settings registry labels it
   'Days before an old bio is archived' (0-3650) and its Apply path writes the
   same key, so an advisor-applied 'days' value is enforced as seconds (an
   86400x error) and the registry describes an archival behavior that does not
   exist.

2. **[high]** inactive_channel_id (and inactive_role_id) are exposed as plain
   writable settings in the advisor registry, but the dashboard deliberately
   routes this change through POST /config/inactive/channel, a setup flow that
   creates the @Inactive role, grants it access to the new channel and revokes
   it from the previous one — an advisor Apply writes only the key, skipping
   the permission plumbing and leaving the old channel visible to inactive
   members forever (the exact failure the setup flow documents preventing).

3. **[high]** All five QA Tracker config keys (qa_enabled, qa_role_id,
   qa_channel_id, qa_reward, qa_daily_cap) are writable from two surfaces: the
   qa-tracker panel (PUT /api/qa/settings -> save_qa_settings) and the AI
   advisor's settings_registry Apply path
   (validate_config_change/apply_config_change -> set_config_value). The
   advisor surface is also semantically wrong: its 'qa_rewards' Feature
   describes a fictional 'Q&A rewards' feature ('Pays members coins for
   answering questions in a help channel', 'Coins per accepted answer', 'Role
   that can mark answers') that has never existed in the codebase, points
   admins at a nonexistent panel 'Config -> Q&A rewards' (the real panel is
   Dev -> QA Tracker), and validates ints against bounds 10x-100x wider than
   the panel's route (registry max 100000 for both qa_reward and qa_daily_cap
   vs the route's le=10000 / le=1000) — so an advisor-applied qa_daily_cap of
   e.g. 5000 would be accepted, paid out by record_verdict, and then be un-
   resaveable from the panel form (input max=1000). Gap detection additionally
   nudges admins of unconfigured guilds to set up the fictional feature, since
   qa_enabled and qa_channel_id are marked required.

4. **[medium]** booster_swatch_dir is a single global key (written pinned to
   guild_id=0, read only at guild 0 by color_palette), yet every guild's
   Global panel renders it as a per-server 'Server File Paths' field — any
   guild's admin sees the home deployment's host filesystem path and can
   overwrite the shared value for all guilds from their own panel.

5. **[medium]** welcome_trigger is a closed two-option select on the Welcome
   panel but freeform writable text in the advisor registry (which supports
   choices and does not use them), and enforcement matches only the exact
   strings 'join' and 'verified' — any other advisor-applied value silently
   disables welcome messages entirely.

6. **[medium]** intake_reference_channel_id has two writers with divergent
   side effects: the panel's PUT /config/intake/reference is the only caller
   of sync_channel, so an advisor Apply of the registry's 'Procedure reference
   channel' stores the key without syncing — the procedure blocks never post
   to the new channel until someone re-saves from the panel, despite the
   registry help text promising 'the bot keeps this channel in sync'.

7. **[medium]** inactive_sweep_cap has divergent bounds on its two write
   surfaces: PUT /config/inactive clamps to 1-200 while the advisor registry
   allows 1-10000, and the enforcing reader clamps only the lower bound, so
   the advisor path can set a sweep cap 50x above what the panel permits.

8. **[low]** bios_questions_per_bio bounds also diverge between write
   surfaces: the bios API and panel cap it at 10 while the advisor registry
   allows up to 50, and the loader applies no upper clamp, so an advisor write
   can exceed what the panel can ever display or re-save.
   *Verifier correction: One nuance: the panel CAN display an out-of-range
   value — the GET route (bios.py:180-182) returns the stored value unclamped
   and the input renders it — it just marks the form invalid and refuses to
   save until it is lowered to ≤10. "Cannot re-save" is exact; "cannot
   display" is a slight overstatement.*

9. **[low]** PUT /api/games/config/games/photo remains a second live write
   path to the same games_game_config row (guild_id, 'photo') — including the
   enabled flag and options JSON — that the standalone Photo Challenge panel
   writes via PUT /api/photo-challenge/config; 'photo' was left in
   ALL_GAME_TYPES when the feature went standalone, though no panel currently
   calls it (the panel mounts the photo bank with hasStatus:false).

### Unenforced controls — the panel promises behavior the code does not deliver (30)

10. **[high]** Every model selector on config-ai — Moderation Model, Wellness
   Model, and the per-command Model dropdown on all six prompt cards — is
   unenforced: ollama_client.chat() deliberately ignores its model argument on
   both backends, so the eight dropdowns write ai_mod_model /
   ai_wellness_model / ai_model_* keys that change nothing, while the panel
   copy claims 'Which model answers this command.'

11. **[high]** The DM Permissions panel's 'Request Channel' picker is stored
   but never read by any code path: DM requests are delivered by DMing the
   target directly, nothing is ever posted to the configured channel, and the
   panel hint falsely promises 'Where a pending DM request is posted so your
   moderators can approve or decline it'.
   *Verifier correction: Claim stands as written, with one extension: the hint
   is wrong not just about the channel being used but about the approval model
   itself — the design is target-consent via DM buttons (AskConsentView), so
   moderators were never the approvers; additionally the hint's "(none) →
   nobody can approve" warning is false, since DM delivery ignores the setting
   entirely.*

12. **[high]** The Moderation panel's 'Notify moderators when a ticket is
   opened' toggle is inert: the dashboard writes ticket_notify_on_create per-
   guild, but the enforcing read omits guild_id and so only ever sees the
   guild_id=0 row (which does not exist in prod), always falling back to the
   default '1' — turning the toggle off changes nothing.

13. **[high]** All six duel-game panels claim the Games Global Config allowed-
   channels list gates these games ("No channels are allowed to host party
   games yet, so this game cannot be played anywhere" / "every channel that
   may host party games"), but no duel/lobby code path ever consults
   games_allowed_channels — with the per-game allowlist empty (prod
   duel_config has zero rows) every duel game runs in every channel regardless
   of the global list.

14. **[high]** The games-ama panel ships a full Question Bank UI
   (add/bulk/pool/tags) for game_type 'ama', but no code path ever reads ama
   bank rows — AMA questions come from member submissions, so everything
   curated there is silently never served.

15. **[high]** The Config Advisor can set (and gap-detection advertises)
   guess_inactivity_ping_hours, which is stored in prod, but no code anywhere
   reads it — the Guess Who nudge feature it describes was never built, so the
   dial does nothing.

16. **[high]** The Survivor panel exposes three season dials —
   wipeout_annul_through_week, double_pick_min_alive, accord_max_alive — that
   no enforcing code reads (stages 6b/6c/6d are unbuilt), and two prod seasons
   already completed with the wipeout-annul dial silently inert despite its
   plan deadline of Week 2.
   *Verifier correction: Two cited-evidence details are wrong though the claim
   stands: (a) there is no last_reckoned_week column on survivor_seasons
   (schema is id/guild_id/name/season_year/status/config) — season 2's
   progression to week 4 is derived from MAX(week) in survivor_picks; (b) "two
   prod seasons already completed" overstates: season 1 had 1 player and zero
   picks (abandoned shell), only season 2 was a real 10-player, 4-week run.
   Also note the real NFL season starts Sept 10 2026, so these completed
   seasons are pre-launch practice runs — the "Wk 2" plan deadline has not yet
   passed in calendar terms, making the dial unenforced-but-not-yet-
   consequential rather than having already failed live.*

17. **[medium]** The Bump Tracker panel's 'Send Bump Reminders' checkbox
   promises 'Unchecked, bumps are still recorded but nobody is pinged', but
   the auto-detect listener returns early when enabled=0, so detector-recorded
   bumps silently stop being recorded the moment reminders are switched off
   (only manual /bump log keeps working).

18. **[medium]** The 'Restore Original' button on config-ai prompt cards
   cannot restore a prompt shadowed by a legacy guild-0 row: reset_prompt
   deletes only the active-guild row, get_prompt then falls back to the
   surviving guild_id=0 override, so for ai_prompt_query_channel (live in prod
   at guild 0) the button reverts to the old override, the 'Edited' badge
   returns, and the shipped default prompt is unreachable from any surface.
   *Verifier correction: Core defect confirmed, with one overstated clause.
   The 'Restore Original' button is indeed a no-op for
   ai_prompt_query_channel: reset_prompt deletes only the (active_guild, key)
   row, no such row exists (prod has only the guild_id=0 row),
   get_prompt_with_source then falls back to that surviving legacy row and
   reports is_override=True, so the old 85-char override ('These are bulk
   messages from a discord channel. Review them and answer the question .')
   comes back and the Edited badge returns on reload (the panel even hardcodes
   'Original' immediately after reset, so the UI briefly lies). However, 'the
   shipped default prompt is unreachable from any surface' is not strictly
   true: clearing the textarea and pressing Save stores an empty string at the
   active guild (set_prompt/set_config_value have no empty guard), and since
   the guild row now exists, get_config_value returns '' without falling back;
   the falsy raw makes get_prompt serve the shipped default with
   is_override=False. So the default is reachable via a non-obvious save-
   empty-text workaround, just not via the button built for it.*

19. **[medium]** The Greeting Watch notify list cannot be cleared: PUT
   /config/greeting-watch writes only greeting_watch_notify_user_ids and never
   clears the legacy greeting_watch_notify_user_id, and both the enforcement
   path and the panel read fall back to the legacy key whenever the CSV is
   empty — so removing the last subscriber silently resurrects them,
   contradicting the panel hint 'leave it empty and nothing is sent'.

20. **[medium]** 'Leave empty to switch this off' on Image Guard's Spoiler-
   Required Channels is impossible for the home guild: PUT /config/spoiler
   replaces only the per-guild config_ids bucket, and both the panel read and
   enforcement fall back to an unreachable legacy guild_id=0 bucket holding 4
   stale channels (2 of them not in the current list) the moment the per-guild
   set is emptied.

21. **[medium]** Removing an auto-react rule (or unchecking its Tips toggle)
   does not stop tip charging on emoji the bot already placed: DELETE
   /config/auto-react/{channel_id} deletes only the auto_react_config row,
   leaving reaction_tip_rungs orphaned, and apply_tip consults only the
   placement receipt plus the rungs table — never the rule's existence or
   tips_enabled — so old messages stay paid tip buttons, directly
   contradicting the panel's own confirm copy 'the emoji stop being tip
   buttons immediately'.

22. **[medium]** The XP settings pane's 'Channels That Earn No XP' hint
   promises 'Messages, reactions, and voice time in these channels earn
   nothing', but the voice XP tick never consults xp_excluded_channel_ids — a
   voice channel on the exclusion list still pays voice XP every interval.

23. **[medium]** The Level Curve Factor dial on the XP page is not consulted
   by the /reports/time-to-level-5 endpoint: it calls
   get_time_to_level_details(conn, guild_id, 5) and xp_required_for_level(5)
   with DEFAULT_XP_SETTINGS, so for prod guild 1469... (factor 20.0) the
   report computes crossings and 'XP required' at the default 15.6 curve
   (249.6 XP instead of the real 320).

24. **[medium]** Saveable Fields is only enforced on the save side, not the
   restore side: the room-creation path gates on disable_saves alone and re-
   applies the whole stored profile (saved name, limit, access state,
   trust/block lists), so unchecking a field stops new saves but a previously
   saved value keeps restoring forever — in prod, saved access states still
   restore on every room creation even though 'access' is absent from the
   guild's saveable list.
   *Verifier correction: The code mechanism is confirmed exactly as claimed,
   but the prod-impact sentence is overstated: no saved access state is
   actually restoring in prod today. The only voice_master_profiles row with
   any non-default access flag (user 1330327972627873792, updated 2026-05-23)
   has hidden=1 with locked=0/spectator=0/age_gated=0, and
   profile_access_state() deliberately collapses legacy hidden-only rows to
   ACCESS_OPEN (voice_master_service.py:904-920). So the defect is real but
   currently latent for 'access' in guild 1469491362444480666 — it would fire
   for any legacy locked/spectator/age_gated row, or for
   name/limit/trusted/blocked (15 saved name/limit profiles, 4 trusted rows, 1
   blocked row exist) the moment an admin unchecks one of those fields.*

25. **[medium]** Risky Rolls' 'Minimum Round Length: 0 lets the host close a
   round the moment it opens' is only half-enforced: saving 0 deletes the
   config key, and the auto-close-on-N-players path then falls back to
   DEFAULT_MIN_GAME_SECONDS=1800, so a guild showing 0 on the panel still
   waits 30 minutes before auto-closing a full round (manual close honors 0,
   auto-close does not).

26. **[medium]** The LegitLibs template form exposes a 'Maximum Players' field
   that is stored and displayed on template cards, but neither classic nor
   quiplash mode ever consults player_max — only player_min gates the start;
   latecomers are never capped.

27. **[medium]** The LegitLibs panel's editable Minimum/Maximum Players values
   are silently discarded on save: create_ll_template unconditionally derives
   player_min/player_max from the blanks JSON (ignoring the body's values),
   and update overwrites them whenever blanks accompany the save — which the
   form always sends when the blanks table has rows.

28. **[low]** Bump auto-detection only fires for detector-bot messages posted
   in the configured Reminder Channel, a constraint the panel's Detection hint
   never states — a listing bot whose confirmations land in any other channel
   is silently never detected, and clearing the Reminder Channel (allowed
   while reminders are off) also kills detection.
   *Verifier correction: Confirmed, but understated: detection is gated on
   `enabled` itself (cog line 564), not just on the channel. Turning "Send
   Bump Reminders" off kills auto-detection entirely even with a channel set —
   directly contradicting the panel's own hint (js:109-112) "Unchecked, bumps
   are still recorded but nobody is pinged", which is true only for manual
   /bump log. The claim's "clearing the channel while reminders are off also
   kills detection" scenario is real but subsumed: with reminders off,
   detection is already dead regardless of the channel.*

29. **[low]** With 'Send Bump Reminders' unchecked the bot still posts/edits
   the live status widget in the reminder channel whenever someone runs /bump
   log — the manual-log path checks only channel_id and never the enabled
   flag, the inverse of the panel's claim that the widget comes with reminders
   being on.
   *Verifier correction: Claim stands, with one precision: the widget refresh
   sends no ping (and prefers an in-place edit, force_resend=False), so the
   hint's "nobody is pinged" clause stays true; what's unenforced is
   specifically the live-status-widget half — with reminders unchecked, /bump
   log still edits the widget, and posts a fresh widget message if none exists
   or the old one was deleted.*

30. **[low]** The config-roles panel's Log Channel hint promises 'a private
   record of every time this role is given or taken away', but log_channel_id
   is only ever written to on the grant path — no code posts a removal/taken-
   away message to that channel (removals are recorded only in the role_events
   DB table), so half of what the control claims to configure does not exist.

31. **[low]** The Policy Tickets 'Voting Deadline' dial is written for
   whatever guild is active on the dashboard, but the enforcing loop only ever
   resolves expired votes for the home guild (ctx.guild_id), so on any other
   guild the dial saves successfully and never takes effect — expired
   proposals there are never auto-resolved at any deadline.
   *Verifier correction: One nuance: the defect is broader than the dial —
   auto-resolution as a whole is home-guild-only, so on any other guild
   expired 'voting' proposals hang forever at ANY dial value (default 72h
   included); the dial saving without effect is a symptom of that. Manual
   resolution (mod buttons/commands) still works on any guild.*

32. **[low]** Unchecking every Saveable Fields checkbox cannot take effect:
   the route stores an empty CSV, but the loader treats empty as 'use the full
   default set' (including 'access'), so the all-off state silently snaps back
   to all five fields enabled on the next read and the panel re-renders all
   boxes checked.

33. **[low]** The Voice Transcription model picker lets an admin select and
   save a model that is not downloaded — the PUT accepts it (only membership
   in VALID_MODELS is checked, not cache state), and the cog then fails every
   transcription with a swallowed log.warning, so the feature reads enabled
   while silently producing nothing; the 'has to read Downloaded before you
   can choose it' rule is hint-only.
   *Verifier correction: Minor scope refinement: an arbitrary/unknown model
   name cannot be saved (it is silently coerced to the default base.en); the
   unenforced case is exactly a VALID model (tiny.en or base.en) that is not
   yet downloaded, which the PUT accepts without a cache check. In current
   prod both valid models are cached, so nothing is broken live today — the
   finding is a real unenforced control, not an active outage.*

34. **[low]** The Games Global Config audit-channel hint promises 'Every game
   that starts, finishes, or is canceled is recorded here', but the only
   writers to the audit channel are anonymous-submission mirrors from seven
   question-bank game cogs — no game start/finish/cancel event is ever posted,
   and duel games and Risky Rolls never write to it at all.

35. **[low]** The Pen Pals room intro embed hard-codes the swap allowance as
   '(3 max)' regardless of the dashboard's max_question_swaps dial, so members
   are told the wrong limit whenever an admin changes it (the dial itself IS
   enforced).

36. **[low]** The Discord Board card's hint text promises 'The buttons are
   moderator-only', but the board's third button (Sign-Offs) is deliberately
   NOT behind the moderator gate — it is gated on admin-or-economy-manager-
   role instead, so a configured moderator without the manager role is refused
   by a button the dashboard told them was theirs, and an economy manager who
   is not a moderator can use it. manual.html states the split correctly ('Add
   and Complete are moderator-only… Sign-Offs asks for a little more'); the
   panel hint does not.
   *Verifier correction: The claim is accurate as stated. One nuance: the
   hint's preceding sentence names only the Add and Complete buttons (never
   mentioning Sign-Offs exists), so "The buttons are moderator-only" is over-
   broad rather than a description of Sign-Offs specifically — the fix is hint
   copy on the panel, mirroring manual.html:2309's correct carve-out; the gate
   split itself is intentional (todo_cog.py docstring) and should not be
   "fixed" by putting Sign-Offs behind _require_mod. Also worth adding to the
   record: is_mod additionally short-circuits on manage_guild, so even a
   manage_guild-holding mod (not just role-configured mods) is refused by
   Sign-Offs; and prod guilds 1502099268188639293 / 1507788887374692494
   currently exhibit the mismatch for real (mod roles configured, no manager
   role).*

37. **[low]** The same board card's second hint promises 'It shows today's
   chores first … then everything else outstanding', but the rendered board
   puts the quest sign-off section ABOVE the chores whenever a sign-off is
   pending (sign-offs lead by design), and the hint never mentions the sign-
   off section at all — so the described ordering is wrong exactly when the
   board has all three sections.

38. **[low]** The Enabled toggle's hint promises 'Off pauses verdict buttons;
   existing cards stay put', but the archive sweep never consults qa_enabled
   (or any per-guild settings — it is guild-agnostic): with the tracker
   disabled, a passed card that has been verified for 10+ minutes is still
   deleted from the Discord channel and force-archived. Adjacent gap in the
   same dial: the post-commit hook also never reads qa_enabled, so new cards
   keep posting while disabled (the hint does not promise otherwise, but 'off'
   does not freeze the feature — only the buttons are gated).

39. **[low]** The Model Files hint promises 'A model has to read Downloaded
   before you can choose it above', but nothing enforces that: the model
   <select> offers both models regardless of cache state, the PUT only checks
   membership in VALID_MODELS, and saving an un-downloaded model is accepted —
   after which every transcription attempt fails silently (WhisperModel is
   loaded with local_files_only=True, the exception is swallowed with a
   log.warning and no transcript or user-visible error is posted). Currently
   dormant in prod because both models are cached.

### Dead keys — stored config nothing reads (19)

40. **[medium]** Prod table give_role_permissions (2 rows granting two users
   the right to hand out role 1487814583300128941) has no reader or writer
   anywhere in src/ and is not created or dropped by any migration — the
   legacy predecessor of grant_role_permissions, its rows are a silent no-op
   invisible to the config-roles panel's 'Who Can Hand This Out' list and to
   the privacy/export surfaces despite holding user IDs.

41. **[medium]** ticket_panel_channel_id and ticket_panel_message_id config
   rows exist in prod (guild 0 and home guild) but nothing reads them — the
   live panel record moved to the ticket_panels table — yet settings_registry
   still declares ticket_panel_channel_id as the tickets feature's required,
   model-writable setting, so advisor gap detection consults a stale key and
   could propose a write that changes nothing (the exact failure its own
   DEAD_KEYS guard exists to prevent).

42. **[medium]** Five of AutoDeleteSettings' eleven fields (min_age_seconds,
   min_interval_seconds, max_messages, max_chars_per_msg, max_total_chars)
   have no reader anywhere in src/, and because the intended min_* floors are
   dead, PUT /config/auto-delete/{channel_id} has no server-side minimum at
   all — max_age_seconds=0 / interval_seconds=0 are accepted (the only >=1
   guard is client-side JS), which would make the rule due on every poll tick
   and delete every non-pinned message immediately.
   *Verifier correction: One shading, not a refutation: "delete every non-
   pinned message immediately" is precise only for messages in the tracked
   queue (auto_delete_messages) — the scheduled tick is queue-driven;
   untracked history is swept by the separate startup catch-up scan (which
   also skips pinned). Practical effect is as claimed. Also, that
   min_age_seconds/min_interval_seconds were "intended" as server-side floors
   is inference from their names/comments; what is established is that they
   are dead and no server-side floor exists. Negative values are accepted too,
   not just 0.*

43. **[medium]** econ_price_text_room and econ_price_voice_room exist in prod
   for both guilds (one hand-tuned to 230), stay in the PUT /economy/config
   whitelist, and are advertised to members via the stats affordability card
   and pricing hints — but no purchase/rental code path reads them (the
   stage-6 private-rooms feature was never built) and no panel renders an
   input for them.
   *Verifier correction: One wording caveat: the affordability card and
   pricing hints are on the admin-gated dashboard stats/metrics surfaces, so
   the prices are shown to admins as if tunable/live rather than 'advertised
   to members' in Discord; no member-facing surface names these prices. The
   substance of the defect stands unchanged.*

44. **[medium]** price_text_room and price_voice_room are set in prod for both
   guilds (one deliberately tuned to 230) and remain in the API's editable
   whitelist, but no purchase path for private text/voice rooms was ever built
   — their only readers are the affordability/pricing-hint display tables, so
   the prod values buy nothing and the Statistics affordability card
   advertises rooms nobody can rent.

45. **[medium]** duel_config.allow_early_revert exists in the prod schema and
   in the bot's config defaults dict but has no reader anywhere in src/ — an
   early-nickname-revert feature that was never built, and no surface can set
   it.

46. **[medium]** quickdraw_config.void_on_double_noshow exists in the prod
   schema and in the bot's defaults dict, but the double-no-show void is
   unconditional — no code ever reads the flag, and the dashboard Quickdraw
   panel does not expose it.

47. **[medium]** hp_group_config.shake_threshold and hp_group_config.pass_mode
   have no reader anywhere in src/ and no dashboard control; their defaults
   even disagree between schema and code (pass_mode DEFAULT 'choose' in SQL vs
   'clockwise' in db.py), which nothing can notice because nothing reads them.
   *Verifier correction: Minor refinement only: "no reader anywhere" is true
   functionally, but the config GET's SELECT * merge (config.py:773-780) would
   echo both columns to the panel JSON if a row existed (none do in prod, and
   the panel ignores them); and shake_threshold's 0.70 does exist in behavior
   — as a hardcoded default parameter of game.py shake_emoji, never sourced
   from config.*

48. **[medium]** lobby_timeout in hp_group_config, chicken_config and
   mc_config is loaded via get_lobby_params but every caller discards it, and
   stale-lobby cleanup instead hardcodes 90s-since-last-action in each
   fetch_sweepable_games — a stored, defaulted dial (60.0) that changes
   nothing and that no panel exposes.

49. **[medium]** Prod games_game_config holds clapback options {"min_players":
   2, "max_players": 16} that nothing reads — the clapback cog consults only
   rounds/timer/vote_timer/anonymous/tags and caps players with the hard-coded
   MAX_PLAYERS constant — and the PUT endpoint's merge-only update
   (existing_opts.update) means such stale keys can never be cleared from the
   dashboard.

50. **[medium]** pen_pals_config.auto_round_dow and auto_round_hour are stored
   columns with no reader anywhere in src/, and prod guild 1476525656115515484
   has a non-default auto_round_dow=4 stored — an admin's scheduling choice
   that silently does nothing.

51. **[low]** Legacy guild-0 grant config rows (denizen/nsfw/veteran _role_id,
   _grant_message, _announce_channel_id, denizen_log_channel_id, 10 rows)
   survive in prod with their only reader being the one-shot
   migrate_grant_roles, which early-returns for the home guild (grant_roles
   already populated) and is never invoked for any other guild — and
   settings_registry.DEAD_KEYS documents only the three *_role_id keys,
   leaving the message/channel variants undocumented as dead.
   *Verifier correction: Substance confirmed, one count correction: prod holds
   12 legacy guild-0 grant rows, not 10 — the claim's list omits
   nsfw_log_channel_id and veteran_log_channel_id (both present with value
   "0"). Otherwise exact: sole reader is the one-shot migrate_grant_roles,
   which early-returns for the home guild and is never invoked for any other
   guild, and DEAD_KEYS documents only the three *_role_id variants (plus
   veil_*), leaving the _grant_message/_announce_channel_id/_log_channel_id
   keys undocumented as dead.*

52. **[low]** nsfw_grant_message, nsfw_announce_channel_id, and
   nsfw_log_channel_id exist in the prod config table with zero readers
   anywhere in src/ (superseded by the grant_roles table), and unlike their
   sibling nsfw_role_id they are missing from settings_registry.DEAD_KEYS, so
   nothing stops a future pass from resurfacing them as real settings.
   *Verifier correction: The defect stands, but "zero readers anywhere in
   src/" is literally false: migrate_grant_roles
   (src/bot_modules/core/db_utils.py:287-291) reads exactly these three keys
   via dynamic f-strings — f"{grant_name}_log_channel_id" /
   f"{grant_name}_announce_channel_id" / f"{grant_name}_grant_message" with
   grant_name="nsfw" from _DEFAULT_GRANT_ROLES (db_utils.py:241) — and runs at
   every bot startup (src/dungeonkeeper/__main__.py:192). However, it early-
   returns whenever grant_roles already has rows for the guild
   (db_utils.py:282), which prod does (guild 1469491362444480666 has 6
   grant_roles rows including nsfw), so in practice the keys are never read
   again. Crucially, that same migration also reads nsfw_role_id
   (db_utils.py:286), which IS in DEAD_KEYS — so the registry's own standard
   already treats "read only by the completed one-time legacy migration" as
   dead, and by that standard the three companion keys belong in DEAD_KEYS
   identically. Also, denizen/veteran suffer the same gap
   (denizen_role_id/veteran_role_id are in DEAD_KEYS but their
   _grant_message/_announce_channel_id/_log_channel_id companions are not;
   prod holds e.g. denizen_grant_message under guild_id=0). Web dashboard
   grant-role routes (src/web_server/routes/config.py:2676-2759) read/write
   only the grant_roles table, never these config keys.*

53. **[low]** The music_channel_settings table still holds a live per-guild
   config row in prod (guild 1476525656115515484: voice channel
   1525522177951137954, always_on=1, an autoplay Spotify playlist URL) but
   nothing in src/ reads the table any more — only migration 006 creates it,
   and music/logic.py records that the always_on feature it served was removed
   2026-07-28 — so the stored autoplay/always-on preference is a silent no-op
   with no dashboard surface and no reader.

54. **[low]** AutoDeleteKeywords / AUTO_DELETE_KEYWORDS and its sole consumer
   parse_duration_seconds() are a dead command-era parsing chain:
   parse_duration_seconds has no caller in src/ (only tests), so the
   run_keywords/named_intervals/duration_pattern configuration dataclass
   configures nothing on any live path — leftover from the deleted command-
   managed auto-delete surface that CLAUDE.md says should be removed with the
   commands.

55. **[low]** The saveable-field values 'locked', 'hidden', and 'spectator'
   accepted by POST /voice-master/config are dead vocabulary: no panel ever
   sends them and no enforcement site ever tests them —
   should_save_profile_field is only ever called with saveable_key in {name,
   limit, access, trusted, blocked}, so a hand-crafted request storing them
   changes nothing.
   *Verifier correction: Core claim holds, with two refinements: (1) a hand-
   crafted request storing ONLY locked/hidden/spectator does change something
   — POST replaces the whole CSV, wiping the live fields and disabling all
   real profile saving; the three tokens are inert only as additions. (2) The
   sharper defect is the inverse: valid_fields omits "access", the token the
   panel offers (voice-settings.js:10) and enforcement actually tests
   (commands:509; default at voice_master_service.py:86) — checking "Room
   access" makes the panel's own save fail 400 "Unknown fields: {'access'}",
   and prod's stored CSV lacking access is consistent with that.*

56. **[low]** VoiceMasterConfig.panel_channel_id and panel_message_id are
   loaded on every config read but never consumed through the dataclass — the
   cog manages those two keys via raw get_config_value, and the
   panel_message_id that GET /voice-master/config returns is never used by
   voice-settings.js — dead fields in the config surface.

57. **[low]** quest_board_monthly is stored in prod and accepted by the config
   PUT, but the board draw explicitly no longer reads it and no panel exposes
   it since monthly became a guild-wide goal; its one surviving reader is a
   leaderboard summary line that renders the monthly goal as a personal-board
   draw ('N yours') it no longer is.
   *Verifier correction: The dead-key core is confirmed, but the claim's
   "stale 'N yours' line renders in prod" detail is wrong — the key is even
   deader than claimed. leaderboard.py:653-656 does read
   settings.quest_board_monthly into sizes, but that read is inert: at
   leaderboard.py:355 every active quest with qtype 'monthly' is diverted into
   the community-goals list ("if row[\"qtype\"] in (\"community\",
   \"monthly\")") and never appended to data.quests, so at :662 the monthly
   pool comprehension is always empty and the loop hits "if not pool:
   continue" — the Monthly "N yours · pool N" row can never render, in prod or
   anywhere. Corrected statement: econ_quest_board_monthly is stored in prod
   (guild 1469491362444480666, value 1), accepted and persisted by the config
   PUT (routes/economy.py:146 whitelists it; :280-287 saves via
   save_econ_settings), exposed by no panel (economy-quests.js BOARD_FIELDS
   lists only quest_board_daily/quest_board_weekly at :28-29), excluded from
   the board draw (economy_quests_service.board_sizes:607-616 returns
   daily/weekly only, and :1325/:1399/:2011 exclude monthly from personal-
   board paths), and its sole attribute read (leaderboard.py:655) is dead in
   effect — the value can influence no output at all.*

58. **[low]** confession_config.max_attachments (default 4) is stored, loaded
   into GuildConfig, and round-tripped on every upsert, but confessions have
   no attachment handling anywhere — no code consults it, and no panel or PUT
   exposes it.

### Unwired readers — code reads a setting no surface can set (19)

59. **[high]** The Voice Control 'Room access' saveable field can never be
   enabled from the dashboard: POST /voice-master/config validates
   saveable_fields against
   {name,limit,locked,hidden,spectator,trusted,blocked} which omits 'access',
   yet the panel offers an 'access' checkbox (default-checked for a fresh
   guild via _DEFAULT_SAVEABLE), so any save with Room access checked 400s the
   whole settings form, and the enforcing reader (should_save_profile_field
   with saveable_key='access') can never see 'access' in a dashboard-written
   list — prod has already lost it.
   *Verifier correction: Minor nuance only: once a guild has lost "access"
   (the one prod row), the checkbox renders unchecked, so its saves succeed
   while silently keeping access persistence off — the whole-form 400 hits
   fresh/default guilds and anyone re-checking the box; that is how prod lost
   the field and why the dashboard cannot restore it.*

60. **[high]** The Income Sources panel edits host_bounty_per_joiner and
   host_bounty_cap but GET /economy/income-sources omits both from its faucets
   dict, so the inputs render '?? 0' and every faucet Save silently overwrites
   the guild's configured host bounty with 0 (prod holds 100/8 and 30/8).

61. **[high]** wellness_config.role_id and channel_id gate the entire wellness
   feature (member opt-in refuses without role_id; the active-list and
   milestone posts refuse without channel_id) but nothing in src/ ever writes
   them — the Wellness admin panel saves only default_enforcement — and the
   bot's own error text tells admins to 'configure it from the web dashboard',
   a control that does not exist; guild 1476…484 sits at 0/0 so its members
   can never opt in.

62. **[high]** The scheduler's enable gate reads games_game_config for
   game_type 'risky_roll' (and 'legitlibs'), but PUT
   /api/games/config/games/{type} validates against ALL_GAME_TYPES which
   spells it 'risky_roller' and omits 'legitlibs' — so no dashboard call can
   ever disable scheduled Risky Rolls or LegitLibs, while the settable
   'risky_roller' entry is a key nothing anywhere reads.

63. **[medium]** config-ai is primaryOnly ('bot-global') but its routes write
   guild-scoped rows at the primary guild's id, so a non-primary guild's
   enforced readers (prompt text for /ai commands and wellness, which pass
   guild.id) resolve to legacy guild-0 rows or code defaults that no panel can
   view or edit — the live second guild (1476525656115515484) silently
   inherits the stale guild-0 query-channel prompt override and the abandoned
   cloud-switch model ids, and diverges from what the primary panel shows the
   moment the primary saves its own row.

64. **[medium]** advisor_config_tools is read on both ask surfaces to decide
   whether admin asks get the config-tool loop or the inline settings dump,
   but the config-advisor panel exposes no control for it — GET/PUT
   /config/advisor omit it entirely, so the only ways to flip it are a raw DB
   write or asking the advisor itself to propose the change via its registry
   entry.

65. **[medium]** The XP/promotion-review feature's 'spicy access' tracking
   reads the grant stored under the literal internal key "nsfw"
   (grant_roles.get("nsfw")), which no dashboard control designates: the
   config-roles panel lets an admin delete or never-create that exact key (a
   guild naming its grant "adult" or "spicy" gets a permanent silent no-op),
   with no warning that level-5 card fields and NSFW-role-change card
   refreshes die with it.

66. **[medium]** The settings_registry 'rules-watch' feature entry is wired to
   the wrong keys: it requires server_guide_channel_id (a Welcome-panel key)
   and omits rules_watch_channel_id entirely, so advisor gap detection can
   report Rules Watch 'configured' while its actual alert channel is unset,
   and can never suggest the real control; its blurb ('Points members at the
   server guide when they ask a rules question') describes behavior the
   monitor does not have.

67. **[medium]** All four live-auction dials (auction_min_bid,
   auction_min_increment, auction_soft_close_seconds,
   auction_max_duration_hours) are read and enforced by the auction service
   but are absent from EconomyConfigUpdate and from every dashboard panel, so
   the shipped /bank auction feature's guard-rails can only be tuned by a
   direct DB write.

68. **[medium]** shop_item_expire_days (how long a custom-item order waits on
   staff before auto-refunding, default 14) is enforced by the hourly sweep
   but is missing from EconomyConfigUpdate and from the Pricing panel, while
   every sibling review-window dial (qotd_sponsor_expire_days,
   emoji_sponsor_expire_days, pin_expire_days, theme_expire_days) is editable
   there.

69. **[medium]** Custom shop items' availability window
   (available_from/available_until), description, and sort_order are accepted
   by the API and enforced/used by the bot (the shop hides items outside the
   window and the todo card shows the description), but the Shop & Perks item
   editor renders no input for any of them — create hardcodes them empty and
   the row editor omits them — so limited-time/scheduled items are a designed
   feature with no dashboard control.

70. **[medium]** duel_config.nick_denylist is enforced on every duel/lobby
   nickname and stakes validation (extra per-guild patterns on top of the
   built-in denylist), but no dashboard panel, endpoint, or command can set it
   — the only way to add guild-specific banned nickname patterns is direct DB
   edits.

71. **[medium]** The scheduler's launch gate reads the games_game_config
   enabled toggle for every schedulable game type, but six of them — mfk,
   compliment, ttl, hottakes, story, fantasies — have no dashboard panel
   exposing the toggle (the API accepts them via ALL_GAME_TYPES, yet only the
   nine mountGamePanel panels plus pen-pals ever call PUT
   /api/games/config/games/{type}), so those games can never be disabled from
   the dashboard even though the code would honor it.

72. **[medium]** Risky Rolls' scheduler enable check reads game_type
   'risky_roll', but the config API's ALL_GAME_TYPES spells it 'risky_roller',
   so PUT /api/games/config/games/risky_roll 404s and a 'risky_roller' row
   would never be consulted — the toggle is unreachable in both spellings.

73. **[low]** ai_rules_watch_check resolves its model via registry key
   'ai_prompt_rules_watch', which does not exist in ai_config._PROMPTS, so the
   lookup unconditionally falls through to the mod model — no panel, and not
   even a hand-written DB row, can ever give the rules-watch guard its own
   model (doubly dead today since chat() ignores model anyway).

74. **[low]** quest_board_monthly can no longer be set anywhere (the Quests
   panel's board form dropped it and no other JS mentions it) and the board-
   draw service declares it inert, yet the public leaderboard still reads it
   to size a 'Monthly' personal-board summary, the PUT whitelist still accepts
   it, and prod still carries econ_quest_board_monthly=1.
   *Verifier correction: One cited detail overstates the reader:
   leaderboard.py:655 does read settings.quest_board_monthly into its sizes
   dict, but that read can never affect rendered output. The same builder
   routes every qtype=='monthly' quest into the community-goals branch
   (leaderboard.py:355 `if row["qtype"] in ("community", "monthly")`), so
   data.quests never contains a monthly QuestLine; the summary loop's `pool =
   [q for q in data.quests if q.qtype == qtype]` is always empty for 'monthly'
   and the row is skipped via `if not pool: continue`. So the setting is not a
   live-but-orphaned dial sizing a visible 'Monthly' row — it is fully dead: a
   dead read in leaderboard.py, a zombie whitelist entry (economy.py:146 →
   save_econ_settings; the panel never sends it, and no bot-side writer
   touches it — economy_cog.py's save calls write only panel ids/cursor), and
   a stale prod key. The IA defect stands (unsettable key still accepted by
   the PUT, still a dataclass field at economy_service.py:197, still in prod),
   with lower user-facing severity than the claim implies.*

75. **[low]** The TTL cog reads a per-server vote_timer default from
   games_game_config ('ttl'), but no dashboard panel exists for TTL, so that
   server-level default can never be set (only per-schedule overrides via the
   scheduling panel reach it).

76. **[low]** The Rushmore cog reads a per-server 'mode' default (snake/blitz)
   from games_game_config that the games-rushmore panel's optSchema does not
   expose — the server-level fallback can only be reached by hand-editing the
   DB or raw API options.

77. **[low]** GET /api/moderation/anon-audit accepts and applies an actor_id
   filter that no dashboard surface (or any other caller) ever sets — the
   filter plumbing is reachable only by a hand-typed URL.

### Missing controls — admin-tunable behavior that is hard-coded (17)

78. **[medium]** The Role Menus panel will publish a self-service button for a
   role that is also a configured grant role, silently bypassing that grant's
   'Role Required First' prerequisite (enforced even against moderators in
   /grant) and its per-grant keeper allow-list — server-side validation checks
   only managed/hierarchy/dangerous-permission-bits, so an access-gating role
   like NSFW (no dangerous perms) passes with no warning or refusal.

79. **[medium]** The four live-auction dials (auction_min_bid,
   auction_min_increment, auction_soft_close_seconds,
   auction_max_duration_hours) are enforced on every auction but are absent
   from the EconomyConfigUpdate whitelist and from every panel, so they are
   tunable only by hand-editing the config table.

80. **[medium]** shop_item_expire_days (the refund window for unfulfilled
   custom-shop orders, where 0 disables the sweep) is enforced by the order-
   expiry sweep but is not in the EconomyConfigUpdate whitelist and appears on
   no panel, while its sibling expiry dials (emoji/qotd/pin/theme review
   windows) are all editable on Pricing.

81. **[medium]** There is no way to disable a duel game from the dashboard:
   question-bank games get an 'Available on This Server' toggle, but the six
   duel games have no enabled flag anywhere (their types are not in
   games_game_config's vocabulary), and the advertised off-switch — emptying
   the global games channel list — does not actually apply to them (see the
   games_allowed_channels finding).

82. **[medium]** LegitLibs is the only party game in the cluster with no
   'Available on This Server' toggle at all: 'legitlibs' is absent from
   ALL_GAME_TYPES so the config PUT rejects it, the cog never calls
   check_game_enabled, and the scheduler's check_game_enabled('legitlibs') can
   never find a row — a guild cannot disable LegitLibs.

83. **[medium]** Pen Pals scheduled-mode rounds fire at a hard-coded 8am
   America/New_York (_SCHEDULED_MATCH_HOUR/_SCHEDULED_MATCH_TZ), ignoring the
   guild's tz_offset_hours, even though match_mode='scheduled' is a dashboard
   dial and dead DB columns (auto_round_dow/auto_round_hour) exist for exactly
   this knob.

84. **[low]** The automated Rules Watch guard's system prompt
   (_RULES_WATCH_SYSTEM) is hard-coded and absent from the config-ai prompt
   registry, making the only per-message automated AI moderation surface the
   one whose instructions admins cannot tune, while all five /ai command
   prompts and the wellness prompt are editable on the panel.

85. **[low]** The birthday announcement hour is hard-coded to 09:00 guild-
   local while every other birthday behavior (two channels, messages, pinning,
   timezone) is a per-guild dial, so an admin cannot move announcements
   without a code change.

86. **[low]** The greeter arrival ping is hard-coded member-visible copy with
   a forced @here ('@here - {mention} has arrived') on both the join and post-
   verification paths, while every sibling greeting surface (welcome message,
   leave message, birthday message, intake card) has editable copy and ping
   controls on the dashboard.
   *Verifier correction: Minor refinement only: the intake card sibling has a
   configurable ping target (greeter role via greeter_role_id) and editable
   checklist steps, but its arrival copy ("{mention} has arrived",
   intake_views.py:361-362) is hard-coded too — "editable copy" fully applies
   to welcome/leave/birthday, only partially to the intake card.*

87. **[low]** The advisor registry's feature labeled 'Inactivity prune'
   actually carries the inactive-sweep keys, while the real prune feature
   (config-prune, 'Auto-Remove Role (Inactive)', backed by
   inactivity_prune_rules) has no registry entry at all — conflating the two
   overlapping inactivity features on the advisor surface and leaving the
   prune rule invisible to gap detection; similarly the bios registry entry
   omits bios_wizard_category_id even though BiosConfig.configured requires
   it, so an unconfigured bios reads as configured.

88. **[low]** DM request lifecycle limits are hard-coded module constants —
   24-hour request expiry and a cap of 5 concurrent pending requests per
   member — with the '24 hours' string baked into member-facing embeds, while
   every comparable lifecycle dial in this area (policy deadline, greeting
   window, warning threshold) is dashboard-tunable.
   *Verifier correction: Core claim stands as cited. One comparator
   overstated: I found no dashboard-tunable "policy deadline" dial anywhere
   (no *deadline* config key in code or prod DB; event_echo deadlines are per-
   event data, not a setting). The greeting window and warning threshold
   comparators are real; the claim's "every comparable lifecycle dial" list
   should drop or rename "policy deadline".*

89. **[low]** The level threshold for the automatic role grant is hard-coded
   at 5: XpSettings.role_grant_level exists but is excluded from both the
   config loader's coefficient lists and the dashboard's coefficient table,
   and the reports route hard-codes literal 5 — the 'Level 5 Role' / 'Level 5
   Log Channel' pickers bake the number into their labels with no way to tune
   it.

90. **[low]** Icon-catalog display order is real (list and shop picker are
   ORDER BY sort_order, and Discord's 25-option cap trims a large catalog 'by
   sort order'), and the PATCH endpoint accepts sort_order, but the Shop &
   Perks icon rows offer no reorder control, so every icon stays at sort_order
   0 and a >24-icon catalog trims by insertion id with no admin recourse.

91. **[low]** LegitLibs blank axes and blank prompts (legitlibs_blank_axes /
   legitlibs_blank_prompts) drive the template editor's dropdowns and every
   in-game blank prompt, but are read-only from the dashboard — no
   create/update/delete endpoint exists, so this content is tunable only by
   migration or direct DB edit.

92. **[low]** FFA bank draws treat 'truth' and 'dare' as reserved required
   tags (a truth-kind round only serves rows tagged 'truth'), but the games-
   ffa panel is plain free-tag mode with the generic hint — nothing tells the
   curator the contract, unlike Traditional's enforced category dropdown, so
   untagged questions silently never serve in truth/dare rounds.

93. **[low]** Whisper's guesses-per-whisper cap is fixed at 3 by a schema
   default with no config key or panel control, inconsistent with the sibling
   Guess Who game whose max_guesses_per_round is dashboard-tunable on config-
   guess.

94. **[low]** How long a passed card lingers before the sweep deletes it from
   the channel is hard-coded at 10 minutes (ARCHIVE_SWEEP_DELAY), while every
   other policy dial of the feature (reward, cap, role, channel, enabled) is
   dashboard-tunable — an admin who wants passed cards visible for a day for
   crew morale/visibility has no knob. Plausibly a deliberate declutter
   default for a single-guild dev feature, so low.
