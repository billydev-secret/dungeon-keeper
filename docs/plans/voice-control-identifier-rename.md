# Voice Control — identifier rename (deferred)

**Status: not started, deliberately deferred (2026-07-27).** The user-visible
rename ("Voice Master" → "Voice Control") shipped on 2026-07-27 as a display-text
change (with one lookup-key fix it turned out to need — see below). This plan records the *second half* — renaming the internal
`voice_master` identifiers — and why it was split off rather than bundled.

## What already shipped

125 prose occurrences across 36 files: bot embeds and ephemeral copy, Discord
audit-log `reason=` strings, dashboard panel copy, `manual.html`, and the live
specs. No table, route, filename, or config key was touched, so it carried no
migration.

**It was not, however, risk-free — "display string" is not a safe category on
its own.** Code review caught one renamed label that was silently a lookup key:
`advisor_context._FEATURE_LOADERS` derived its slug with `_slugify(label)`, so
renaming the label moved the key from `voice_master` to `voice_control` while
`settings_registry.Feature.slug` stayed put. That split the union in
`fetch_feature_settings` — neither name returned both the own-table settings
and the KV registry section, and `FEATURE_KEYS` advertised the feature twice.
Fixed by making the loader slug explicit rather than derived (plus an alias so
the display name stays callable), and pinned by
`test_loader_slugs_are_stable_keys_not_derived_from_labels`, which reads the
real tables — the pre-existing merge test monkeypatched them and so could never
catch this.

**Lesson for anyone doing the rest of this plan:** before renaming any string,
grep for it being slugified, lowercased, or used as a dict key — not just for
where it is printed.

Deliberately left alone in that pass:

- `docs/plans/*` and `docs/reviews/*` — dated historical records of work as it
  was done; rewriting them would falsify the record.
- `src/migrations/*.sql` — applied migrations are immutable, comments included.

## What Job B would cover

### 1. Config keys (the dangerous part)

19 keys are defined in code; **17 currently hold live rows** in the `config`
table:

```
voice_master_block_cap              voice_master_max_per_member
voice_master_category_id            voice_master_owner_grace_s
voice_master_control_channel_id     voice_master_post_inline_panel
voice_master_create_cooldown_s      voice_master_saveable_fields
voice_master_default_bitrate        voice_master_spectator_gate_role_id
voice_master_default_name_template  voice_master_trust_cap
voice_master_default_user_limit     voice_master_trusted_prune_days
voice_master_disable_saves          voice_master_panel_channel_id   (unset)
voice_master_empty_grace_s          voice_master_panel_message_id   (unset)
voice_master_hub_channel_id
```

**The failure mode is silent.** Rename these in code without migrating the rows
and every lookup misses, falls through to the built-in default, and the guild's
voice configuration resets — hub channel unset, category unset, caps back to
stock. Nothing raises, nothing logs, and the first symptom is a member
reporting that joining the hub stopped creating rooms. Any attempt at this
needs an `UPDATE config SET key = ...` migration landing in the *same* commit
as the code change, plus `settings_registry.py`'s `slug`/setting names.

### 2. Tables (5, with data)

| Table | Live rows |
| --- | --- |
| `voice_master_profiles` | 15 |
| `voice_master_trusted` | 4 |
| `voice_master_channels` | 1 |
| `voice_master_blocked` | 0 |
| `voice_master_name_blocklist` | 0 |

Each needs `ALTER TABLE ... RENAME TO ...` in a new numbered migration. The
existing `005`/`055`/`060` migrations stay exactly as they are — a new
migration renames forward, it does not edit history.

### 3. Python/JS identifiers and filenames

~488 occurrences of the `voice_master` token overall. 15 paths are named for it:

```
src/bot_modules/voice_master/                 src/web_server/routes/voice_master.py
src/bot_modules/cogs/voice_master_cog.py      src/web_server/static/js/panels/config-voice-master.js
src/bot_modules/commands/voice_master_commands.py
src/bot_modules/services/voice_master_service.py
docs/voice_master_spec.md
tests/test_voice_master_{cog,commands_glue,logic,service}.py
tests/web/test_voice_master_routes.py
src/migrations/{005_voice_master,055_voice_master_spectator,060_voice_master_age_gated}.sql   ← never rename
```

Plus `VoiceMasterCog` and friends, and the `voice_master` feature slug in
`settings_registry.py` / `advisor_context.py`.

### 4. HTTP surface and dashboard routing

- `/api/voice-master/*` — 9 endpoints, called only by our own panel, so both
  sides move together. Cheap, but it invalidates any admin's bookmarked
  dashboard deep link.
- Panel id `config-voice-master` — this is the dashboard hash route
  (`/#/config-voice-master`), linked from `manual.html` in two places. Renaming
  it breaks existing bookmarks unless an alias is kept.
- Cache key `voice-master-channels` in `routes/voice_master.py`.

### 5. Audit-log action codes

18 `vm_*` codes, ~770 rows already written (`vm_channel_delete` 365,
`vm_channel_create` 184, `vm_channel_rename` 100, …). These are stable event
identifiers in historical data; renaming them either orphans the history or
requires backfilling every row. **Recommend leaving `vm_*` alone permanently**
even if the rest of Job B happens — the prefix is opaque to members.

## Why it was deferred

The member-facing command is `/voice` (`voice_master_cog.py:121`), not
`/voicemaster`. Members type `/voice rename`, `/voice access`, `/voice trusted`.
So **Job B buys zero user-visible change** — its entire benefit is internal
tidiness, paid for by putting 17 live config rows and 5 tables at risk.

## If it is picked up later

Ship it on its own, never bundled with feature work, in this order:

1. **Migration first, code second, same commit.** Rename tables and rewrite
   `config.key` values in one numbered migration; the code change that reads
   the new names lands beside it. A half-applied state is the silent-reset bug.
2. **Back up `dungeonkeeper.db` before the deploying restart.** This checkout is
   production.
3. **Leave `vm_*` audit codes and the `005`/`055`/`060` migration files alone.**
4. **Keep a route alias** for `/#/config-voice-master` (or accept broken
   bookmarks knowingly).
5. Rename files last — a pure `git mv` pass once the behaviour is green, so the
   risky diff and the noisy diff are separately reviewable.

## Related, not part of this

Todo #81 ("change voice master channel access name from age-gate to NSFW") was a
different rename in the same feature, and **todo-triage shipped it in `cc2913c0`
on 2026-07-27** — the member-facing copy of the access dial now reads NSFW
everywhere (button hints, `/voice access` choices, confirmations, both panel
embeds, the three status lines), covered by a logic-layer sweep asserting no
member-facing access string says "age-gat".

This branch briefly duplicated that work before noticing it had already landed;
the duplicate was dropped, and only the surfaces `cc2913c0` didn't reach were
kept (`/modhelp`'s one-liner, `economy_spec.md`, and internal comments). If you
are picking up work in this feature, **check the todo list before renaming
anything** — two sessions independently reached the same wording within an hour.

What both passes deliberately left is one more item for this plan:

- **`voice_master_profiles.age_gated`** — the column, the `VoiceProfile`
  dataclass field, and the `access_state_profile_flags` dict key (30
  occurrences). Renaming it to `nsfw` needs an `ALTER TABLE ... RENAME COLUMN`
  against 15 live profile rows. Unlike the config keys above this one fails
  *loudly* (a missing column raises `OperationalError`), so it is the least
  dangerous item in this plan — but it is still a live-data migration with no
  user-visible payoff, which is why it waits here.

Also unchanged, and intentionally: "age-gated" as ordinary prose elsewhere in
the repo — Starboard's NSFW leak guard, Guess's channel requirement, Pen Pals,
`games_traditional`, and CLAUDE.md's safety rule ("NSFW gates on
`channel.is_nsfw()` — Discord's own age-gate"). There the term is accurate and
carries meaning the word "NSFW" alone does not. Todo #81 was scoped to Voice
Control's access dial, not to the vocabulary repo-wide.
