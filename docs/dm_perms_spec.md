# DM Permissions — Feature Spec

A consent gate for Discord DMs **within this community**. Every member carries one of three "DM mode" roles (Open / Ask / Closed). When someone wants to contact an Ask-mode member, they open a request via a persistent server panel; the target gets a DM with Accept / Deny buttons. Accept records a **bidirectional consent pair**, both sides get a confirmation DM, and either side can revoke at any time. Every state transition writes to an audit log and optionally fan-outs to a mod-visible audit channel.

This is **not** a Discord-friend system — it lives entirely inside the bot. The only Discord-side artifacts are role assignments and the DM messages carrying the buttons.

## Surface

| Command | Type | Permission | Purpose |
|---|---|---|---|
| **Open DM Request Form** button | Persistent panel | Everyone | Opens the recipient picker + request modal |
| **My DM Settings** button | Persistent panel | Everyone | Opens the ephemeral settings panel below |
| Mode buttons (Open / Ask / Closed) | Ephemeral settings panel | Everyone | Assign yourself one of the `DMs: Open / Ask / Closed` roles; the active one is marked |
| User picker | Ephemeral settings panel | Everyone | "✅ connected" / "❌ no connection yet" check against the picked member |
| **Remove connection** button | Ephemeral settings panel | Everyone | Remove an existing consent pair; only shown when one exists |
| **Accept** / **Deny** buttons | Persistent in-DM | Target only | Routes via the message id back to the pending request |
| DM config | Web (dashboard) | Admin | Set the request channel and the audit channel |
| Post panel | Web (dashboard) | Admin | Force-(re)post the panel into a chosen channel |
| DM audit log | Web (dashboard) | Admin | Paginated audit-log browser with optional action/type filters |

## Behavior

### The three DM modes
- **Open** — anyone can DM. No request needed.
- **Ask** — a request is required; the target chooses to accept or deny.
- **Closed** — no requests possible; the system refuses up front.

Picking a mode removes the other two role assignments and grants the chosen one, against the member's roles **as of that click** — the settings panel re-seats its cached member on every press, since a second change in one sitting would otherwise compute the removal from the role set the panel opened with. If a member somehow ends up with multiple DM-mode roles (race, manual edit), the role added in that same update — the one the member just chose — is kept and the rest are stripped; a duplicate with no such role falls back to keeping the highest-position one. Every mode change writes `mode_set` to the audit log with `mode=<open|ask|closed>` in its notes.

### Sending a request
The persistent panel's **Open DM Request Form** button opens an ephemeral picker (user-select + DM-or-friend-request type buttons + Continue, 5-minute timeout). Continue opens a modal with one optional reason field, capped at **250 characters**. Switching the type re-renders the picker, so the chosen user is re-sent as the select's default value and stays selected.

On submit the system pre-checks:
- Target is in the guild.
- Target isn't the requester (no self-requests).
- Target isn't a bot.
- Target isn't already Open (no request needed) or Closed (refused).
- No existing consent pair (no duplicate-requesting).
- No existing pending request from the same requester to the same target.

A requester can hold at most **5 pending requests** at once.

When the pre-checks pass, the target is DM'd a request embed with Accept / Deny buttons. The requester gets a confirmation DM. If the target's DMs are closed, nothing is persisted — the requester is told and that's the end of the attempt.

### Accept / Deny / Expire
The DM's buttons survive bot restarts; per-request state is recovered via the DM message id.

- **Accept** records the consent pair (bidirectional), edits the DM in place with the acceptance embed, DMs both sides the same confirmation, and writes `request_accepted` to the audit log.
- **Deny** edits the target's DM to a denial embed, DMs the requester a denial DM (with the lowercased type label inside the sentence), and writes `request_denied`.
- **Expire**: pending requests age out after **24 hours**. A background sweep runs hourly (and once at startup, so requests aged-out during downtime are swept on next boot), flips them to `expired`, DMs the requester an expiry notice, and writes `request_expired`. Aged-out rows are retained as evidence.

If the bot has been removed from the guild between the request being sent and the click, the target's DM is replaced with a "this guild is no longer available" embed and the request row is dropped.

The panel auto-bumps to the bottom of its channel on any new message there, debounced to one bump per **2 seconds** per guild to avoid thrashing during busy periods.

The panel is also **posted automatically on boot** into each guild's configured panel channel. Since the 2026-07-28 command-surface audit it is the only route to a member's own DM settings, so it can't depend on an admin remembering to press "post panel" on the dashboard. The bootstrap uses `place_or_refresh`, which edits in place when the panel is already the configured channel's — restarts refresh it rather than stacking duplicates. A guild with no configured panel channel is skipped (there is nowhere to put it); a configured channel that has been deleted or is no longer a text channel logs a warning and is skipped. The dashboard's **Post panel** action still exists for moving the panel to a different channel.

### Revoke
Either party can remove the connection from the settings panel (pick the member, then **Remove connection**). The consent pair is removed; if the original request DM is still on file, it gets edited in place with a revoke embed (buttons cleared). Both sides receive a revoke DM; the panel is ephemeral, so nothing about the revoke is visible in the channel. The audit log records `relationship_revoked` with the actor's name.

### Status check
The settings panel's status line is a one-line "connected" or "no connection yet" lookup against the in-memory consent map — it does not surface the original reason, who initiated, or when. Every button on the panel redraws it, so the member being looked at is re-sent as the select's default value and stays selected — the status line and the **Remove connection** button both name that person, and an emptied select next to them read as a bug.

### Mod audit
The dashboard's audit log lists every state transition: requested, accepted, denied, expired, revoked, and a member setting their own DM mode. Optional filters: action name and request type (DM vs friend-request label). The audit channel — if configured — also receives a one-line embed for each event in real time.

## Permissions

- The bot needs **Manage Roles** to create and assign the three DM-mode roles, with its top role above them. Since 2026-08-22 they are provisioned through `core/role_provision.py`: an existing role of the same name is adopted rather than twinned, the created roles carry no permissions, and a Discord failure (a rate limit, a 5xx) now degrades to leaving the member's current modes alone instead of escaping into their button click; **Send Messages** + **Embed Links** in the panel channel and the audit channel; **Read Message History** in the panel channel for the bump-to-bottom guard.
- The settings panel is guild-only (it reads mode roles) and refuses to open from a DM; no other Discord-side gate.
- The panel button is open to everyone; Accept / Deny hard-check that the clicker is the target.
- Dashboard endpoints require the **admin** role.
- DM delivery to the target is best-effort — Discord-side, not bot-grantable. A target with closed DMs can't be reached at all.

## User-visible errors

| When | The user sees |
|---|---|
| Target is Open mode | "X has their DMs open — no request needed." |
| Target is Closed mode | "X isn't accepting DM requests right now." |
| Requester targets themselves | "You can't send a request to yourself!" |
| Target is a bot | (request rejected with a "can't target a bot" message) |
| Already connected | "You two already have a connection — no need to request again." |
| Already a pending request to that target | "You already have a pending request to them." |
| At the 5-pending cap | "You already have N pending DM requests. Wait for some to be answered or expire (max 5)." |
| Target's DMs are closed | "I couldn't DM that user — they may have DMs disabled." |
| Non-target clicks Accept / Deny | "This request isn't for you." |
| Accept/Deny click on an already-resolved request | (DM edits to "this request has already been resolved or expired") |
| Bot removed from the guild between request and click | (DM edits to "this guild is no longer available to the bot") |
| Accept when one member has left | "Couldn't find one or both users in this server." |
| Mode change fails on role permission | "I don't have permission to manage roles here." |
| Remove connection with no existing pair | "You don't have a connection with X." |
| Panel refresh fails | "Couldn't refresh the panel — I may not have permission to post in that channel." |
| Status check, no connection | "❌ no connection yet" |

## Non-goals

- **No Discord-friend integration.** A "Friend Request" type is just a label on the consent pair; the bot cannot send platform-level friend requests.
- **No per-channel scoping.** Consent is guild-wide; revoke is all-or-nothing.
- **No specific-user blocklist.** Refuse via Deny, or switch to Closed.
- **No mod-side force-disconnect.** Mods can audit but cannot retroactively break two members' consent.
- **No expiry on accepted pairs.** Once accepted, a pair survives until revoked.
- **No retraction by the requester.** A pending request can't be cancelled — wait for the target to act or for the 24-hour expiry.
- **No offline-revoke notification beyond the best-effort DM.** If the revoke DM fails, only the audit log records it.

## Configuration

Per-guild settings an admin chooses via the dashboard:

- **Request channel** — where the persistent panel lives.
- **Audit channel** — where one-line audit posts fan out (optional; events still persist when unset).

The panel message id is bot-managed (written when the panel is posted or bumped) and not user-editable.

Built-in behavioral constants (not user-tunable): pending requests expire after 24 hours, the expiry sweep runs hourly, requesters cap at 5 simultaneous pending requests, reason fields cap at 250 characters, and the panel-bump debounce is 2 seconds per guild.

## Stored data

Per guild: the live consent map (one row per **pair**, with the request type, optional reason, and a pointer to the originating DM so revoke can edit it in place), and every pending request (requester, target, type, reason, message id, timestamp).

Per event: the audit log records every asked / accepted / denied / expired / revoked transition with actor, both parties, and a free-form notes field carrying the request type. Accepted and denied requests delete the pending row; expired requests are kept with a flipped status. Revoked pairs are hard-deleted from the consent table — only the audit log retains evidence the pair existed.

In-memory and rebuilt on restart: a per-guild consent set (both orderings, for O(1) mutual checks), the pending request map keyed by requester+target, panel-bump locks, and the background expiry task.
