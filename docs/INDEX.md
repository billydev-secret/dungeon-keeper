# Documentation Index

Welcome! This folder holds the specs, deployment notes, and audits for Dungeon Keeper. It has grown organically alongside the bot, which means the documents here are **not all equally trustworthy** — some describe the bot as it runs today, others describe what we *planned* to build, and a few describe features that were never fully built.

> **⚠️ Read this first: specs come in three flavors**
>
> - **Reference** — matches current behavior. Safe to treat as documentation.
> - **Design spec** — written *to implement* a feature. The feature usually exists, but the spec may lag behind the code in details. Verify against the source before relying on specifics.
> - **Aspirational** — documents things that were never fully built. Read with care: commands, modules, and flows described here may simply not exist.
>
> Each entry below is classified. When in doubt, the code wins.

---

## Reference specs (match current behavior)

| Doc | What it covers |
|---|---|
| [README.md](README.md) | Feature overview + slash-command reference (recently corrected) |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Production deploy: permissions, env vars, DB, Cloudflare tunnel |
| [ai_moderation_spec.md](ai_moderation_spec.md) | AI moderation cog (review/scan/query, label feedback) |
| [auto_react_spec.md](auto_react_spec.md) | Auto React (listener-only image auto-reactions; dashboard/API-configured) |
| [bios_cog_spec.md](bios_cog_spec.md) | Bios cog (profiles, wizard) |
| [birthday-announcement.md](birthday-announcement.md) | Birthday announcement message format |
| [birthday_spec.md](birthday_spec.md) | Birthday set/remove, daily celebration |
| [bump_tracker_spec.md](bump_tracker_spec.md) | Bump Tracker (`/bump log`/`status`, multi-site cooldown reminders) |
| [confessions_spec.md](confessions_spec.md) | Anonymous confessions, anon replies, mod log |
| [dk_pvp_games_suite_spec.md](dk_pvp_games_suite_spec.md) | PvP duel/group games (Pressure Cooker, Quickdraw, Chicken, Hot Potato, Musical Chairs) |
| [dm_perms_spec.md](dm_perms_spec.md) | DM permission system (open/ask/closed, consent pairs) |
| [docs_cog_spec.md](docs_cog_spec.md) | `/docs` cog: posts dashboard-authored docs into channels (not this docs/ folder) |
| [embed_style_guide.md](embed_style_guide.md) | Conventions for bot-generated embeds/panels (accent color, section spacing, monospace tables, persistent views, ping allow-listing) |
| [dungeon_keeper_jail_ticket_spec.md](dungeon_keeper_jail_ticket_spec.md) | Jail/ticket/policy/warn system |
| [emoji_stealer_spec.md](emoji_stealer_spec.md) | Emoji stealer (URL command + context menu) |
| [games_system_spec.md](games_system_spec.md) | Party games suite (`/games play <slug>`). Photo Challenge left this suite — it's now a standalone scheduled dashboard feature (own channel + schedule, `/api/photo-challenge`, panel `photo-challenge.js`) |
| [greeting_watch_spec.md](greeting_watch_spec.md) | Greeting Watch: DMs a member when a "good morning"/"hello" goes unanswered in a watched channel (dashboard-configured, no commands) |
| [guess_spec.md](guess_spec.md) | Guess image game (`/guess submit\|round\|delete\|optin\|confess\|leaderboard\|prompt`) |
| [hidden_channels_spec.md](hidden_channels_spec.md) | Hidden Channels (`/hidden hide\|restore\|list`) |
| [inactive_spec.md](inactive_spec.md) | Inactive member management (`/inactive mark\|release\|panel\|sweep`); sweep settings configured on the web dashboard |
| [mod_spec.md](mod_spec.md) | Mod cog (`/help`, `/purge`) — distinct from tools_spec.md |
| [needle_spec.md](needle_spec.md) | Needle auto-threading (`/close`, `/title`) |
| [pen_pals_spec.md](pen_pals_spec.md) | Pen Pals pooling + private channels |
| [pressure_cooker_spec.md](pressure_cooker_spec.md) | Pressure Cooker duel |
| [privacy_spec.md](privacy_spec.md) | Data deletion (`/delete_me`, `/delete_user`) |
| [quote_renderer_spec.md](quote_renderer_spec.md) | Quote/banner card renderer (shared service: themes, fonts, slim/custom borders) |
| [rename_spec.md](rename_spec.md) | `/rename` (moderator nickname change/reset) |
| [web_testing.md](web_testing.md) | Dashboard test suite overview: authz sweep, snowflake-precision sweep, manual broken-link check, plus the browser suite (layout + panel-load health); marker/tiers/where-each-runs |
| [mobile_layout_testing.md](mobile_layout_testing.md) | Browser-driven responsive-layout gate: overflow/clip checks across every panel at phone/tablet/desktop; scoped per-commit, full nightly |
| [reporting_spec.md](reporting_spec.md) | Reporting / dashboard reports |
| [role_grant_spec.md](role_grant_spec.md) | Role Grant (`/grant`, fixed allowlist grants) — distinct from role_menus_spec.md |
| [server_announcement_style.md](server_announcement_style.md) | Member/mod-facing guide for formatting server announcements & pinned posts (spacing, headings, links). A draft to post/pin in Discord, not bot behavior |
| [server_map.md](server_map.md) | Server map: channel/category guide + role groupings for The Golden Meadow. **Snapshot** from the live Discord API (2026-07-18) — re-generate rather than hand-editing when it drifts |
| [rules_watch_cog.md](rules_watch_cog.md) | Rules Watch cog design |
| [risky_roll_spec.md](risky_roll_spec.md) | Risky Rolls (`/risky start`, roll mechanics) |
| [setup_spec.md](setup_spec.md) | `/setup` onboarding wizard — distinct from DUNGEON_KEEPER_TEST_ENV_SPEC.md |
| [starboard_spec.md](starboard_spec.md) | Starboard (threshold, self-star block, NSFW guard) |
| [todo_spec.md](todo_spec.md) | Server todo (`/todo` + context menu) |
| [voice_master_spec.md](voice_master_spec.md) | Voice Master (hubs, profiles, trust/block) |
| [voice_transcription_spec.md](voice_transcription_spec.md) | Voice-clip transcription listener (faster_whisper) — distinct from whisper_spec.md |
| [whisper_spec.md](whisper_spec.md) | Whisper (anon send, 3-guess reveal) |
| [xp_spec.md](xp_spec.md) | XP system (sources, leveling, leaderboard) |

**One caveat in this group:**

| Doc | What it covers | Caveat |
|---|---|---|
| [wellness_guardian_spec.md](wellness_guardian_spec.md) | Wellness | Current-behavior body now matches code (only `/wellness setup`/`away on\|off` are slash commands; caps/blackouts/partners/streaks are real but dashboard- and engine-only). **Activation gap:** no code path provisions a guild's `role_id`/`channel_id`, so the whole feature is dormant unless that row is seeded manually — see the callout at the top of the doc. Unbuilt member-facing commands moved to its Roadmap section. |

## Design specs (written to implement; may lag the code)

| Doc | What it covers | Notes |
|---|---|---|
| [announcements.md](announcements.md) | Announcements: dashboard-queued one-shot channel posts (embed + ping line, live preview, guild-local schedule, sent history, self-assign role buttons) | Built 2026-07-19, role buttons 2026-07-20; awaiting live testing; plan in [plans/timed-announcements.md](plans/timed-announcements.md) |
| [auto_delete_spec.md](auto_delete_spec.md) | Auto-delete message rules | |
| [chat_revive_spec.md](chat_revive_spec.md) | Chat Revive ("Ember"): rhythm-aware lull questions | v1 built; dashboard-managed (no slash commands); plan in [plans/chat-revive.md](plans/chat-revive.md) |
| [beta_tools_spec.md](beta_tools_spec.md) | Beta tools: synthetic activity for testers | Built |
| [DUNGEON_KEEPER_TEST_ENV_SPEC.md](DUNGEON_KEEPER_TEST_ENV_SPEC.md) | Test env with beta puppets | Built |
| [economy_spec.md](economy_spec.md) | Economy & perk shop (currency, quests, rentals) | Stages 0–4 built (rooms/v2 still design) |
| [plans/economy-sinks-round-2.md](plans/economy-sinks-round-2.md) | Sink round 2: paid quest rerolls, sponsor-a-QOTD (mod-approved), burn list, PvP coin wagers | In progress 2026-07-19 |
| [events_spec.md](events_spec.md) | Events cog | |
| [MUSIC_COG_CLAUDE_CODE_SPEC.md](MUSIC_COG_CLAUDE_CODE_SPEC.md) | Music cog (Lavalink) | Built |
| [post_monitoring_spec.md](post_monitoring_spec.md) | Post monitoring | |
| [plans/qa-tracker.md](plans/qa-tracker.md) | QA Tracker (volunteer testing crew: verdict cards, currency rewards, admin void) | Stages 0–4 built (schema/service, cog, poster cards, dashboard, auto-archive sweep); bounty idea still open |
| [plans/live-leaderboard.md](plans/live-leaderboard.md) | Live leaderboard panel (today's pulse, pace, anonymous feed, event-driven debounced refresh) | Built 2026-07-18; awaiting live testing |
| [plans/quest-variety-and-community-weeklies.md](plans/quest-variety-and-community-weeklies.md) | Quest engagement round: 13 new trigger kinds, auto-tracking community weeklies (tiered), live tracker, dynamic targets, board add-ons | Built 2026-07-18 (all stages); awaiting live testing + post-restart seed script |
| [role_menus_spec.md](role_menus_spec.md) | Role Menus (self-assign roles via buttons/dropdown, Oracle builder) | Plan: `plans/role-menus.md` |
| [survey_spec.md](survey_spec.md) | Anonymous Survey (launcher button, DM walkthrough, de-identified responses) | **Zero code** — no cog, no launcher, no DM session logic anywhere in `src/`. Pure design doc; not started. |
| [TGM-Dashboard-Concept-Spec.md](TGM-Dashboard-Concept-Spec.md) | Web dashboard concept | |
| [tools_spec.md](tools_spec.md) | Bot tools | |

## Aspirational specs (⚠️ read with care — not fully built)

These describe features or shapes of the system that don't match reality. They're kept as records of intent, but a reader taking them at face value **will be misled**.

| Doc | What it covers | Why it's aspirational |
|---|---|---|
| [duel_minigame_flows_v2.md](duel_minigame_flows_v2.md) | Duel minigame UX flows | **Partially aspirational** — Liar's Dice and Minesweeper flows are specced but unbuilt |

**2026-07-15 correction pass:** `dk_pvp_games_suite_spec.md`, `games_system_spec.md`, `guess_spec.md`, and `voice_master_spec.md` were rewritten to match current code and moved to the Reference table above; each now ends with (or, for `voice_master_spec.md`/`guess_spec.md`, never needed) a "Not Yet Built / Roadmap" section that preserves the design/unbuilt material they used to present as current (Minesweeper Duel, Liar's Dice, consent-gating, channel-allowlist admin commands, phantom `/guess` commands, etc.) instead of deleting it. `wellness_guardian_spec.md` got the same treatment but stays flagged above — its drift ran the opposite direction from expected (most of the doc's content turned out to be built, just dormant).

## Audits

| Doc | What it covers |
|---|---|
| [reviews/2026-07-01-deep-review.md](reviews/2026-07-01-deep-review.md) | Full system audit (2026-07-01) |
| [reviews/2026-07-01-rules-watch-followups.md](reviews/2026-07-01-rules-watch-followups.md) | Rules Watch follow-ups (audit follow-up) |
| [reviews/2026-07-20-rules-watch-tuning.md](reviews/2026-07-20-rules-watch-tuning.md) | Rules Watch tuning investigation (2026-07-20): why the guard flagged 98.7% of everything, what the ban record shows, and three failed attempts at automated detection. Boundary-gate fix shipped (45.4→7.7 alerts/day); **detection unsolved, human reporting primary** |
| [reviews/2026-07-20-automated-moderation-research.md](reviews/2026-07-20-automated-moderation-research.md) | Literature review companion to the tuning spec (2026-07-20): external evidence that the 0.61–0.66 detection ceiling is **structural, not local** — the closest published analogue (CGA derailment forecasting) hits the same wall with 1000× the labels and a 34–44% FPR. Confirms 13 positives is below every supervised/PU/weak-supervision floor; endorses the ledger + human-primary approach |

---

*One last reminder: `games_system_spec.md` and `dk_pvp_games_suite_spec.md` were corrected on 2026-07-15 and now describe current state (their remaining unbuilt ideas live in each doc's own Roadmap section). `wellness_guardian_spec.md` is still the one to read carefully — most of what it describes is real but dormant behind an unfilled activation gap; see its caveat above.*
