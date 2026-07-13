# Testing Queue

Changes that pass pytest + the fake-driven smoke checks but still need a
**live-server** pass before we fully trust them (Discord API behaviour that
can't be exercised offline). Move an item to the bottom "Done" section once
it's been verified in the dev guild, with a date.

---

## Pending

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

- [ ] Bot restarts clean — `/bank pay`, `/bank shop`, `/bank gift`, and the
      `/bank role` subgroup all appear with no boot error.
- [ ] `/bank pay` a small amount → lands in **both** ledgers (payer `transfer_out`,
      recipient `transfer_in`) and DMs the recipient.
- [ ] `/bank pay` **>100** → shows the confirmation step before debiting.
- [ ] Disable transfers in config → `/bank pay` is refused with a branded notice.
- [ ] `/bank shop` shows branded prices; icon/gradient rows reflect the server's
      role features (gated when the guild lacks them).
- [ ] Rent a **colour**, then `/bank role color` → the personal role appears
      **above** the booster swatch band and shows the colour.
- [ ] Try a **staff-adjacent** colour → the ΔE refusal **names** the staff role it
      clashes with.
- [ ] Rent a **gradient** → the gradient renders and **supersedes** the solid colour.
- [ ] `/bank gift @friend` a colour → the friend gets the DM + role and the payer
      sees the gift rental in `/bank wallet`.
- [ ] A **blocklisted** role name is refused by `/bank role name`.
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
