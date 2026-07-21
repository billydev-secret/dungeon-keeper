# Games System — Feature Spec

An 18-mode party-games suite. Every mode is one subcommand under a single top-level `/games` command group: players start a game with `/games play <slug>` (e.g. `/games play ffa`), which spins up a public embed with buttons; players join and play in-place. Games share a per-channel session window for the `/recap` highlight reel, a per-channel allowlist and per-guild enable/disable matrix (both managed from the web dashboard), an optional audit channel for anonymous content, a question bank with AI-generation fallback for prompts, and a dashboard for content authoring. Bank draws are round-robin (least-recently-served row first, so a small pool doesn't repeat until every row has been served once — see **Question-bank / AI-augmented games**). Games also feed the economy — completing quest-relevant actions credits economy quest triggers (see **Economy integration**).

The companion 1-v-1 nickname duel **Pressure Cooker** and the dice game **Risky Rolls** appear in the shared game registry (`GAME_ICONS`/`GAME_NAMES`) but are **separate features** with their own entry points and specs — they are not part of this suite. See [[pressure-cooker-spec]].

> **Command surface.** Only the `games` group is registered to the command tree (`command_groups.py`). Party games hang off the nested `games play` subgroup; meta/admin commands hang directly off `games` or its `config`/`track` subgroups. The one exception is `/recap`, which is a top-level command. There is no bare `/ffa`, `/wyr`, … — those forms are historical and no longer exist.

## Current Behavior

### Games

All party games launch with `/games play <slug>`. Params below are the actual slash options.

| Game | Command | Permission | Notes |
|---|---|---|---|
| Anonymous Truth or Dare | `/games play ffa kind:[truth\|dare\|random] tags:[csv] prompt:[str]` | Everyone | Drops a T-or-D prompt; players reply anonymously via modal, posted by the bot |
| Truth or Dare Card | `/games play ffa_banner kind:[truth\|dare\|random] tags:[csv] prompt:[str]` | Everyone | Card-only variant of FFA — just posts a prompt card for open chat, no interactive state |
| Photo Challenge | *(no command — standalone)* | — | **Moved out of the games menu + shared scheduler.** Now a standalone dashboard feature (**Photo Challenge** nav → Setup & Schedule): one dedicated channel, its own recurring schedule, a ping role, an enabled toggle. Auto-posts a photo-prompt card on schedule; members post their shots in the channel. Prompts come from the shared bank (`game_type='photo'`); a post that earns enough distinct reactions feeds the economy photo-react quest. Config/schedule via `/api/photo-challenge` |
| Truth or Dare | `/games play traditional single_choice:[bool]` | Everyone | SFW/NSFW Truth & Dare opt-in pools; `single_choice:true` makes each player pick exactly one category (radio-style) |
| Spin the Compliment | `/games play compliment` | Everyone | Derangement-paired giver → receiver |
| Marry, Fornicate, Kiss | `/games play mfk options:[csv]` | Everyone | `options:` overrides the three default labels |
| Would You Rather | `/games play wyr question:[a\|b] tags:[csv]` | Everyone | Multi-round; `question` seeds an opening `a \| b`, else pulled from the bank |
| Never Have I Ever | `/games play nhie question:[str] lives:[0-10] tags:[csv]` | Everyone | Lives mode (default 3); `lives:0` disables elimination |
| Most Likely To | `/games play mlt question:[str] tags:[csv]` | Everyone | 3-player minimum; self-votes allowed |
| Two Truths & a Lie | `/games play twotruths prompt:[str]` | Everyone | Statements shuffled at display time; resubmit allowed until your round is revealed; optional per-round vote timer (dashboard `vote_timer`, 0 = host advances) |
| Hot Takes | `/games play hottakes` | Everyone | Anonymous submissions; 5-step temperature vote |
| Story Builder | `/games play story max_sentences:[≤30] visibility:[blind\|full] starter:[str]` | Everyone | Default 10 sentences, default blind |
| Anonymous AMA | `/games play ama mode:[unfiltered\|screened] format:[hot_seat\|panel]` | Everyone | Two independent axes (see below); long-running; nightly 24h sweep cleans up |
| Fantasies & Dealbreakers | `/games play fantasies` | Everyone | Anonymous submit + Same / Not-for-me vote, multi-round |
| Name Your Price | `/games play price source:[host\|players\|ai\|bank\|both]` | Everyone | $ prices, reveal sorted; round/timer knobs live on the lobby buttons, not slash args |
| Mt. Rushmore Draft | `/games play rushmore topic:[str] source:[host\|ai\|bank] mode:[snake\|blitz]` | Everyone | 4 rounds, no duplicates; snake (turn-by-turn) or blitz (everyone picks at once, first-come wins dupes); 60s post-draft backfill for skipped slots |
| Clapback | `/games play clapback start_in:[1-60]` | Everyone | Head-to-head matchups; `start_in` shows a lobby countdown (host still clicks Start). Unanimous winners get a CLAPBACK bonus |
| LegitLibs | `/games play legitlibs mode:[classic\|quiplash] tier:[1-4] template_id:[str] tag:[str]` | Everyone | Mad-Libs template fill; tiers 1 Flirty / 2 Spicy / 3 Filthy / 4 Unhinged, default tier 2 |

That is **18 `/games play` commands** (Anonymous AMA's two axes are one command). `ffa_banner` is a card-only variant of FFA, so counting distinct games it is 17 plus the banner variant.

### Meta & admin commands

| Command | Permission | Purpose |
|---|---|---|
| `/recap` | Everyone | Highlights from the current channel's last 30 minutes of games |
| `/games help` | Everyone | Catalog embed built from the game name/icon/description registry |
| `/games support` | Everyone | Static support-server invite |
| `/games end` | Host or Mod/Admin | End the active game in this channel (confirm popup). AMA additionally tears down its per-question views |
| `/games join [user]` · `/games leave [user]` | Self, or Host/Mod/Game-Host to move others | Add/remove yourself (or, with elevation, someone else) in a running game that has a roster. Open-submission games reply that there's nothing to join |
| `/games config game-status` | Mod/Admin (Manage Server or Administrator) | Inspect the active game in the current channel |
| `/games config game-end` | Mod/Admin | Force-close the active game and post a "Game Force-Closed" notice |
| `/games track watch <channel> <bot>` | Mod/Admin | Watch a channel + bot and start banking its game-result messages (external Gamebot tracking) |
| `/games track status` | Mod/Admin | Show tracking state, watched channel/bot, and messages banked |
| `/games track disable` · `/games track enable` | Mod/Admin | Pause / resume banking (data is retained while paused) |
| `/games track sample [channel] [count]` | Mod/Admin | Dump recent bot messages (raw content + embeds) as JSON to confirm the format |
| `/games dev fill` · `/games dev answer` | Dev/testing only | Populate a lobby with fake players / submit fake Clapback answers — a developer surface, not a player command |

`/games track *` is a format-agnostic collector: an `on_message`/`on_message_edit` listener banks every message from the watched channel+bot RAW (keyed on message id, de-duplicated across restarts/edits) into `games_external_messages`. Nothing is parsed at ingest — metrics are derived later, so a format change never loses history. This is collection infrastructure toward our own leaderboards for games we don't run; there is no leaderboard surface yet.

### Dashboard-managed configuration

These settings are **live and enforced** but are configured from the web dashboard (`/api/games/*`), **not** slash commands. There are no slash commands to manage them.

- **Channel allowlist** (`games_allowed_channels`). Every game preflights `check_allowed_channel`; a channel that isn't on the allowlist refuses all games. Managed via the dashboard channels panel.
- **Per-guild per-game enable/disable** (`games_game_config`, default enabled). Checked by `check_game_enabled`.
- **Audit channel** (`games_audit_channel`). When set, anonymous submissions are mirrored there with the original author visible.
- **Game Host / editor role** (`games_editor_role`). Holders pass the Game-Host check for content authoring on the dashboard and can add/remove other players via `/games join|leave`.
- **LegitLibs per-channel tier cap** (`legitlibs_channel_config.max_tier`, default 4), set per-row on the Games Config → Allowed Channels table, and LegitLibs template/vocabulary content.

### Dashboard

The dashboard mirrors the config surfaces above and adds full LegitLibs template authoring, question-bank import/export, and AI-prompt editing. Banks also share a **global pool** — bank rows stored under the reserved `global` game type, which gameplay never selects. Every bank manager has a per-question *Pool* button (copy to the pool; duplicate texts are skipped, and Traditional's category tags are translated to the generic `nsfw` tag or dropped) and a pool browser that imports selected pool questions into that game's bank (duplicates skipped; Traditional requires choosing the category the imports are filed under, other games carry the pool tags over). Two permission tiers gate it: `mod` (Administrator / Manage Server) for config writes, and a Game Host tier (Administrator OR the configured editor role) for content authoring (bank, templates, AI generate, history, stats). AI-prompt config is re-loaded per request so dashboard edits take effect mid-game without a restart.

## Behavior

Every interactive game follows the same skeleton: preflight (channel allowlisted? game enabled for this guild?), insert a "live game" row keyed by channel id, post the embed + view, and on close archive the game's final payload into a history table and free the live-game slot. Each channel can host at most one live game at a time. A 24-hour sweep closes orphaned games that nobody closed; closing copies the payload into the history archive and frees the slot. (Photo Challenge is fire-and-forget — it records a history row for stats but keeps no interactive state, since people just reply in the channel.)

The games cluster by shape; each cluster shares interaction patterns.

### Question-bank / AI-augmented games

**Would You Rather, Never Have I Ever, Most Likely To, Mt. Rushmore Draft, Name Your Price, Clapback** — and AMA's question rewriting — draw prompts from a pre-seeded bank first, falling back to AI generation when the bank is empty for the requested game (except Clapback, which is bank-only). Bank draws are round-robin, not pure-random: each row tracks when it was last served and selection prefers the least-recently-served match (ties broken at random), so a small pool doesn't repeat a question until every row has been served once — including across separate game sessions. The multi-round ones rotate through rounds: each round opens with a fresh question (from bank, AI, or a host-supplied queue), collects votes or submissions, closes the previous round's view, then opens the next. `wyr` parses an optional `a | b` opening question; `nhie` clamps `lives` to 0–10 and disables elimination when set to 0; `rushmore`, `price`, and `clapback` show a live countdown timestamp the host can skip with a button. `rushmore` drafts in one of two modes (slash `mode:` arg or dashboard option; default snake): **snake** pings each player on their turn — the ping (and the 10-second nudge) carries its own **Make Your Pick** button so nobody scrolls back to the board — while **blitz** has everyone with an empty slot pick simultaneously each round, duplicates resolved first-come with an ephemeral "taken, try again". After the draft, skipped slots get a 60-second **backfill window** (own button, duplicates still blocked) before boards go final; boards that are still all-skip are hidden from the FINAL BOARDS embed and excluded from the vote. `clapback` pairs answers into head-to-head matchups with a special-case round-robin for 3-player games; a unanimous winner earns a "CLAPBACK!" bonus. Bank lookups are NSFW-gated on the channel (`channel_allows_nsfw`), so NSFW prompts only surface in age-gated channels.

### Anonymous-submission games

**Anonymous Truth or Dare (FFA), Hot Takes, Fantasies & Dealbreakers, Anonymous AMA** post submissions to the play channel without the author's name attached. If an audit channel is configured for the guild, the same submission is mirrored there with the original author visible — staff can tie content back to a person without exposing them in the play channel. Without an audit channel, the audit step is a silent no-op. `hottakes` runs in two phases (submit, then a 5-step temperature vote per take with a live results bar). `fantasies` is multi-round; each round runs Submit → Reveal → Same/Not-for-me per entry, and the host can keep running rounds before the final recap.

`ama` is the largest and longest-running game, with **two independent axes**:

- **Content mode** — `unfiltered` (questions post immediately) or `screened` (host approves via DM before the question appears; rejected questions never post and never pay out quest credit).
- **Format** — `hot_seat` (one person answers at a time; the seat rotates) or `panel` (ask anyone who has opted into the panel, chosen from a dropdown).

AMA carries a per-question lifecycle (pending → answered / passed / rejected / expired), a screened-mode approval queue, DM notifications to the original asker when their question gets a reply, a retention window for unanswered screened questions, and stale-target guarding: a modal submitted after the hot seat rotated (or after the target left the panel) is rejected with a "please try again" notice.

### Pool / pairing / draft games

**Spin the Compliment, Marry-Fornicate-Kiss, Truth or Dare (traditional), Two Truths & a Lie** open with a join-pool phase and a host-only "close pool" button that transitions to play. `compliment` requires 2 players and produces a giver → receiver pairing with no fixed points; the public ping is auto-deleted after 15 seconds. `mfk` requires 4 and gives each player a deterministic 3-name slice from the shuffled pool (never themselves); `options:` lets the host override the three category labels. `traditional` toggles each player into any combination of four category pools (SFW Truth, SFW Dare, NSFW Truth, NSFW Dare) and weights target selection by least-asked count so one chatty player doesn't soak up turns. Its host-only **Bank Round** button deals every opted-in player one question from the web-managed bank; bank questions share the same per-(player, category) asked history as host-written ones, so each player is served at most once per opted-in category and re-pressing after new players join only serves the newcomers (the host summary reports how many already-asked players were skipped). Like the other bank-backed games, the draw itself is round-robin (least-recently-served row first) so a category's pool doesn't repeat a question across separate games until every row in it has been served. `twotruths` collects three statements + the lie index per player via a components-v2 modal — the full prompt renders as static text inside it (the 45-char title used to truncate it), and the prompt is repeated on every round's guess embed for mid-game joiners. Statements are shuffled at display time and the room votes per player. Players can resubmit (the modal prefills their previous entry) via **Submit Statements** or the mid-game **Join / Edit** button until their own round is revealed; after that, statements are locked (`played` list in the payload). Only the lobby message's player roster is ever edited on submit — the modal never touches the active guess embed (2026-07-20 fix: a mid-game join used to overwrite statement 1's field). Rounds advance on the host's **Next** button, or automatically when the dashboard `vote_timer` option is set (>0 seconds; default 0 keeps host pacing). The recap's "fooled the fewest" award is **🪞 Open Book** (with fooled count); Best Liar and Best Guesser both earn the economy game-win bonus.

### Sequential storytelling

**Story Builder** builds a story sentence-by-sentence; the default "blind" visibility shows only the previous sentence in the modal while `full` shows everything so far. The host can skip a slow player. Max 30 sentences, default 10.

### LegitLibs

`legitlibs` runs a Mad-Libs-style template fill in one of **two** modes. **Classic** fills blanks one at a time round-robin, with a volunteer rescue path when a player times out. **Quiplash** has every player fill in parallel and reveals one filled version per player at the end. Templates are picked by tier (1 Flirty → 4 Unhinged, default 2) with optional tag filtering, and the picker avoids the five most-recently-used templates per guild. Each channel has a `max_tier` cap (dashboard-managed); requesting a higher tier silently downgrades and warns the user ephemerally. Per-blank fill prompts and example text are resolved through a fallback chain (most specific → bare part-of-speech) so even an under-specified template still renders a useful modal.

### Meta surfaces

`/recap` reads the current channel's session window — the past 30 minutes of finished games — and renders highlights based on each game's final payload: most divisive WYR question, guiltiest NHIE player, best TTL liar, hottest hot take, and so on. `/games help` shows the full catalog from the game name/icon/description registry. `/games support` posts a static support-server invite.

### Close & archive

Every game's Close/End path opens a confirm popup ("Are you sure you want to end this game?" → Yes/No). On confirm: the view disables, the game's final payload is copied into the history archive, and the live-game row is freed. `/games end` (host or mod) and `/games config game-end` (mod only) both run this teardown; AMA runs its extra view/message cleanup first so nothing is orphaned. A 24-hour sweep runs the same path for orphans.

## Coin wagers (duel + group games)

All six duel/group games (Pressure Cooker, Quickdraw, Hot Potato, Hot Potato
Group, Chicken, Musical Chairs) accept an optional `wager:` amount on their
challenge/start command: equal ante from every player, winner takes the pot,
**no rake**. Escrow lives in `econ_game_wagers` (economy side, migration 094)
and settles through the shared terminal-state seam — see
`docs/economy_spec.md` §6 and `docs/plans/economy-sinks-round-2.md` stage 4b.
The rule that inverts this module's usual one: `pay_game_rewards` swallows
every error because economy must never block game flow, but an escrow
**debit** raises and refuses the join/accept — you cannot enter a wagered
game you can't pay for.

A wager replaces the nickname stake: a wagered game with no custom `stakes:`
text records "Coins on the line — winner takes the pot." as its stakes and
resolves announce-only — no rename button, no nickname preflight. The rename
flow only runs when neither `stakes:` nor `wager:` is given (the default
"name" stake); see `docs/dk_pvp_games_suite_spec.md` §4.

## Economy integration

Games are wired into the economy quest system. Quest-relevant actions call `fire_member_trigger` (`bot_modules.economy.game_rewards`) to credit a member's economy quest progress — for example AMA credits an `ama_ask` trigger when a question actually becomes visible (on submit in unfiltered mode, on host approval in screened mode; AI-seeded idle questions and rejected questions never pay). Photo Challenge payout is handled by the economy directly (`EconomyCog._on_photo_post`): a member's image post in the configured photo channel pays the `photo_post` quest on the post itself — no reactions needed — the card itself just sets the prompt. Several other game cogs (`ffa`, `clapback`, `mlt`, `price`, `rushmore`, `traditional`, `nhie`, `wyr`) import the same game-rewards path. Credit is best-effort: an economy failure never unwinds a game.

## Permissions

**Bot needs:** Send Messages, Embed Links, Read Message History, Attach Files (Photo Challenge and card renders), Use External Emojis, Manage Messages (so `/games play compliment` can self-delete its public ping). The bot does **not** need Manage Nicknames — that's a Pressure Cooker requirement only.

**User needs:**
- Everyone can run every game command, subject to the channel allowlist and per-guild enable flag.
- Ending a game (`/games end`) is allowed for the game's host or a Mod/Admin. `/games config game-status` and `/games config game-end` require Mod/Admin (Manage Server or Administrator).
- `/games join`/`/games leave` are self-service; adding or removing *another* player requires the host, a Mod/Admin, or the configured Game-Host role.
- `/games track *` requires Mod/Admin.
- Dashboard config writes need `mod`; dashboard content authoring needs Administrator OR the configured editor role.

## User-visible errors

| When | The user sees |
|---|---|
| Game runs in a non-allowlisted channel | Ephemeral: "This channel isn't set up for games. An admin can enable it from the web dashboard." |
| Game is disabled for this guild | Ephemeral: "{Game} is currently disabled on this server." |
| `/games play wyr` opening question doesn't have two options | Ephemeral: question must have two options separated by `|` |
| Question bank is empty AND AI generation failed | The game posts a "bank is empty" notice and closes (or, for scheduled/launch-only games like Photo and Clapback, the run is skipped) |
| Bot lacks send/attach/view permissions | Followup ephemeral perms hint listing the missing permissions |
| Non-host/non-mod clicks a host-only button, or tries `/games end` without rights | Ephemeral: only the host or a moderator can do that |
| Adding/removing another player without elevation | Ephemeral: only the host, a moderator, or a Game-Host-role holder can add or remove other players |
| LegitLibs tier exceeds the channel cap | Ephemeral warning that the channel's cap is lower; plays at the capped tier |
| LegitLibs has no templates matching tier / tag | Ephemeral: no published templates for that tier/tag; ask a mod to add some |
| AMA modal submitted after the hot seat rotated / target left the panel | Ephemeral: the seat/panel changed while you were typing — try again |
| AMA modal submitted after the game ended | Ephemeral: the game closed while you were typing — your question was not submitted |
| Audit channel was deleted | Silently swallowed; the game continues |
| AI generation API errors / times out | Falls back to bank-only or manual entry |
| Mod-only config/track command run without rights | Ephemeral: you need moderator or admin permissions |

## Non-goals

- **No cross-channel state.** Each channel runs one game at a time; the session window is also per-channel.
- **No cross-guild or in-app leaderboards yet.** History and external-game messages are collected per guild, but no leaderboard surface is rendered.
- **No per-user game settings.** Configuration is per-guild or per-channel only.
- **No pre-game RSVP.** Players join by clicking a button on the live embed.
- **No mid-game inactivity timeouts** except AMA's per-question lifecycle, the Clapback lobby's 10-minute quiet-timeout (the lobby message greys out to "Lobby timed out"), and the 24-hour orphan sweep. The host is trusted to close the game; the sweep is the safety net.
- **No matchmaking.** Pairings are random — no skill or history awareness.
- **No persistent seasons.** History is raw rows only.
- **No voice, music, or TTS integration.** Games are text + embed only.
- **Pressure Cooker and Risky Rolls are not part of this suite.** They share the game registry for naming/icons but have their own entry points and infrastructure; Pressure Cooker is documented in [[pressure-cooker-spec]].

> **Note — economy is no longer a non-goal.** Earlier versions of this spec claimed games have "no XP/economy integration." That is now false: games credit economy quest triggers and Photo Challenge registers reply-cards. See **Economy integration**.

## Configuration

### Per-guild (dashboard)

| Knob | Default | Purpose |
|---|---|---|
| Per-game `enabled` | on | Toggle a game on/off for the guild |
| Per-game `options` | empty | Free-form per-game knob bag (only a few games consume it, e.g. Photo's ping role) |
| Audit channel | unset | Mirror anonymous submissions here with original authors visible |
| Editor / Game Host role | unset | Role whose holders pass the Game Host check on the dashboard and can move other players |
| External tracking watch | unset | Channel + bot whose result messages are banked (`/games track`) |

### Per-channel (dashboard)

- **Channel allowlist** — only allowlisted channels accept any game command.
- **LegitLibs `max_tier`** — hard cap on tier (default 4); higher requests silently downgrade.

### Environment / files

- An Anthropic API key is required for AI question generation. Without it, AI fallback returns nothing and games default to bank-only or manual entry.
- AI-prompt text per game is stored in a config file editable via the dashboard; reads are not cached, so edits take effect on the next game without a restart.
- A LegitLibs starter pack of templates is loaded once per boot (idempotent — already-present templates are skipped).

### In-memory

- The external-tracking watch cache (`guild → channel+bot`) is warmed on load and kept in sync by the `/games track` commands so the `on_message` hot path never touches the DB.
- A per-game payload lock serialises mutations within one game; the lock is freed on game close.

## Stored data

Per-guild content (the seeded question bank, LegitLibs templates and their revision history, AI-prompt overrides) plus per-game runtime state (the live game's payload, the session window, audit channel and editor-role settings, allowlisted channels) and an archive of every finished game's final payload. External-tracking config and raw banked bot messages are stored per guild. User ids appear inside game payloads and the history archive. Photo Challenge registers card metadata for the economy photo-reply quest.

LegitLibs additionally stores a small vocabulary table (parts of speech, domains, forms) and per-blank prompt text used to render fill modals, plus an anti-repeat window of the last few templates used per guild, channel tier caps, and user-submitted abuse reports on filled-in answers.

No DM data is stored. No filesystem cache outside the prompt-config file and the LegitLibs seed file.

## Not Yet Built / Roadmap

The following were described as current behavior in earlier versions of this spec but are **not implemented today**. The underlying *capabilities* they described mostly still exist — they're just reached a different way now (usually the dashboard), or were removed outright. They're recorded here so the design intent isn't lost.

### Never-built or removed command surfaces

- **`/games allow-channel | disallow-channel | list-channels`** — no such slash commands exist. Channel allowlisting is real and enforced, but is managed from the web dashboard. Only the *slash-command interface* was ever documented; it was never wired. (The embed/logic helpers for these strings still linger in `games_config/logic.py` and `embeds.py` but nothing calls them.) A future slash interface for allowlisting is a reasonable roadmap item.
- **`/games audit-channel [channel]`** — likewise unbuilt as a slash command. The audit channel is a live feature, set from the dashboard (`games_audit_channel`).
- **`/games portal-grant | portal-revoke | portal-list`** — removed. The `games_portal_access` table these wrote was dropped in migration `041_drop_games_portal_access.sql`. Dashboard/Games access is now governed solely by Discord admin/mod permissions plus the `games_editor_role` (Game Host role) — the editor role **supersedes** the old portal sign-in allowlist concept.
- **`/legitlibs-admin reload | cap-tier | preview | list | killswitch | enable`** — no such commands exist. LegitLibs content management, tier caps, and template preview live in the dashboard. The module-level **kill switch** described in older specs has been removed entirely (no `killswitch` code remains). LegitLibs tier caps are configured per channel from the dashboard, not via a slash command.

### Removed gameplay concepts

- **Consent system (`/consent`, `/consent-status`).** Fully deleted from the codebase — only an empty `games_consent/__pycache__/` remains. There is no per-user opt-in flag and no game blocks on consent. If per-user consent gating is ever revived, it would be a new build, not a re-enable of paused wiring. (Note: unrelated "consent" systems exist elsewhere — DM permissions and Rules Watch — and are *not* part of the games suite.)
- **LegitLibs Hot Seat mode.** Older specs listed a third "Hot Seat" LegitLibs mode as a stub. It has been removed; only `classic` and `quiplash` exist today. A one-at-a-time LegitLibs variant could be revisited as a roadmap item.

### Collection-without-surface

- **External game leaderboards.** `/games track *` and the `games_external_messages` collector are built and banking raw messages, explicitly to power our own leaderboards/streaks for games we don't run (e.g. Gamebot CAH). The parser and leaderboard surface are not built yet — this is the next stage.
