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
| [bios_cog_spec.md](bios_cog_spec.md) | Bios cog (profiles, wizard) |
| [birthday-announcement.md](birthday-announcement.md) | Birthday announcement message format |
| [birthday_spec.md](birthday_spec.md) | Birthday set/remove, daily celebration |
| [confessions_spec.md](confessions_spec.md) | Anonymous confessions, anon replies, mod log |
| [dm_perms_spec.md](dm_perms_spec.md) | DM permission system (open/ask/closed, consent pairs) |
| [dungeon_keeper_jail_ticket_spec.md](dungeon_keeper_jail_ticket_spec.md) | Jail/ticket/policy/warn system |
| [emoji_stealer_spec.md](emoji_stealer_spec.md) | Emoji stealer (URL command + context menu) |
| [pen_pals_spec.md](pen_pals_spec.md) | Pen Pals pooling + private channels |
| [pressure_cooker_spec.md](pressure_cooker_spec.md) | Pressure Cooker duel |
| [privacy_spec.md](privacy_spec.md) | Data deletion (`/delete_me`, `/delete_user`) |
| [quote_renderer_spec.md](quote_renderer_spec.md) | Quote/banner card renderer (shared service: themes, fonts, slim/custom borders) |
| [reporting_spec.md](reporting_spec.md) | Reporting / dashboard reports |
| [rules_watch_cog.md](rules_watch_cog.md) | Rules Watch cog design |
| [risky_roll_spec.md](risky_roll_spec.md) | Risky Rolls (`/risky start`, roll mechanics) |
| [starboard_spec.md](starboard_spec.md) | Starboard (threshold, self-star block, NSFW guard) |
| [todo_spec.md](todo_spec.md) | Server todo (`/todo` + context menu) |
| [voice_master_spec.md](voice_master_spec.md) | Voice Master (hubs, profiles, trust/block) |
| [whisper_spec.md](whisper_spec.md) | Whisper (anon send, 3-guess reveal) |
| [xp_spec.md](xp_spec.md) | XP system (sources, leveling, leaderboard) |

**One caveat in this group:**

| Doc | What it covers | Caveat |
|---|---|---|
| [guess_spec.md](guess_spec.md) | Guess image game | Mostly Reference, but contains **phantom** commands: `/guess optout` and `/guess stats` don't exist, and the real `/guess prompt` is undocumented |

## Design specs (written to implement; may lag the code)

| Doc | What it covers | Notes |
|---|---|---|
| [auto_delete_spec.md](auto_delete_spec.md) | Auto-delete message rules | |
| [beta_tools_spec.md](beta_tools_spec.md) | Beta tools: synthetic activity for testers | Built |
| [DUNGEON_KEEPER_TEST_ENV_SPEC.md](DUNGEON_KEEPER_TEST_ENV_SPEC.md) | Test env with beta puppets | Built |
| [economy_spec.md](economy_spec.md) | Economy & perk shop (currency, quests, rentals) | Stages 0–4 built (rooms/v2 still design) |
| [events_spec.md](events_spec.md) | Events cog | |
| [MUSIC_COG_CLAUDE_CODE_SPEC.md](MUSIC_COG_CLAUDE_CODE_SPEC.md) | Music cog (Lavalink) | Built |
| [post_monitoring_spec.md](post_monitoring_spec.md) | Post monitoring | |
| [TGM-Dashboard-Concept-Spec.md](TGM-Dashboard-Concept-Spec.md) | Web dashboard concept | |
| [tools_spec.md](tools_spec.md) | Bot tools | |

## Aspirational specs (⚠️ read with care — not fully built)

These describe features or shapes of the system that don't match reality. They're kept as records of intent, but a reader taking them at face value **will be misled**.

| Doc | What it covers | Why it's aspirational |
|---|---|---|
| [dk_pvp_games_suite_spec.md](dk_pvp_games_suite_spec.md) | PvP duel/group games | Stale module path (`dk/cogs/games/` doesn't exist); §9.3 Minesweeper Duel and §9.6 Liar's Dice are fully specced with **zero code**; contains a BaseGame/BaseGame copy-paste bug |
| [games_system_spec.md](games_system_spec.md) | Party games suite | Says "19-game" (17 exist); uses old standalone `/ffa` format instead of `/games play ffa`; documents phantom admin commands; omits Photo Challenge; the consent system it references has been removed |
| [wellness_guardian_spec.md](wellness_guardian_spec.md) | Wellness | Documents ~22 `/wellness` commands; only 3 exist (`/wellness setup`, `/wellness away on\|off`); caps, blackouts, partners, etc. are unbuilt |
| [duel_minigame_flows_v2.md](duel_minigame_flows_v2.md) | Duel minigame UX flows | **Partially aspirational** — Liar's Dice and Minesweeper flows are specced but unbuilt |

## Audits

| Doc | What it covers |
|---|---|
| [reviews/2026-07-01-deep-review.md](reviews/2026-07-01-deep-review.md) | Full system audit (2026-07-01) |
| [reviews/2026-07-01-rules-watch-followups.md](reviews/2026-07-01-rules-watch-followups.md) | Rules Watch follow-ups (audit follow-up) |

---

*One last reminder: the three aspirational specs — `games_system_spec.md`, `dk_pvp_games_suite_spec.md`, and `wellness_guardian_spec.md` — should be read as **intent, not current state**.*
