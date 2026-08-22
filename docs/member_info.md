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
| Level | `member_xp`, plus `xp_events` grouped by source, plus XP-to-next-level from `xp_required_for_level` against the guild's own curve factor |
| Roles | live Discord roles, highest first, `@everyone` dropped, capped at 12 with a `+N more` |
| Activity | `processed_messages` — 30-day count, last-seen, top 3 channels **and threads** |
| Wallet | `load_econ_settings` / `get_balance` / `list_member_rentals` / `get_streak_shields` / `get_streak_summary` (passed today's local day, so a lapsed streak reads as zero rather than as the stale stored number) |
| More | `/ask` (only when `AdvisorCog` is loaded, named via `resolve_assistant_name_conn` — the assistant's name is per-guild branding) and `/delete_me` (only when `PrivacyCog` is loaded) |
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
| DMs | the cog is loaded | `resolve_mode` → open / ask / closed |
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

### Two numbers that were half-told

The Level field used to end at "Level 7 · 12,345 XP", which asks a question it
did not answer; it now carries the XP still owed for the next level. The
threshold comes from `xp_required_for_level` against the guild's live curve
factor, and `xp_to_next_level` clamps at zero — the stored level lags the XP
that earns it, so "already past the threshold" is reachable and must not render
as a negative.

The wallet line printed "🛡️ streak shield held" beside no streak, because the
shield helpers read `econ_streaks` for its `shields` column only and nothing
read the streak itself. `get_streak_summary` fills that gap — and takes
today's local day, because `current_streak` is **stored, not live**: only
`process_login` rewrites it, on a message or a voice award. A member who
stopped posting a week ago still has the old number in the column, so reading
it verbatim would announce a run their next message resets to 1. The helper
replays the stored state through `evaluate_login` — the same pure rules a real
login applies — and reports zero when the run cannot survive. The personal
best is left alone: that is history, not a live claim.

### Resilience

Every feature block in `_feature_states` is independently guarded, and the
whole render is wrapped. Both matter for the same reason: the command
`defer()`s first, and `events_cog._on_tree_error` only speaks when the
interaction is still unanswered. An exception escaping after the defer is not
an error message — it is a member left on "thinking…" forever. Reusing seven
features' internals is seven chances for a helper to drift or a table to be
missing (wellness's are created by the web server's startup, not by a
migration), so one failure costs its own row and nothing else.

The same reasoning caps the Roles field by length, not just by count: Discord
allows 100-character role names, twelve of them overrun the 1024-byte field
limit, and a rejected embed past the defer is a card that never arrives.

### Threads count as channels — and archived ones can't be named

`award_message_xp` accepts a `discord.Thread` and stores `message.channel.id`,
so `processed_messages` holds rows keyed by *thread* id — while
`guild.channels` excludes threads. `_viewable_channel_ids` unions
`guild.threads` in for exactly this reason.

That covers live threads only, and it cannot be made to cover archived ones.
discord.py drops a thread from the guild cache the moment it archives
(`state.parse_thread_update` → `guild._remove_thread`), Discord archives after
24h–1 week of quiet, and `processed_messages` stores no parent id — so for an
archived thread there is no cached object *and* no parent channel whose
visibility could stand in. `get_channel_or_thread` does not help: it reads the
same two caches. Those rows stay excluded, because visibility that cannot be
verified is not visibility.

What that must not do is make the field contradict its own header. A member
whose month was spent in since-archived threads has a real 30-day count and
nothing nameable to attribute it to, so `_activity_value` distinguishes the
two empty cases: "no messages recorded" only when the count is genuinely zero,
and otherwise a line saying the places can't be listed. Fixing this properly
would mean storing `parent_id` on `processed_messages` — a migration plus an
ingest change in the XP path, out of scope here.

## The buttons re-enter existing flows

`member_info/views.py` grants no role, writes no opt-in and clears none. Every
button calls the flow that already owns its feature:

| Feature | Entry point |
|---|---|
| Pen Pals | `pen_pals_cog._handle_join` / `_handle_leave`, `source="info_panel"` |
| Whispers | `WhisperCog._optin_impl` / `_optout_impl` |
| Guess | `GuessCog._optin_impl` / `_optout_impl` |
| DMs | `dm_perms_cog.open_dm_settings` |
| Wellness | `WellnessCog.open_setup` — labelled "Redo wellness setup" even for an opted-in member, because that is what it is: finishing the wizard re-runs `opt_in_user`, whose upsert overwrites `notifications_pref` and `enforcement_level`. There is no member-facing read-only settings view to point at. The `notifications_pref` reset that once made that label a warning was fixed in `opt_in_user` on 2026-08-22 |
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

### Where /info is listed

`/help`'s **General** page is a hand-maintained literal in
`mod_cog._build_help_pages`, not generated — so a new member command has to be
added there by hand or it is reachable only through the secondary
"Browse by Module" pager. CLAUDE.md names `/help` and `manual.html` as the
command reference, so both carry `/info`, and a test fails if the help list
loses it.

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
