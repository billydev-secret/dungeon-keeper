# Testing Queue

Changes that pass pytest + the fake-driven smoke checks but still need a
**live-server** pass before we fully trust them (Discord API behavior that
can't be exercised offline). Move an item to the bottom "Done" section once
it's been verified in the dev guild, with a date.

---

## Pending

### Rules Watch enable/disable/set-channel moved to the web dashboard  (commit TBD)

`/rules-watch enable`, `/rules-watch disable`, and `/rules-watch set-channel` are removed —
they duplicated the existing `config-rules-watch.js` dashboard panel, which already wrote the
same `rules_watch_enabled`/`rules_watch_channel_id` keys. `digest`, `stats`, and `label` are
unaffected.

- [ ] Restart the bot → `/rules-watch` in Discord no longer offers `enable`, `disable`, or
      `set-channel` as autocomplete options; `digest`/`stats`/`label` still do.
- [ ] Toggle "Enable monitoring" and set an alert channel on the dashboard's Rules Watch config
      panel → save, then post a message that should trip a signal — it still gets flagged into
      the queue and (if immediate-tier) alerts to the configured channel.

### `/inactive config` removed, settings moved to a new Inactive Sweep panel  (commit TBD)

The `/inactive config` command (threshold_days/auto/cap) is deleted — the same three keys are
now set from a new **Inactive Sweep** panel on the web dashboard (Config section). `/inactive
mark|release|panel|sweep` are unaffected.

- [ ] Restart the bot → `/inactive` in Discord no longer offers `config`.
- [ ] Open the dashboard's Inactive Sweep panel → it shows the current threshold/cap/auto-sweep
      values (defaults 30/25/off on a guild that's never set them) and saves changes.
- [ ] With auto-sweep enabled and an inactive channel configured (`/inactive panel`), confirm
      `/inactive sweep` (dry run) reflects a changed threshold/cap from the panel.

### Six duel/group games get web config panels (Pressure Cooker, Quickdraw, Hot Potato, Hot Potato Group, Chicken, Musical Chairs)  (commit TBD)

Each game's `config` slash command was already dead code (stripped from the Discord command
tree in `setup()` before this change), so there was previously no way to change these settings
short of a direct SQLite edit. New dashboard panels (Games nav section, one "Config" heading
per game) now cover the same settings, plus `channel_allowlist`/`max_nick_length`/
`max_stakes_length` for the five games that never had a way to set them (they were always
enforced by the shared duel base classes). This is additive — no Discord command was removed
for these six games, only dead Python methods.

- [ ] For each of the 6 games' Config panels on the dashboard: open it on a guild with no
      existing config row → confirm the documented defaults render (e.g. Chicken:
      cooldown 48h, climb 25s, 2–8 players), change a value, save, and reload → the change
      persists.
- [ ] Pick one game (e.g. Pressure Cooker) → set `channel_allowlist` to a single test channel
      on the panel → confirm `/games pressure challenge` is refused in a different channel and
      allowed in the allowlisted one.
- [ ] Lower a game's `cooldown_hours` to 0 on the panel → confirm the same pair can immediately
      rematch in Discord.

### Risky Rolls roster shows names instead of `<@id>` numbers  (184934d)

The roll list — and the result / reroll fields — in the Risky Rolls embed now
print cached **display names as plain text** instead of `<@id>` mentions, which
some viewers' clients couldn't resolve (mainly members who'd left). A name is
cached when a player rolls and backfilled from the guild member cache on a miss.
The winner/loser question prompts still ping via message **content** — only the
embed display changed, so nothing that used to notify stopped notifying.

- [ ] Start a round and have several people **Roll** → the roster shows each
      player's display name as text, no `<@…>` numbers — check on a phone/second
      account that hasn't loaded the member list.
- [ ] Close the round → the **Result** ("Asks" / "Answers") and any tie-rolloff
      lines show names too, not numbers.
- [ ] The loser still gets **pinged** — the question prompt is a separate
      message (content), not the embed.
- [ ] Force a highest tie → the ⚔️ **Reroll** field ("Tied" / "Waiting on")
      shows names, and the "still waiting for … to reroll" nudge still pings.
- [ ] Restart the bot mid-round → the restored roster still shows names for
      players who are still in the server.

### Economy — remove the join-time onboarding DM  (5a9e439)

The onboarding "starter path" DM (sent to every new member on join, listing the
`onboarding`-flagged quests) is deleted — same opt-in concern as the streak DMs:
it pushed the economy at members who never took the game role. No join-time
economy DM fires now; members find quests via `/quests`. The quest editor's
🧭 Onboarding-path toggle is removed. DB schema (`onboarding` column,
`econ_onboarding_dms` table) is left inert.

- [ ] Join the dev guild with an alt (economy enabled, a quest previously
      flagged onboarding) → **no DM** arrives.
- [ ] Config → Economy → Quests: the authoring form has **no** "🧭 Onboarding
      path" checkbox, and the quest table shows no 🧭 badge. Create/edit a quest
      → still saves fine.

### Bios — trigger button copy is dashboard-configurable  (388acb8)

New: the persistent "Create / Update Bio" button's embed **title and body** are
now editable at **Config → Bios → Config** (keys `bios_trigger_title` /
`bios_trigger_body`) instead of being hardcoded; unset/blank falls back to the
built-in defaults. The main guild's copy is seeded to the Golden Meadow welcome
text. Applying edits to the **live** message needs a re-post; the code change
itself needs a **bot restart**.

- [ ] Restart the bot → boots clean.
- [ ] Config → Bios → Config: the **Trigger title** / **Trigger message** fields
      show the seeded Meadow copy; edit both, Save, reload the panel → the edits
      persisted.
- [ ] Click **Post trigger button** → the button embed in the bios channel now
      shows the configured title + body (Discord markdown rendered), replacing
      the old "📝 Share your bio" default.
- [ ] Clear the Trigger message and Save → rejected (min length 1); the live copy
      is unchanged.
- [ ] Tap the button → the bio wizard still opens normally (unchanged flow).

### Economy — streak/milestone DMs respect the opt-in game role  (bb4fa1e)

Bug fix: recurring streak/milestone/grace/reset DMs (§3.1) were reaching every
earner, even members who never took the opt-in **economy game role**
(`game_role_id`). They now flow through `notify_member(..., require_game_role=True)`,
so only role-holders are DMed; everyone else keeps earning silently. Only
applies when a game role is configured (Config → Economy); with no role set the
behavior is unchanged (everyone notified). Transactional rental-billing DMs are
intentionally *not* gated.

- [ ] With a **game role configured** and yourself **without** it: earn a
      milestone/streak event (e.g. hit a 7-day streak) → **no DM**, but the
      payout still lands (check `/bank` wallet balance/streak).
- [ ] **Add** the game role to yourself, earn the next streak/milestone event →
      you **do** get the streak DM.
- [ ] Confirm quest-completion cards still DM role-holders only (unchanged) and
      rental billing DMs still reach renters regardless of the game role.
### Greeting Watch — DM when a "good morning"/"hello" goes unanswered  (8dc4a8c)

New feature: greetings in watched channel(s) that get no reply/@mention within a
window make the bot DM a chosen member. Configured at **Config → Greeting Watch**
(admin-only); no Discord command surface. Detection runs live in `on_message`
(content is judged in-memory — storage level "none" drops it); a 60s loop
(`greeting_watch_loop`) decides the verdict off `user_interactions_log`. Migration
078 adds the `greeting_watch` table. No message content is stored.

- [ ] Config → Greeting Watch: enable, pick your main chat channel(s), set
      **Notify this member** to yourself, window = **1 min** (for testing) →
      Save → reload → settings persist.
- [ ] Have an alt post "good morning" in a watched channel and **leave it
      alone** → ~1–2 min later you get a **DM** naming them, the channel, and a
      working **jump link**.
- [ ] Post another greeting and this time **reply to it** (or @mention the
      greeter) within the window → **no DM** (counts as acknowledged).
- [ ] Post a non-greeting sentence ("does anyone know when the store opens") →
      **no DM** (not detected as a greeting).
- [ ] Same person greets twice in a row before the window closes → at most
      **one** DM (per-author dedup).
- [ ] Turn the feature **off** while a greeting is mid-window → no DM fires for
      it (retired as skipped).
- [ ] Confirm the notify DM still arrives after a **bot restart** with a
      greeting left pending across the restart (row is persisted, not in-memory).

### Photo Challenge is now a standalone scheduled feature  (88c5125)

Photo Challenge left the Games menu and the shared Game Scheduling panel. The
`/games play photo` slash command is gone. It's now its own top-level dashboard
section (**Photo Challenge → Setup & Schedule**) with a dedicated channel it
always posts in, its own recurring schedule, a ping role, and an enabled toggle.
Under the hood it still rides the shared scheduler loop (schedule rows in
`games_scheduled`, `game_type='photo'`) and the shared prompt bank. Payout is now
reaction-gated (see the next entry). Needs a bot + dashboard restart to pick up
(new route + JS + cog change).

- [ ] `/games play photo` no longer exists (autocomplete shows nothing); Photo
      Challenge is gone from the **Games** nav and from the **Game Scheduling**
      game dropdown + list.
- [ ] Dashboard → **Photo Challenge → Setup & Schedule**: set the channel to a
      real channel (e.g. `1528057071235371088`), pick a ping role, tick Enabled,
      **Save setup** → reload → values persist.
- [ ] Add a **daily** schedule a minute out (or use **Run now**) → a challenge
      card posts **to the configured channel** (not wherever you were), pinging
      the role once.
- [ ] Change the channel in setup → existing schedule follows the new channel
      (next post lands there).
- [ ] Turn **Enabled** off → next scheduled slot is skipped (no card).
- [ ] **Prompt Bank** on the panel (and **Prompts & AI**) still add/edit photo
      prompts; a scheduled post with no custom prompt pulls a random one.

### Photo Challenge — payout is reaction-gated on channel posts, not replies  (23d6a60)

The economy no longer pays for *replying* to a Photo Challenge card. Instead a
member's **image post in the Photo Challenge channel** (the dedicated channel set
under **Photo Challenge → Setup & Schedule**) pays when it earns
**`react_threshold` distinct reactions** (default 5; the author's own react and
bots never count), capped **once per guild-local day**. The trigger kind
`photo_reply` was renamed to **`photo_react`** (migration 079 rewrites existing
quests + income-source rows, so the live "Picture This" quest keeps working). The
Photo Challenge **Setup** panel gained two options: **Reactions to earn**
(default 5) and **Auto-react emoji** (the bot seeds this on each photo so members
can one-tap pile on).

**Worth knowing while testing:** payout is dormant until a channel is set on the
Setup panel, and still requires an **active `photo_react` quest** in Economy →
Quests with the income source enabled — without one, posts pay nothing (by
design). "Picture This" is **weekly**, so it pays once *per week* per qualifying
member, not per day; make a daily `photo_react` quest to see the once-per-day cap.
The distinct-reactor count and auto-react are pure Discord-API behavior that
can't be exercised offline — hence this live pass.

- [ ] On **Photo Challenge → Setup**: **Reactions to earn** shows 5 and
      **Auto-react emoji** is a text box. Set an emoji (e.g. 📸), Save → reload →
      both round-trip alongside the channel/ping/enabled fields.
- [ ] Ensure an **active `photo_react` quest** exists (Economy → Quests) with a
      currency reward and the `photo_react` income source enabled.
- [ ] Post an image in the photo channel → the bot **auto-reacts** with the
      configured emoji. Clear the emoji + Save → next post is **not** auto-reacted.
- [ ] Get **4 different people** to react → **no payout**. The **5th distinct
      person** reacting → the poster is paid once (✅ + reply/DM per `game_role`).
      One person adding 5 different emoji does **not** qualify; the poster
      reacting to their own photo does **not** count.
- [ ] Post a **second** photo the same day that also hits 5 → **no second
      payout** (once-per-day cap). Confirm a photo posted **outside** the
      configured channel never pays.
- [ ] For a **sign-off** `photo_react` quest, crossing the threshold posts a
      manager sign-off card (📝) and pays only after approval.

### Photo Challenge — ping role is now a dropdown, not a pasted ID  (5af3480)

The Photo Challenge panel's **Ping role on post** field was a free-text box you
pasted a numeric role ID into; it's now a proper role `<select>` populated from
the guild's roles (same picker the Scheduling panel already uses). Implemented
as a new `role` option type on the shared `mountGamePanel` component, so any
game panel can use it. Stored value is unchanged (role-ID string, blank = no
ping), so existing configs load into the dropdown as their current selection.

**Worth knowing while testing:** dashboard-only, no Discord surface for the
config itself — but confirm the ping actually fires so the round-trip through
the new picker is real. A previously-pasted ID that no longer matches any role
falls back to **(none)** in the dropdown.

- [ ] Open Photo Challenge config → **Ping role on post** is a dropdown of
      roles (with **(none)** first), not a text box. Any previously-set role is
      pre-selected.
- [ ] Pick a role → Save → reload the panel → the same role is still selected.
- [ ] Post a photo challenge (manual `/games play photo`) in an allowed
      channel → the card **pings that role once**.
- [ ] Set it back to **(none)** → Save → next challenge posts **un-pinged**.

### Rules Watch — tier tabs now filter with "Unlabeled only" off  (100c724)

The All/Immediate/Digest/Logged tabs are a tier filter. It worked while
**Unlabeled only** was checked, but unchecking it routed through
`get_all_events`, which ignored `tier` entirely — so every tab showed the same
unfiltered list. `get_all_events` now honors `tier` (mirroring
`get_pending_events`) and the route passes it through.

**Worth knowing while testing:** the underlying SQL is trivial and there's no
Discord surface — this is a dashboard-only check. Fully exercisable from a
browser; live pass is just to confirm the deployed static JS + route agree.

- [ ] Open Rules Watch → **uncheck "Unlabeled only"** → click each of All /
      Immediate / Digest / Logged. Each tier tab now shows a **distinct** list
      (Immediate has the most; Logged is a short list, not a copy of All).
- [ ] Re-check "Unlabeled only" → filtering still works; Logged shows **empty**
      (no unlabeled logged events) rather than a copy of another tab.

### XP — level-ups won on silent award paths now get announced  (b6ca6bf)

`member_xp.announced_level` (migration 075) now tracks what's actually been
announced, separately from `level`. A level won with no Discord handle in scope
(quest XP payouts) used to be dropped instead of announced; it's now owed and
delivered on the member's next ordinary award. Silent paths (backfill, recompute,
the migration seed) catch the column up so a deploy doesn't replay level history.

**Worth knowing while testing:** the actual embed post is the only thing that
can't be exercised offline — the owed/seed bookkeeping is fully unit-tested. The
migration already applied on prod; the seed's whole job is that the **first award
after restart announces nothing** for existing members.

- [ ] Restart → first messages from active members post **no** backlog of
      level-up embeds (the seed held).
- [ ] Claim a quest whose XP crosses a level (e.g. a fresh/low member) → **no**
      embed yet; then send an ordinary message → the crossed level-up embed posts
      now (owed level delivered).
- [ ] A normal level-up (message XP crossing a level with a handle in scope)
      still announces immediately as before — no regression.

### Economy — curated role-icon catalog (Sinks page) + pay memo  (9ddc456)

New **Sinks** dashboard page owns the flat perk prices (moved off Settings) and a
per-guild catalog of named role icons, each weekly-priced (migration
077_economy_icon_catalog). Renting one reuses the `role_icon` perk + personal-role
projector. `/bank pay` gains an optional memo shown in both ledgers.

**Worth knowing while testing:** everything touching Discord's role `display_icon`
is the live-only risk — upload, switch, and the presence-only re-upload guard
(`projected_icon_path`). Needs a guild with `ROLE_ICONS`. Billing/guard logic is
unit-tested.

- [ ] Sinks page: add two catalog icons with different weekly prices; confirm the
      flat perk prices also live here now (gone from Settings).
- [ ] `/bank shop` role-icon row shows a **picker** (not a flat Rent button) when
      a catalog exists → rent one → the icon appears on your personal role.
- [ ] **Switch** to the other catalog icon → the role icon actually changes on
      Discord (the presence-only re-upload guard fires).
- [ ] Disable an icon → it vanishes for new renters, current renter keeps it; try
      to delete an icon that's rented → blocked.
- [ ] Edit a catalog icon's price → the rental reprices at the **next**
      anniversary, not immediately.
- [ ] `/bank pay @member amount memo:"lunch"` → the memo shows in the recipient's
      wallet ledger and in the dashboard bank-manager ledger's Memo column
      (escaped, single line).

### Quote renderer — bounded Twemoji fetch + fail-soft body  (e86352d)

Hardening only, no visible change in normal operation. pilmoji's emoji fetch
now has a 5 s timeout (was unbounded — a stalled CDN could hang the render
thread), and a fetch failure during the body render degrades the card to
tofu-emoji instead of failing it (the attribution/header already did this).

**Fully unit-tested** — the timeout value reaching the HTTP call and both
fail-soft paths are covered, and there's no way to manufacture a CDN outage on
the live server. Nothing to verify by hand; listed for traceability only.

- [ ] (Optional) Post a normal quote with an emoji → still renders in color as
      before. No regression is the only thing to confirm.

### PvP games — "Nickname Applied" showed the new name twice  (0fc2016)

The result embed for a nickname-stake game read "**NewNick** is now known as
**NewNick**" instead of "**OldName** is now known as **NewNick**". The render
runs after the loser is renamed, so it was reading the loser's live
`display_name` (already the new nick) for the "from" side. Fixed by capturing
the old name before the edit and threading it in. All six games shared the bug
(quickdraw, pressure cooker, hot potato 1v1 + group, musical chairs, chicken).

**Worth knowing while testing:** purely a display fix — the actual rename,
24-hour sentence, and auto-revert are unchanged. Fully unit-tested (the render
is a string), so this live check is just a spot-confirm.

- [ ] Win a **quickdraw** (or any nickname game) against someone, click **Name
      the loser**, submit a nick → the result embed reads "**{their old name}**
      is now known as **{new nick}**", not the new name on both sides.
- [ ] Repeat once with a **group** game (musical chairs / group hot potato) to
      confirm the multiplayer path reads the same.

### Voice Master — knock is now private (DM the owner)  (553aaf4)

`/voice knock` used to post "X is asking to join Y's locked room" into the
control channel, where everyone could read it. It now **DMs the owner** the
Accept/Deny buttons. If the owner's DMs are closed it **falls back** to the
control channel (unchanged behavior), so a knock is never silently dropped.

**Worth knowing while testing:** the buttons had to be reworked to resolve the
guild/channel/requester from the bot cache instead of `interaction.guild`,
because a DM interaction has no guild — so **the accept path is the highest-risk
thing to verify live**. The requester's confirmation no longer names the control
channel ("you'll hear back if they let you in").

- [ ] With the owner's **DMs open**: knock a locked channel → the owner gets a
      **DM** with Accept/Deny; nothing appears in the control channel.
- [ ] Owner clicks **Accept** *in the DM* → requester gains access and gets the
      jump-link DM; audit row `vm_invite` / `via: knock` is written. (This is
      the guild-from-cache path — the one to watch.)
- [ ] Owner clicks **Deny** in the DM → buttons disable, requester gets nothing.
- [ ] With the owner's **DMs closed**: knock → the embed falls back to the
      **control channel** (owner mentioned), and Accept/Deny still work there.
- [ ] DMs closed **and** no control channel configured → requester sees
      "Couldn't deliver the knock — the owner's DMs are closed…".
- [ ] A non-owner who somehow reaches the buttons still can't use them.
- [ ] DM error path: **delete the voice channel**, then click **Accept** from
      the DM → the owner sees "That channel no longer exists." and nothing
      crashes (ephemeral-in-DM semantics, only exercisable live).
- [ ] The DM embed **names the server** ("… to join **General** in **<your
      server>**") — matters if you own a same-named channel in two servers.

### QA Tracker — per-feature cards for the role checklists  (this commit)

The three role checklists (`docs/testing/*_testing_checklist.md`) are regrouped
into `###` feature blocks (38 admin / 36 mod / 43 user, no items lost), and the
poster now cards **any** doc's `###` blocks — each checklist's cards post into
its own dev channel with a doc-prefixed entry key, never the queue's configured
channel. Flat docs without `###` still post as plain text.

- [ ] `#admin-tests`, `#moderator-tests`, `#user-tests` each repopulate as
      feature cards (with plain-text section headers between groups).
- [ ] A checklist card's Pass/Fail/Blocked buttons work exactly like queue
      cards (verdict recorded, 🪙 paid, thread note on fail).
- [ ] The dashboard board lists the checklist features alongside queue entries.
- [ ] A same-named feature in two checklists gets two distinct cards/rows
      (doc-prefixed keys — check `Confessions` in mod vs user).
### Emoji Stealer — steal custom emoji from reactions  (490bd9d)

The **Steal Emoji** right-click menu now also pulls custom emoji that were
added to the message as **reactions**, not just ones written in its text. No new
context menu (we're at Discord's 5-menu cap) and no reaction listener — the
existing menu already receives the message, and its `.reactions` come with it.

**Worth knowing while testing:** Unicode reactions (😀) are skipped — only
custom emoji are stealable. An emoji that's both in the text and reacted is
offered once. The picker/upload path is unchanged; only the set of emoji fed
into it grew.

- [ ] React to a message with a **custom emoji from another server**, then
      right-click it → **Apps → Steal Emoji**. The reaction emoji is offered
      and uploads.
- [ ] A message with a custom emoji in its **text** and a *different* custom
      emoji as a **reaction** → both appear in the picker.
- [ ] React with a plain **Unicode** emoji (😀) on a text-only message →
      "No custom emojis found in that message or its reactions."
- [ ] Same custom emoji in text **and** as a reaction → offered only once.

### Testing-queue posts get a clickable ✅ reaction  (this commit)

Each entry the mirror posts into `#testing-queue` now arrives with a ✅ reaction
pre-added by the bot, so a tester can one-click it to mark the entry verified
(Discord markdown has no clickable checkbox). The reaction sits on the entry's
last message when a long entry spans several. This very entry should demonstrate
it — if you're reading it in the channel, it should already carry a ✅ you can add
your own click to.

- [ ] This entry appeared in `#testing-queue` with a ✅ already on it (added by the bot).
- [ ] Clicking the ✅ registers your reaction alongside the bot's (count goes to 2).
- [ ] The reaction is on the entry's **last** message, not a mid-entry chunk (post a long entry to confirm, or trust the short case).
- [ ] The role checklists (`#admin-tests` etc.) did **not** get reactions — only `#testing-queue`.

### Testing docs mirrored into the dev channels + post-commit hook  (this commit)

`scripts/post_testing_docs.py` posts this queue and the three role checklists into
their `dev` channels (`#testing-queue`, `#admin-tests`, `#moderator-tests`,
`#user-tests`), chunked at heading boundaries so no entry is split mid-checklist.
A `post-commit` hook then mirrors **only the entries a commit newly adds** into
`#testing-queue`, stamped with the real short sha + subject (the doc's own
"(this commit)" is ambiguous once an entry is read on its own in a channel).

**Worth knowing while testing:** the hook is installed into `.git/hooks/` on
purpose, **not** via `core.hooksPath` — that would silently disable the
pre-commit framework's hook living in the same directory. The common git dir is
shared, so the hook is live in **every worktree**, and the configured scope is
all branches, so a commit on a feature branch posts to the live channel too.
Entries already sent are remembered in `.git/testing_queue_posted.json` (seeded
with the 48 entries of the first dump); that ledger — not the git diff — is what
stops `--amend`/rebase from re-posting the same entry against the same parent.
Escape hatch: `DK_NO_QUEUE_POST=1 git commit`.

- [ ] Commit a change that adds a new `###` entry here → exactly that entry
      appears in **#testing-queue** with a `sha · subject` footer, and no other
      entry is re-posted.
- [ ] `git commit --amend` that same commit → **nothing** posts a second time
      (the ledger, not the diff, is what catches this).
- [ ] Commit something that doesn't touch this file → hook posts nothing and the
      commit isn't visibly slowed.
- [ ] Edit an existing entry's body, or let a later commit rewrite its
      `(this commit)` marker to a real sha → **no** re-post (entry identity
      ignores the trailing parenthetical).
- [ ] **Move a verified entry down to `## Done` with a date** → **no** re-post.
      Worth doing by hand: a date written *outside* the trailing parentheses
      changes the heading the entry is keyed on, so this only stays quiet
      because everything below `## Done` is skipped outright.
- [ ] Reword a **pending** entry's title (not just its body) → this *does*
      re-post. Known sharp edge of keying on the heading; rename deliberately,
      or drop the stale key from `.git/testing_queue_posted.json`.
- [ ] Commit from a **worktree** under `.claude/worktrees/` → the entry still
      posts, and it's that worktree's copy of the file that gets read.
- [ ] `DK_NO_QUEUE_POST=1 git commit` with a new entry → posts nothing.
- [ ] Break the network or the token → the commit still **succeeds** (hook exits
      0, capped at 90 s) and only prints an error.
- [ ] `python3 scripts/post_testing_docs.py --dry-run` reports every chunk under
      Discord's 2000-char cap for all four docs.
### Privacy — clear images / text / all modes  (f8ea556)

`/delete_me` and `/delete_user` take an optional **mode**: `all` (default,
unchanged), **Images & files only**, **Text messages only**. The partial modes
are *scrubs* — they delete only that slice of the member's Discord messages and
**never** purge XP/activity/profile. Omitting the option behaves exactly as
before.

The `/delete_me` prompt now also discloses that the server keeps its own copy of
the messages for moderation. That retention isn't new — it's what
`keep_messages=True` has always done — but it was previously only mentioned in
the summary *after* the member had already confirmed. Behavior is unchanged;
only the copy is.

**Worth knowing while testing:** media is classified **during the scan**, from
the live message — attachments, stickers, and `image`/`video`/`gifv` embeds.
Link previews (`link`/`article`/`rich`) deliberately do *not* count, so a chatty
message with a URL survives a media scrub; that's the edge case most worth
poking. A posted image *URL* has no attachment but does embed as `image`, so it
**should** be caught. The scan still walks every channel either way, so a mode
doesn't make the run faster — only the delete list is shorter.

- [ ] `/delete_me` with **no mode** → prompt still reads as a full erasure, and
      now says the server keeps its own copy. Button: "Yes, delete everything".
- [ ] `/delete_me mode: Images & files only` on a test account with a photo, a
      gif, and some chat → only the photo/gif go, the chat stays, and **XP and
      profile are untouched** (check the member's level / `/bank wallet`).
      Button reads "Yes, delete my images & files".
- [ ] Post a message that's *just a link* (so Discord renders a preview) → it
      **survives** the media scrub, and a text scrub removes it.
- [ ] Post a bare image **URL** (no upload) → the media scrub **does** remove it.
- [ ] `/delete_user member:<x> mode: Text messages only` → their text goes,
      their images stay, their XP survives, and the button says "their".
- [ ] A full `/delete_user` still hard-purges the archive as before.
- [ ] A sticker-only message counts as media.

### Quote cards — stylised display names + emoji in the attribution  (a88a3dd)

Names written in Mathematical Alphanumeric Symbols (`𝓟𝓻𝓲𝓷𝓬𝓮𝓼𝓼 𝓡𝓪𝓬𝓱𝓮𝓵`) drew as a
row of **tofu boxes** — not just the emoji, the *whole name*: no bundled TTF
carries U+1D4xx. `author_name` is now NFKC-folded on entry, and the attribution
line + no-pfp header draw through pilmoji like the body already did, so a
Unicode emoji in a name renders in color.

**Worth knowing while testing:** a plain ASCII name is byte-for-byte the same
card as before (verified by pixel-diff), so nothing should *look* different for
most members. NFKC folds case-sensitively — `𝓟` → `P`, not `p`. Twemoji is
fetched over HTTP per render; if that fetch fails the line degrades to tofu and
logs `quote_renderer: emoji text fell back to plain PIL` rather than killing the
card, so an all-tofu name in the wild now means *network*, not fonts.

- [ ] Quote a message from **@princessrachel** (`rachel_132`, display name
      `𝓟𝓻𝓲𝓷𝓬𝓮𝓼𝓼 𝓡𝓪𝓬𝓱𝓮𝓵 💋`) → attribution reads `— Princess Rachel 💋` with a
      **red** kiss mark, not boxes.
- [ ] Quote someone with an ordinary name → card looks exactly as it did.
- [ ] A name that's *only* emoji still renders (attribution isn't blank).
- [ ] QOTD banner (no-pfp header path, `author_name="Question of the Day"`) is
      unchanged and still centered — this path was touched too.
- [ ] A long stylised name still doesn't slide behind the left gold frame —
      width is now measured through pilmoji, so re-check the clamp.

### Economy — per-guild quest board size  (37c2090)

The per-member board (how many daily/weekly/monthly quests a member sees at
once) was hardcoded at 2 per cadence; it's now `quest_board_daily/_weekly/
_monthly` on the dashboard **Quests → Board size** section. Default stays 2,
so an untouched guild behaves exactly as before. **0 turns a cadence off.**

**Worth knowing while testing:** the board is recomputed from
`(pool, user_id, period_index, size)` on every read, so a size change lands on
the **next `/quests` open — no midnight wait.** What it is *not* is a stable
subset: the draw starts at `(period_index * size) % poolsize`, so lowering
daily 2→1 doesn't leave one of the two quests you already saw, it re-draws
(e.g. pool of 6: n=2 → quests 3,6 but n=1 → quest 5). Expected, not a bug.
Two accounts should see different subsets.

- [ ] Quests page loads for an admin; **Board size** shows daily/weekly/monthly
      inputs at 2 and the library summary reads `pool: daily N active → 2 shown`
      (not the old `daily N/1` slot text).
- [ ] Set daily to 1 → save → summary flips to `→ 1 shown`; `/quests` for a
      member shows exactly one daily, right away.
- [ ] Set daily to **0** → `/quests` shows **no** dailies at all, and a
      daily trigger-word/game quest pays **nothing**. The regression to watch
      for is the inverse: 0 showing/paying the *whole* pool.
- [ ] Weekly/monthly still behave normally while daily is 0 (per-cadence).
- [ ] A **community** goal and an **event** quest still appear/pay while every
      board cadence is 0 — they're not board cadences and must be unaffected.
- [ ] Open Quests as a **manager-role (non-admin)** holder: the Board size card
      shows read-only prose, no inputs, and no console 403 noise breaks the page.
- [ ] Values above 25 are rejected by the API (the dial is capped at `POOL_CAP`).

### Economy — QOTD dashboard page + selectable ping role  (this commit)

New **Economy → QOTD** page (admin-only) owning `qotd_ping_role_id`. `/qotd post`
previously sent the card with no `content` at all, so it could never ping
anything. It now mentions the configured role in the message content beside the
card (same reason as Chat Revive — a mention can't live inside an image), on both
the card path and the plain-embed fallback. Unset (default) posts silently, as
before. The reward stays on Income Sources; this page only owns the ping.

**Discord gotcha worth checking first:** `allowed_mentions(roles=True)` only
whitelists the mention — Discord still won't notify anyone unless the role is
**mentionable** in its role settings, or the bot has "Mention @everyone, @here,
and All Roles". If the ping renders as gray inert text, that's the cause, not the
code.

- [ ] Economy → QOTD page loads for an admin; the ping-role picker shows
      `(none)` on a fresh guild and the "How it works" card shows the real
      reward + currency name.
- [ ] Leave the role unset → `/qotd post` posts the card **bare**, no empty
      content line above it, and pings nobody (today's behavior, unchanged).
- [ ] Set a **mentionable** role → `/qotd post` posts the mention above the card
      and role-holders **actually get a notification** (not just blue text).
- [ ] Set a **non-mentionable** role → mention renders as inert text and nobody
      is pinged. Confirms the hint on the page is the right advice.
- [ ] Save takes effect with **no bot restart** (settings are read fresh per
      post) — change the role and post again without restarting.
- [ ] Clear the picker back to `(none)` → posts go silent again.
- [ ] The question card itself still renders (server icon background, midnight
      theme) and replies still earn the QOTD award once per member.

### Chat Revive — question posts as a banner card (this commit)

`send_revive` (the choke point for both the monitor loop and the dashboard
**Fire** button) now renders the question through the quote renderer — the same
card behind `/quote banner` and the QOTD post, midnight theme, server icon as
the graded background — instead of posting the question as plain text. A role
mention can't live inside an image, so the ping and flourish stay in the message
content beside the card. Plain text is now a **fallback**, taken when the guild
has no icon, the icon won't read, the question exceeds the card's 280-char
limit, or the renderer raises; the ping fires on every path.

- [ ] Dashboard **Fire** on an enabled channel → card posts with the question
      on it, "Ember" heading, server icon graded behind it.
- [ ] With pinging enabled and the role due → `🔥 *flourish* @chat-revive`
      appears as text **above** the card and actually pings the role's members.
- [ ] With flourish off and pinging off/not due → the card posts **bare**
      (no empty content line above it).
- [ ] A question containing `@everyone` or a user mention → still pings nobody
      (allowed-mentions whitelist unchanged), card renders the text as-is.
- [ ] Add a bank question longer than 280 characters and fire it → posts as the
      old plain-text line with the **full** question, not a trimmed card.
- [ ] Auto-fire from the monitor loop looks identical to the Fire button's post.
- [ ] Follow-up measurement still records success/fail against the card message
      (check the panel's history/stats after conversation follows).

### AMA — switchable Open Panel format (this commit)

`/games play ama` gained a `format` option: **Hot Seat** (the classic
one-at-a-time rotation, unchanged default) or **Open Panel**, where everyone
who taps Volunteer joins a live roster and anyone can Ask a Question, pick a
panelist from a dropdown, and get an anonymous question aimed at them —
no single seat, no rotation, no 4-question turn limit. Reuses the existing
per-question target field so reply/pass, screened approval, and view recovery
work unchanged in both formats. This merge also reconciles the Open Panel
rewrite with the newer `ama_ask` economy quest trigger (added afterward) —
both `AskQuestionModal.on_submit` and `ScreenedQuestionView.approve` now call
`_fire_ama_ask_trigger` after the renamed `after_question_posted` status hook.

- [ ] `/games play ama format:Hot Seat` → unchanged classic flow (one hot
      seat, rotation, 4-question turn limit, Skip/New Hot Seat controls).
- [ ] `/games play ama format:Open Panel` → embed prompts to Volunteer, no
      single seat; tapping Volunteer joins the roster (shown in the embed).
- [ ] In Open Panel, tap **Ask a Question** → a dropdown of current panelists
      appears; pick one, submit → anonymous question posts aimed at them,
      they can Reply/Pass exactly like hot-seat.
- [ ] A panelist leaves (drops off the roster) while someone has the Ask
      modal open, then submits → "That person left the panel while you were
      typing — please try again" (not a crash).
- [ ] With an **ama_ask** economy quest active (see the quest-faucets entry
      below): asking in an **unfiltered** panel/hot-seat AMA pays immediately;
      in a **screened** one, only on host approval (rejection never pays).
- [ ] Panel roster with many members renders without exceeding Discord's
      1024-char field limit (embed shows a truncated "…and N more" tail).
- [ ] Scheduler: schedule an AMA with format "Open Panel" from the dashboard
      → the launched game runs in panel mode (survives a bot restart mid-game).

### Chat Revive — fix 404 on Fire/opt-in-post/role saves from ID precision loss  (this commit)

The dashboard was converting Discord snowflake IDs (channel/role) to a JS
`Number` before sending them to the API. Snowflakes exceed
`Number.MAX_SAFE_INTEGER`, so the conversion silently rounded them to a
different, nonexistent ID — the backend then couldn't find that channel and
returned `404: No such text channel.`. Fixed by sending the ID as a string
(FastAPI/Pydantic coerces numeric strings to full-precision ints) instead of
running it through `Number()`. Affected: the channel row's **Fire** button,
**Post opt-in button**, the guild `role_id` setting, and a channel's
`role_id_override`.

- [ ] Chat Revive → invite a channel, click **Fire** on its row → posts a
      revive message in that channel (no 404).
- [ ] Set the guild **Role** (ping role) and save → persists after a refresh.
- [ ] Set a channel's **Role override** and save → persists after a refresh.
- [ ] **Post opt-in button** in a channel → button appears there (no 404).

### Dashboard — Docs, Role Menus, Chat Revive moved from Moderation to Config  (this commit)

Nav-only move: the three panels now live in the Config section (Role Menus
after Auto-Role, Chat Revive and Docs after Starboard). They stay
moderator-visible (no adminOnly), matching their moderator-gated APIs.
Deep links are unchanged — the hash router keys on panel id only.

- [ ] Config section shows Docs, Role Menus, Chat Revive; Moderation no
      longer lists them.
- [ ] As a **moderator** (non-admin), the three panels still appear under
      Config and load.
- [ ] Old deep links (`#/docs`, `#/role-menus`, `#/chat-revive`) still open
      the right panel, highlighted under Config.

### Economy — bank guide panel stops re-sticking on bot messages  (this commit)

The sticky `on_message` listener now ignores **bot** messages (`message.author.bot`):
previously it re-stuck the panel under the bot's own repost and economy notices,
and the id-cache guard couldn't reliably catch the repost's own gateway event
(it can arrive before the new id is cached), so the panel could repost itself
over and over. It still re-sticks under member activity.

- [ ] With a guide panel posted, leave the channel quiet (no member messages)
      and watch for ~1 min: the panel should **not** repost on its own.
- [ ] Trigger a bot-authored message in that channel (e.g. an economy notice /
      another bot post): the panel should **not** hop to the bottom for it.
- [ ] Send a **member** message: after ~6s of quiet the panel re-sticks to the
      bottom exactly once (old panel deleted, one fresh panel).

### Economy — bank guide Spending points at the shop  (this commit)

The `/bank post-guide` panel's **Spending** field no longer lists per-perk
prices inline; it names the perks (color/name/gradient/icon) and defers the
numbers to the shop ("Prices and renewal terms are shown in the shop").

- [ ] Re-run `/bank post-guide` and read the **Spending** field: the `/bank
      shop` line names the four perks with **no** prices and points to the shop
      for pricing; `/bank gift` and (if transfers on) `/bank pay` still listed.
- [ ] Open `/bank shop` and confirm the actual prices + renewal terms show
      there, so nothing is lost by dropping them from the guide.

### Economy — bank guide panel "Joining" note + sticky-to-bottom  (this commit)

The `/bank post-guide` panel now carries a **Joining** field ("Opt in any time
from Channels & Roles to join the game economy", where "Channels & Roles" is the
`<id:customize>` onboarding mention), and it now sticks to the bottom of its
channel via an `on_message` delete-and-repost (debounced ~6s).

- [ ] Re-run `/bank post-guide` in the bank channel and confirm the new
      **Joining** field renders with a **clickable** "Channels & Roles" link
      (blue, opens the onboarding customise screen) — not the literal text
      `<id:customize>`. Requires server onboarding to be enabled.
- [ ] Post a message in the bank channel; within ~6s of the channel falling
      quiet the guide panel re-appears at the **bottom** (old copy removed, not
      stacked). Confirm no duplicate panels accumulate.
- [ ] Trigger an economy notice into the bank channel (e.g. a quest sign-off
      card) and confirm the panel re-sticks **below** it.
- [ ] Chat several messages in quick succession → the panel reposts **once**
      after the burst, not per message.
- [ ] `/bank post-guide` in the **same** channel still edits in place (panel
      does **not** jump to the bottom) — only new activity re-sticks it.

### Games — /games dev fill fixed for non-Clapback games + honest embed status  (this commit)

`/games dev fill` used to report "Added N fake players" unconditionally even
when the visible lobby embed never updated (a swallowed exception, or the
lobby message not posted yet), and it silently wrote to the wrong payload key
for compliment/mfk (`participants`, not `players`) and missed traditional's
`prefs` entry — so fake joins never showed up for those games despite a
"success" reply. `ttl`/`hottakes` now explicitly refuse (they're
submission-based, no player-list concept).

- [ ] Start a Clapback lobby, run `/games dev fill` immediately (before anyone
      can see the lobby message settle) — should say "lobby message isn't
      posted yet", not silently claim success.
- [ ] Start a Clapback lobby, wait for it to post, run `/games dev fill` —
      the lobby embed's player list visibly updates and the reply has no
      warning.
- [ ] Start a Compliment or MFK lobby, run `/games dev fill` — the fake
      players actually show up in the pool (host can close and it pairs
      them), not just an empty-looking "Added N" reply.
- [ ] Start a Traditional lobby, run `/games dev fill` — the fake players
      show real preferences when a question is asked, not silently absent.
- [ ] Start a TTL or Hot Takes lobby, run `/games dev fill` — get a clear
      "doesn't support" message instead of a fake success.

### Health dashboard — heatmap timezone + day-of-week fix  (this commit)

The Community Health heatmap (`#/health-heatmap`) bucketed hour-of-day
straight from UTC timestamps with no guild timezone shift, and a shared
day-of-week SQL formula was off by one weekday (also used by the DAU/MAU
tile's day-of-week breakdown). Both now shift by the guild's configured
`tz_offset_hours` (heatmap) / are correctly aligned to Mon=0 (both tiles).

- [ ] Dashboard → Community Health → Heatmap deep-dive: peak/quiet slot
      labels and the grid's hour columns match your wall-clock time, not UTC.
- [ ] Post a message and confirm it lands in the correct weekday row (not
      one day ahead) in both the heatmap grid and the DAU/MAU day-of-week
      chart.

### Chat Revive (stages 0–4) — rhythm-aware lull questions  (this commit)
### Chat Revive — dashboard management, /revive commands removed  (this commit)

Same-day revision: the whole `/revive` command group is stripped; the
dashboard (Moderation → Chat Revive) is now the only management surface.
The monitor loop and the persistent opt-in button are unchanged.

- [ ] Restart the bot → `/revive` no longer appears in Discord's command
      picker after the command tree re-syncs (stale entries may linger
      briefly in the client until it refreshes).
- [ ] Dashboard → Moderation → Chat Revive: settings save (enable seeds the
      starter pack once), a channel can be enabled with categories/ping
      dials, and "Check" explains the current verdict in plain language.
- [ ] "Fire" from the dashboard posts the plain-text question in the channel
      and the Scoreboard reflects it after a refresh.
- [ ] "Post opt-in button" publishes the button and tapping it toggles the
      role (including after another restart).
- [ ] Question bank: add, bulk add (tagged lines), retire — all from the
      panel; a duplicate add shows the conflict message inline.

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

### Economy — confession/AMA/whisper/quote quest faucets  (this commit)

Four new trigger kinds (`confession`, `ama_ask`, `whisper`, `quote`) wired at
existing engagement hooks. All pay through the normal silent trigger-claim path;
create an active daily/weekly quest for each kind first (quest editor).

- [ ] Quest editor trigger-kind dropdown lists the four new options
      (🙊 confession, 🎤 ask in an AMA, ✉️ send a whisper, 🖋️ quote a message),
      and the Income Sources page shows their descriptions.
- [ ] With a **confession** quest active, submit a confession → the confession
      posts **anonymously with no "quest complete" message in the feed**, but the
      confessor's `/quests` log shows the claim and a `quest` ledger row appears
      for them. Verify **both** the forum-channel and text-channel dest configs.
- [ ] With an **ama_ask** quest active: in an **unfiltered** AMA, asking a
      question pays immediately; in a **screened** AMA, asking does **not** pay
      until the host **approves** (a **rejected** question never pays). AI-seeded
      idle questions pay no one.
- [ ] With a **whisper** quest active, send an anonymous whisper that delivers →
      the sender earns it once; a whisper that fails to deliver (target DMs off,
      rolled back) pays nothing.
- [ ] With a **quote** quest active, reply+ping the make-it-a-quote role on a
      message → the **invoker** (not the quoted author) earns it; quoting the
      **same** message again pays nothing (once per quoted message).

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
(name / color / gradient / icon), a fresh rental's confirmation carries the
same button, and `/bank role name|color|gradient` are **removed**. Icon emojis
are now **this server's custom emojis only** (the bot stores the emoji's
image); `/bank role icon` survives image-upload-only. Live pass:

- [ ] After restart the command picker shows `/bank role icon` (image param
      required) and **no** `/bank role name|color|gradient` — global command
      sync may take a few minutes.
- [ ] `/bank shop` with nothing rented → all Rent buttons; rent a color →
      confirmation shows a **Set color** button that opens the hex modal and
      the color applies; reopening `/bank shop` shows that row as ✅ rented
      with a green **Set color** button.
- [ ] Bad hex / blocklisted name / clashing color typed into a modal → the
      usual friendly ephemeral errors.
- [ ] Rent the icon perk → confirmation notes `/bank role icon` for images.
      Icon modal: a typed `:server_emoji:` works and the role shows the emoji
      image; a pasted emoji (`<:name:id>`) works; a unicode emoji ✨ and a
      foreign server's emoji are refused; an animated emoji is refused.
- [ ] `/bank role icon` with an uploaded PNG still sets the icon.
- [ ] `/bank gift` a color to someone with no rentals → their `/bank shop`
      shows **Set gifted color** (plus a Rent color button), and their DM
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
/`ROLE_ICONS` gates, ΔE ≥ 25 staff-color guard, Voice Master name blocklist);
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
- [ ] Rent a **color**, then set it via the shop's color modal → the personal
      role appears **above** the booster swatch band and shows the color.
- [ ] Try a **staff-adjacent** color → the ΔE refusal **names** the staff role it
      clashes with.
- [ ] Rent a **gradient** → the gradient renders and **supersedes** the solid color.
- [ ] `/bank gift @friend` a color → the friend gets the DM + role and the payer
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
logic; the Discord-side delete behavior needs a live pass:

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
embed unit tests cover it; the Discord-button behavior needs a live pass:

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

### Role Menus — v1 (self-service roles from the Oracle panel)  (this branch)

New feature end-to-end: `docs/role_menus_spec.md`, plan `docs/plans/role-menus.md`.
Members self-assign roles via buttons/dropdown on a DK-posted embed; everything
is built and published from the new **Role Menus** page (Moderation section).
Mode engine, db, routes, and the interaction path are unit-covered; the live
Discord surface needs a pass:

- [ ] Restart the bot → boots clean (migration 073 applies; `role_menus_cog`
      loads — watch for the context-menu/extension drift warning).
- [ ] Oracle → Role Menus: create a menu, add 2–3 choices (role picker should
      only offer roles below DK's top role; a mod-permission role only appears
      after checking the per-row ⚠ override), watch the live preview track the
      form, and **Publish** to a test channel.
- [ ] Click a button as a normal member → role appears, reply is ephemeral
      ("✅ You now have @X"), and the mod channel gets the compact
      `🎭 @user +X (Menu)` line.
- [ ] Click again (toggle mode) → role removed. Switch the menu to **Unique**
      and hold two menu roles → picking one drops the other.
- [ ] Dropdown style: submitted selection **becomes** your set — holding A and
      submitting only B removes A and adds B, and the ephemeral reply spells
      out `+B, −A`. (Note: Discord can't pre-check your current roles in the
      dropdown; this is the spec'd behavior, verify it feels OK live.)
- [ ] **Binding** mode: first pick locks ("permanent" on the second try, even
      after a restart).
- [ ] Guardrails: set a required role you don't have → gated message; set
      max roles = 1 and try a second → cap message; set a 30s cooldown and
      double-click → "slow down".
- [ ] Edit the published menu and Save → the live message updates in place
      (no delete/repost). **Unpublish** → post stays but greys out; clicking
      while off (stale client) → "turned off" reply. Turn back on.
- [ ] Delete a menu role from the server, then click its button → member gets
      the polite failure, mod channel gets **one** alert (second click stays
      quiet), and the panel list/editor shows the ⚠️ health warning.
- [ ] Restart the bot and click an already-published menu → still works
      (DynamicItems rebuilt from custom_ids).
- [ ] `/api/role-menus` mutations show up in the Audit Log panel
      (`role_menu.*` actions; elevated override logs its own action).

### QA Tracker (stages 0–2) — queue entries post as verdict cards  (this commit)

The `#testing-queue` mirror now posts each new entry as a **QA card**: an
embed with Pass / Failed / Blocked buttons, backed by a `qa_tests` row in the
prod DB (plan `docs/plans/qa-tracker.md`). This entry is stage 2's own live
test, and clicking it exercises stages 0 (service/schema) and 1 (cog) too.
The QA-crew **role** and **channel** are dashboard knobs arriving in stage 3;
until then the tracker runs enabled with admins-only clicking (role_id 0).
Role-checklist channels are unchanged plain text, and queue entries no longer
get the ✅ reaction (the buttons replace it).

- [ ] This entry arrived in `#testing-queue` as a card — one embed with three
      buttons (✅ Passed / ❌ Failed / 🚧 Blocked), `sha · subject` in the
      footer, and **no** ✅ reaction.
- [ ] Before a bot restart the buttons are dead (interaction fails); after a
      restart they respond (DynamicItems dispatch on the custom_id).
- [ ] As an **admin**, click **Passed** → ephemeral confirms and pays **+15 🪙**
      (check `/bank`), the card turns **green** and gains a "Verified by" field.
- [ ] Click **Failed** → a modal demands a required "what went wrong" note; on
      submit the note lands in a **thread on the card** and the card turns red.
- [ ] Re-click a different verdict → the card updates but you are **not** paid
      again (one payment per tester per test).
- [ ] As a non-admin **without** the crew role, click any button → friendly
      ephemeral rejection ("join the QA crew"), nothing recorded.

### QA Tracker (stage 3) — dashboard board, void, config  (this commit)

The admin oversight surface lands on the dashboard: a **QA Tracker** page
under **Dev** with the card board (expandable verdicts, void with clawback,
archive, jump links), a top-testers scoreboard, and the config knobs the cog
reads live (crew role, cards channel, reward, daily cap, enabled). Void and
archive also re-render the Discord card immediately through the in-process
bot; a Discord hiccup leaves the DB change intact and the card self-heals on
the next button click.

- [ ] The **QA Tracker** page appears under **Dev** for an admin and is
      absent (nav and API both) for a mod-only account.
- [ ] The board lists the live cards from `#testing-queue` with the correct
      status chips (colors matching the embeds); clicking a row expands its
      verdicts, and "Open in Discord" jumps to the card.
- [ ] Config saves (change the crew role) and the cog honors the new role on
      the next button click **without a restart**.
- [ ] Void a paid verdict → toast reports the clawed amount, the tester's
      `/bank` balance drops, and the Discord card re-renders (status/tally
      update, buttons stay).
- [ ] Archive a test → the card's verdict buttons disappear and the embed
      dims to the archived gray.
- [ ] Top testers shows the verdict counts and coins earned for everyone
      who's clicked so far.

---

## Done

### Chat Revive (stages 0–4) — retired unverified 2026-07-17  (superseded; /revive commands since removed)

New feature ("Ember"): a monitor loop learns each enabled channel's per-band
message rhythm from `processed_messages` and posts a bank question into a
genuinely unusual lull — never over an active room, never twice in a row,
never overnight. Migration 074 adds five `revive_*` tables **and a new
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


