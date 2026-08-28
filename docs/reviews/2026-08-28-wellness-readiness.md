# Wellness — readiness review, 2026-08-28

**Trigger:** members are asking "what is Wellness / how do I join?" and Billy
wants it verified ready before re-presenting it to newer members.
**Scope agreed:** verify-it-works, onboarding funnel, trim proposal, docs/pitch
refresh. Explicitly **not** a re-run of the 2026-08-05 code/privacy audit
(`2026-08-05-wellness.md`, clean bill) — its verdict stands, with one exception
noted below that it missed.
**Method:** 27-agent workflow (six investigation lanes; every defect claim
adversarially verified by two independent refuters; completeness critic).
All prod reads via `sqlite3 -readonly`; no writes, no restarts.

## Verdict: FIX-FIRST

Not RETHINK: the core loop is provably alive (streak credits stamped 08-28,
week-34 reports generated 08-23, AI leg healthy with a proven fallback), members
are organically asking to join, and every blocker found is copy-level or
small-code. Not GO: re-presenting today means pitching a manual that promises
unbuilt admin capabilities, a funnel that demonstrably stalls 100% of enrollees
at the same step, a dashboard that hides the feature from everyone who hasn't
already joined, one CLAUDE.md-violating contact surface, and recurring outputs
whose delivery to real members has never been confirmed.

### The adoption story in one paragraph

Wellness is ~6,635 source lines serving 3 all-time enrollees, one of whom is
paused until the year 2125 (the deleted `/wellness pause` command's deliberate
"indefinite" encoding — commit `148a67d0`, not a bug) and owns the only cap and
only blackout ever created. The two active members joined in late July, have
**zero caps and zero blackouts to this day**, and panel telemetry since 07-28
shows the Caps, Blackouts, Partners, and History panels have **never been
opened**. Discovery is pull-only (/help, /info, the manual), the dashboard nav
hides Wellness until after opt-in, and the post-opt-in experience is dead air:
the wizard asks "how firm should your boundaries be?" then never prompts the
member to create a boundary. Streaks and weekly reports run config-free — the
part that works — but with nothing to violate, compliance is vacuously 100%.

## 1. What is verified alive (the good news)

- **All three loops** (60s tick, weekly report, hourly active-list) run under
  the resilient-task supervisor; streak credits stamped 2026-08-28; zero
  wellness error lines in the current boot log.
- **Weekly reports** generated every ISO week through week 34 (08-23) for both
  active members; week-34 `ai_text` is genuine llama-server output; weeks 32–33
  carry the static fallback verbatim — proving the Ollama-down path degrades
  cleanly (llama-server answered /health 200 during this review).
- **Enforcement code path** is live and self-contained (ingest-time message
  counting at `events_cog.py:895` → `wellness_enforcement.py`; no dependency on
  xp_events or retention jobs; nothing has rotted). A fresh cap would count and
  escalate today.
- **GC sweeps** (`gc_opted_out_users`, `gc_old_cap_data`) alive by inference —
  the nightly branch executed through the 08-28 00:05 UTC window without error;
  they have simply never had a row to delete.
- **Away mode** is the only organically-used member feature: 5 `/wellness away
  set` invocations, 3 rate-limit rows proving auto-replies fired (07-30), one
  member maintains a custom away message today.
- **Reset-siblings sweep**: post-f2e23cda, the settings route is
  omission-preserving and every away/pause/opt-out write path preserves
  untouched fields end-to-end (4 latent exceptions in §3).

## 2. What has never once executed in prod

- **Cap/blackout enforcement** — `wellness_cap_counters`, `wellness_cap_overages`,
  `wellness_blackout_overages`, `wellness_blackout_active`, `wellness_slow_mode`:
  0 rows, all-time. The only cap and blackout belong to the 2125-paused user,
  whom every enforcement leg explicitly skips. Nobody unpaused has ever had one.
- **Partners** — 0 partnerships ever; and an accepted partnership *does nothing*
  (no code reads `wellness_partners` except the dashboard list; the promised
  "see each other's streaks / send nudges" was never built).
- **The public/social layer** — all 3 members have `public_commitment=0`, so the
  pinned "Active in Commitment" embed has shown *"No one has opted in with
  public commitment yet"* for months (active anti-pitch in the one always-visible
  channel surface), and milestone celebrations are silently skipped (user
  829… sits at 🔥 earned vs 🌟 celebrated). One member's 0 is plausibly residue
  of the pre-08-22 setup-reset bug — their celebrated 🌟 proves commitment was
  once ON.

## 3. Confirmed defects (each survived two adversarial refuters)

| # | Sev | Defect | Where |
|---|-----|--------|-------|
| D1 | high | **Admin panel cannot provision wellness.** Nothing in the codebase writes `wellness_config.role_id`/`channel_id`; the admin panel edits only `default_enforcement`, while `/wellness setup` on an unprovisioned guild refuses with copy pointing at a dashboard control that does not exist. Guild 1476…484's row (`0|0|0|gentle` — non-default enforcement) proves an admin actually tried and got no further. TGM's row was hand-seeded. | `wellness_routes/admin.py:106-129`, `wellness-admin.js:40-52`, `wellness_cog.py:222-231` |
| D2 | high | **Daily wellness DM is orphaned for wellness members without the economy-game role.** The only daily touchpoint rides the economy morning digest (`09a1dd6e`), gated `require_game_role=True` with silent drop. `role_events` shows no economy-game grant ever for 884…466 (while densely recording ~15 of their other role changes), so their daily line has most likely never been sent despite `notifications_pref='both'`. No surface mentions the dependency. | `events_cog.py:1362-1399`, `economy_service.py:1338-1403` |
| D3 | high | **Partner request path violates the no-contact rule.** `POST /partners/request` DMs an arbitrary member from a raw pasted ID and never consults `is_no_contact_conn` (no wellness file references the no-contact service); its refusal copy is also distinguishable ("has not opted in" vs "could not DM"). The 08-05 audit never examined this path. Whatever happens to partners, this must not stay reachable. | `wellness_routes/api.py:926-996` |
| D4 | med | **Weekly report delivery is never recorded.** `sent_at` is written *before* the DM is attempted (dedup reservation); a closed-DM member gets only a boot-wiped log warning. Whether the two members received *any* report is unverifiable from surviving evidence — and 884…466 self-tagged "DMs: Closed" on 08-01. The column name actively misleads. | `wellness_scheduler.py:509-532` |
| D5 | med | **Dashboard timezone is written unvalidated** and silently becomes UTC at every point of use (`safe_zone()` fallback) while the form forever displays what the member typed. Member types "PST" → green Saved → caps/resets/blackouts all run on UTC. f2e23cda's shape inverted. | `api.py:437,463` vs `wellness_service.py:598-600` |
| D6 | med | **The 2125 pause renders as a bare time of day** on the member panel (`toLocaleTimeString()` — "Paused until 6:31 PM", no date) and the admin chip shows no duration at all, so a forever-pause and a 60-minute pause are indistinguishable. The state itself is legitimate but unreproducible (current pause writers clamp to 7 days) and will never auto-resume. | `wellness-home.js:27-28`, `wellness-admin.js:65-70` |
| D7 | med | **manual.html promises admin-created per-member caps/blackouts three times**; no such endpoints exist (admin router: dashboard/defaults/pause-resume/exempt only) and the spec explicitly lists it as not built. A mod following the manual's accessibility guidance hits a wall. | `manual.html:1472,1498,1504` vs `admin.py` |
| D8 | low | `/wellness setup` re-run by an active member **resets `opted_in_at`**, shrinking the weekly report's tracked-days denominator — the one field f2e23cda's fix didn't preserve, and the /info "Redo wellness setup" button invites the sequence. | `wellness_service.py:523-526` |
| D9 | low | Home settings form **silently converts a legacy 'cooldown' row to 'gentle'** on any save (value matches no select option → browser picks first). Zero prod exposure today; latent. | `wellness-home.js:79-84,139-145` |
| D10 | low | Re-adding an already-exempt channel **clobbers its stored label** with `#<id>` via the upsert; the dropdown doesn't filter already-exempt channels. Zero rows today; latent. | `admin.py:260`, `wellness_service.py:1567-1580` |
| D11 | low | Unprovisioned-guild copy sends members/admins to a dashboard capability that doesn't exist (member-visible face of D1). | `wellness_cog.py:500-506` |

Also latent, API-only: `PUT /caps/{id}` with a flat limit on a bucketed cap is a
dead write (`bucket_limits` stays authoritative) — reject or clear buckets.

## 4. The funnel, end to end

1. **Discovery is pull-only.** Four surfaces, all requiring the member to go
   looking: /help General, the /info card, manual §15, the advisor AI. Zero
   push surfaces: no onboarding/welcome mention, no role-menu row for the
   wellness role, no announcement. The dashboard nav hides the whole Wellness
   section until after opt-in (`app.js` wellnessGate) — a curious member
   browsing the dashboard never learns it exists, even though `wellness-home.js`
   already contains a good not-opted-in pitch state that is currently reachable
   only by admins and dead deep links.
2. **The wizard is the funnel's best part**: 2 ephemeral steps (timezone select,
   enforcement select), honest copy, no typing, preserves settings on re-run
   (post-f2e23cda; except D8). Keep it.
3. **Post-opt-in dead air is where 100% of real enrollees stalled.** No default
   cap, nothing prompts creating one; the done embed names the dashboard once
   and the bot never mentions it again. Both post-July enrollees sit at zero
   caps/blackouts; their 37- and 30-day streaks are trivially clean and every
   weekly report reads 100% compliance — the feedback loop measures nothing.
4. **The web handoff drops the destination**: a first-time/expired visitor
   hitting any `#/wellness-home` deep link gets `/login` with the hash
   discarded, and login.html's button carries no `return_to` (the machinery
   exists and works for mid-session API 401s). Lands on #/home. This is
   dashboard-wide, not wellness-specific.
5. **Discord-only members can join but not leave**: opt-out has no slash
   command; every configuring control (caps, blackouts, prefs, public
   commitment, pause, leave) is dashboard-only. Discord delivers only the
   ambient layer (streak line — see D2; weekly DM; away replies).

## 5. Docs & pitch

Post-honesty-pass accuracy is verified good — enforcement copy matches code
everywhere, Cooldown is correctly absent — with the D7 exception above and:

- §15 leads with a flat mechanics sentence and **never states member value**
  (streaks/badges appear only inside the *Economy* section; "weekly report"
  appears nowhere in the manual). Pen Pals and Mahjong set a far higher bar.
- The six member self-service panels are documented **under an "Admin
  Overrides" heading** — a member reading §15 concludes the dashboard is admin
  tooling. Every one of those routes is self-scoped `require_user`.
- /help's two wellness lines are mechanics-only; away's enrollment gate is
  undisclosed there and in the manual.
- Wellness is absent from the Quick Reference command table.
- Privacy notice understates the stores: "Wellness settings" doesn't cover the
  streak ledger, cap tallies, or retained weekly summaries + AI note; the
  local-processing list omits the wellness encouragement model (it *is* local —
  omission, not inaccuracy).
- Small: "wellness check-ins" (no such mechanic; /info shows enrollment state);
  done embed says "channels" plural vs one configured channel.

Proposed §15 shape: (1) what you get — 2–3 sentences of member value; (2)
Joining (`/wellness setup`, and "a Wellness section appears in your sidebar
once you've joined"); (3) Your wellness dashboard — the member panels + Leave;
(4) a short honest For-admins block (defaults, pause/resume, exempt channels).

## 6. Trim proposal (KEEP / SIMPLIFY / SHELVE / CUT)

Footprint: ~6,635 source lines (cog 585, services 3,379, routes 1,383, panels
1,288) + 3,483 test lines. Inventory verdicts:

**CUT**
- **Partners** (~500 source + ~200 test lines): flow is completable but
  acceptance does nothing; 0 uses ever; raw-Discord-ID input; carries D3.
  Cutting needs a DROP-TABLE migration (classifier requires explicit approval),
  a `data_register.md` edit, manual/spec updates, purge/export branch removal;
  nav id `wellness-partners` retired-not-reused. Fallback: SHELVE = hide nav
  entry **and disable the request endpoint** (~10 lines) — D3 dies either way.
- **`_SettingsView`** (~130 lines, `wellness_cog.py:290-422`): defined, never
  instantiated, spec-acknowledged dead stub. Pure win.
- **Manual/flat caps section** in the caps panel (~90 panel lines + ~50 lines of
  phantom channel/category/voice scope machinery): the only cap ever created
  came from the sliders; the manual form carries two unkept "coming soon"
  promises. The histogram slider editor (seeded from the member's own last-30-day
  posting pattern) is the feature's best onboarding — keep it as the sole cap
  surface.
- Opportunistic column drops riding whatever migration ships: `cooldown_until`,
  `crisis_resource_url` (both orphaned).

**SIMPLIFY**
- `slow_mode_rate_seconds`: enforced but unsettable (no UI anywhere — the
  inverse of the "never ship an unenforced preference" rule). Hard-code 120s
  and delete the parameter (~30 lines), or surface one Advanced field.
- Nav 6 → 4: Overview (absorbs Away's two controls and the partners remnant),
  Caps, Blackouts, History; `daily_reset_hour` under an Advanced details block.
  ~19 member-facing dials today for what is honestly a 5-setting feature.

**KEEP**: blackouts (most legible pitch: "guards your sleep hours"; templates
are the onboarding hook), away mode (lead the pitch with it — it's the one
thing members found on their own), streaks/badges (with the starter-cap caveat),
weekly reports + AI + history, daily digest line (after D2), public-commitment
stack (it's the social proof a relaunch needs — but invite people to flip the
toggle, or the empty pinned list keeps undercutting the pitch), pause/resume,
enforcement dial (already the collapsed control), timezone/notifications/reset-
hour, the wizard, admin panel incl. exempt channels (borderline, but cutting
orphans the caps `exclude_exempt` flag).

**Honest net deletion: ~850 source lines (~13%)** counted line-by-line, not
extrapolated. The structural win is the point: afterwards every remaining
surface is one a member actually uses or receives, and no copy promises
anything unbuilt.

## 7. Proposed sequencing (pending Billy's calls in §8)

**Stage 0 — ground truth (no code).** Ask the two active members whether the
week-34 report DM arrived (or `sudo journalctl` around 08-23 13:03); check
884…466's economy-game role in live Discord; check whether the wellness role
reveals one channel or a category (fixes the "channels" plural question and
decides whether the pinned list can ever serve discovery).

**Stage 1 — the gate list (before any re-pitch).**
1. Partners: cut or shelve-with-endpoint-disabled (resolves D3).
2. Manual §15 rewrite + D7 correction + privacy-notice expansion + /help copy +
   Quick Reference row — written once, against the post-trim surface.
3. Nav ungate (Wellness Overview visible to all members as the web pitch page)
   **shipped together with** join-state branches on the four panels that
   currently render 403-ing editors to non-members.
4. Post-opt-in follow-through: starter-cap/blackout nudge (see decision Q3) and
   a done-embed that sets expectations for week one.
5. D2: surface or remove the economy-role dependency on the daily DM.
6. One live enforcement drill: throwaway cap on a test account, trip it,
   observe nudge → breather → slow-mode, delete. The only pipeline with zero
   prod executions ever.
7. D1 provisioning ("Activate Wellness" card: role picker/auto-create +
   channel picker) — or, if deferred, at minimum the D11 copy fix.

**Stage 2 — after the pitch (no member-visible risk).** D4 delivered_at column
+ admin undelivered indicator; D5 timezone validation; D6 pause rendering; D8
opted_in_at preservation; D9/D10 latent clobbers; slow-mode-rate decision;
remaining trims; login `return_to` fix (dashboard-wide, separate commit).

## 8. Decisions needed from Billy

1. **Partners: CUT (DROP-TABLE migration, your approval required) or SHELVE
   (hide + disable endpoint)?** Recommendation: cut; the table is empty and the
   feature's promises were never built.
2. **Nav ungate**: show Wellness Overview to all members as the web pitch page?
   One-line gate change + copy; changes who sees the section.
3. **The framing fork the critic surfaced**: is zero-config the product
   ("join in 30 seconds — streaks + a weekly check-in; caps are the power
   tier") or does setup nudge a starter cap/blackout so streaks measure
   something? This decides whether the dead-air fix is copy or a wizard step.
4. **Admin-on-behalf caps/blackouts**: correct the manual (cheap, recommended)
   or build the capability to match the promise?
5. **Provisioning now or later**: the Activate card unblocks guild B and every
   future guild; TGM's relaunch doesn't strictly need it.
6. **Public commitment**: DM the two streak-holders inviting them to flip it on
   (re-lights the pinned list and their pending 🔥 celebrations)? And keep the
   list's day counts (a self-flagged deviation from the spec's badges-only
   non-goal)?
7. **The 2125-paused member**: leave as-is (skipped everywhere, harmless) or
   resume/opt-out with their consent? They're also why "3 members" overstates
   adoption by one.
8. **`/wellness setup` re-run semantics**: pure settings editing (D8 is a bug,
   fix it) or a re-affirmation ritual (denominator reset is a feature)?

## Appendix — deliberately not done

- No re-audit of general correctness/privacy/layering (08-05 clean bill
  stands). One flag for the audit process: the D3 no-contact miss suggests the
  audit checklist should name the CLAUDE.md contact-surface rule explicitly.
- No writes to prod, no restarts, no live Discord reads (role/channel checks
  are Stage 0).
- Timezone select gaps (no Phoenix/Middle East/SE Asia) noted but low for TGM's
  membership; room exists within Discord's 25-option cap.
- The `days ≤ 180` clamp on the wellness activity histogram may need
  re-windowing to ≤ 90 before the xp_events retention dial is ever enabled —
  check whether the xp-events-retention branch already covered it as one of its
  "broken readers".
