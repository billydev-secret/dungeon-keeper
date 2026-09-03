# Guess — Feature Spec

A guess-the-member image game. A consenting submitter posts an image — **SFW or NSFW**; the bot detects an "interesting" region, offers a crop editor to frame it (faces filtered out where possible), and posts the crop to a dedicated channel. Anyone can guess the member from a picker restricted to opted-in members. All-time posting/solving totals are tracked per guild.

> **SFW support (2026-07-27):** Guess was originally NSFW-only, and the web config route rejected any channel without Discord's age-restricted flag. That check is gone — submissions may be SFW or NSFW, and **the bot enforces nothing about where they are posted**. This is a deliberate call by the server owner: placement is policed by moderators, not by the bot. Note the runtime never had an `is_nsfw()` recheck either (the post-time one was removed in 47ca6a5), so the config route was the last vestige of the gate, and it was bypassable anyway — you could save an age-gated channel and then clear the flag. There is no per-submission explicitness verdict stored; NudeNet's detections drive crop selection only.

> **History (2026-06-01):** This feature was originally called "Veil"; an internal rename moved every table, command, cog, and web panel to `guess_*`. The old `/veil` slash commands, cog, and web panels were deleted on the same day. The product is Guess only; there is no Veil variant.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/guess submit <image>` | Slash | Guess role (required — errors if the role isn't configured, or if you don't hold it) | Submit an image (SFW or NSFW); runs the detection pipeline and opens the crop editor / post flow |
| `/guess optin` | Slash | Everyone (errors if the Guess role isn't configured) | Opens a consent view disclosing what joining stores (cached originals, confession text + your id, stats) — the role is granted only on **Join the pool** |
| `/guess optout` | Slash | Everyone | Self-service leave — removes the Guess role; open rounds where you're the answer flip `answer_optout`, past rounds and stats stay |
| `/guess leaderboard` | Slash | Everyone | Posts the top 5 submitters (rounds posted/solved) and top 5 guessers (rounds solved) — fixed, no arguments or categories |
| `/guess round <round_id>` | Slash | Mod (`manage_guild`; hidden from non-mods in the Discord UI via `default_permissions`) | Inspect a specific round (status, submitter, answer, crop, guess/unique-guesser counts, re-roll count) |
| `/guess delete <round_id>` | Slash | Submitter or Mod (`manage_guild`; checked in code only — the command itself isn't permission-restricted client-side) | Soft-delete a round (message best-effort deleted, stats preserved) |
| `/guess confess text:<...>` | Slash | Guess role (requires both the role and the channel to be configured) | Renders an anonymous text confession as an image card and previews it for you to post or cancel |
| Guess Who Submit Prompt | Web (dashboard) | Admin | Config → Guess Who → Post Submit Prompt immediately (re)posts the sticky Submit/Help prompt at the bottom of the configured guess channel. Replaced `/guess prompt` 2026-07-28 |
| `Guess` button (on round post) | Persistent | Everyone except the round's submitter | Opens an ephemeral member picker (see Guessing below) |
| `🎭 Submit Guess` / `❓ Help` buttons (on the sticky prompt) | Persistent | Everyone (Guess role enforced on submit) | Submit opens a URL-paste modal that feeds the same detection pipeline as `/guess submit`; Help shows a short how-to-play blurb |
| Web config panel | Web (dashboard) | Admin | Per-guild role, channel, cooldown, difficulty, image limits. Any channel may be chosen — age-gated or not. The API only checks the channel exists, and only when the bot can resolve the guild |
| Web audit log | Web (dashboard) | Mod | Recent submit / delete / solve / guess-cap events |

There is no `/guess stats` command — it doesn't exist in code.

Bot perms required: **Send Messages**, **Embed Links**, **Attach Files**, **Read Message History** in the guess channel; **Manage Roles** to grant the consent role on `/guess optin`. The bot never removes the role itself — see "Consent and opt-in" below.

## Behavior

### Consent and opt-in

There's a configured **consent role** (`guess_role_id`). It gates submitting (`/guess submit` and the sticky-prompt URL modal), posting confessions (`/guess confess`), and being a pickable answer. Unlike some other guild features, this role has no "Everyone if unset" fallback: if it isn't configured, `/guess submit` and `/guess confess` both refuse with a message telling the user to ask an admin to configure it in the web dashboard. The bot will make that role for you, but **only while you are offering it to members** on Config → Discord Onboarding (create-on-offer, 2026-09-03): creating `@Guess Who` on its own would turn an honestly-unconfigured game into a configured one refusing every member, since nobody would hold the new role. Its live state is on Config → Bot-Managed Roles (`docs/role_provisioning_spec.md`).

- `/guess optin` (since 2026-08-06) opens an ephemeral **consent view** stating what joining stores — original submissions cached on disk until solve (unsolved rounds cleared after 90 days), confession text stored with the author's id and admin-visible, rounds/stats kept — and grants the role only on the **Join the pool** button. Already-holding members just get told so.
- **The consent is evidenced, not just taken** (since 2026-08-06, migration 154). Clicking Join writes a `guess_consents` row: `consented_at` and the `disclosure_version` of the wording that was on screen. GDPR Art 7(1) puts the burden on the controller to *demonstrate* consent, and the Discord role demonstrates only that someone holds it — not what they were shown or when. Bump `GUESS_DISCLOSURE_VERSION` (`guess_repo.py`) whenever the disclosure changes materially: rows carrying an older version recorded agreement to wording the member never saw, and that cannot be reconstructed afterwards.
  - The write happens **after** the role grant succeeds — a consent row for a member who never received the role would misrepresent what happened — and a failure to write it is logged rather than raised, so a DB hiccup can't cost a member their opt-in.
  - **Withdrawal stamps, it does not delete** (`withdrawn_at`). Art 7(3) makes withdrawal as easy as giving consent, but the record that consent *was* held, and for how long, is what makes the processing done under it defensible. Rejoining writes a second row rather than overwriting the first.
  - It is stamped in the **role-removal listener**, not in `/guess optout` — that listener is the one choke point both the command and a mod's manual role removal pass through.
  - `guess_consents` joins `purge_user_data`: a full erasure clears it, an optout does not.
- **`/guess optout`** (since 2026-08-06) removes the role self-service; a mod removing the role in Discord works identically. Either way the `on_member_update` listener flags any of that member's **open** rounds (where they're the answer) as `answer_optout` in the DB. The round itself is not deleted or hidden — its Guess button still works, but clicking it now replies "This round is no longer solvable — the answer opted out." This flag is permanent for that round; re-adding the role later does not clear it.

### `/guess submit` — the crop pipeline

The submitter uploads an image (via `/guess submit <image>`, or via the sticky prompt's URL-paste modal). Per-user submissions are also rate-limited in-memory to `guess_submit_max_per_window` per rolling `guess_submit_window_seconds` (defaults: 5 per hour); past that the bot replies "You've hit the submission limit (N per window). Please wait a bit before submitting again." — the message templates the configured values (resets on bot restart). The bot then:

1. Validates MIME, dimensions (≥ configured min), and file size (≤ configured cap).
2. Saves the original to an on-disk cache, keyed by round id, retained **only** until first correct solve.
3. Runs **candidate detection** — combines NudeNet detections with a separate pose-based detector, merges adjacent different-type genital detections into a single "sex act" candidate, and re-weights scores so more "interesting" regions (genitals/breasts/buttocks) outrank incidental ones (belly/armpits). Candidates overlapping a detected face are filtered out, with a fallback to the single highest-scoring detection (even if it overlaps a face) if that would otherwise eliminate everything. **If no detector returns anything at all, the submission is not rejected** — the editor still opens with a default centered crop box and the note "No detections found — manually frame your crop, then ✓ Post." (Auto is disabled, since there are no candidates to cycle.) This is the normal path for an SFW submission with no person in it — a pet, a desk, a tattoo close-up. Note that pose landmarking works on clothed bodies, so an SFW photo *of a person* still produces torso/hip/thigh candidates and crops automatically.
4. Applies difficulty-tuned padding around the top candidate: **easy** = looser (more context), **medium** = moderate, **hard** = tight crop. Output is clamped to image bounds and expanded if smaller than the minimum.
5. Opens an ephemeral **crop editor**: a D-pad view (move up/down/left/right, zoom in/out, an **Auto** button that cycles through the ranked detected candidates, **✓ Post**, and **✗** cancel). There is no fixed re-roll cap — the submitter can nudge/zoom/cycle as many times as they like before posting. Cancelling before posting discards the submission entirely (nothing is written to the DB until Post).
6. On Post, the crop posts publicly to the guess channel with a **Guess** button, and the bot best-effort reposts the sticky channel prompt underneath it.

### Guessing

Clicking **Guess** first checks that the round hasn't been flagged `answer_optout` and that the clicker isn't the submitter, then opens an ephemeral string-select dropdown of opted-in members, paginated 25 per page (◀/▶ buttons appear when there's more than one page). A **🔍 Filter** button opens a modal for a text query; matches are scored (exact name match, then prefix, then substring, then subsequence) and the same select is rebuilt with the filtered/reordered results, with a **✕ Clear** button to reset. Submitters cannot guess on their own rounds.

Guesses are capped per (user, round) at `guess_max_guesses_per_round` total (default 5) — past that the bot replies "You're out of guesses on this round (cap: N)." with the configured cap. Below that cap, there's also a per-(user, round) cooldown, configurable (default 60 s; 0 disables it). On cooldown, the guesser sees "⏳ On cooldown — you can guess again <t:...:R>." (a Discord relative timestamp). A wrong guess gets "❌ Not it. Keep trying!".

On a **correct first solve**, the bot edits the round's message: the original image (the full submission, not the crop) is attached as a spoiler-prefixed file (click-to-reveal blur), and the embed updates to show "✅ Round #N — Solved!" with the answer, submitter, and "Solved by {user} in N guesses (across M guessers)". All three are **resolved display names**, not `<@id>` mentions: an embed mention is resolved by the reading client from its own cache, so it shows as a bare number to anyone who hasn't seen that member (`services/name_resolver.py`; see `embed_style_guide.md` § Naming members in embeds). The **one exception** is a submitter/answer pair on the no-contact list — there both degrade to a plain "User <id>", because naming the two together in the bot's own voice is the association the list exists to prevent. The solver is always named: a no-contact partner of either side can't have guessed the round. The on-disk original is deleted at this point. The Guess button stays live (now labeled "Guess late") for late correct guesses, which get a generic ephemeral "✅ Correct — but someone already solved this one." (it does not name the first solver).

### Leaderboard

`/guess leaderboard` takes no arguments and always posts both lists together, non-ephemeral, naming every member by resolved display name rather than `<@id>` (the lists rank *past* posters, exactly the people a reader's client is least likely to have cached): **Top Posters** (top 5 by rounds posted, tie-broken by rounds solved, each shown as "posted, solved (pct%)") and **Top Guessers** (top 5 by rounds solved as first correct guesser). Both exclude soft-deleted rounds. There is no per-user `/guess stats` command, no accuracy/streak/hardest-crop tracking, and no leaderboard category argument — none of that is implemented.

### Mod tools

`/guess round` shows a specific round to a mod: status (open / solved / deleted), submitter, answer — each as `Name (id)`, keeping a copyable identifier next to the resolved name, as the whisper mod-log embeds do — difficulty, guess and unique-guesser counts, re-roll count, and the crop image. For dispute resolution. `/guess delete` soft-deletes a round (message best-effort deleted, stats survive; already-deleted rounds are rejected with "Round #N is already deleted."). The web audit panel lists recent submit, delete, solve, and cap events for the guild.

Config → Guess Who → Post Submit Prompt lets an admin force an immediate repost of the sticky Submit/Help prompt message in the configured channel (normally it reposts itself automatically ~2s after the last message in the channel, debounced). Useful if the sticky prompt gets buried or its message is deleted. Re-running it when the prompt is already in that channel **edits it in place** rather than hopping it to the bottom, so a re-brand refresh doesn't move it.

Since 2026-08-06 the prompt runs on the shared `core.sticky.StickyPanel` rather than its own copy of the placer — it was the last hand-rolled one, and it still posted *after* deleting the old prompt (a failed send left the channel with no prompt at all) and left placements unshielded (a cancel landing mid-send orphaned a prompt whose id was never recorded). The prompt is also the reason the shared placer grew a per-panel `target_types`: the Guess channel may be a thread or a voice channel's text view, which the placer previously refused. Behaviour a member sees is unchanged, including the ~2s debounce.

## Permissions

- `/guess submit`, `/guess confess`: require the consent role to be configured **and** held by the caller — there is no "Everyone if unset" fallback; an unconfigured role blocks both.
- `/guess optin`: anyone can run it, but it errors if the consent role isn't configured. No opt-out equivalent exists as a command.
- `/guess leaderboard`: Everyone, no arguments.
- `/guess delete` on your own round: submitter; on someone else's round: Mod (`manage_guild`). This check is enforced in code only — the command isn't hidden from non-mods client-side.
- `/guess round`: Mod (`manage_guild`), and also hidden from non-mods in the Discord UI via `default_permissions`.
- Submitter cannot guess on their own round.

## User-visible errors

| When | The user sees |
|---|---|
| `/guess submit` / `/guess confess` with the Guess role unconfigured | "Guess role is not configured. Ask an admin to set it in the web dashboard." (confess: "Guess is not fully configured...", since it also requires the channel) |
| `/guess submit` without the consent role | "You need the Guess role to submit." |
| Submission rate limit hit (per user, in-memory, config-backed) | "You've hit the submission limit (N per window). Please wait a bit before submitting again." — templates `guess_submit_max_per_window` / `guess_submit_window_seconds` |
| Image not an image / too small / too large | "Please submit an image file.", "Image too small. Minimum dimension is Npx.", or "Image too large. Maximum is N MB." |
| No detections found in the image | **Not an error** — the crop editor opens with a manual default box: "No detections found — manually frame your crop, then ✓ Post." |
| Submitter clicks Guess on their own round | "You can't guess on your own round." |
| Guess on a round the answer opted out of | "This round is no longer solvable — the answer opted out." |
| Guess cap hit (one user exhausts `guess_max_guesses_per_round` on one round) | "You're out of guesses on this round (cap: N)." — templates the configured cap |
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
- **Round reuse / throwback rounds.** Earlier drafts had a reuse system; it's been removed. Its four columns on `guess_rounds` (`allow_reuse`, `is_reuse`, `original_round_id`, `reuse_blocked`) outlived it as write-only ballast and were dropped in migration 184, along with the dataclass fields, the insert parameters and `get_reusable_rounds`, which had no caller outside its own tests. Note that `idx_guess_rounds_reuse` keeps the old name but has indexed `(guild_id, submitter_id, image_hash)` since migration 020 — it is the duplicate-image guard and is unrelated.
- **Web crop override UI.**

## Configuration

| Key | Default | Purpose |
|---|---|---|
| `guess_role_id` | unset | Consent / eligibility role. Unlike some other guild features, unset does **not** open submit/confess to Everyone — it blocks both until an admin configures it |
| `guess_channel_id` | unset | Where crops post and modals launch from |
| `guess_guess_cooldown_seconds` | `60` | Per-user, per-round cooldown between guesses (`0` disables it) |
| `guess_max_guesses_per_round` | `5` | Per-user, per-round total guess cap |
| `guess_inactivity_ping_hours` | `0` | Hours a round may sit with no guesses before the Guess role is pinged once about it (`0` disables the nudge). Bounded at `168` (one week) — the same ceiling the nudge itself applies, so the dial can't be set into a range that would never fire. See "Inactivity nudge" below |
| `guess_last_nudged_round_id` | unset | Internal state, not an admin dial: the round this guild was last nudged about, so a long-unsolved round is only ever pinged once |
| `guess_submit_max_per_window` | `5` | Per-user submission rate limit — max submissions per rolling window (in-memory, resets on restart) |
| `guess_submit_window_seconds` | `3600` | Length of the submission rate-limit window |
| `guess_crop_difficulty` | `medium` | `easy` / `medium` / `hard` — controls crop editor padding, not a per-round choice |
| `guess_min_image_dimension_px` | `400` | Reject submissions smaller than this on either axis |
| `guess_max_image_size_mb` | `10` | Hard cap on upload size |
| `guess_prompt_message_id` | unset | Persistent prompt message at the bottom of the channel |
| `guess_prompt_channel_id` | unset | Where that message actually **is**. Added 2026-08-06 with the `core.sticky` migration: the placer deletes the old prompt through this channel, so pairing a stale message id with a repointed `guess_channel_id` would aim the delete at the wrong channel and strand the old prompt with its buttons live. Repointing the Guess channel therefore leaves the prompt where it is until the next round (or an explicit repost) moves it. **Legacy rows fall back to `guess_channel_id`**: guilds that already had a prompt when this key was added have a message id and no channel id, and before the key existed the prompt was always posted into `guess_channel_id` |

## Inactivity nudge

`guess_inactivity_ping_hours` was offered by the Config Advisor from the start
with no reader behind it — the dial stored a number and nothing ever nudged. It
now has one (`services/guess_nudge_service.py`, driven by a 15-minute loop in
the cog), and a control on **Config → Guess Who** ("Nudge After Silence").

The nudge fires for an **open round that has gone quiet**, never for an empty
channel: a ping saying "come play" when nothing is posted to guess is noise,
and the dial's wording is about silence on something already running.

Each tick, per guild:

* off unless the hour count is positive **and** both `guess_channel_id` and
  `guess_role_id` are set — with no role there is nobody to ping, so the nudge
  would be a bare bump;
* the candidate is the **oldest** round that is unsolved, not soft-deleted, and
  not `answer_optout`, whose last activity — the round going up, or its newest
  guess — falls inside a window: at least the dial's hours old, and **no more
  than `MAX_QUIET_HOURS` (168, one week) old**;
* a round is nudged **at most once**, however long it stays unsolved. The last
  nudged round id is stored per guild in `guess_last_nudged_round_id` and
  excluded from the next search;
* the id is recorded only **after** the message posts, so a send that fails
  (missing permissions, deleted channel) retries on the next tick rather than
  silently burning the round.

The **one-week ceiling** (`MAX_QUIET_HOURS`) is what makes "oldest first" safe,
and it was added on 2026-08-30 after the nudge pinged the role about a round
abandoned in May: "quiet for **2704 hours**". The dial is a *minimum* silence,
so with no maximum the search handed the ping to the most ancient unsolved round
in the guild's history and kept it there. Worse, only one nudged id is
remembered (`guess_last_nudged_round_id`), so each tick burned exactly one
ancient round — production had 31 unsolved rounds from May queued ahead of
everything recent, roughly a month of nudges before the loop would have reached
a round anyone was still playing. Past the ceiling a round is not stale, it is
over: it drops out of consideration permanently, which fixes both the ancient
ping and the backlog walk in one clause. Those old rounds are otherwise left
alone — they stay in the table, unsolved and unreachable by the nudge.

A dial set above the ceiling would leave the window empty and nudge nothing; the
panel, the config route and `settings_registry` all cap it at 168 so that state
can only arise from a value stored under the old 720-hour bound.

The message is plain content, not an embed — a role mention plus a jump link to
the round, so the link previews. Mentions are allow-listed to that one role.

## Stored data

Rounds (one row per submission with crop / answer / solver / counts), guesses (one row per guess attempt), and an audit log (submit / delete / solve / guess-cap events) per guild.

**There is no opt-in table.** Eligibility — who may submit, and who is pickable as an answer — is derived live from Discord role membership (`guess_role.members`), and that is the single source of truth. A `guess_optins` table (`veil_optins` before migration 020) used to exist with a full CRUD layer in `guess_repo.py`, but nothing in the cog or web server ever called it; it was empty in production and was dropped in migration 136 along with its dead functions. Deriving eligibility from the role is what makes a mod removing the role take effect immediately, and it's why the `answer_optout` round flag (see "Consent and opt-in") is the only persisted consent state.

Filesystem cache: original submissions live in a per-round file on disk **only until first correct solve**, at which point the file is deleted and the path cleared. Crops live on disk for the round's lifetime and are deleted on round deletion. The Discord CDN URL of the posted crop is the canonical reference if the local cache is missing.

The original image is never reused for a future round and never published unspoilered. (Since 2026-08-06 this retention policy is disclosed in the `/guess optin` consent view, before the role is granted.)
