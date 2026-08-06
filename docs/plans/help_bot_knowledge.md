# Plan — Billy-bot: lower the threshold for using features

**Branch:** `help-bot-knowledge`
**Status:** all four stages built 2026-07-23

## Problem

Dungeon Keeper has ~138 dashboard panels' worth of settings. Admins aren't
exploring them, so features ship and go unused. Billy-bot already answers
grounded questions (manual corpus, per-feature settings reads, proposal +
Apply-button writes) but two things cap its usefulness for adoption:

1. **It can't help with what isn't set up yet.** `validate_config_change`
   requires the key to already exist in the `config` KV table, so the
   unconfigured feature — the exact adoption case — gets "point them to the
   dashboard panel". Feature-table settings (Economy, Voice Master, …) are
   readable but never writable.
2. **It's pull-only.** Asking requires already knowing the feature exists. An
   admin who's never heard of Chat Revive will never ask about it, so no
   amount of manual grounding reaches them.

## Stages

### Stage 1 — model tiering by asker

Members get Haiku (fast, cheap); mods/admins get Sonnet (better at the
multi-round tool-use loop that config help needs). Two per-guild keys instead
of one, both picked from `ADVISOR_MODELS`.

- `advisor_model` (existing key, unchanged meaning) → member model, default
  `claude-haiku-4-5`.
- `advisor_admin_model` (new) → staff model, default `claude-sonnet-5`.
- Staff predicate lives with the other permission helpers in
  `advisor_context`: administrator, manage_guild, manage_messages,
  moderate_members, kick_members, or ban_members. Broader than
  `can_see_config` (admin-only) on purpose — a mod asking "how do I run QOTD"
  deserves the better model even though they can't see config.

Note: prompt-cache entries are keyed by model, so two models means two cache
entries for the same 22k-token manual. Staff asks are rarer and will miss the
cache more often; at Sonnet rates a cold ask is ~$0.07, which is acceptable.

### Stage 2 — settings registry

The keystone. One declarative inventory of every model-visible setting:

    key, feature, label, type, allowed values / range, default,
    dashboard panel, writable-by-model (opt-in per key)

Replaces shape-inference-from-current-value in `validate_config_change` with
schema lookup, which unblocks:

- proposing a value for a key that has **no** stored row yet (the adoption case)
- extending writes to selected feature-table settings without loosening the
  human Apply gate
- a machine-readable list of what a guild has *not* configured (Stage 3 needs this)

`writable` is opt-in per key, not default-open: widening what model output can
touch is a real expansion of blast radius, and the Apply gate is the only thing
between a prompt-injected pin and a config write.

Three tiers, not two:

- **ordinary** — proposable by any asker who passes the settings gate
  (`administrator` *or* `manage_guild`). Channels, flags, numbers, copy, and
  ping-only roles.
- **`admin_only`** — proposable, but only for full `administrator`. Two classes
  qualify. **Authority:** the jailed role, who may mark Q&A answers, who may
  whisper, the greeter role, the role the inactivity sweep applies in bulk.
  **Disclosure** (added 2026-08-06, from
  `docs/reviews/2026-08-06-advisor-llm-write-path.md` A4): keys that route
  confidential data to a channel — `whisper_log_channel_id`, which names the
  sender behind each anonymous whisper, and `transcript_channel_id`, the jail
  and ticket archive. Neither grants a permission, so the original
  authority-only reading of the tier left both sitting at the `manage_guild`
  bar, where an Apply click could have published them. Both classes are
  re-checked against whoever *clicks* Apply, not just whoever asked, since the
  two need not be the same person.
- **`PRIVILEGE_KEYS`** — never writable at any permission level:
  `admin_role_ids`, `mod_role_ids`, `message_storage_level`. Handing out admin
  or widening message retention isn't a higher tier, it's off the table; a
  mistaken click there is unrecoverable in a way the rest aren't.

`is_admin` defaults to `False` everywhere it's threaded, so a surface that
forgets to pass it under-offers rather than over-offers. The propose tool's key
enum is built per-asker, so a Manage Server admin is never shown a key they'd
only be rejected for naming.

### Stage 3 — `find_setup_gaps` tool

With the registry, compute what the DB alone can't say:

- features never configured at all
- half-configured features (channel set, feature still disabled)
- settings still sitting at their default

Answers "what am I not using?" and "what should I set up next?", and lets the
bot walk an admin through enabling a feature end-to-end — several keys queued
behind one Apply.

### Stage 4 — proactive surfacing

Same gap data, pushed instead of pulled. This is the stage that actually moves
adoption; 2 and 3 are enablers.

Shipped as a **home-page widget** (`setup-suggestions`) rather than a bespoke
strip: the dashboard home is already a perm-gated, user-arrangeable widget grid,
so an admin can move, resize, or remove the card like any other, and removing it
sticks. Backed by `GET /api/help/suggestions` — the same `scan_guild` as the
tool, rendered as structured rows instead of prose, with no model call, so it's
cheap enough to sit on a page that refreshes every 60s.

Existing admins already have a saved layout in localStorage, which a new
registry entry would never reach. `ONE_TIME_ADDITIONS` in `home.js` offers the
widget exactly once per user and records that it did, so it appears for current
admins but doesn't come back if they remove it.

A periodic Discord digest remains a follow-up decision — it's a push into a
channel rather than onto a page the admin chose to open, so it wants its own
opt-in.

### Stage 5 — role grants, and the dead-key trap

The access roles were the obvious gap after stage 4, and investigating them
turned up something worth recording: **most of the `*_role_id` keys sitting in
`config` on live servers are dead.**

- `nsfw_role_id`, `denizen_role_id`, `veteran_role_id` — superseded by the
  `grant_roles` table. Live values are there; the KV rows survive only at
  `guild_id = 0` and nothing reads them.
- `veil_role_id`, `veil_channel_id` and friends — Veil was renamed to Guess in
  migration `020_rename_veil_to_guess.sql`. There is no `veil` code left.
- `unverified_role_id` is genuinely live (read by `AppContext` and the Welcome
  panel), so it's a normal registry entry.

Adding a dead key to the registry would be worse than leaving it out: the admin
clicks Apply, the write succeeds, and nothing changes. `DEAD_KEYS` +
an import-time check make that a hard failure rather than a plausible-looking
future "improvement".

The real fix for role grants is a second write target. `ConfigProposal` gained
`target` (`"config" | "grant_role"`) and `grant_name`, so the existing
one-button-per-change flow renders both kinds identically while
`apply_config_change` dispatches. `propose_grant_role_change` edits one field of
one **existing** grant — creating a grant stays a dashboard action, since
letting the model mint role-handing rows is a different risk entirely.

The whole grant surface is admin-only (every field decides who ends up with a
role, NSFW included) and the tool is only offered when the asker is a full
admin *and* the guild has grants, so an empty enum can never let an arbitrary
name through. `upsert_grant_role` takes a whole row, so applying is a
read-modify-write — tested to confirm untouched fields survive.

`grant_message` is the one free-text field here, and it is sent to the announce
channel as plain content **with mentions enabled** (`role_grant_commands`
passes no `allowed_mentions`, unlike the log embed beside it). So a model-written
`@everyone`/`@here`/role mention would fire on every future grant.
`validate_grant_role_change` rejects one rather than stripping it, and the guard
lives there rather than on the send side on purpose: the live `denizen` grant
message deliberately ends with an `@here`, set by a human on the dashboard. The
rule is about who wrote the text, not about the channel.

### Stage 7 — trust boundary and disclosure (2026-08-06)

From the adversarial review in
`docs/reviews/2026-08-06-advisor-llm-write-path.md`. Three changes, all aimed at
the same premise the Apply gate already assumed — that model output is untrusted:

- **Context is fenced.** `advisor_context` wraps each member-written section
  (channel topics, pins, docs, announcements) in `<untrusted>` tags and strips
  those tags plus any `===` run from the text first, so a pinned message can no
  longer close the context block and open something instruction-shaped. Nicknames
  and role names get the same treatment inline. The system instructions carry a
  matching TRUST BOUNDARY rule naming the tags — **the tags and the rule are one
  contract; changing either means changing both.**
- **Provenance covers both tools.** The "only propose what the asker themselves
  asked for" rule used to sit only in the `propose_config_change` bullet. It is
  now repeated in the `propose_grant_role_change` bullet, which is the
  higher-privilege of the two.
- **The proposal is disclosed in full.** The Apply button label truncates at 80
  characters and the embed description is the model's own prose, so the cog now
  appends one field per queued proposal carrying the complete validator-authored
  `display`, the target, and the key. That field — not the button, not the prose
  — is the authoritative account of what a click will write.

Applying also writes an `audit_log` row (`advisor_config_apply`) in the same
transaction as the config write, recording the clicker, the key, and the before
and after values. `log.txt` is wiped on every boot, so without it the one config
write a model chose the value for was the one with no durable history.

The pin snapshot narrowed at the same time: `refresh_guild_pins` now skips
per-member private rooms (`is_private_room` — a channel where a *named member*
rather than a role was granted view: jail, tickets, Pen Pals, the bios wizard).
The snapshot is guild-wide and only filtered per asker at build time, so the old
"every channel the bot can read, minus NSFW" rule meant any admin could pull a
private room's pins into a prompt. It fails closed: an unreadable overwrite map
counts as private, because over-excluding costs context quality and
under-excluding costs confidentiality.

### Stage 6 — per-feature fetch for KV-backed features (2026-07-25)

`get_server_settings` originally enumerated only the features with their own
tables plus a catch-all `general`. Registry features whose keys live in the
shared config KV (greeting watch, welcome, …) were reachable *only* through
`general`, whose tool description gave no hint they were there — and the
system prompt tells the model that a feature it can't fetch is one it cannot
see. Net effect, observed live 2026-07-25: an admin asking about greeters was
told the assistant couldn't see those settings.

Two fixes, one per drift:

- Every registry feature is now fetchable by its own slug
  (`advisor_context._registry_feature_section`); a slug present in both worlds
  (`voice_master`) returns both sections. New registry entries become
  fetchable automatically, so this class of drift is closed, not patched.
- Intake Cards had shipped with no registry entry at all, so gap detection and
  `propose_config_change` didn't know it existed. It now has one (`intake`,
  KV keys `intake_*`); its step editor and procedure reference blocks stay
  panel-only via `extra_panel_only`.

## Follow-ups

- The registry covers 17 features / 62 settings out of ~240 live config keys,
  a good number of which are dead rows like the above rather than real gaps.
  Extending it is additive and safe (the import-time check enforces the rules).
- Other feature-table settings (Economy, Voice Master dials, Starboard) are
  readable but still panel-only. `grant_roles` is now the worked example for
  making one writable: a validate/apply pair plus a `target` value.
- `support_access_enabled` is live and grants dashboard access to support. Left
  panel-only deliberately — it's an access grant to an outside party, closer to
  `admin_role_ids` than to a feature setting.

## Testing

Logic layer per CLAUDE.md: registry validation, gap detection, and model
resolution are all pure-ish functions over a `conn` + a fake member, tested in
`tests/test_advisor_*.py`. Cogs/routes stay glue.
