# Guess Round Message Chips

**Date:** 2026-05-10
**Status:** Approved

## Overview

Add two disabled "chip" buttons to the public Guess round message: a running guess counter and a censored "Submitted by" indicator. The chips replace the current `Submitted by an anonymous member` embed description and give viewers visible feedback on round activity before they ever click Guess.

The ephemeral guess flow (clicking Guess opens a private select dropdown) is unchanged. Solved-state behavior is unchanged — the existing solved embed already conveys the same information the chips would.

## Files Changed

| File | Change |
|------|--------|
| `cogs/guess_cog.py` | `_game_embed` drops the description; `GameView` (unsolved only) adds two disabled chip buttons; `_on_select` edits the public message to bump the counter after every guess (correct or wrong) |
| `tests/cogs/test_guess_guess.py` | Add coverage: counter increments on every guess; chips absent in solved-state view |

No DB migration, no config knob, no schema change.

## UI

**Unsolved state — public round message:**

```
[ embed: "Round #N"  +  image ]
[ Guess ]
[ Guesses: 0 ]  [ Submitted by ▒▒▒▒▒▒▒ ]
```

- Row 1: existing primary **Guess** button.
- Row 2: two disabled secondary chip buttons.
  - **Guesses: N** — N is the total guess count for the round (all users, correct + wrong).
  - **Submitted by ▒▒▒▒▒▒▒** — seven U+2592 block characters as a redaction bar.

**Solved state — unchanged:**

The existing `_solved_embed` already shows `Submitted by:` and total guess + unique-guesser counts. On solve, the chips disappear and the View collapses to the existing single `Guess late` button (today's behavior).

## Behavior

**Counter increments on every guess attempt.**

After `_do_insert_guess` succeeds in `GuessSelectView._on_select`, before the existing correct/wrong branching, edit the public game message (`self.game_message`) to update the `Guesses: N` chip label. The counter increments on both correct and wrong guesses (matches `count_guesses_for_round`).

When the guess is correct and marks the round solved, the existing `self.game_message.edit(embed=solved_emb, view=new_game_view, ...)` call already replaces the View — no counter bump needed in that branch (the solved embed supersedes the chips).

When the guess is correct but the round was already solved (lost the race), no public edit is needed — the chips on the now-solved message are already gone or about to be replaced by whichever request wins.

**Edit failure tolerance.**

The counter-bump edit is best-effort. If it fails (rate limit, message deleted, permissions), log and swallow — the guess itself has already been recorded in the DB, and the user's ephemeral response is independent.

## Chip Implementation Detail

Disabled buttons in a persistent View need custom IDs for component routing, but since chips have no callbacks, the IDs only need to be unique within the View. Use static IDs `guess_chip_count:{round_id}` and `guess_chip_submitter:{round_id}`.

`GameView.__init__` constructs the chips only when `solved=False`. The solved branch (today's `Guess late` button) is untouched.

The counter-bump edit rebuilds the View with the new label. `GameView` gains an optional `guess_count: int` constructor parameter; the counter chip's label is built from it. On each bump, `_on_select` constructs a fresh `GameView(self.bot, self.round_id, solved=False, guess_count=N)` and calls `self.game_message.edit(view=new_view)`. N is computed locally (prior count from `_do_count_guesses_for_round` plus one for the just-inserted guess) so we avoid a second DB roundtrip.

The persistent-view registration done at cog load (`bot.add_view(GameView(..., solved=False))`) is unaffected — Discord routes button interactions by `custom_id`, and the `Guess` button's `custom_id=f"guess_guess:{round_id}"` is stable across rebuilds. The chip buttons have stable custom IDs but no callbacks.

## Tradeoffs

- **One extra Discord API call per guess.** Each guess does an additional `message.edit`. For a single round this is negligible; for very high-velocity bursts (dozens of guesses/second on a popular round) it could brush Discord's per-channel edit budget. Accepted — Guess rounds don't realistically see that traffic, and the failure path (log and swallow) keeps the game flow intact.
- **Counter only increments forward.** If a guess record is later deleted (e.g. via moderation), the chip won't decrement. The chip reflects "guesses ever recorded," not "guesses currently in the table." Acceptable for this UX; the inspector embed shows the authoritative count.
- **Chips occupy a second action row.** The View still has room (max 5 rows) for future additions.

## Out of Scope

These were considered and dropped during brainstorming:

- A `Guess #N` chip duplicating the embed title — title stays.
- A `New Guess` second action button — separate feature if pursued later.
- An `Unguess` reveal/give-up button — separate feature if pursued later.
- Embedding the guess dropdown into the public message (StringSelect or UserSelect) — ephemeral flow retained for fresh member-list lookup and personalized cooldown/cap feedback.
- A per-round community guess cap (X/3) — would be a game-mechanic change, not a UI tweak.
