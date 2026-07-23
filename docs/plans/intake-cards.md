# Intake cards — per-newcomer welcome tracker + procedure reference sync

**Status:** Plan (not yet built).

## Context

`#welcome-procedure` is a static staff checklist welcomers must remember to
follow; the bot has no idea whether an intake finished, stalled, or never
started ("no mid-intake visibility" is the pain point). This feature has two
halves:

1. **Intake cards** — when a member joins, the bot posts a card in greeter
   chat that tracks the procedure as it happens. The open cards *are* the
   queue.
2. **Reference sync** — the `#welcome-procedure` content itself (procedure
   text + question lists) moves to the dashboard as source of truth and the
   bot keeps the channel in sync, with **each question rendered as its own
   message** so greeters copy-paste a single question in one tap.

The intake procedure (one welcomer end-to-end; mods judge fit informally —
there is no formal verdict step):

1. Newcomer arrives; a welcomer greets them in greeter chat.
2. Welcomer runs the question list conversationally (never recorded).
3. Welcomer grants the member role via the existing **`/grant`** command
   (role_grant_spec.md), which unlocks the 5 SFW questions in main chat.
4. NSFW access is granted (again `/grant`), then 5 NSFW questions follow.
5. The welcomer posts the **ending welcome message**, which contains a
   **test code**; the bot spots the code and completes the card.

Design decisions (2026-07-22 discussion):

- **Card is a tracker, not a control panel.** No grant buttons — role grants
  happen through `/grant` / manual role adds and the card auto-ticks by
  watching **role ids** (not grant keys), so mod-hand-added roles count too.
- **One card in greeter chat.** Promotion-reviews channel keeps Level 5 /
  return / sleeper only; we reuse its display idiom (persistent `DynamicItem`
  buttons, one open card per member, resolver stamping) but share no schema.
- **Arrival ping:** the card pings the **greeter role** (replacing today's
  bare `@here — has arrived` line when the feature is on).
- **Auto-greet:** "Greeted" ticks when a greeter-role member posts a message
  in greeter chat that @mentions the newcomer — the same signal the Greeter
  Response report measures.
- **Completion = test code.** A message from a greeter-role member (or mod),
  **in any channel**, containing the configured code **and @mentioning the
  newcomer**, completes that card. Unambiguous attribution; the poster is
  stamped as the welcomer of record.
- **Code always wins:** if steps are unticked when the code fires, the card
  completes anyway and those steps are stamped **skipped** — analytics
  surface procedure shortcuts, nothing blocks.
- **No ghosts policy: stay open.** Cards close only on completion, Dismiss,
  or the member leaving/being banned. No auto-expiry.
- **Steps are dashboard-configured** and snapshotted onto each card at
  creation (editing the list never mutates in-flight cards).
- **Answers are never recorded**; the question lists stay conversational.
- **No DM welcome in v1** (dropped; revisit after cards bed in).
- **Reference channel is bot-owned, generalized:** the whole
  `#welcome-procedure` channel is bot-synced from dashboard-edited content.
  The feature is generic (any guild, any channel); a one-time
  **import-from-channel** action seeds the editor from the channel's
  existing messages, so this guild's real content populates its config
  without retyping and without hard-coding data.
- **Flexible sections:** the editor is an ordered list of blocks — *text
  section* or *question list*, any number of each — covering today's three
  lists (intake ritual / SFW / NSFW) and whatever comes later.

## Default step list (dashboard-editable)

| # | Step | Kind |
|---|------|------|
| 1 | Greeted | auto: `greeted` |
| 2 | Verified | auto: `verified` (unverified role removed) |
| 3 | Member role granted | auto: `role_gained` + configured role id |
| 4 | SFW questions asked | manual |
| 5 | NSFW access granted | auto: `role_gained` + configured role id |
| 6 | NSFW questions asked | manual |

(Age eligibility — the server's 30+ bar — is handled outside intake, so it
is deliberately not a checklist step.)

Auto kinds v1: `greeted`, `verified`, `role_gained(role_id)`. Manual steps
render as buttons (secondary → success), gated to greeter role / mods,
toggleable to undo misclicks; ticking records who/when.

## Card lifecycle

1. **Join** → card posts to the intake channel (default
   `greeter_chat_channel_id`), content line pings the greeter role. Embed:
   member mention, account age, invited-by (`invite_tracker`), a **step
   progress bar** (`▰▰▱▱▱▱ 2/6` — reuse `economy.leaderboard.progress_bar`,
   the style guide's `▰▱` vocabulary), and the checklist as ✅/⬜ lines
   (done steps show who/when; skipped steps show ⏭️ after completion).
   Suppresses the legacy bare arrival ping while enabled. Skips bots and
   jailed rejoiners (same guard as today's join path). The bar + lines
   re-render on every tick, so the card reads at a glance in the queue.
2. **Auto-ticks** as the procedure happens: greeter mention, unverified role
   removed (`on_member_update`), configured roles gained
   (`on_member_update`, so `/grant` and manual adds both count).
3. **Manual ticks** for the question phases.
4. **Test code** in a greeter/mod message mentioning the newcomer →
   card completes: "🎉 Intake complete — welcomed by @X"; unticked steps
   stamped skipped; buttons removed.
5. **Dismiss** button (mods) closes without completing; member
   leave (`on_member_remove`) or ban (`on_member_ban`) closes as
   `left`/`banned`.
6. **Stale nudge:** background loop bumps a card once (reply pinging the
   greeter role) after N hours open with no progress; `nudged_at` prevents
   repeats.

**Hot path:** `on_message` pre-filters with an O(1) per-guild watch set of
members with open cards (pattern: `promotion_review_service.is_watched`) —
only messages mentioning a watched member (greet detection) or containing
the code + a watched mention hit the DB.

### Ships dark

No behavior changes until `intake_enabled` + a channel resolves. Disabled ⇒
join path identical to today. Reference sync is separately dark until
`intake_reference_channel_id` is set.

## Reference sync (procedure docs)

- **Blocks** are dashboard-edited, ordered per guild: `kind ∈ text |
  questions`, optional title, body. A questions block's body is one question
  per line.
- **Rendering:** text block → one message; questions block → an optional
  bold header message + **one message per question**.
- **Sync on save:** a differ (pure logic, unit-tested) compares the rendered
  message list against the stored mapping and emits minimal operations —
  edit changed messages in place (ids stable when only wording changes),
  post new, delete surplus, repost from the first structural divergence when
  order shifts. Pattern-match the `/grant_audit` card's stored-message-id
  bookkeeping.
- The bot only ever edits/deletes **its own tracked messages**; human posts
  in the channel are left alone (recommend locking the channel to bot-posts
  after adoption — docs note, not enforced).
- **Import:** admin-gated "seed from channel" reads the channel's existing
  messages in chronological order into draft text blocks; the admin splits
  question lists out in the editor. One-shot convenience, generic to any
  guild/channel; refuses to overwrite a non-empty editor.

## Data

- Migration: `intake_cards` — id, guild_id, user_id, created_at,
  channel_id/message_id, nudged_at, resolved_at, resolved_by, resolution
  (`completed`/`dismissed`/`left`/`banned`), completed_by (code poster).
  Partial unique index: one open card per (guild, member) — same dedup as
  `promotion_review_cards` (migration 112).
- `intake_card_steps` — card_id, position, step_key, label, auto_kind,
  auto_role_id, done_at, done_by, skipped (0/1). Snapshot from config at
  card creation. Per-step `done_by` feeds per-welcomer analytics.
- `intake_reference_blocks` — guild_id, position, kind (`text`/`questions`),
  title, body. `intake_reference_messages` — guild_id, block_id, item_index
  (0 = header/body, 1..n = questions), message_id, content_hash — the sync
  mapping.
- Config keys (→ typed `GuildConfig` fields): `intake_enabled`,
  `intake_channel_id` (0 = fallback to `greeter_chat_channel_id`),
  `intake_steps` (JSON list `{key, label, auto, role_id?}`),
  `intake_completion_code`, `intake_stale_hours`,
  `intake_reference_channel_id`. Reuses existing `greeter_role_id`,
  `unverified_role_id`.

## Code (pattern-match: promotion review + greeting watch)

| Piece | File |
|---|---|
| Logic/ledger/gating (unit under test) | `src/bot_modules/services/intake_service.py` |
| Embed + persistent step/dismiss buttons | `src/bot_modules/services/intake_views.py` |
| Stale-nudge loop | `src/bot_modules/services/intake_loop.py` |
| Hooks | `cogs/events_cog.py`: `on_member_join` (create card, suppress bare ping), `on_member_remove` / `on_member_ban` (close), `on_member_update` (verified + role_gained ticks), `on_message` (greet detect + code detect); `__main__` (warm watch set + `add_dynamic_items`) |
| Reference sync logic (blocks, renderer, differ, import parsing) | `src/bot_modules/services/intake_reference_service.py` |
| Dashboard route | `routes/config.py`: GET section + `PUT /config/intake`, `PUT /config/intake/reference` (blocks, triggers sync), `POST /config/intake/reference/import` |
| Dashboard panel | `static/js/panels/config-intake.js` (step-list editor with kind/role pickers, channel, code, stale hours; block editor for the reference content with import button), registered in `app.js` |
| Analytics | `routes/reports.py`: `GET /intake-report`; panel `static/js/panels/intake-report.js` next to Greeter Response — open queue (age, progress), time-to-complete, per-welcomer completions, skipped-step rates |

Reuse: `resolve_accent_color`, `invite_tracker` (invited-by),
promo-card `_can_action`-style gating, `DynamicItem` custom-id scheme
(`intake:step:<card>:<key>`, `intake:dismiss:<card>`).

No new slash commands. `#welcome-procedure` untouched. Promotion-review
machinery untouched.

## Stages (each commits with its tests)

1. **Service + schema:** migration, `intake_service.py` (create/dedupe, step
   snapshot, tick/untick + gating, auto-tick matching incl. role_gained,
   code detection incl. skip-stamping, close paths, stale scan, config
   parsing). `tests/test_intake_logic.py`.
2. **Discord surface:** `intake_views.py` + events_cog hooks + loop +
   warm/registration. Views stay glue; behavior tested via the service.
3. **Dashboard config:** route + `config-intake.js`; authz sweep covers the
   new route automatically; browser layout check.
4. **Analytics:** report route + panel.
5. **Reference sync:** `intake_reference_service.py` (render, diff, import
   parse — all pure logic), sync executor, routes + block editor UI.
   `tests/test_intake_reference_logic.py`.
6. **Docs:** `docs/intake_spec.md` + INDEX.md entry, `manual.html` +
   `help-sections.js`. README: no command changes to document.

## Verification

- `python scripts/gate.py --scoped` per commit; full CI on push.
- Live checks (Testing: checkboxes on the behavior-changing commits):
  - feature off → join behavior identical to today
  - enable + join → card posts, greeter-role ping (no @here), invited-by shown
  - greeter mention auto-ticks Greeted; stripping unverified ticks Verified;
    `/grant` and a manual role add both tick their role steps
  - non-greeter can't tick manual steps; tick/untick toggles
  - code + mention from a greeter completes the card anywhere; unticked
    steps show skipped; poster stamped as welcomer
  - code from a non-greeter, or without a mention, does nothing
  - dismiss / leave / ban close the card
  - stale card nudges exactly once
  - restart bot → buttons on open cards still work
  - reference: import seeds blocks from #welcome-procedure; save syncs the
    channel with one message per question; wording edit keeps message ids;
    add/remove/reorder reconciles; human messages in the channel untouched
