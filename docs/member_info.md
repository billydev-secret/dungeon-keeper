# `/info` — the member's own card

> **Classification: Reference** — matches current behavior (built 2026-08-22).

The member-facing counterpart to `/modinfo`. One ephemeral embed showing a
member their own account, activity, level, wallet and opt-in states, with
buttons to change the opt-ins.

## Scope: self only

There is no member argument, and adding one is not a small change. A lookup of
*another* member would have to consult the no-contact list
(`docs/no_contact_spec.md` — every surface that puts two members in contact
does) and re-argue every field's leak surface. None of that is needed for a
card you can only point at yourself, so the command takes no target.

## What it shows

| Section | Source |
|---|---|
| Account | `member.created_at`, `member.joined_at` |
| Level | `member_xp`, plus `xp_events` grouped by source |
| Roles | live Discord roles, highest first, `@everyone` dropped, capped at 12 with a `+N more` |
| Activity | `processed_messages` — 30-day count, last-seen, top 3 channels |
| Wallet | `load_econ_settings` / `get_balance` / `list_member_rentals` / `get_streak_shields` |
| Your opt-ins | one row per configured feature — see below |

A 30-day XP-by-source chart is attached, rendered by `render_activity_chart`,
the same builder `/modinfo` uses.

### What it deliberately omits

- **The watch-list count.** `/modinfo` shows how many mods are watching a
  member. Telling the member is telling the subject of an investigation that
  there is one.
- **Warnings, jail history and tickets.** Their own moderation record is not
  shown here. This was an explicit product decision (2026-08-22), not an
  oversight: if it is ever revisited, it changes in
  `member_info/embeds.py` and in what the cog fetches, not by widening an
  existing field.
- **Any no-contact count.** See below.

## The opt-in rows

`member_info/logic.py` holds one `_FeatureSpec` per feature — its label, and
how each of the three states (`in` / `out` / `unset`) reads and what it
offers. `build_optin_rows` is pure: the cog reads state, the table decides
copy and buttons.

| Feature | "Configured" means | Member state read from |
|---|---|---|
| Pen Pals | `pen_pals_config.enabled` | `pen_pals_optouts` → out; `pen_pals_pool` or a live session → in |
| Whispers | whisper `role_id` set | holds the role |
| Guess pool | `guess_role_id` set | holds the role |
| DMs | any DM-mode role configured | `resolve_mode` → open / ask / closed |
| Wellness | wellness `role_id` set | `wellness_users.is_active` |
| Birthday | the cog is loaded | `has_birthday` |
| No-contact | the cog is loaded | *nothing* — see below |

Two rules are enforced in the logic layer rather than the view, because they
are the ones that leak if a later edit gets them wrong:

1. **An unconfigured feature produces no row at all.** `/info` must not
   advertise a feature the guild never set up, nor offer to join it.
2. **A button is only offered when the member can actually act.** Pen Pals
   with an opt-in role the member lacks renders the status and no button —
   the flow would only refuse.

`unset` is kept distinct from `out` on purpose: someone who never joined Pen
Pals gets an invitation, someone who left gets an acknowledgement. Re-pitching
a decision someone just made is the bot arguing with them.

### No-contact carries no count

`/nocontact list` filters out entries the *other* party created against the
viewer (`is_visible_to`). A count on this panel derived from the raw table
would leak exactly what that filter exists to hide; a count derived after
filtering could still be differenced against other surfaces. So the row's text
is identical in every state and contains no digits — enforced by
`test_no_contact_row_states_are_indistinguishable` — and the button opens the
existing filtered view.

## The buttons re-enter existing flows

`member_info/views.py` grants no role, writes no opt-in and clears none. Every
button calls the flow that already owns its feature:

| Feature | Entry point |
|---|---|
| Pen Pals | `pen_pals_cog._handle_join` / `_handle_leave`, `source="info_panel"` |
| Whispers | `WhisperCog._optin_impl` / `_optout_impl` |
| Guess | `GuessCog._optin_impl` / `_optout_impl` |
| DMs | `dm_perms_cog.open_dm_settings` |
| Wellness | `WellnessCog.open_setup` |
| Birthday | `_BirthdayModal`, or `BirthdayCog.remove_impl` |
| No-contact | `NoContactCog.list_impl` |

This is a safety property, not a convenience. Each of those flows carries the
gate that is its feature's whole compliance story — Guess and Whispers show
consent copy and only grant the role once it is accepted, Pen Pals checks the
guild's opt-in role, wellness cannot opt anyone in without a timezone. A panel
that flipped roles itself would be a second, ungated door into all of them.

Several of those entry points were extracted from command bodies in this
change (Guess, wellness, DM settings, birthday-remove, no-contact-list). The
commands now call the same impls, so there is one implementation per flow.

If a feature's cog is not loaded, `FeatureState.actionable=False` — the status
row still renders, the button is never built.

## Data

No new tables, so no `docs/data_register.md` row. Every read is either the
caller's own data or guild configuration. The panel is always ephemeral.

## Tests

`tests/test_member_info_logic.py` covers every feature's configured /
unconfigured branch, every state's action, the not-actionable degradation, the
top-channel visibility filter, the no-contact indistinguishability rule, and
the empty-state card for a brand-new member. The embed is under the repo-wide
accent contract as `member_info.panel` in
`tests/test_embed_accent_contract.py`.
