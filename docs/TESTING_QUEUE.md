# Testing Queue

Changes that pass pytest + the fake-driven smoke checks but still need a
**live-server** pass before we fully trust them (Discord API behaviour that
can't be exercised offline). Move an item to the bottom "Done" section once
it's been verified in the dev guild, with a date.

---

## Pending

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
