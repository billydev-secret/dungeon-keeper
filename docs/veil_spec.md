# Veil — Dungeon Keeper Module Spec

A guess-the-member game. Submitter posts an NSFW image; the bot tight-crops an "interesting" region (face excluded) and posts the crop to a dedicated channel. Anyone can guess the member via a modal autocomplete restricted to opted-in members. All-time stats tracked.

## Core Concepts

- **Veil role**: Per-server role gating consent. Only members with this role can be submitters or appear as guess-targets in the autocomplete. Anyone in the server can guess.
- **Round**: A single submission. Persists indefinitely; never auto-closes. Marked "solved" when first correct guess lands, but guessing remains open after solve so latecomers can still play.
- **Crop pipeline**: Auto only for v1. NudeNet for candidate regions → face detector to exclude → difficulty-tuned tightness → final crop.

## Configuration (per-guild)

- `veil_role_id` (required) — the consent/eligibility role
- `veil_channel_id` (required) — where crops post and modals are launched from
- `guess_cooldown_seconds` (default `30`) — per-user, per-round
- `crop_difficulty` (default `medium`) — `easy` | `medium` | `hard`, controls crop tightness
- `min_image_dimension_px` (default `400`) — reject submissions smaller than this on either axis
- `max_image_size_mb` (default `10`)

> The reuse system has been retired. Older drafts of this spec described `reuse_enabled`, `reuse_quiet_hours`, `reuse_min_age_days`, and `reuse_min_post_interval_hours` config keys plus an `allow_reuse` per-round flag — these are no longer surfaced through the slash command or web config panel. Some dormant database columns and repo helpers remain for backward compatibility but are not exercised.

Stored in existing Dungeon Keeper guild config table; namespace keys under `veil.*`.

## Slash Commands

### `/veil submit <image: attachment>`
- **Permission**: Veil role required. Reject with ephemeral error otherwise.
- **Channel**: Must be invoked in `veil_channel_id` (or DM-tolerant; ephemeral notice if elsewhere).
- **Validation**: image MIME, dimension/size limits, presence of at least one NudeNet detection. If no detections, ephemeral error: "Couldn't find a viable crop region — try a different image."
- **Flow**:
  1. Defer ephemerally.
  2. Download attachment; persist the original to `./veil_cache/orig/<round_id><ext>` (deleted on first solve — see Storage).
  3. Run crop pipeline (see Crop Pipeline below).
  4. Post crop publicly to `veil_channel_id` with a "Guess" button.
  5. Persist round row.
  6. Ephemeral confirm to submitter with the crop preview + a "Re-roll crop" button (max 3 re-rolls before submission locks; new crops use a different candidate region if available).

### `/veil optin` / `/veil optout`
- Toggles the Veil role on the invoker. `optin` is a confirmation modal: "By opting in, you consent to (a) submitting NSFW images of yourself for cropping and (b) being a possible answer in the guessing autocomplete. You can opt out anytime." Yes/No buttons.
- `optout` removes the role and removes the user from autocomplete immediately. **Open rounds where they were the answer are NOT deleted** (would leak by deletion timing); they're flagged `answer_optout=true` and the autocomplete excludes them, so the round becomes effectively unsolvable. Acceptable v1 tradeoff — document this in the optin modal.

### `/veil stats [user: optional]`
- No user → invoker's own stats.
- With user → that user's stats (public).
- Returns submitter stats + guesser stats (see Stats schema).

### `/veil leaderboard [category]`
- Categories: `submitters` (most rounds posted), `guessers` (most correct), `accuracy` (correct/total, min 10 guesses), `streaks` (longest current correct-guess streak), `hardest_crops` (highest avg guesses-to-solve, min 5 attempts).
- Top 10, ephemeral by default, public flag to share.

### `/veil round <round_id>`
- Mod-only. Inspect a specific round (submitter, answer, crop, guess log). For dispute resolution.

### `/veil delete <round_id>`
- Submitter or mod can delete their own round. Soft-delete (preserves stats integrity); message removed from channel.

## Crop Pipeline

Models loaded once at module init, kept in memory.

### Stage 1: Candidate region detection
- **NudeNet** (`nudenet` Python package, ONNX runtime, CPU). Returns labeled bounding boxes with confidence scores.
- Filter to `score >= 0.5`. Sort by score descending.
- If zero candidates after filter, fail submission.

### Stage 2: Face exclusion
- **OpenCV Haar cascade** or **mediapipe face detection** (mediapipe is more robust on partial faces; pin version for reproducibility).
- For each candidate region, compute IoU with every detected face. Drop candidates with `IoU > 0.1`. If a candidate is fully inside a face box, drop it.
- If all candidates eliminated, fall back to the highest-confidence non-face NudeNet box even with face overlap, but shrink the crop to exclude the face bbox region (clip to non-face area; if result < min crop size, fail submission).

### Stage 3: Crop selection & tightness
- Pick highest-confidence surviving candidate.
- Apply difficulty-tuned padding around the bbox:
  - `easy`: padding = 60% of bbox max-dimension (looser, more context)
  - `medium`: padding = 25%
  - `hard`: padding = 5% (extreme tight)
- Clamp to image bounds. Enforce minimum output size of 200x200; if smaller, scale up with bicubic to 200px on the short edge.
- Output: JPEG quality 85.

### Stage 4: Re-roll
- "Re-roll crop" button cycles through remaining ranked candidates. After exhausting candidates, button disables.

## Guessing Flow

### Posted message format
- Embed with crop image, "Round #<id>", "Submitted by an anonymous member" (submitter identity hidden until solved or round-deleted).
- Components: `[Guess]` button, `[How to play]` button (ephemeral explainer).

### Guess button → Dropdown
- Use a Discord **string select menu** (the dropdown component) populated dynamically with members holding the Veil role. Discord caps select options at 25, so paginate with prev/next buttons. Pre-filtered to exclude `answer_optout=true` users at the time of click (live snapshot, not round-creation snapshot — this is fine because opted-out users were already excluded).
- For larger servers where scrolling 25-at-a-time is tedious, also offer a `[Search by name]` button alongside the dropdown that opens a text modal; bot does fuzzy match against opted-in members and replies ephemerally with up to 5 matches as clickable buttons. Pick one to submit guess.

### Cooldown
- Per (user, round) pair. Stored in-memory + DB-backed for restart resilience. On cooldown, ephemeral: "Try again in Xs."

### Guess submission
- Insert guess row.
- If correct AND round not yet solved: mark round `solved_at`, `solver_id`. Edit the original message: replace the cropped attachment with the **submitter's full original image as a `SPOILER_`-prefixed file** (click-to-reveal blur), and update the embed to show "✅ Solved by <user> in <N> guesses (across <M> guessers)" and reveal submitter + answer. The on-disk original is then deleted and `original_path` cleared. Keep guess button live (now labeled "Guess late").
- If correct AND already solved: ephemeral "Correct — but <user> got there first."
- If wrong: ephemeral "Not it. Try again in <cooldown>s."

### Anti-collusion
- Submitter cannot guess on their own round (button rejects with ephemeral).
- Optional v1.5: detect rapid sequential correct guesses from alts (skip for v1).

## Reuse System (retired)

> **Status: retired.** The `/veil submit` `allow_reuse` flag and the four `reuse_*` config keys are no longer surfaced; no scheduler runs. The section below is kept for archival reference only — none of the eligibility, trigger, or stats rules below execute today.

When the channel goes quiet, the bot can re-post a previously-used crop as a new round to keep the game alive.

### Eligibility
A round's crop is eligible for reuse when ALL of the following hold:
- `allow_reuse = true` on the original round
- Original round is solved (`solved_at IS NOT NULL`)
- Original round is at least `reuse_min_age_days` old
- Submitter still holds the Veil role (consent is current)
- Submitter is not `answer_optout` flagged
- Original round is not soft-deleted
- This crop has not been reused in the last `reuse_min_age_days` (no back-to-back recycling of the same image)

### Trigger
A background scheduler in the cog runs hourly (per guild). For each guild with `reuse_enabled = true`:
1. Check time since last *new* submission posted to `veil_channel_id`. If less than `reuse_quiet_hours`, skip.
2. Check time since last *reuse* post in the channel. If less than `reuse_min_post_interval_hours`, skip.
3. Query eligible crops. If none, skip silently.
4. Pick one at random (uniform). Future v1.5: weight by submitter's `rounds_reused` count to spread the love.
5. Create a new round row with `is_reuse = true`, `original_round_id = <prior round id>`, same `submitter_id`, same `answer_id`, same `crop_path`/`crop_url`.
6. Post the crop to `veil_channel_id` with a "Guess" button. Embed footer: "🔁 Throwback round — this crop has appeared before."
7. The "throwback" tag is intentional: it warns guessers who remember, and frames the reuse as a feature rather than a stealth re-run.

### Solving a reused round
- Functions identically to a fresh round: first correct guess marks `solved_at` / `solver_id`, reveals submitter and answer.
- The answer reveal is the same person as the original round. Anyone who saw the original solve has the answer for free — accepted tradeoff, mitigated by the age gate and the explicit "🔁 Throwback" footer.

### Stats treatment
- Submitter: reuse rounds do NOT increment `rounds_posted`. They DO increment `rounds_reused` (new stat).
- Guessers: reuse rounds count fully toward all guesser stats (correct guesses, accuracy, streaks, first-solver count).
- `hardest_crops` leaderboard: combine attempts across original + all reuses of the same crop, weighted average. (Implementation: aggregate by `original_round_id` or `id` if not a reuse.)

### Mod controls
- `/veil reuse_pause` — pause reuse system for this guild without changing config.
- `/veil reuse_now` — mod-only. Force a reuse post immediately, ignoring the quiet-hours and post-interval gates. Useful for testing.
- `/veil reuse_block <round_id>` — mark a specific original round ineligible for future reuse (e.g. submitter quietly asked, mod judgment).

## Storage

### Image storage
- Original image: written to `./veil_cache/orig/<round_id><ext>` on submit and **deleted as soon as the round is solved** (immediately after the spoilered reveal is posted). Path is recorded in `veil_rounds.original_path` and cleared at unlink time. While unsolved, the bytes stay on disk so the bot can attach them to the reveal even after a restart.
- Cropped image: written to a private cache dir (`./veil_cache/<round_id>.jpg`) so re-posts/edits work; deleted on round deletion.
- Discord CDN URL of the posted crop is stored in the round row as the canonical reference. If the cache file is missing (e.g. after restart cleanup), the bot uses the CDN URL.
- **The original is retained only until first correct guess; it is never reused for future rounds and never published unspoilered.** Document this prominently in the optin modal so submitters understand the (short) retention window.

### Database (SQLite via aiosqlite, matches existing DK pattern)

```
veil_rounds
  id INTEGER PK
  guild_id INTEGER
  submitter_id INTEGER
  answer_id INTEGER  -- who is in the picture (= submitter for v1; future: submitter can pick someone else who's opted in)
  channel_id INTEGER
  message_id INTEGER
  crop_path TEXT  -- local cache path
  crop_url TEXT   -- discord CDN url
  difficulty TEXT
  candidate_count INTEGER  -- nudenet detections, for stats
  reroll_count INTEGER
  allow_reuse BOOLEAN DEFAULT 0
  is_reuse BOOLEAN DEFAULT 0  -- this row is a reuse-triggered round
  original_round_id INTEGER NULL  -- if is_reuse, points to the source round
  reuse_blocked BOOLEAN DEFAULT 0  -- mod set this to prevent future reuses
  created_at TIMESTAMP
  solved_at TIMESTAMP NULL
  solver_id INTEGER NULL
  guesses_to_solve INTEGER NULL  -- across all guessers
  unique_guessers_to_solve INTEGER NULL
  answer_optout BOOLEAN DEFAULT 0
  deleted_at TIMESTAMP NULL

veil_guesses
  id INTEGER PK
  round_id INTEGER FK
  guesser_id INTEGER
  guessed_user_id INTEGER
  correct BOOLEAN
  created_at TIMESTAMP

veil_optins
  user_id INTEGER
  guild_id INTEGER
  opted_in_at TIMESTAMP
  PRIMARY KEY (user_id, guild_id)
  -- Source of truth is the role; this table tracks consent acknowledgment timestamp for audit.

veil_config
  guild_id INTEGER PK
  veil_role_id INTEGER
  veil_channel_id INTEGER
  guess_cooldown_seconds INTEGER
  crop_difficulty TEXT
  min_image_dimension_px INTEGER
  max_image_size_mb INTEGER
```

Indexes: `veil_guesses(round_id)`, `veil_guesses(guesser_id)`, `veil_rounds(guild_id, created_at)`, `veil_rounds(submitter_id)`, `veil_rounds(guild_id, allow_reuse, solved_at, reuse_blocked)` (composite for the reuse eligibility query).

## Stats Schema

### Submitter stats (per guild)
- Rounds posted (original submissions only)
- Rounds reused (times the bot recycled their crops)
- Rounds solved / unsolved
- Avg guesses-to-solve (across solved rounds, original + reuse aggregated by crop)
- Avg unique guessers-to-solve
- Hardest crop (highest guesses-to-solve)
- Most-rerolled rounds count

### Guesser stats (per guild)
- Total guesses
- Correct guesses
- Accuracy (correct / total)
- First-solver count (won the round)
- Current correct-guess streak
- Longest correct-guess streak
- Fastest solve (time from round-post to correct guess, first-solver only)

## Module Structure

```
dungeon_keeper/cogs/veil/
  __init__.py
  cog.py                # main VeilCog, command registration
  commands/
    submit.py
    guess.py
    stats.py
    leaderboard.py
    admin.py            # round inspect, delete, config
    optin.py
  services/
    crop_pipeline.py    # NudeNet + face detection orchestrator
    nudenet_client.py   # model loader, inference
    face_detector.py    # mediapipe wrapper
    crop_renderer.py    # PIL crop + resize + jpeg encode
    reuse_scheduler.py  # hourly background task, eligibility query, post trigger
  ui/
    guess_dropdown.py   # dropdown pagination + search modal
    submit_preview.py   # ephemeral preview + reroll buttons
  data/
    models.py           # dataclasses
    repo.py             # aiosqlite queries
  config.py
  permissions.py        # role checks
```

## Dependencies (additions to DK requirements.txt)

- `nudenet>=3.0` (ONNX-based, CPU)
- `mediapipe>=0.10` (or `opencv-python` if mediapipe is too heavy)
- `Pillow>=10`
- `numpy` (already present likely)

Model files are downloaded on first run and cached. Document model storage location in module README.

## Acceptance Criteria

### Submission
- [ ] Non-Veil-role member invoking `/veil submit` gets ephemeral rejection.
- [ ] Submission with no NudeNet detections gets ephemeral rejection with clear message.
- [ ] Submission with detected face fully overlapping all candidate regions either succeeds (face clipped out) or fails gracefully with explanation.
- [ ] Successful submission posts a publicly visible cropped image in `veil_channel_id`.
- [ ] Submitter sees ephemeral preview with "Re-roll crop" button before posting.
- [ ] Re-roll button cycles candidates and disables after exhaustion.
- [ ] Original uploaded image is never written to persistent storage (verify via filesystem inspection during integration test).

### Crop pipeline
- [ ] Face is excluded from final crop in 95%+ of test images with detectable faces (build a 20-image test set covering varied poses, lighting, partial faces).
- [ ] `easy`/`medium`/`hard` produce visibly different crop tightness on the same source image (snapshot test).
- [ ] Pipeline completes in <3 seconds on the home server hardware for a 4MB image.

### Guessing
- [ ] Guess button opens a dropdown populated only with current Veil-role-holders excluding `answer_optout=true` users.
- [ ] Dropdown paginates correctly past 25 members.
- [ ] Search-by-name button opens text modal and returns up to 5 fuzzy matches as clickable buttons.
- [ ] Submitter cannot guess on own round.
- [ ] Cooldown enforced per (user, round) and survives bot restart.
- [ ] First correct guess marks round solved, edits message, reveals submitter + answer.
- [ ] Late correct guesses get ephemeral acknowledgment but don't change `solver_id`.

### Stats
- [ ] `/veil stats` returns accurate counts for both submitter and guesser views.
- [ ] `/veil leaderboard` for each category returns correctly ranked top 10.
- [ ] Streak correctly resets on a wrong guess and increments on a correct one (per-guild scoped).
- [ ] Soft-deleted rounds excluded from stats; round deletion does not orphan guess rows.

### Opt-out
- [ ] Opting out removes role and immediately drops user from new guess autocompletes.
- [ ] Existing rounds where opted-out user is the answer get flagged and become unsolvable; round message remains visible (no leak via deletion).

### Reuse
- [ ] Submission with `allow_reuse=false` (default) never appears in reuse eligibility queries.
- [ ] Submission with `allow_reuse=true` becomes eligible only after solve + `reuse_min_age_days`.
- [ ] Reuse scheduler skips guilds where time-since-last-submission < `reuse_quiet_hours`.
- [ ] Reuse scheduler skips guilds where time-since-last-reuse < `reuse_min_post_interval_hours`.
- [ ] Reused crops get a "🔁 Throwback round" footer in the embed.
- [ ] Reused round increments `rounds_reused` for submitter, not `rounds_posted`.
- [ ] Reused round counts fully toward guesser stats.
- [ ] Submitter losing the Veil role makes all their crops immediately ineligible for reuse.
- [ ] `/veil reuse_block <round_id>` prevents that round's crop from being picked again.
- [ ] `/veil reuse_now` (mod) triggers a reuse post immediately, bypassing time gates.
- [ ] `hardest_crops` leaderboard aggregates across original + all reuses of the same crop.

## Testing Strategy

Per the four-tier pytest pattern already established for DK:

1. **Unit**: crop pipeline stages with fixture images (synthetic + a small curated NSFW test set kept out of repo, loaded from `VEIL_TEST_IMAGES_DIR` env var).
2. **Integration**: full submit→post→guess→solve flow against mocked Discord client.
3. **Second-server**: deploy to test guild, exercise all commands with a small Veil-roled test cohort.
4. **Production smoke**: post-deploy, run `/veil submit` with a known-good fixture and verify channel post.

## Out of Scope for v1

- Web crop override UI (deferred per spec discussion).
- Submitter posting on behalf of another member (`answer_id != submitter_id`). Schema supports it but commands don't expose it.
- Per-round difficulty override (uses guild default only).
- Cross-guild stats / global leaderboards.
- Image moderation beyond NudeNet's existing labels (e.g. age verification — out of scope, rely on Discord ToS + Veil-role gating + optin consent).
- Alt-account collusion detection.

## Open Questions for Implementation

1. **NudeNet model variant**: 320n (fastest) vs 640m (more accurate). Recommend starting with 320n; revisit if face exclusion accuracy is poor.
2. **mediapipe vs OpenCV Haar for face detection**: mediapipe is better on partial/occluded faces but heavier. Start with mediapipe; fall back to Haar if install footprint is a problem.
3. **Cache cleanup policy**: cron-style cleanup of `veil_cache/` for rounds older than N days where the Discord message still exists (CDN URL takes over)? Or never clean? Suggest: clean at 30 days, document the tradeoff (re-edits to old messages will lose the local crop).
