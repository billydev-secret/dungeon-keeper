# Guess — Feature Spec

A guess-the-member image game. A consenting submitter posts an NSFW image; the bot detects an "interesting" region, offers a crop editor to frame it (faces filtered out where possible), and posts the crop to a dedicated channel. Anyone can guess the member from a picker restricted to opted-in members. All-time posting/solving totals are tracked per guild.

> **History (2026-06-01):** This feature was originally called "Veil"; an internal rename moved every table, command, cog, and web panel to `guess_*`. The old `/veil` slash commands, cog, and web panels were deleted on the same day. The product is Guess only; there is no Veil variant.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/guess submit <image>` | Slash | Guess role (required — errors if the role isn't configured, or if you don't hold it) | Submit an NSFW image; runs the detection pipeline and opens the crop editor / post flow |
| `/guess optin` | Slash | Everyone (errors if the Guess role isn't configured) | Grants you the consent role immediately — no confirmation step. Makes you eligible to submit and to be picked as an answer |
| `/guess leaderboard` | Slash | Everyone | Posts the top 5 submitters (rounds posted/solved) and top 5 guessers (rounds solved) — fixed, no arguments or categories |
| `/guess round <round_id>` | Slash | Mod (`manage_guild`; hidden from non-mods in the Discord UI via `default_permissions`) | Inspect a specific round (status, submitter, answer, crop, guess/unique-guesser counts, re-roll count) |
| `/guess delete <round_id>` | Slash | Submitter or Mod (`manage_guild`; checked in code only — the command itself isn't permission-restricted client-side) | Soft-delete a round (message best-effort deleted, stats preserved) |
| `/guess confess text:<...>` | Slash | Guess role (requires both the role and the channel to be configured) | Renders an anonymous text confession as an image card and previews it for you to post or cancel |
| `/guess prompt` | Slash | Mod (`manage_guild`; hidden from non-mods in the Discord UI) | Immediately (re)posts the sticky Submit/Help prompt message at the bottom of the configured guess channel |
| `Guess` button (on round post) | Persistent | Everyone except the round's submitter | Opens an ephemeral member picker (see Guessing below) |
| `🎭 Submit Guess` / `❓ Help` buttons (on the sticky prompt) | Persistent | Everyone (Guess role enforced on submit) | Submit opens a URL-paste modal that feeds the same detection pipeline as `/guess submit`; Help shows a short how-to-play blurb |
| Web config panel | Web (dashboard) | Admin | Per-guild role, channel, cooldown, difficulty, image limits. The channel must be age-gated (NSFW) — the API rejects non-NSFW channels when the bot can resolve them |
| Web audit log | Web (dashboard) | Mod | Recent submit / delete / solve / guess-cap events |

There is no `/guess optout` and no `/guess stats` command — neither exists in code.

Bot perms required: **Send Messages**, **Embed Links**, **Attach Files**, **Read Message History** in the guess channel; **Manage Roles** to grant the consent role on `/guess optin`. The bot never removes the role itself — see "Consent and opt-in" below.

## Behaviour

### Consent and opt-in

There's a configured **consent role** (`guess_role_id`). It gates submitting (`/guess submit` and the sticky-prompt URL modal), posting confessions (`/guess confess`), and being a pickable answer. Unlike some other guild features, this role has no "Everyone if unset" fallback: if it isn't configured, `/guess submit` and `/guess confess` both refuse with a message telling the user to ask an admin to configure it in the web dashboard.

- `/guess optin` grants the role to the invoking member immediately — there is **no confirmation modal** and no explanation dialog. If the member already has the role, the bot just says so instead of re-adding it. The success message tells them "to leave, ask a mod to remove the role."
- **There is no `/guess optout` command.** Opting out means a mod (or anyone with `Manage Roles`) removes the consent role from the member directly in Discord — the bot has no self-service removal path. When the bot observes a member losing that role (via an `on_member_update` listener), it flags any of that member's **open** rounds (where they're the answer) as `answer_optout` in the DB. The round itself is not deleted or hidden — its Guess button still works, but clicking it now replies "This round is no longer solvable — the answer opted out." This flag is permanent for that round; re-adding the role later does not clear it.

### `/guess submit` — the crop pipeline

The submitter uploads an image (via `/guess submit <image>`, or via the sticky prompt's URL-paste modal). Per-user submissions are also rate-limited in-memory to 5 per rolling hour; past that the bot replies "You've hit the submission limit (5 per hour). Please wait a bit before submitting again." (resets on bot restart). The bot then:

1. Validates MIME, dimensions (≥ configured min), and file size (≤ configured cap).
2. Saves the original to an on-disk cache, keyed by round id, retained **only** until first correct solve.
3. Runs **candidate detection** — combines NudeNet detections with a separate pose-based detector, merges adjacent different-type genital detections into a single "sex act" candidate, and re-weights scores so more "interesting" regions (genitals/breasts/buttocks) outrank incidental ones (belly/armpits). Candidates overlapping a detected face are filtered out, with a fallback to the single highest-scoring detection (even if it overlaps a face) if that would otherwise eliminate everything. **If no detector returns anything at all, the submission is not rejected** — the editor still opens with a default centered crop box and the note "No detections found — manually frame your crop, then ✓ Post."
4. Applies difficulty-tuned padding around the top candidate: **easy** = looser (more context), **medium** = moderate, **hard** = tight crop. Output is clamped to image bounds and expanded if smaller than the minimum.
5. Opens an ephemeral **crop editor**: a D-pad view (move up/down/left/right, zoom in/out, an **Auto** button that cycles through the ranked detected candidates, **✓ Post**, and **✗** cancel). There is no fixed re-roll cap — the submitter can nudge/zoom/cycle as many times as they like before posting. Cancelling before posting discards the submission entirely (nothing is written to the DB until Post).
6. On Post, the crop posts publicly to the guess channel with a **Guess** button, and the bot best-effort reposts the sticky channel prompt underneath it.

### Guessing

Clicking **Guess** first checks that the round hasn't been flagged `answer_optout` and that the clicker isn't the submitter, then opens an ephemeral string-select dropdown of opted-in members, paginated 25 per page (◀/▶ buttons appear when there's more than one page). A **🔍 Filter** button opens a modal for a text query; matches are scored (exact name match, then prefix, then substring, then subsequence) and the same select is rebuilt with the filtered/reordered results, with a **✕ Clear** button to reset. Submitters cannot guess on their own rounds.

Guesses are capped per (user, round) at 5 total — past that the bot replies "You're out of guesses on this round (cap: 5)." Below that cap, there's also a per-(user, round) cooldown, configurable (default 60 s; 0 disables it). On cooldown, the guesser sees "⏳ On cooldown — you can guess again <t:...:R>." (a Discord relative timestamp). A wrong guess gets "❌ Not it. Keep trying!".

On a **correct first solve**, the bot edits the round's message: the original image (the full submission, not the crop) is attached as a spoiler-prefixed file (click-to-reveal blur), and the embed updates to show "✅ Round #N — Solved!" with the answer, submitter, and "Solved by {user} in N guesses (across M guessers)". The on-disk original is deleted at this point. The Guess button stays live (now labeled "Guess late") for late correct guesses, which get a generic ephemeral "✅ Correct — but someone already solved this one." (it does not name the first solver).

### Leaderboard

`/guess leaderboard` takes no arguments and always posts both lists together, non-ephemeral: **Top Posters** (top 5 by rounds posted, tie-broken by rounds solved, each shown as "posted, solved (pct%)") and **Top Guessers** (top 5 by rounds solved as first correct guesser). Both exclude soft-deleted rounds. There is no per-user `/guess stats` command, no accuracy/streak/hardest-crop tracking, and no leaderboard category argument — none of that is implemented.

### Mod tools

`/guess round` shows a specific round to a mod: status (open / solved / deleted), submitter, answer, difficulty, guess and unique-guesser counts, re-roll count, and the crop image. For dispute resolution. `/guess delete` soft-deletes a round (message best-effort deleted, stats survive; already-deleted rounds are rejected with "Round #N is already deleted."). The web audit panel lists recent submit, delete, solve, and cap events for the guild.

`/guess prompt` lets a mod force an immediate repost of the sticky Submit/Help prompt message in the configured channel (normally it reposts itself automatically ~2s after the last message in the channel, debounced). Useful if the sticky prompt gets buried or its message is deleted.

## Permissions

- `/guess submit`, `/guess confess`: require the consent role to be configured **and** held by the caller — there is no "Everyone if unset" fallback; an unconfigured role blocks both.
- `/guess optin`: anyone can run it, but it errors if the consent role isn't configured. No opt-out equivalent exists as a command.
- `/guess leaderboard`: Everyone, no arguments.
- `/guess delete` on your own round: submitter; on someone else's round: Mod (`manage_guild`). This check is enforced in code only — the command isn't hidden from non-mods client-side.
- `/guess round`, `/guess prompt`: Mod (`manage_guild`), and also hidden from non-mods in the Discord UI via `default_permissions`.
- Submitter cannot guess on their own round.

## User-visible errors

| When | The user sees |
|---|---|
| `/guess submit` / `/guess confess` with the Guess role unconfigured | "Guess role is not configured. Ask an admin to set it in the web dashboard." (confess: "Guess is not fully configured...", since it also requires the channel) |
| `/guess submit` without the consent role | "You need the Guess role to submit." |
| Submission rate limit hit (>5/hour, per user, in-memory) | "You've hit the submission limit (5 per hour). Please wait a bit before submitting again." |
| Image not an image / too small / too large | "Please submit an image file.", "Image too small. Minimum dimension is Npx.", or "Image too large. Maximum is N MB." |
| No detections found in the image | **Not an error** — the crop editor opens with a manual default box: "No detections found — manually frame your crop, then ✓ Post." |
| Submitter clicks Guess on their own round | "You can't guess on your own round." |
| Guess on a round the answer opted out of | "This round is no longer solvable — the answer opted out." |
| Guess cap hit (>5 guesses on one round by one user) | "You're out of guesses on this round (cap: 5)." |
| Guess on cooldown | "⏳ On cooldown — you can guess again <t:...:R>." |
| Wrong guess | "❌ Not it. Keep trying!" |
| Late correct guess (already solved) | "✅ Correct — but someone already solved this one." (does not name the first solver) |
| `/guess delete` by someone other than submitter or mod | "Only the submitter or a mod can delete this round." |
| `/guess delete` on an already-deleted round | "Round #N is already deleted." |
| `/guess round` by a non-mod | "Only mods (manage_guild permission) can inspect rounds." |
| `/guess confess` with disallowed content | "That confession contains disallowed content. Please rephrase." |

## Non-goals

- **Submitting on behalf of another member.** Submitter is always the answer.
- **Per-round difficulty override.** Uses the guild default only.
- **Cross-guild stats / global leaderboards.**
- **Image moderation beyond the built-in detector.** No age verification — Discord ToS and the consent role are the gate.
- **Alt-account collusion detection.**
- **Round reuse / throwback rounds.** Earlier drafts had a reuse system; it's been removed.
- **Web crop override UI.**

## Configuration

| Key | Default | Purpose |
|---|---|---|
| `guess_role_id` | unset | Consent / eligibility role. Unlike some other guild features, unset does **not** open submit/confess to Everyone — it blocks both until an admin configures it |
| `guess_channel_id` | unset | Where crops post and modals launch from |
| `guess_guess_cooldown_seconds` | `60` | Per-user, per-round cooldown between guesses (`0` disables it) |
| `guess_crop_difficulty` | `medium` | `easy` / `medium` / `hard` — controls crop editor padding, not a per-round choice |
| `guess_min_image_dimension_px` | `400` | Reject submissions smaller than this on either axis |
| `guess_max_image_size_mb` | `10` | Hard cap on upload size |
| `guess_prompt_message_id` | unset | Persistent prompt message at the bottom of the channel |

## Stored data

Rounds (one row per submission with crop / answer / solver / counts), guesses (one row per guess attempt), and an audit log (submit / delete / solve / guess-cap events) per guild. There's also a `guess_optins` table with full CRUD support in `guess_repo.py` (insert/delete/get/list-by-guild, keyed on user + guild with an `opted_in_at` timestamp) — but it's currently **dead**: nothing in the cog or web server calls it. `/guess optin` only grants the Discord role; eligibility is derived live from role membership (`guess_role.members`), not from this table.

Filesystem cache: original submissions live in a per-round file on disk **only until first correct solve**, at which point the file is deleted and the path cleared. Crops live on disk for the round's lifetime and are deleted on round deletion. The Discord CDN URL of the posted crop is the canonical reference if the local cache is missing.

The original image is never reused for a future round and never published unspoilered. (Note: there is no opt-in confirmation modal in the current code — see "Consent and opt-in" — so this retention policy is not currently disclosed to the submitter at opt-in or submit time via any in-app text.)
