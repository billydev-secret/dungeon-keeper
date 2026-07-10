# Games System — Feature Spec

A 19-game party-games suite. Every game is one slash command that spins up a public embed with buttons; players join and play in-place. Games share a per-channel session window for the `/session-recap` highlight reel, an admin allowlist, a per-guild enable/disable matrix, an optional audit channel for anonymous content, a question bank with AI-generation fallback for prompts, and a dashboard for content authoring. The companion 1-v-1 nickname duel is a separate feature — see [[pressure-cooker-spec]].

## Commands

### Games

| Game | Command | Permission | Notes |
|---|---|---|---|
| Free For All | `/ffa question:<str>` | Everyone | Anonymous reply modal posts via the bot |
| Truth or Dare | `/traditional single_choice:[bool]` | Everyone | SFW/NSFW Truth & Dare opt-in pools; `single_choice:true` makes each player pick exactly one category (radio-style) |
| Spin the Compliment | `/compliment` | Everyone | Derangement-paired giver → receiver |
| Marry, Fornicate, Kiss | `/mfk options:[csv]` | Everyone | `options:` overrides the three default labels |
| Would You Rather | `/wyr question:[a\|b]` | Everyone | Multi-round; per-round question queue |
| Never Have I Ever | `/nhie question:[str] lives:[0-10]` | Everyone | Lives mode (default 3); `lives:0` disables elimination |
| Most Likely To | `/mlt question:[str]` | Everyone | 3-player minimum; self-votes allowed |
| Two Truths & a Lie | `/twotruths prompt:[str]` | Everyone | Statements shuffled at display time |
| Hot Takes | `/hottakes` | Everyone | Anonymous submissions; 5-step temperature vote |
| Story Builder | `/story max_sentences:[2-30] visibility:[blind\|full] starter:[str]` | Everyone | Default 10 sentences, default blind |
| Anonymous AMA | `/ama mode:[unfiltered\|screened]` | Everyone | Long-running; nightly 24h sweep cleans up |
| Fantasies & Dealbreakers | `/fantasies` | Everyone | Anonymous submit + Same / Not-for-me vote, multi-round |
| Name Your Price | `/price rounds:[1-20] timer:[s] vote_timer:[s] source:[host\|players\|ai\|bank\|both]` | Everyone | $1–$999,999,999 prices, reveal sorted |
| Mt. Rushmore Draft | `/rushmore topic:[str] timer:[s] source:[host\|ai\|bank] vote_timer:[s]` | Everyone | Snake draft, 4 rounds, no duplicates |
| Clapback | `/clapback rounds:[1-15] timer:[s] vote_timer:[s] source:[ai\|bank\|both] anonymous:[bool]` | Everyone | Head-to-head matchups, unanimous winners get a CLAPBACK bonus |
| LegitLibs | `/legitlibs mode:[classic\|quiplash\|hotseat] tier:[1-4] template_id:[str] tag:[str]` | Everyone | Mad-Libs–style template fill; Hot Seat mode is stubbed |

### Meta & admin

| Command | Permission | Purpose |
|---|---|---|
| `/consent`, `/consent-status` | Everyone | Opt-in toggle (consent enforcement currently disabled) |
| `/session-recap` | Everyone | Highlights from the current channel's last 30 minutes of games |
| `/games-help`, `/games-support` | Everyone | Catalogue embed + support-server invite |
| `/games allow-channel | disallow-channel | list-channels` | Administrator | Channel allowlist for every game |
| `/games game-status | game-end` | Manage Server / Manage Channels / Administrator | Inspect or force-close the active game in the current channel |
| `/games audit-channel [channel]` | Administrator | Set or clear the audit log target for anonymous submissions |
| `/games portal-grant | portal-revoke | portal-list <user>` | Administrator | Dashboard sign-in allowlist |
| `/legitlibs-admin reload | cap-tier | preview | list` | Manage Server | LegitLibs content management |
| `/legitlibs-admin killswitch | enable` | Administrator | Module-level kill switch (in-memory; resets on restart) |

### Dashboard

The dashboard at `/api/games/*` mirrors the admin slash commands and adds full LegitLibs template authoring, question bank import/export, and AI prompt editing. Two permission tiers gate it: `mod` (Administrator / Manage Server) for config writes, and a Game Host tier (Administrator OR a configured editor role) for content authoring (bank, templates, AI generate, history, stats). Reads of AI prompt configuration are re-loaded per request so dashboard edits take effect mid-game without a restart.

## Behaviour

Every game follows the same skeleton: preflight (channel allowlisted? game enabled for this guild?), insert a "live game" row keyed by channel id, post the embed + view, and on close archive the game's final payload into a history table and free the live-game slot. Each channel can host at most one live game at a time. A 24-hour sweep closes orphaned games that nobody closed; closing copies the payload into the history archive and frees the slot.

The 19 games cluster by shape; each cluster shares interaction patterns.

### Question-bank / AI-augmented games

**Would You Rather, Never Have I Ever, Most Likely To, Mt. Rushmore Draft, Name Your Price, Clapback** — and AMA's question rewriting — draw prompts from a pre-seeded bank first, falling back to AI generation when the bank is empty for the requested game. The multi-round ones rotate through rounds: each round opens with a fresh question (from bank, AI, or a host-supplied queue), collects votes or submissions, closes the previous round's view, then opens the next. `/wyr` parses an optional `a | b` opening question; `/nhie` clamps `lives` to 0–10 and disables elimination when set to 0; `/rushmore`, `/price`, and `/clapback` show a live countdown timestamp on the embed that the host can skip with a button. `/clapback` pairs answers into head-to-head matchups with a special-case round-robin for 3-player games; a unanimous winner earns a "CLAPBACK!" bonus.

### Anonymous-submission games

**Free For All, Hot Takes, Fantasies & Dealbreakers, Anonymous AMA** post submissions to the play channel without the author's name attached. If an audit channel is configured for the guild, the same submission is mirrored there with the original author visible — staff can tie content back to a person without exposing them in the play channel. Without an audit channel, the audit step is a silent no-op. `/hottakes` runs in two phases (submit, then a 5-step temperature vote per take with a live results bar). `/fantasies` is multi-round; each round runs Submit → Reveal → Same/Not-for-me per entry, and the host can keep running rounds before the final recap. `/ama` is the largest and longest-running game: a per-question lifecycle (pending → answered / passed / rejected / expired), an approval queue in screened mode, DM notifications to the original asker when their question gets a reply, a 7-day retention window for unanswered screened-mode questions, and a hot-seat rotation that rejects stale submissions if the seat changed while the modal was open.

### Pool / pairing / draft games

**Spin the Compliment, Marry-Fornicate-Kiss, Truth or Dare, Two Truths & a Lie** open with a join-pool phase and a host-only "close pool" button that transitions to play. `/compliment` requires 2 players and produces a giver → receiver pairing with no fixed points; the public ping is auto-deleted after 15 seconds. `/mfk` requires 4 and gives each player a deterministic 3-name slice from the shuffled pool (never themselves); `options:` lets the host override the three category labels. `/traditional` toggles each player into any combination of four category pools (SFW Truth, SFW Dare, NSFW Truth, NSFW Dare) and weights target selection by least-asked count so one chatty player doesn't soak up turns. `/twotruths` collects three statements + the lie index per player via modal; statements are shuffled at display time and the room votes per player.

### Sequential storytelling

**Story Builder** builds a story sentence-by-sentence; the default "blind" visibility shows only the previous sentence in the modal while `full` shows everything so far. The host can skip a slow player. Max 30 sentences, default 10.

### LegitLibs

`/legitlibs` runs a Mad-Libs-style template fill in one of three modes. **Classic** fills blanks one at a time round-robin, with a volunteer rescue path when a player times out. **Quiplash** has every player fill in parallel and reveals one filled version per player at the end. **Hot Seat** is a stub today — the slash choice exists but the runner replies "Hot Seat mode coming soon!" and ends. Templates are picked by tier (1 Flirty → 4 Unhinged) with optional tag filtering, and the picker avoids the five most-recently-used templates per guild. Each channel has a `max_tier` cap; requesting a higher tier silently downgrades and warns the user ephemerally. An admin kill switch can disable the module guild-wide until re-enabled; the kill switch ends any active LegitLibs games. Per-blank fill prompts and example text are resolved through a fallback chain (most specific → bare part-of-speech) so even an under-specified template still renders a useful modal.

### Meta surfaces

`/session-recap` reads the current channel's session window — the past 30 minutes of finished games — and renders highlights based on each game's final payload: most divisive WYR question, guiltiest NHIE player, best TTL liar, hottest hot take, and so on. `/consent` toggles per-user opt-in (currently advisory; enforcement is disabled but the wiring is preserved). `/games-help` shows the full catalogue from the game name/icon/description map. `/games-support` posts a static support-server invite.

`/games game-end` force-closes the active game in the current channel and posts a "Game Force-Closed" notice; AMA additionally stops its per-question views.

### Close & archive

Every game's Close button opens a 30-second confirm popup ("Close this game?" → Yes/No). On confirm: the view disables, the game's final payload is copied into the history archive, and the live-game row is freed. A 24-hour sweep runs the same path for orphans.

## Permissions

**Bot needs:** Send Messages, Embed Links, Read Message History, Use External Emojis, Manage Messages (so `/compliment` can self-delete its public ping). The bot does **not** need Manage Nicknames — that's a pressure-cooker requirement only.

**User needs:**
- Everyone can run every game command, subject to the channel allowlist and per-guild enable flag.
- The Close button on any game's embed is host-only or Manage Server / Administrator.
- Channel allowlist, audit channel, portal grants, and the killswitch are Administrator.
- Game force-close (`/games game-end`) and game status are Manage Server / Manage Channels / Administrator.
- LegitLibs content moderation (`reload`, `cap-tier`, `preview`, `list`) is Manage Server; the kill switch is Administrator.
- Dashboard config writes need `mod`. Dashboard content authoring needs Administrator OR membership in the configured editor role.

## User-visible errors

| When | The user sees |
|---|---|
| Game runs in a non-allowlisted channel | Ephemeral: "This channel isn't set up for games. An admin can enable it with `/games allow-channel`." |
| Game is disabled for this guild | Ephemeral: "{Game} is currently disabled on this server." |
| `/wyr` opening question doesn't have two options | Ephemeral: "Question must have two options separated by `\|`, e.g. `fly \| be invisible`." |
| Question bank is empty AND AI generation failed | Channel message: "The question bank is empty! Use **Pose Question** to submit your own..." then the game closes |
| Bot lacks Send Messages / View Channel / Embed Links | Followup ephemeral: "I don't have access to send messages here. Please grant me View Channel, Send Messages, and Embed Links." |
| Non-host clicks a host-only button | Ephemeral: "Only the host or a mod can {action}." |
| LegitLibs killswitch is active | Ephemeral: "LegitLibs is currently disabled. Ask an admin to re-enable it." |
| LegitLibs tier exceeds the channel cap | Ephemeral warning: "This channel's tier cap is N ({label}). Using tier N instead." Then plays at the capped tier. |
| LegitLibs has no templates matching tier / tag | Ephemeral: "No published templates found for that tier/tag. Ask a mod to add some!" |
| AMA modal submitted after the hot seat rotated | Ephemeral: "The hot seat changed while you were typing — please try again." |
| AMA modal submitted after the game ended | Ephemeral: "The game closed while you were typing — your question was not submitted." |
| Audit channel was deleted | Silently swallowed; the game continues |
| AI generation API errors / times out | Falls back to bank-only or manual entry; user sees a one-line "AI generation failed" notice |
| Non-admin runs an admin command | Ephemeral: "You need administrator permissions to use this command." |

## Non-goals

- **No XP, economy, or other cross-system integration.** Game outcomes don't award XP and don't affect any other Dungeon Keeper feature.
- **No cross-channel state.** Each channel runs one game at a time; the session window is also per-channel.
- **No cross-guild leaderboards.** History is queryable per guild but no leaderboard surface exists.
- **No per-user game settings.** Configuration is per-guild or per-channel only.
- **No pre-game RSVP.** Players join by clicking a button on the live embed.
- **No inactivity timeouts** (except AMA's per-question lifecycle and the 24-hour orphan sweep). The host is trusted to close the game; the sweep is the safety net.
- **No spectator-only mode.** Anyone in the channel can vote and submit.
- **No matchmaking.** Pairings are random — no skill or history awareness.
- **No persistent leaderboards or seasons.** History is raw rows only.
- **No voice, music, or TTS integration.** Games are text + embed only.
- **Pressure Cooker is not part of this system.** It does not register a live-game row, does not share infrastructure, does not appear in `/games-help`, and is documented in [[pressure-cooker-spec]].
- **Consent enforcement is paused.** The opt-in flag is still recorded but no game blocks on it today.
- **LegitLibs Hot Seat mode is stubbed.** The choice appears in the picker; selecting it ends with a "coming soon" message.

## Configuration

### Per-guild

| Knob | Default | Purpose |
|---|---|---|
| Per-game `enabled` | on | Toggle a game on/off for the guild |
| Per-game `options` | empty | Free-form per-game knob bag (only a few games consume it) |
| Audit channel | unset | Mirror anonymous submissions here with original authors visible |
| Editor role | unset | Role whose holders pass the Game Host check on the dashboard |
| LegitLibs `max_tier` (per channel) | 4 | Hard cap on tier; higher requests silently downgrade |

### Per-channel

- Channel allowlist — only allowlisted channels accept any game command.

### Environment / files

- An Anthropic API key is required for AI question generation. Without it, AI fallback returns nothing and games default to bank-only or manual entry.
- AI prompt text per game is stored in a config file editable via the dashboard; reads are not cached, so dashboard edits take effect on the next game without a restart.
- A LegitLibs starter pack of templates is loaded once on each boot (idempotent — already-present templates are skipped).

### In-memory

- The LegitLibs kill switch is process-local — flipping it does not survive a restart.
- A per-game payload lock serialises mutations within one game; the lock is freed on game close.

## Stored data

Per-guild content (the seeded question bank, LegitLibs templates and their revision history, AI prompt overrides) plus per-game runtime state (the live game's payload, the session window, audit channel and editor-role settings, allowlisted channels, portal access grants) and an archive of every finished game's final payload. User opt-in flags are stored per-user; user ids also appear inside game payloads and the history archive.

LegitLibs additionally stores a small vocabulary table (parts of speech, domains, forms) and per-blank prompt text used to render fill modals, plus an anti-repeat window of the last few templates used per guild, channel tier caps, and user-submitted abuse reports on filled-in answers.

No DM data is stored. No filesystem cache outside the prompt config file and the LegitLibs seed file.
