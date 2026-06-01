# Guess — Feature Spec

A guess-the-member image game. A consenting submitter posts an NSFW image; the bot tight-crops an "interesting" region (face excluded) and posts the crop to a dedicated channel. Anyone can guess the member from an autocomplete restricted to opted-in members. All-time stats are tracked per guild.

> **History (2026-06-01):** This feature was originally called "Veil"; an internal rename moved every table, command, cog, and web panel to `guess_*`. The old `/veil` slash commands, cog, and web panels were deleted on the same day. The product is Guess only; there is no Veil variant.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/guess submit <image>` | Slash | Guess role (Everyone if unset) | Submit an NSFW image; opens the crop editor and post flow |
| `/guess setup channel:[#ch] [role:@role]` | Slash | Manage Server | First-time setup: configure the Guess channel and consent role |
| `/guess optin` / `/guess optout` | Slash | Everyone | Toggle the consent role and your eligibility as an answer |
| `/guess stats [user]` | Slash | Everyone | Submitter + guesser stats for yourself or another member |
| `/guess leaderboard [category]` | Slash | Everyone | Top 10 for submitters / guessers / accuracy / streaks / hardest crops |
| `/guess round <round_id>` | Slash | Mod | Inspect a specific round (submitter, answer, crop, guess log) |
| `/guess delete <round_id>` | Slash | Submitter or Mod | Soft-delete a round (message removed, stats preserved) |
| `/guess confess text:<...>` | Slash | Guess role | Post an anonymous text confession in the guess channel |
| `Guess` button (on round post) | Persistent | Everyone | Open the picker / search-by-name flow |
| Web config panel | Web (dashboard) | Admin | Per-guild role, channel, cooldown, difficulty, image limits |
| Web audit log | Web (dashboard) | Mod | Recent submit / delete / solve / guess-cap events |

Bot perms required: **Send Messages**, **Embed Links**, **Attach Files**, **Read Message History** in the guess channel; **Manage Roles** to toggle the consent role on opt-in / opt-out.

## Behaviour

### Consent and opt-in

There's a configured **consent role**. Without it set, anyone in the guild can submit and be an answer. With it set:

- `/guess optin` opens a confirmation modal explaining that opting in means consenting to (a) submitting NSFW images of yourself for cropping and (b) being a possible answer in the autocomplete. On confirm, the role is granted.
- `/guess optout` removes the role and removes you from the autocomplete immediately. **Open rounds where you are the answer are not deleted** (which would leak by deletion timing); instead they're flagged as answer-opted-out and the autocomplete excludes you, so the round becomes effectively unsolvable. This tradeoff is documented in the opt-in modal.

### `/guess submit` — the crop pipeline

The submitter uploads an image. The bot:

1. Validates MIME, dimensions (≥ configured min), and file size (≤ configured cap).
2. Saves the original to an on-disk cache, keyed by round id, retained **only** until first correct solve.
3. Runs **candidate detection** (NudeNet, filtered to confidence ≥ 0.5). If nothing passes, the submission is rejected with "Couldn't find a viable crop region — try a different image."
4. Runs **face exclusion** — any candidate that overlaps a detected face is dropped or shrunk to the non-face region. If shrinking would drop the crop below the minimum size, the next candidate is tried.
5. Picks the highest-confidence surviving candidate and applies difficulty-tuned padding around it: **easy** = looser (more context), **medium** = moderate, **hard** = tight crop. Output is clamped to image bounds and scaled up if smaller than the minimum.
6. Shows the crop to the submitter ephemerally with **Re-roll crop** (up to 3 re-rolls, cycles through ranked candidates) and **Post**.
7. On Post, the crop posts publicly to the guess channel with a **Guess** button.

### Guessing

Clicking **Guess** opens a string-select dropdown of opted-in members (paginated 25 per page) plus a **Search by name** button that takes a text query and returns up to 5 fuzzy matches as clickable buttons. Submitters cannot guess on their own rounds.

Per-(user, round) cooldown: configurable (default 60 s). On cooldown, the guesser sees "Try again in Xs."

On a **correct first solve**, the bot edits the round's message: the original image (the full submission, not the crop) is attached as a spoiler-prefixed file (click-to-reveal blur), and the embed updates to show "Solved by {user} in N guesses (across M guessers)" and reveals the submitter and answer. The on-disk original is deleted at this point. The Guess button stays live (now labeled "Guess late") for late correct guesses, which get an ephemeral "Correct — but {user} got there first."

### Stats and leaderboards

`/guess stats` shows submitter stats (rounds posted, rounds solved/unsolved, average guesses-to-solve, average unique guessers, hardest crop, most-rerolled count) and guesser stats (total guesses, correct, accuracy, first-solver count, current and longest correct streaks, fastest solve).

`/guess leaderboard` shows top 10 in one of five categories: `submitters`, `guessers`, `accuracy` (min 10 guesses), `streaks` (current correct-guess streak), `hardest_crops` (highest average guesses-to-solve, min 5 attempts).

### Mod tools

`/guess round` shows a specific round to a mod: submitter, answer, crop, full guess log. For dispute resolution. `/guess delete` soft-deletes a round (message goes, stats survive). The web audit panel lists recent submit, delete, solve, and cap events for the guild.

## Permissions

- `/guess submit`, `/guess optin`, `/guess optout`, `/guess stats`, `/guess leaderboard`, `/guess confess`: consent role required (or Everyone if the role is unset).
- `/guess delete` on your own round: submitter; on someone else's round: Mod.
- `/guess setup`: Manage Server.
- `/guess round`: Mod.
- Submitter cannot guess on their own round.

## User-visible errors

| When | The user sees |
|---|---|
| `/guess submit` without the consent role | "You need the Guess role to submit." |
| `/guess submit` outside the configured channel | Ephemeral notice with the configured channel mention |
| Image too small or too large | Ephemeral with the limit values |
| No viable crop region found | "Couldn't find a viable crop region — try a different image." |
| Submitter clicks Guess on their own round | "You can't guess on your own round." |
| Guess on cooldown | "Try again in Xs." |
| Guess on a soft-deleted round | "This round is no longer available." |
| Wrong guess | "Not it. Try again in <cooldown>s." |
| Late correct guess | "Correct — but {user} got there first." |
| `/guess delete` by someone other than submitter or mod | Permission denied |
| `/guess round` by a non-mod | Permission denied |

## Non-goals

- **Submitting on behalf of another member.** Submitter is always the answer.
- **Per-round difficulty override.** Uses the guild default only.
- **Cross-guild stats / global leaderboards.**
- **Image moderation beyond the built-in detector.** No age verification — Discord ToS, the consent role, and the opt-in modal are the gate.
- **Alt-account collusion detection.**
- **Round reuse / throwback rounds.** Earlier drafts had a reuse system; it's been removed.
- **Web crop override UI.**

## Configuration

| Key | Default | Purpose |
|---|---|---|
| `guess_role_id` | unset | Consent / eligibility role; unset = Everyone can submit |
| `guess_channel_id` | unset | Where crops post and modals launch from |
| `guess_guess_cooldown_seconds` | `60` | Per-user, per-round cooldown between guesses |
| `crop_difficulty` | `medium` | `easy` / `medium` / `hard` |
| `min_image_dimension_px` | `400` | Reject submissions smaller than this on either axis |
| `guess_max_image_size_mb` | `10` | Hard cap on upload size |
| `guess_prompt_message_id` | unset | Persistent prompt message at the bottom of the channel |

## Stored data

Four tables per guild: rounds (one row per submission with crop / answer / solver / counts), guesses (one row per guess attempt), opt-ins (consent timestamps), and an audit log (submit / delete / solve events).

Filesystem cache: original submissions live in a per-round file on disk **only until first correct solve**, at which point the file is deleted and the path cleared. Crops live on disk for the round's lifetime and are deleted on round deletion. The Discord CDN URL of the posted crop is the canonical reference if the local cache is missing.

The original image is never reused for a future round and never published unspoilered. This retention policy is surfaced prominently in the opt-in modal.
