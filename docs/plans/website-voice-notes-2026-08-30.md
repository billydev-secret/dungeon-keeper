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
   ☐ — "the idea is not built yet, doesn't need to be in the web page." Keep it
   as notes somewhere instead.
8. **Backfill Jobs may be removable** (`admin-backfill`) ☐ — "not sure it's really
   needed anymore."
9. **Mention Awards needs a second look** (`mention-awards`) ☐ — "not totally sure
   what it is doing here."
10. **Quick Reference still feels a bit long** (`help-quickref`) ☐ — mild.
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
