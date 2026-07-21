# Server Map — The Golden Meadow

> **Snapshot, not live-synced.** Pulled directly from the Discord API on
> 2026-07-18 (77 channels, 109 roles at the time). Channels/roles change as
> the server evolves — treat this as a point-in-time reference, not a
> mirror. Re-generate rather than hand-editing when it drifts. (No
> generator script is checked in — the 2026-07-18 snapshot was produced
> ad hoc; small factual rot has been spot-fixed since, last 2026-07-21.)

A guided tour of the server's channels and roles, organized the way a
member sees them. Channel descriptions below are the server's own channel
topics where one exists; channels without a topic are described from their
purpose in the bot's feature specs (see `docs/INDEX.md`).

---

## Channels, by category

### Welcome
| Channel | Notes |
|---|---|
| ✔️│verify-here | Verify with Double Counter to gain entry to the rest of the server. |
| 🕊️│welcome-chat | Say hello — new members introduce themselves and settle in. |
| 🏢│welcome-procedure | Staff-facing: the onboarding checklist for new members. |
| 🏢│greeter-chat | Staff-facing: coordination channel for the Greeters role. |
| 🏢│leave-join-log | Staff-facing: automated join/leave log. |
| 🏢│promotion-reviews | Staff-facing: discussion of member promotions (e.g. to Moderator). |

### Front Office
| Channel | Notes |
|---|---|
| 📣│announcements | Official server news, updates, and events. |
| 👋│staff-bios | Meet the team running the Meadow — who we are and how to reach us. |
| 🫂│server-friends | Partnered and affiliated servers. |
| 📜│rules-and-faq | House rules and answers to common questions — read before posting. |
| 📈│level-up | XP level-up pings and role rewards land here. |
| 🙏│booster-perks | Perks and thank-yous for server boosters. |
| 🎟️│open-help-ticket | Open a private ticket with staff (spawns a `ticket-*` channel under **Tickets**). |
| 💌│dm-permissions | Set who can DM you server-side — opt in or out, consent first. |
| 🔝│bumpatorium | Bump the server on listing sites when the cooldown's up. |

### Community
| Channel | Notes |
|---|---|
| 💛│the-meadow | Main gathering spot. Conversation can get spicy; media must stay strictly SFW. |
| 👋│about-us | General "about the server" info. |
| 🤳│selfies | SFW selfies and images. |
| 📸│photo-fun | General SFW photo sharing. |
| ⭐│star-board | React ⭐ to pin a message here — the server's hall of fame. |
| 🎵│music | Queue tracks with the music bot (`/play`). |
| 💜│big-feelings | Softer space for venting and support — SFW, lead with care. |
| 💛│golden-girls | Role-gated lounge for the Golden Girls crew. |
| ✨│special-interests (forum) | Members create their own topic threads — SFW only. |

### Spicy *(NSFW-gated)*
| Channel | Notes |
|---|---|
| 🫦│spicy-chat | Auto-deletes every 30 days. Vulnerable-space etiquette (CWs, consent for tags/photos, no screenshotting). |
| 🫦│photo-challenge | Timed NSFW photo prompts — own content only. |
| 🔥│flash-channel | Spicy conversation and NSFW media. |
| 😮‍💨│spicy-audios | Spicy audio clips. |
| 🤷│guess-who | Guess-the-member game — see `docs/guess_spec.md` (`/veil`, crop-and-guess flow). |
| 🎲│risky-rolls | `/roll`-driven Q&A game — high roll asks, low roll answers. Safeword: Kiwi 🥝. |
| 🫦│spicy-games | NSFW party games and prompts. |
| 🤐│confessions | Anonymous confessions via the bot — logged for admin review. |
| 🤫│whisper | Anonymous DM-relay game with a 3-guess reveal — see `docs/whisper_spec.md`. |
| 🙋‍♂️│ama | Ask-me-anything hot seat. |
| 🫦│spicy-interests (forum) | NSFW discussion forum, thread-tagged for consent. |

### Spicy games
*(empty at snapshot time — reserved category)*

### Games
| Channel | Notes |
|---|---|
| 🎲│games | Party games, minigames, and leaderboards (`/games`). |
| 🎲│wordle | Daily Wordle — post your grid, chase the streak. |
| 🎲│co-ordle | Co-op Wordle. |
| 🎲│cat-bot | Home for the external Cat Bot. |
| 🎲│quizzlers | Quiz game channel. |

### economy game
| Channel | Notes |
|---|---|
| 🏦│how-it-works | Explains the currency/quest/perk-shop economy — see `docs/economy_spec.md`. |
| 📈│stats | Economy stats/leaderboard. |
| 🪙│shop | Perk shop. |

### Voice Channels
| Channel | Notes |
|---|---|
| events (voice) | Scheduled voice events. |
| Join To Create (voice) | Voice Master hub — join to spin up your own personal voice channel. |
| voice-how-to | How to use Join To Create rooms — make your own channel, control who gets in. |

### Admin *(staff-only)*
| Channel | Notes |
|---|---|
| 🏢│mod-chat | Moderator coordination. |
| 🤖│bot-command-spam | Staff sandbox for bot commands. |

### Jail *(staff-only, ephemeral)*
One `jail-<member>-<date>` channel per jailed member, auto-created by the
jail/ticket system — see `docs/dungeon_keeper_jail_ticket_spec.md`.

### Tickets *(staff-only, ephemeral)*
One `ticket-<member>-<date>` channel per open help ticket, spawned from
🎟️│open-help-ticket.

### Hidden Channels
*(empty at snapshot time — working category for the `/hidden` cog, which
temporarily relocates channels here; see `docs/hidden_channels_spec.md`)*

### intro_pages
| Channel | Notes |
|---|---|
| 👋│server-rules | Rules reference. |
| 👋│frequently-asked-questions | FAQ reference. |
| 👋│moderation-team-bios | Mod team bios. |

### Bio-writer
*(empty at snapshot time — working category for the Bios cog's private per-member profile-wizard channels)*

### dev *(staff-only)*
| Channel | Notes |
|---|---|
| testing-queue | QA Tracker cards post here (from each commit's `Testing:` section; the old `docs/TESTING_QUEUE.md` mirror was retired 2026-07-18). |
| admin-tests / moderator-tests / user-tests | Role-scoped QA checklists. |
| dev-discussion | Dev coordination. |

---

## Roles, at a glance

109 roles exist; most are functional (self-assigned via role menus,
bot-managed, or cosmetic name colors) rather than a strict permission
ladder. Grouped by purpose rather than listed individually:

- **Staff & access** — `Dungeon-Keeper` (bot, admin), `Admin`, `#### Mods`,
  `Moderator`, `Greeters`, `game-host`, `Jailed`.
- **Membership milestones** — `Member` (base verified role), `Denizen`,
  `veteran`, `✨Golden Girl✨`, `Boosters ❤️`.
- **DM preference** (self-assigned, mutually exclusive) — `DMs: Open`,
  `DMs: Ask`, `DMs: Closed` — see `docs/dm_perms_spec.md`.
- **Content/interest self-select** — `Spicy`, `Games 🃏`, `Spicy Games`,
  `Risky rolls🎲`, `Guess Who`, `whisper`, `veil`, `ama`, `Music League`,
  `Worldle`, `Co-ordle`, `Cat Bot`, `FlashChannel`, `TruthOrDare`,
  `Policy polls 🤓`, `QOTD`.
- **Age ranges** (self-select) — `21-29` … `60-69`.
- **Regions** (self-select) — `North America`, `South America`, `Europe`,
  `Asia`, `Oceania`, `Africa`.
- **Cosmetics** (name-color only, no permissions) — `dusk ember`,
  `firefly`, `golden hour`, `meadow sunrise`, `midnight poppy`,
  `molten core`, `neon meadow`, `velvet dusk`, `wildflower`, `rose gold`,
  plus several emoji-named color roles.
- **Bot-managed utility** — `Double Counter`, `Needle`, `Gait`,
  `To-do Bot`, `OpenMusicBot`, `Cat Bot`, `Gamebot`, `DISBOARD.org`,
  `Top.gg`, `Discadia`, `QuizBot` — attached to their respective
  integrations, not assigned to members.

Most self-select roles above are granted through Role Menus
(`docs/role_menus_spec.md`) rather than commands.
