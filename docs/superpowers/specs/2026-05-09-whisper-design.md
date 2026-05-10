# Whisper — Dungeon Keeper Module Spec

A Whisper-style anonymous-message-with-guessing game. Members send anonymous messages to each other; the bot announces in a public feed channel and DMs the target the actual content. The target gets 3 guesses to identify the sender. Mod log of all whispers is mandatory.

Inspired by the Whisper Discord bot (`whsper.me`), with a 3-guess variant instead of the canonical 1-guess.

## Core Concepts

- **Whisper role**: Per-server opt-in role. Required to send whispers AND to be a valid target. Users opt in/out via slash command. Members without the role do not appear in the `/whisper` autocomplete and cannot send.
- **Whisper feed channel**: The public channel where the bot posts whisper announcements (target mention, no content) and persistent `[Send Whisper] [Check Whispers] [Check Hidden Whispers]` buttons.
- **Whisper DM**: When a whisper is sent, the bot DMs the target with the message content and per-whisper action buttons `[Guess] [Share] [Hide]`.
- **Whisper state machine**: `pending` → `shared` (made public) or `hidden` (kept off the public feed). Whispers are independently `solved` once a correct guess is made; solving and sharing are orthogonal.
- **Mod log**: Every whisper is logged to a configured mod channel with the sender's real identity, target, and content. Non-optional.

## Configuration (per-guild)

All configuration is managed via the existing **web admin panel**, not slash commands. Stored in the existing Dungeon Keeper guild config table; namespace keys under `whisper.*`.

- `whisper.channel_id` (required) — public feed channel
- `whisper.log_channel_id` (required) — mod log channel
- `whisper.role_id` (required) — opt-in role gating send and receive

A whisper cannot be sent in a guild where any of the three keys are missing.

## Slash Commands

### `/whisper <target: user> <message: string>`
- **Permission**: Sender must have `whisper.role_id`.
- **Validation**:
  - Sender ≠ target.
  - Target must have `whisper.role_id`.
  - Message non-empty, ≤ 1000 chars.
  - Bot must be able to DM the target.
- **Autocomplete on `target`**: Restricted to opted-in members.
- **Flow**:
  1. Defer ephemerally.
  2. Insert `whispers` row in `pending` state with `guesses_left = 3`, `solved = false`.
  3. Post announcement in `whisper.channel_id`: "Someone sent @Target an anonymous message" + the persistent feed buttons. Save `channel_msg_id`.
  4. DM target: "Someone in [Server] sent you a secret message:\n```[message]```" with `[Guess] [Share] [Hide]` buttons. Save `dm_msg_id`.
  5. Post mod log entry to `whisper.log_channel_id` with sender, target, content.
  6. Ephemeral confirm to sender: "Whisper delivered."

### `/whisper optin`
- Confirmation modal: "By opting in, you'll be able to send whispers and receive them from other opted-in members. You can opt out anytime."
- On confirm: grant `whisper.role_id` to invoker.

### `/whisper optout`
- Removes `whisper.role_id` from invoker.
- **Behavior for active whispers where invoker is sender or target**: rows are NOT deleted. Sender can still be guessed (their identity is preserved in the row regardless of role state). Target can still interact with received whispers in DM.

## Persistent Buttons

All buttons use stable `custom_id` encodings so they survive bot restarts. View handlers are registered on cog load.

### Feed channel buttons (attached to every whisper announcement)
Each whisper announcement message in the feed channel carries the same three static buttons. Custom IDs are static — no whisper_id encoded.

- `[Send Whisper]` (`custom_id="whisper:send"`) → opens a modal with `target` user-select + `message` text input. Equivalent to `/whisper`.
- `[Check Whispers]` (`custom_id="whisper:check"`) → ephemeral list of received whispers in `pending` or `shared` state, newest first, with action buttons inline.
- `[Check Hidden Whispers]` (`custom_id="whisper:check_hidden"`) → ephemeral list of received whispers in `hidden` state.

### Per-whisper buttons (in target's DM)
Custom IDs encode the whisper id: `whisper:guess:{id}`, `whisper:share:{id}`, `whisper:hide:{id}`, `whisper:expose:{id}`.

- `[Guess]` → opens a modal with member-search input. Validates that invoker is the target and `guesses_left > 0` and `solved = false`. Decrements `guesses_left`. On correct: marks `solved=true`; bot posts a NEW message in the feed channel: "You're Right! @Target figured out who sent the whisper: '[content]'" with an `[Expose]` button (and the standard feed buttons). On wrong: ephemeral "Wrong, X guesses left." On final wrong: ephemeral "No more guesses. The sender stays anonymous forever." and `[Guess]` is removed from the DM view.
- `[Share]` → only valid in `pending` state. Transitions to `shared`. Edits the original feed channel announcement message to "A fresh Whisper was shared. Someone sent @Target an anonymous message! '[content]'". The standard feed buttons remain. Removes `[Share]` and `[Hide]` from the DM view.
- `[Hide]` → only valid in `pending` state. Transitions to `hidden`. Original feed announcement message is unchanged. Removes `[Share]` and `[Hide]` from the DM view.
- `[Expose]` → only appears on the "You're Right!" message after a correct guess; only the target can press. On press: edits that message to append "Sender was @Sender." and removes the `[Expose]` button. Cannot be reversed.

## Data Model

New SQLite tables created via migration in `services/whisper_repo.py:init_db()`.

```sql
CREATE TABLE IF NOT EXISTS whispers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    sender_id       INTEGER NOT NULL,
    target_id       INTEGER NOT NULL,
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'shared' | 'hidden'
    solved          INTEGER NOT NULL DEFAULT 0,
    exposed         INTEGER NOT NULL DEFAULT 0,
    guesses_left    INTEGER NOT NULL DEFAULT 3,
    channel_msg_id  INTEGER,
    dm_msg_id       INTEGER
);
CREATE INDEX idx_whispers_target ON whispers(guild_id, target_id, state);
CREATE INDEX idx_whispers_sender ON whispers(guild_id, sender_id);

CREATE TABLE IF NOT EXISTS whisper_guesses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    whisper_id  INTEGER NOT NULL REFERENCES whispers(id) ON DELETE CASCADE,
    guessed_id  INTEGER NOT NULL,
    correct     INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_whisper_guesses_whisper ON whisper_guesses(whisper_id);
```

## File Structure

- `cogs/whisper_cog.py` — Discord-facing layer: slash commands, modals, persistent views, button handlers. Thin; delegates to service.
- `services/whisper_service.py` — Business logic: send, guess, share, hide, expose, optin, optout. Pure functions where possible (validation, state transitions). All Discord I/O isolated in cog.
- `services/whisper_repo.py` — SQL queries + `init_db()`. CRUD for `whispers`, `whisper_guesses`. Selectors: `get_whisper(id)`, `list_received(target_id, state)`, `list_sent(sender_id)`.
- `services/whisper_models.py` — Dataclasses: `WhisperConfig`, `Whisper`, `WhisperGuess`.

The cog is registered in `dungeonkeeper.py` alongside existing cogs.

## Web Panel

A new "Whisper" section in the existing config web panel (mirroring how `veil_cog` and `confessions_cog` configs are exposed). Fields:
- Channel picker for `whisper.channel_id`
- Channel picker for `whisper.log_channel_id`
- Role picker for `whisper.role_id`

Same validation: all three required before whispers can be sent.

## Error Handling

| Trigger | Behavior |
|---|---|
| Sender lacks role | Ephemeral: "You need the Whisper role to send whispers. Use `/whisper optin` to join." |
| Target lacks role | Ephemeral: "That member hasn't opted in to receive whispers." |
| Sender = target | Ephemeral: "You can't whisper yourself." |
| Whisper not configured (any of 3 keys missing) | Ephemeral: "Whispers aren't set up in this server yet." |
| Bot can't DM target (Forbidden) | Ephemeral to sender: "Couldn't deliver — that user has DMs disabled." Whisper row is NOT created (atomic). |
| Non-target presses `[Guess]` | Ephemeral: "Only the recipient can guess." |
| Guesser picks themselves in modal | Ephemeral: "You can't guess yourself." |
| Guess on already-solved whisper | Ephemeral: "This whisper has already been solved." Buttons should already be disabled. |
| Press `[Share]` on shared/hidden whisper | Ephemeral: "Already decided." (Button should be removed from view; this is defense-in-depth.) |
| `[Expose]` pressed by non-target | Ephemeral: "Only the recipient can expose this." |
| Feed channel message edit fails (deleted by mod) | Log warning, swallow exception. State is still updated in DB. |

## Testing

Following existing project pattern (`pytest`, `pytest-asyncio`, services tested as pure logic, cogs tested with mocked Discord interaction).

### `tests/services/test_whisper_service.py`
- `send_whisper` happy path: creates row, returns content for both channel post and DM
- `send_whisper` rejected when sender lacks role
- `send_whisper` rejected when target lacks role
- `send_whisper` rejected on self-target
- `send_whisper` rejected when config incomplete
- `record_guess` decrements `guesses_left` on wrong guess
- `record_guess` marks `solved` on correct guess
- `record_guess` raises when `guesses_left == 0`
- `record_guess` raises when already solved
- `share_whisper` transitions `pending` → `shared`
- `share_whisper` rejects when not pending
- `hide_whisper` transitions `pending` → `hidden`
- `expose_whisper` sets `exposed=true` only when `solved=true`
- Optin/optout are pure role manipulations; existing whispers unaffected

### `tests/services/test_whisper_repo.py`
- CRUD: insert + fetch round-trips
- `list_received` filters by state correctly
- `list_received` excludes other guilds
- `whisper_guesses` cascade delete when whisper deleted

### `tests/cogs/test_whisper_cog.py`
- `/whisper optin` grants role and sends confirmation modal
- `/whisper optout` removes role
- `/whisper @target message` invokes service, posts feed message, DMs target, writes mod log, returns ephemeral confirm
- `/whisper @target` with closed DMs surfaces clean error and does NOT persist whisper
- `[Guess]` non-target → ephemeral rejection
- `[Guess]` target wrong → ephemeral wrong + decrement
- `[Guess]` target correct → DM reveals sender, feed message edited to "You're Right!", `[Expose]` button appears
- `[Share]` target → feed message edited, state transitions
- `[Hide]` target → feed message untouched, state transitions
- `[Expose]` target after solve → feed message reveals sender
- Persistent custom_ids decode to correct whisper_id on a freshly-loaded cog (simulated bot restart)

## Out of Scope (v1)

- User blocking (block specific opted-in users from sending you whispers). Defer to v2 if requested.
- Replies to whispers (canonical Whisper supports them). Defer.
- Attachments / images.
- Server-wide stats / leaderboards.
- Migration from the `confessions_cog`.
