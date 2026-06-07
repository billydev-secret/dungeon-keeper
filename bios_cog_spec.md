# Dungeon Keeper — Bios Cog Specification

**Target:** Claude Code implementation handoff
**Stack:** Python · discord.py · aiosqlite · existing DK web dashboard (Discord OAuth)
**Status:** Functional spec — describes behavior, not implementation. No code.

---

## 1. Overview

A wizard-driven member bio system for The Golden Meadow.

A member triggers the wizard, the bot creates a private, single-use channel, and walks them one step at a time through a community-authored set of profile fields plus a rotating pool of icebreaker questions. On completion the bot renders a styled embed and posts it to the bios channel, then destroys the wizard channel. Bios are edited in place on update and removed when a member leaves the server.

Profile fields and the icebreaker pool are not hardcoded — they are managed entirely through the DK web dashboard.

---

## 2. Core principles

1. **Fully data-driven.** There is no hardcoded profile field set. The active template defines every field, and the cog renders whatever is configured. The only fixed concept is *which* field is designated the headline (see §7).
2. **Snapshot on answer.** Field labels and question text are stored alongside the user's answers at the moment they answer. Editing the template or retiring a question later never silently rewrites an already-posted bio.
3. **Edit in place.** Updating a bio edits the existing message, preserving its position in the channel. The bot reposts only as a fallback when the original message is gone (404).
4. **Ephemeral wizard state.** Nothing is written to the bios tables until the wizard completes successfully. An abandoned, cancelled, or timed-out wizard leaves no database rows — no half-bios.
5. **Create and destroy the venue.** The wizard runs in a throwaway private channel that exists only for the duration of one session, then is deleted.

---

## 3. Data model

All tables are guild-aware. Use the existing `dungeonkeeper.db` and `aiosqlite`. Foreign keys reference for clarity; enforce in application logic if PRAGMA foreign_keys is not enabled project-wide.

### 3.1 `bio_templates`
One template per guild, versioned. The active version defines the current field set.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `guild_id` | INTEGER | |
| `version` | INTEGER | increments on field-set change |
| `active` | INTEGER | 0/1; exactly one active per guild |
| `created_at` | TEXT | ISO 8601 |

### 3.2 `bio_fields`
The profile fields belonging to a template, rendered in order.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `template_id` | INTEGER | FK → bio_templates.id |
| `key` | TEXT | stable internal identifier, e.g. `gender`, `dms` |
| `label` | TEXT | display label, e.g. "How you found The Golden Meadow" |
| `field_type` | TEXT | `short` \| `paragraph` \| `choice` |
| `choices` | TEXT | JSON array; used only when `field_type = choice` |
| `required` | INTEGER | 0/1 |
| `is_headline` | INTEGER | 0/1; exactly one field per template flagged 1 (see §7) |
| `sort_order` | INTEGER | render + wizard order |
| `active` | INTEGER | 0/1; soft-retire, never hard-delete |
| `max_len` | INTEGER | per-field char cap, default 1024 |

### 3.3 `bio_questions`
The rotating icebreaker pool. Guild-scoped, independent of templates.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `guild_id` | INTEGER | |
| `prompt` | TEXT | the question text |
| `active` | INTEGER | 0/1; soft-retire |
| `weight` | INTEGER | selection weighting, default 1 |
| `created_at` | TEXT | |

### 3.4 `bios`
Tracks each posted embed message.

| Column | Type | Notes |
|---|---|---|
| `user_id` | INTEGER PK | one bio per user per guild |
| `guild_id` | INTEGER | |
| `message_id` | INTEGER | the posted embed |
| `channel_id` | INTEGER | the bios channel at post time |
| `created_at` | TEXT | |
| `updated_at` | TEXT | |

### 3.5 `bio_field_values`
A user's answers to template fields. Label snapshotted.

| Column | Type | Notes |
|---|---|---|
| `user_id` | INTEGER | |
| `field_id` | INTEGER | FK → bio_fields.id |
| `field_label` | TEXT | snapshot at answer time |
| `value` | TEXT | |
| | | PK (`user_id`, `field_id`) |

### 3.6 `bio_answers`
A user's answers to rotating questions. Keyed by stable **slot** index so a per-slot re-roll is a clean overwrite and embed order stays stable. Question text snapshotted.

| Column | Type | Notes |
|---|---|---|
| `user_id` | INTEGER | |
| `slot` | INTEGER | 0..N-1, stable display order |
| `question_id` | INTEGER | FK → bio_questions.id |
| `question_text` | TEXT | snapshot at answer time |
| `answer` | TEXT | |
| | | PK (`user_id`, `slot`) |

---

## 4. Per-guild configuration

Stored in the cog's config (reuse DK's existing per-guild config mechanism; these are the keys the cog reads):

| Key | Purpose | Suggested default |
|---|---|---|
| `bios_channel_id` | where embeds are posted | — (required) |
| `wizard_category_id` | category under which wizard channels are created | — (required) |
| `questions_per_bio` | N questions drawn per bio | 3 |
| `embed_color` | single ember accent, identical across all bios | `0xC8763E` |
| `wizard_timeout` | idle minutes before auto-cancel | 15 |
| `archive_grace` | seconds after completion before the wizard channel is deleted | 60 |

---

## 5. Wizard flow

### 5.1 Trigger
- `/bio` slash command, **or**
- a persistent **"Create / Update Bio"** button posted in the bios channel. The button uses a fixed `custom_id` and its View is re-registered on cog load so it survives bot restarts.

### 5.2 Session setup
- **One active session per user.** Re-triggering while a session is live prompts the user to resume or restart, and never spawns a second wizard channel.
- On trigger, look up the user's `bios` row for this guild → determines **new** vs **edit** mode.
- Create a **private text channel** under `wizard_category_id`:
  - Named `bio-{user_id}` (collision-free and identifiable for orphan cleanup).
  - Permission overwrites: deny `@everyone` view; allow the triggering user and the bot to view and send. No one else can see it.
- Initialize in-memory session state: mode, ordered active field list (by `sort_order`), drawn question slots, current step index, collected answers so far, and (edit mode) the user's existing values/answers for pre-fill.

### 5.3 Question draw
- Draw `questions_per_bio` questions from the active pool using weighted-random selection, no duplicates within the session.
- In **edit** mode, do **not** re-draw. Load the user's existing answered questions from `bio_answers` into their original slots.

### 5.4 Stepping through fields
Walk the active field list in `sort_order`. Behavior per `field_type`:

- **`short` / `paragraph`** — post the field label as a prompt; capture the user's next message in the channel via `wait_for("message", check=...)` scoped to this channel + this author.
- **`choice`** — post the field label with a selection control: **buttons if ≤5 choices, a select menu otherwise**. Capture via the component interaction.

Per-step controls (rendered as buttons on the prompt):
- **Skip** — offered only when the field is not `required`. A required field will not advance until answered.
- **Back** — return to the previous step to re-answer.
- **Cancel** — abort: delete the channel, discard all in-memory state, write nothing.

Validation: enforce `max_len` at input; if exceeded, re-prompt with the limit noted. Long input is caught here so the embed-side truncation (§6.2) is only a safety net.

### 5.5 Stepping through questions
After the last field, step through the drawn question slots in order. Each slot has the same **Skip / Back / Cancel** controls, plus:

- **Re-roll (🎲)** — discards the current slot's question and draws a fresh one from the active pool, excluding every question already drawn this session (all other slots) and the slot's current question. The user then answers the new question.
  - If the active pool offers no distinct alternative (pool too small / all drawn), the re-roll control is disabled with a brief inline note ("No other questions available right now").

### 5.6 Edit-mode pre-fill
When editing, each step shows the user's stored value/answer and lets them keep it (send `keep`) or send a replacement.
- A field step pre-fills with the stored `value`.
- A question step pre-fills with the stored `question_text` + `answer`. Re-rolling on a question step in edit mode discards the old question entirely and replaces that slot — new `question_id`, new `question_text`, new `answer`.

### 5.7 Timeout
If the session is idle longer than `wizard_timeout`, auto-cancel: delete the channel, discard state, and (if possible) notify the user briefly via the triggering context.

### 5.8 Completion
- Render the embed (§6).
- **New:** post to the bios channel; insert the `bios` row, all `bio_field_values` rows, and all `bio_answers` rows. Wrap the writes so a posting failure does not leave partial rows.
- **Edit:** fetch the stored `message_id`; rebuild the embed; edit the message in place; overwrite the user's `bio_field_values` and `bio_answers` rows; update `bios.updated_at`. On **404** (message gone), repost to the bios channel and update `message_id` + `channel_id`.
- Post a confirmation in the wizard channel with a jump link to the posted bio.
- After `archive_grace` seconds, delete the wizard channel.

---

## 6. Embed rendering

The embed is built **from the user's snapshotted values/answers**, never from live template lookups — so a posted bio always reflects exactly what the user entered, even if the template or question pool changed afterward.

### 6.1 Structure
- **Author line:** member display name, with the member avatar as the author icon.
- **Title:** the value of the field flagged `is_headline` (the name/nickname field).
- **Thumbnail:** member avatar.
- **Color:** the guild's configured `embed_color`, identical across every bio so the channel reads as one cohesive set. Not per-user.
- **Profile fields:** rendered in `sort_order`.
  - `short` and `choice` fields → **inline** (`inline=True`); Discord packs up to 3 inline fields per row, producing a tidy stat row.
  - `paragraph` fields → **full-width** (`inline=False`), each its own labeled block.
  - Skipped / empty optional fields → **omitted entirely** (no blank labels).
  - Values render as **raw Discord markdown** — no sanitizing, no escaping. The member's own emoji, smart quotes, ellipses, and formatting pass through untouched; that is where the personality lives.
- **Icebreakers:** grouped after the profile fields. Each answered question is a full-width field — the question text is the field **name** (prefixed with a leading `›` glyph to distinguish it from profile labels), the answer is the field **value**. **Plain text** — no blockquote, no special font (Discord embeds can't change fonts anyway).
- **Footer:** **timestamp only** — set `embed.timestamp = created_at`. No footer text, no footer icon, no server name.

### 6.2 Length handling
- Cap each field value at **1024 chars** (Discord's embed field limit); truncate with a trailing `…`. Because the wizard enforces `max_len` at input, this is a rarely-hit safety net.
- If total embed content approaches the **6000-char** ceiling, truncate the longest `paragraph` fields first until under budget.

---

## 7. Headline field designation

The template must know which field supplies the embed title. Use an explicit **`is_headline`** boolean on `bio_fields` rather than positional convention — admins control it directly from the dashboard. Exactly one active field per template should be flagged. If none is flagged (misconfiguration), fall back to the first active field by `sort_order` and surface a dashboard warning.

---

## 8. Member leave handling

An `on_member_remove` listener:
1. Look up the leaving member's `bios` row for that guild.
2. Delete the posted message (ignore 404 — already gone).
3. Delete the user's rows from `bios`, `bio_field_values`, and `bio_answers`.

If no `bios` row exists, do nothing.

---

## 9. Orphan channel cleanup

Because the wizard venue is a real channel (not a self-archiving thread), a bot crash or restart mid-wizard can leave a stranded private channel. Mitigation:

- **On cog load**, sweep `wizard_category_id` and delete any `bio-*` channel that has no matching live in-memory session. This clears anything orphaned by a crash or restart.
- The `wizard_timeout` (§5.7) still handles idle-but-alive sessions during normal operation.

---

## 10. Web dashboard management

Slots into the existing self-hosted DK dashboard (Discord OAuth, admin-gated). Three editors:

### 10.1 Field / template editor
- List the active template's fields with an `active` toggle.
- Add / edit / **soft-retire** fields — set `active = 0`, never hard-delete, to preserve referential integrity for already-posted bios.
- Editable per field: `label`, `field_type`, `choices` (choice type only), `required`, `max_len`, `is_headline`, `sort_order`.
- Reorder fields via drag → updates `sort_order`.
- Editing the field set creates/updates the active **template version**.
- Enforce exactly one `is_headline` field; warn if zero.

### 10.2 Question editor
- List questions with an `active` toggle.
- Add / edit / **soft-retire** questions.
- Editable per question: `prompt`, `weight`.

### 10.3 Config editor
- `bios_channel_id`, `wizard_category_id`, `questions_per_bio`, `embed_color`, `wizard_timeout`, `archive_grace`.

---

## 11. Implementation defaults (proposed, override as needed)

- **Headline mechanism:** explicit `is_headline` flag (chosen over positional convention).
- **Choice control threshold:** ≤5 choices → buttons; >5 → select menu.
- **Wizard channel naming:** `bio-{user_id}`.
- **Archive grace:** 60 seconds after completion before channel deletion.
- **Capture mechanism:** `wait_for` scoped to (wizard channel, author) for text steps; component interaction for choice and control steps.
- **Selection:** weighted-random draw using `bio_questions.weight`.

---

## 12. Edge cases & guarantees checklist

- Re-trigger during a live session → resume/restart prompt, no second channel.
- Cancel / timeout / crash → no DB rows written, channel cleaned up (immediately on cancel/timeout, on next cog load for crash).
- Question pool smaller than `questions_per_bio` → draw as many distinct as exist; re-roll disables when no alternative.
- Template field retired after a user posted → their posted bio is unaffected (snapshotted label/value); it simply isn't re-asked on their next edit.
- Question retired after a user answered it → unaffected in their posted bio (snapshotted text); re-roll/redraw won't surface it again.
- Posted message manually deleted → next edit hits 404 → repost + update `message_id`.
- Member leaves → bio message and all rows removed.
- Optional field skipped → omitted from embed, no empty label.
- Member with no bio leaves → no-op.

---

*End of specification.*
