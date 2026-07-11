# Testing Queue

Changes that pass pytest + the fake-driven smoke checks but still need a
**live-server** pass before we fully trust them (Discord API behaviour that
can't be exercised offline). Move an item to the bottom "Done" section once
it's been verified in the dev guild, with a date.

---

## Pending

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

---

## Done

_(none yet)_
