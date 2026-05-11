# Whisper Guess Dropdown

**Date:** 2026-05-10
**Status:** Approved

## Summary

Replace the free-text modal for guessing a whisper's sender with an ephemeral
message containing a paginated `discord.ui.Select` filtered to opted-in members.

## Motivation

The current `WhisperGuessModal` requires the user to type or paste a member ID /
mention. A dropdown filtered to whisper-role members is faster, less error-prone,
and prevents guessing members who could not have sent the whisper.

## Flow

1. User clicks **Guess** button (in DM or inbox).
2. Bot responds with an ephemeral message containing `WhisperGuessSelectView`.
3. User picks a name from the dropdown.
4. Select callback records the guess and delivers outcome feedback.

The `WhisperGuessModal` class is deleted; nothing else calls it.

## Components

### `WhisperGuessSelectView(discord.ui.View)`

- `timeout=120`
- Constructor args: `bot`, `whisper_id`, `members: list[discord.Member]`, `page: int = 0`
- Always contains one `WhisperGuessMemberSelect` for the current page slice.
- Contains `WhisperGuessPrevButton` and `WhisperGuessNextButton` only when
  `len(members) > 25`; buttons are disabled at boundaries.
- Prev/Next handlers call `interaction.response.edit_message(view=new_view)`
  where `new_view` is a fresh `WhisperGuessSelectView` at the new page index.

### `WhisperGuessMemberSelect(discord.ui.Select)`

- Built from `members[page*25 : (page+1)*25]`.
- Each `SelectOption`: `label=member.display_name`, `value=str(member.id)`.
- `placeholder="Pick the sender…"`
- On select: parses `guessed_id = int(values[0])`, re-loads whisper, calls
  `_handle_guess_outcome`.
- After outcome: calls `interaction.response.edit_message(content=outcome_text, view=None)`
  to replace the select with the result text in-place (no extra ephemeral).

### `_handle_guess_outcome(interaction, bot, whisper, guessed_id)` (async free function)

Extracted from current `WhisperGuessModal.on_submit`. Handles:
- **Correct** — congratulates user, posts solved announcement to feed channel,
  adds `WhisperExposeView` to feed message.
- **Exhausted** — removes Guess button from original DM, sends failure message.
- **Wrong with guesses remaining** — sends count-down message.

Feed channel lookup uses `interaction.guild or bot.get_guild(whisper.guild_id)`
(same pattern as the share-button fix).

## `WhisperGuessButton.callback` — replacement logic

1. Load whisper; error if missing.
2. Pre-checks: invoker must be target; not already solved; guesses remaining > 0.
3. Resolve guild via `interaction.guild or self.bot.get_guild(whisper.guild_id)`;
   error if None.
4. Load config; get role via `guild.get_role(cfg.role_id)`; error if missing or
   `role_id == 0`.
5. Filter `role.members` to exclude `whisper.target_id`; sort by display name.
   If empty: ephemeral "No other opted-in members to guess from."
6. Send ephemeral `WhisperGuessSelectView(bot, whisper_id, members, page=0)`.

## Error messages (user-facing)

| Condition | Message |
|---|---|
| Guild not found | "Couldn't find the server — try again." |
| Role not configured | "Whisper role isn't configured." |
| Role not found in guild | "Whisper role no longer exists." |
| No guessable members | "No other opted-in members to guess from." |

## Data flow diagram

```
Guess click
  → load whisper (DB)
  → pre-checks
  → resolve guild
  → load config + role
  → filter + sort role.members
  → send ephemeral WhisperGuessSelectView (page 0)
      ↕ Prev/Next edits same ephemeral
  → member selected
      → re-load whisper
      → _handle_guess_outcome
          → evaluate_guess (service)
          → _do_record_guess (DB)
          → correct: post to feed, expose view
          → exhausted: edit DM, inform user
          → wrong: inform user with count
```

## Testing

**Unit:**
- Page-slicing helper: N members → correct page count, boundaries correct.

**Cog-level (`tests/cogs/test_whisper_guess.py`):**
- Guess button with ≤25 role members: ephemeral message sent, no Prev/Next.
- Guess button with >25 role members: ephemeral message sent, Prev/Next present.
- Prev/Next: edit message with correct page.
- Member selected → correct outcome: feed posted, DM edited.
- Member selected → wrong: feedback sent.
- Member selected → exhausted: Guess button removed from DM.
- Error paths: no guild, no role, empty member list.

## Files changed

| File | Change |
|---|---|
| `cogs/whisper_cog.py` | Remove `WhisperGuessModal`; add `WhisperGuessMemberSelect`, `WhisperGuessSelectView`; replace `WhisperGuessButton.callback`; extract `_handle_guess_outcome` |
| `tests/cogs/test_whisper_guess.py` | Replace modal tests with select tests |
