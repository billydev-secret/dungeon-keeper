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

`writable_by_model` is opt-in per key, not default-open: widening what model
output can touch is a real expansion of blast radius, and the Apply gate is
the only thing between a prompt-injected pin and a config write.

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

## Follow-ups

- The registry covers 16 features / 56 settings out of ~240 live config keys.
  Extending it is additive and safe (the import-time check enforces the rules);
  the gap report gets better with every feature added.
- Feature-table settings (Economy, Voice Master dials, Starboard) are readable
  but still panel-only. Making selected ones writable now only needs registry
  entries plus a per-feature setter, since the shape/validation path is generic.

## Testing

Logic layer per CLAUDE.md: registry validation, gap detection, and model
resolution are all pure-ish functions over a `conn` + a fake member, tested in
`tests/test_advisor_*.py`. Cogs/routes stay glue.
