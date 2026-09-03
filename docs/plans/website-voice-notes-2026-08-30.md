# Website walkthrough — voice notes, 2026-08-30

Billy walked the whole dashboard dictating observations. This is the triage of
that pass: **114 raw notes → 6 cross-cutting themes, 5 confirmed defects, 13
feature asks, 11 IA/copy changes, 3 research questions.**

**Everything here is against current main.** The service restarted at 22:03 PDT
(main HEAD `f567830f`, 17:14), so nothing observed was a stale build — Mod
Coverage, Ping Response and NSFW by Gender were all live. No note falls into the
"already built, awaiting restart" bucket.

Status legend: ☐ not started · ◐ in progress · ☑ shipped.

---

## Where this stands (updated 2026-09-02)

**Wave 1 — shipped, live** (merged `72af8da9`, visible since the 15:50 restart on
09-01). All six cross-cutting themes (Part 1 A–F), all five defects (Part 2), and
Part 4 items 1, 2 and 4. The tabs helper landed as `static/js/tabs.js`, extracted
from the Bios pattern.

**Wave 2 — shipped** (merged `dbe2ebd9`, awaits a restart). Part 3 #1 (Rules Watch
bulk labelling + keyboard triage) and Part 4 #5, #10, #11. Plus a **cross-guild
security fix** found while building it: `get_event()` selected on id alone, so any
moderator could read *and* label another guild's event by id. `guild_id` is now a
required argument.

**Wave 3 — shipped** (merged `dfe908d2`, awaits a restart). Part 3 #4, #5, #7,
#8, #9, #11, #12 and Part 4 #7, plus Part 5 #3. A follow-up commit
(`fc367600`) rewrote `docs/birthday_spec.md`, which is classified *Reference*
and still described the retired two-channel model after migration 200.

**Wave 4 — built** (this branch, awaits merge). The three items that had a
verdict and needed no decision from Billy:

* **Part 5 #1 — Sentiment & Tone → Flagged Messages** (`50ec4d6c`). Done as the
  investigation recommended: composite score, badge, pos:neg ratio, spike count,
  emotion breakdown, trend line and per-channel chart all removed; the negative
  half of the feed promoted to be the whole panel, grouped by channel with jump
  links. `/api/health/sentiment-feed` gained a `polarity` parameter so a
  cheerful stretch of chat can't push the negatives past the shared limit.
* **Part 4 #6 — Auto-Thread relabel** (`a9196e91`). The labelling fix the
  not-a-defect finding pointed at. Also caught a real mismatch on the way:
  `archive_immediately` was labelled "Mark Answered as Soon as Someone Else
  Replies" but only ever calls `remove_reaction` — it has never archived
  anything. Now "Clear the Open Marker on the First Reply", with a hint saying
  so outright.
* **Off-primary control convention** (`412b416d`). Resolved as *no code change*:
  Rules Watch explains the absence because two other surfaces send admins there;
  config-global omits its card silently because nothing does. Written into
  `dashboard_ia.md` so the mismatch isn't flattened later.

**Left for Billy, not built:** the narrow composite `health-sentiment` home
widget still renders the retired average/ratio/spikes. Removing it would drop
it from saved home layouts, which is an owner's call rather than a cleanup —
say the word and it goes. The wide `health-sentiment-feed` widget was
relabelled to **Flagged Messages** to match the panel.

**Superseded, not done by us:** Part 5 #2 (Quality Score) — another session did
exactly this research and retired the panel for **Contributors** (`quality-score`
id kept, frozen). Nothing left to do.

**Closed as not-a-defect:** Part 4 #6 (Auto-Thread's waiting / answered /
archived-or-locked states). Billy read them as leftover question-thread scaffolding,
but `needle_cog.py` uses every one to add and remove real Discord reactions marking
whether a thread is open, replied to, archived or locked. Removing the controls
would strand live behaviour. If they read as confusing, that is a **labelling**
problem — the controls are doing something real. *Relabelled in wave 4.*

### Investigated, awaiting Billy's call

* **Part 4 #9 — Mention Awards: keep as-is.** It works, is fail-closed, and has
  real currency moving weekly. It is unrecognisable because it was *built* to be
  invisible: it posts no Discord confirmation, and its one live rule sits in the
  second guild. The fix, if any, is visibility — not code.
* **Part 5 #1 — Sentiment & Tone: rework, don't retire.** The composite score,
  badge, pos:neg ratio, trend and per-channel chart rest on invented thresholds
  over a VADER signal miscalibrated for this server's register. What earns its
  place is the flagged-messages triage queue with jump-to-Discord links, promoted
  to *be* the panel. Same shape as Quality Score → Contributors.
  *Built in wave 4 — `50ec4d6c`.*

### Decided 2026-09-03 — no longer blocked

Billy worked through these question by question. Full decision tables live in
the two design docs; the short form:

* **Part 3 #2 — consolidate approvals** ☑ — shipped. One unified list on the
  frozen `shop-approvals` id, unioning **six** queues (themed day, sponsored
  question, pin, sponsored emoji, quest sign-off, custom-item order) — two more
  than the plan assumed, because Pin of the Day and custom-item orders had no
  working web surface at all. Custom-item orders **widened from admin to
  economy manager, refund included**; the admin gate had been rendering a
  permissions error box on a manager-visible page, so it was a live defect
  either way.

  **The shape came out better than planned.** It is a *finder*, not a move:
  every row links to where that product is already handled, and nothing that
  resolves a request changed. That means none of the costs the investigation
  priced actually had to be paid — `economy-claims` and
  `economy-qotd-submissions` keep their route ids and their telemetry, QOTD's
  ping-role dial stays where it is, and `tests/web/test_shop_split.py` passes
  untouched. That last one matters: the split existed because
  `config-helpers`' unsaved-edit flag is a module global, so putting QOTD's
  settings form on a page with a queue re-creates a real bug. Not moving the
  form avoids it entirely. `dashboard_ia.md` still gained a stated exception,
  because a queue is found by asking "is anyone waiting?" rather than by
  knowing which feature you want.
* **Part 3 #3 — Policy Tickets for members.** A ballot is a **thread in the
  channel it was launched in**, recorded as a policy ticket, with the mod
  channel uninvolved. Fully public names; anyone who can see the thread may
  vote; admins only may open; simple majority, ties fail, no turnout floor. A
  pass **records a result only** — adoption stays a separate mod action. See
  [policy-tickets-member-voting.md](policy-tickets-member-voting.md).
* **Part 3 #10 — role autocreate round 2.** Roster page **plus provenance**;
  "(none)" on the economy notification dial becomes real (reversing the
  2026-08-22 call); both R4 dials reopen create-on-offer, closing the spectate
  room's @everyone exposure; the invite stays narrow and the Onboarding panel
  explains the missing permission instead. No role-delete button. See
  [role-autocreate-round-2.md](role-autocreate-round-2.md).
* **Part 4 #6 — Auto-Thread states: "not sure".** Left alone; the wave-4
  relabel already addressed the confusion without removing behaviour.
* **Part 4 #8 — Backfill Jobs: dropped** (`81b88193`), with the ping job ported
  to `scripts/backfill_ping_events.py` first so the outstanding recovery of
  5,774 historical pings was not stranded.
* **Part 4 #9 — Mention Awards: keep.** No action.

### Raised, awaiting a separate call

* **`econ_game_role_id = 0` in guild 1358148226850492618** (96k messages, live).
  The other two guilds hold real role ids, so only this one's 🔔 button is dead.
  Clearing the row re-enables it, but that is a production config write.
* **A DK config export/import.** What "duplicable server template" actually
  means once Discord's template API is ruled out — bigger than all of round 2,
  deliberately not smuggled into it.
* **Porting the role backfill to a script.** `role_events` has a measured 15x
  production undercount and two dashboard surfaces read it; it is the one job
  from the retired panel worth reconsidering.

### Previously blocked (all now decided above)

1. **Part 3 #2 — consolidate all approvals** into one place (spending, QOTD, shop,
   claims). A real IA change across four surfaces.
2. **Part 3 #3 — Policy Tickets for standard users** (a veteran channel voting).
   New member-facing surface; needs a design.
3. **Part 3 #10 — role autocreate / opt-in roles, round 2.** Billy wants "another
   look"; too vague to build against as written.
4. **Part 4 #3 — a Privacy subheading** under Moderation & Safety. Sits against the
   2026-08-29 IA decision that relabelled `config-moderation` to "Moderation &
   Privacy" precisely to surface `message_storage_level`.
5. **Part 4 #8 — retire Backfill Jobs.** "Not sure it's really needed anymore" is
   not an instruction; this deletes an admin surface.

**Part 3 #6 and #13** need no work: the XP level-distribution scope shipped in wave
1, and Ping Response only wants its one-time backfill press after a restart.

---

## Part 1 — cross-cutting themes

These are the multipliers. Six patterns account for ~55 of the 114 notes; fixing
each once fixes it everywhere. Do these before the per-panel list.

### A. Prose overload — "terrifying to the user" ☐

Giant explanatory text blocks where a formula and a one-line blurb would do.
Billy named **Pricing as the reference**: a wall of text, but well broken up, and
"this looks good actually — this is how these pages should be."

| Panel | Route id | The block |
|---|---|---|
| AI Assistant | `config-advisor` | worst offender — "terrifying to the user" |
| Casino | `config-casino` | same, be more brief |
| XP & Leveling | `config-xp` | "how XP is calculated" |
| Promotion Review / grant role | `config-xp` | giant text block |
| Rules Watch (settings) | `config-rules-watch` | under the monitoring checkbox |
| Help → Getting Started | `help` | wall of text, needs restructuring |

### B. Wall of blocks → tab navigation ☐

**Bios (`config-bios`) already has the tab pattern and Billy likes it** — he
noted it's used nowhere else and wants it spread. Candidates, in his words:

* **Chat Revive** (`chat-revive`) — "this page is a monster"
* **Bank** (`economy-bank-manager`) — tabs for Grant/Remove · perk rentals · ledger edit/audit
* **Quests** (`economy-quests`) — "really long"
* **Statistics** (`economy-stats`) — "a lot of information on one page"
* **Shop & Perks / Color Palettes** (`economy-sinks`) — "definitely tabs"
* **Music Playlist** (`music-playlist`) — boxes good, stacking weird
* **Activity Heatmap** (`health-heatmap`) — wall of blocks

### C. Spacing: helper text jammed against the next heading ☐

Needs more room between a field's description text and the heading below it.
Observed on `config-xp` ("post card silently" smashed against "Level 5 log
channel"; same under Message XP), `economy-config`, `mahjong`, `pen-pals`.
Likely one shared rule rather than per-panel patching.

### D. Colliding boxes — missing gap ☐

Distinct from C: adjacent cards/boxes touching each other. Observed on
`health-heatmap`, `health-gini`, `config-bump-tracker`, `music-playlist`.
Almost certainly one CSS gap rule.

### E. One giant tile that wants to be several ☐

* **XP Leaderboard** (`xp-leaderboard`)
* **Intake Queue** (`intake-report`) — skip steps, welcomers etc. as separate boxes
* **Voice Control** (`config-voice-master`) — one box per large heading
* **Pen Pals** (`pen-pals`)
* **Meadow Mahjong** (`mahjong`) — Billy talked himself partway out of this; see Q5

### F. Deleted channels render as raw ids ☐ — **root cause found**

`src/web_server/static/js/config-helpers.js:755`:

```js
export function channelName(channels, id) {
  if (!id || id === "0") return "(disabled)";
  const ch = channels.find((c) => c.id === id);
  return ch ? `#${ch.name}` : id;      // ← bare id when the channel is gone
}
```

Every caller inherits this, which is why Billy saw it on **Cleanup**
(`config-cleanup`), **Games Global Config** (`games-config`) and "a lot of
channels… they're just numbers, they don't exist anymore." One fix in the shared
helper. Needs a display decision — `(deleted channel)`, or keep the id visible
for forensics. `roleName()` three lines below has the identical hole.

---

## Part 2 — confirmed defects

| # | Panel | Defect |
|---|---|---|
| 1 | `invite-effectiveness` | Numbers don't reconcile: a board shows **28 invites / 7 still active**, but expanding the row renders "No joins recorded for this inviter" (`invite-effectiveness.js:96`). The join figures themselves also look wrong. |
| 2 | `economy-sinks` | "What's On Sale" — the checkbox isn't adjacent to its label; renders wrong. |
| 3 | `home` | The suggested-setup box still shows after everything is complete. It should disappear on completion, not merely be dismissible (`home.js` has a `dk_seen_setup_suggestions` dismiss flag but no all-done path). |
| 4 | *(cross-cutting F)* | Deleted channels as raw ids. |
| 5 | `manual.html:2789` | **Design-conversation copy leaking to users.** Verbatim: *"Results are never echoed, only things you can still act on or worth marking, so the link is always worth clicking."* That's the rationale we settled in design, addressed to nobody. Sweep for siblings. |

---

## Part 3 — feature asks

Ordered by Billy's evident pain, not by size.

1. **Rules Watch bulk false-positive marking** (`rules-watch`) ☐ — "there's too
   many here for me to manage the way that it is now." Wants bulk marking and/or
   keyboard shortcuts "so I could rip through it real fast." *This is the one
   actively costing him time.*
2. **One place for all approvals** ☐ — spending approvals are there, but QOTD
   approvals live elsewhere (`shop-approvals`, `economy-qotd-submissions`,
   `economy-claims`). Consolidate.
3. **Policy Tickets for standard users** (`mod-policy-tickets`) ☐ — expose voting
   to non-mods in a channel, e.g. a veteran-only channel voting on something.
4. **Birthdays: arbitrary channel count** (`config-birthday`) ☐ — replace fixed
   "main + second channel" with an add-button list, matching the pattern used
   elsewhere. Billy filed this as future work.
5. **DAU/MAU trend scaling** (`health-dau-mau`) ☐ — scale/zoom the trend graph,
   view the metrics over time.
6. **XP Leaderboard level distribution scope** (`xp-leaderboard`) ☐ — currently
   includes everyone; should be active-in-last-30-days.
7. **Mod Coverage split by moderator** (`mod-coverage`) ☐.
8. **Docs editor width** (`docs`) ☐ — markdown and preview stacked top/bottom
   instead of side by side, or move document selection to a dropdown so the
   editor can go full width.
9. **Newcomer Funnel caching** (`health-newcomer-funnel`) ☐ — slow on click,
   expected to be cached.
10. **Role autocreate / opt-in roles, round 2** (`config-roles`, `onboarding`) ☐ —
    likes what's there, wants another look at how roles get added; could drive
    server setup via Discord's community features / a duplicable template.
11. **Move the AI moderation prompt off the Models page** (`config-ai`) ☐ — the
    prompt belongs under Rules Watch config, or on a dedicated AI-moderation
    settings surface.
12. **Centralize economy channel pickers** (`economy-config`) ☐ — post-to-Discord,
    perk shop, bounty board, economy panel each pick a channel; these should be
    set once in the core settings at the top (the bank channel is where the guide
    posts).
13. **Ping Response has no data yet** (`ping-response`) — expected; it's new and
    needs the one-time backfill press after this restart. No action beyond that.

---

## Part 4 — IA, labels and copy

1. **Role Menus → "Reaction Roles"** ☐ — what people actually call it. The route
   id `role-menus` is **frozen**; this is a label change only.
2. **Games "Overview & Logs" → something more descriptive** ☐ — Billy floated
   "Play Statistics". Id `games-logs` frozen.
3. **"Privacy" as its own subheading under Moderation & Safety** ☐ — collect
   Delete Me and the GDPR surfaces. ⚠️ **Adjacent to a recent decision:**
   `config-moderation` was deliberately relabelled *"Moderation & Privacy"* on
   2026-08-29 (IA3) because it holds `message_storage_level`, "the biggest
   privacy dial on the dashboard, which the bare label hid entirely." A new
   Privacy subheading is compatible with that but shouldn't undo it.
4. **"Rapid fire slowdown" is under the wrong heading** (`config-xp`) ☐ — belongs
   under Anti-farming.
5. **Image Guard settings: move reporting out** (`config-spoiler`) ☐ — "recent
   activity" and detections belong in the reports, where they already exist. The
   reports copy stays; the settings page drops them.
6. **Auto-Thread: drop the question-thread states** (`config-needle`) ☐ —
   "waiting / answer / archived or locked" never made sense; it isn't that kind
   of thread.
7. **Income Sources: an unbuilt feature is on the page** (`economy-income-sources`)
   ☑ — "the idea is not built yet, doesn't need to be in the web page." Keep it
   as notes somewhere instead. *Shipped in wave 3 (`fb19d114`): the
   "Ideas Not Built Yet" card and its `SUGGESTIONS` const were deleted, and the
   same commit preserved every idea verbatim, with its blocker, in
   `docs/plans/economy-and-perk-shop.md` (the "keep it as notes" half of the
   ask). A sweep of all ~150 panels and manual.html found no other surface
   advertising an unbuilt feature.*
8. **Backfill Jobs may be removable** (`admin-backfill`) ☐ — "not sure it's really
   needed anymore."
9. **Mention Awards needs a second look** (`mention-awards`) ☐ — "not totally sure
   what it is doing here."
10. **Quick Reference still feels a bit long** (`help-quickref`) ☑ — mild.
    *Renamed to **Everyday Commands**. Measured against the manual's other 31
    sections it was the 5th shortest — a quarter of the median — so the length
    complaint was never about size. It read long because the name promised a
    complete index and delivered 16 hand-picked rows, so they felt both
    incomplete and too many at once. Renaming resets the promise; the page
    also lost a duplicated sign-off and two descriptions that wrapped to three
    lines. No row was cut — the table is already curated, and the wellness
    review has one queued to add.*
11. **Yellow text coloring is jarring** ☐ — home/quick reference; wants an easier
    color.

---

## Part 5 — research, not yet actionable

1. **Sentiment & Tone feels useless** (`health-sentiment`) — "we gotta figure out
   something for that." Rethink the metric or retire the panel.
2. **Quality Score metric** (`quality-score`) — looks representative of what he
   sees in the world, but worth researching how to improve it.
3. **Feature Rotation settings layout** (`feature-rotation`) — the three-wide
   settings box things differently from every other panel and "looks a little
   ugly." Functional; not turned on yet.

---

## Part 6 — explicitly good, no action

Home (aside from the setup box) · Mod Engagement · Activity · Activity Drops ·
Participation Gini · Voice Activity · Connection Graph ("I like the rework we did
yesterday") · Interactions · One-Sided Attention ("representative") · Cohort
Retention · Greeter Response · Intake Queue (content) · Join Times · Time to
Level 5 · Jails · Message Search ("I like the chips") · No-Contact · Policy
Tickets · Tickets · Todo List · Warnings · Image Guard (all three surfaces) ·
all audit logs · Announcements · Branding · Global · Welcome & Leave · Bios ·
Gender Tagging · Inactive Sweep · Inactive Role Removal · DM Permissions ·
Auto-React · Starboard · Quote Tool · Greeting Watch · Pricing *(the reference
for good long-form)* · Claims · Wellness (all) · External Tracking · Games Global
Config · Scheduling · Anonymous AMA · Chicken · Hot Potato · Quickdraw ·
LegitLibs · Pressure Cooker · Photo Challenge · Survivor · all question banks ·
Social section · Feature map.

**Mod Workload** (`health-mod-workload`) — "seems kinda lopsided." Recorded as an
observation about the data, not a defect, unless Billy means the layout.

---

## Part 7 — open questions

| # | Question |
|---|---|
| Q1 | *"Well, potassium is fine. Right. Maybe not."* — said right after Global, before proposing the Role Menus rename. Best guess: musing on whether the nav **taxonomy** is fine, and half-retracting it. Confirm or discard. |
| Q2 | *"See, my patch looks good. Looks look good."* — position in the walk says **Casino** and **Pools** (`config-casino`, `config-pools`). Confirm. |
| Q3 | Music Playlist's two colliding boxes — heard as *"connection and watch"*; the panel has both a Spotify **Connection** section and a **Watched channel** section, so that's the likely pair. |
| Q4 | Games Global Config — *"it seems like push on the list."* Unresolved. |
| Q5 | **Meadow Mahjong**: split into separate boxes, or spacing fix only? Billy started to ask for the split and then said "no." Filed as spacing-only pending his call. |

---

## Part 8 — checked against prior decisions

Nothing in this pass re-opens a settled call, but three items sit next to one:

* **Privacy subheading** (Part 4 #3) — see the IA3 note above.
* **Pen Pals splitting** (theme E) — the IA audit's decision **D1** deliberately
  kept Policy Tickets and Pen Pals *merged*. Splitting Pen Pals' **boxes within
  the page** is not the same as splitting the page, so this is compatible; worth
  stating so nobody reads it as a reversal.
* **"Level 5" wording** (theme C, `config-xp`) — the IA audit **deferred #89**:
  `role_grant_level` stays hard-coded at 5 because three member-facing surfaces
  bake "Level 5" into their copy. Touch the spacing, not the wording.

Also relevant: `docs/plans/dashboard-config-ia.md` records `pressure_config` as a
standing orphan table, and deferrals **#91** (LegitLibs read-only prompts) and
**#53** (closed by migration 193). None of Billy's notes touch these.
