# Quest variety, community weeklies, live tracker

**Status:** planned · **Owner:** economy · **Spec:** `docs/economy_spec.md` §4

## Goal

Three-part engagement round, shaped by live-service quest research (Hearthstone
cadence, Sea of Thieves community grades, Helldivers Major Orders — and their
documented failure modes):

1. **Pool variety** — 13 new trigger kinds so the per-user board draws from a
   much wider slice of the bot's features.
2. **Server-wide weeklies** — community quests rebuilt from manager-hand-cranked
   progress into auto-tracking collective goals with milestone tiers.
3. **Live tracker** — a "happening now" section on the Statistics page plus a
   richer leaderboard embed, so the current state of every public quest is
   visible at a glance.

Driven by user request 2026-07-18: "I want the random pool to be varied as
possible … look at server-wide weeklies again … a live tracker for all the
public quests, that's what the stats page should have."

## Locked decisions (user Q&A 2026-07-18)

| Decision | Choice |
|---|---|
| New kinds scope | All 13 (Tier A + B below) |
| Bump tracker | Add member attribution, then `bump` kind on top |
| Library seeding | Seed + **activate** calibrated quests for the main guild |
| `voice_room_host` bar | Fires at **2+ other members** in the room |
| Community tiers | **3 tiers at 40/70/100%** of target, each pays flat |
| Community payout | Flat to all 30d-active **+ small top-contributor bonus** (top 3–5) |
| Narration | **DM the beat sheets to the owner** (kickoff / tier crossed / final-24h / resolution) — the owner plays host and posts manually. No bot-posted narration for now. |
| Cadence | **Gap week**: one ISO week on, one off (biweekly in practice) |
| Target sizing | **Fully automatic** from the last 4 weeks of that kind's activity, sized so a typical week lands ~70–80% |
| Tracker surfaces | Statistics page live section + richer pinned leaderboard embed |
| Completion ticker | **Anonymous aggregates only** (no member names) |
| Add-ons in scope | All four: weekly flip announcement, daily free reroll, clear-the-board set bonus, weekly 2× spotlight |
| Personal targets (follow-up ask) | **Dynamic per member**: counted targets derive from the member's own trailing activity, clamped to the quest's band; Gaussian draw stays as the no-history fallback. Reward stays flat (effort-equity, not output-pay). |

## Stage 1 — new trigger kinds

Each kind: entry in `quests.TRIGGER_KINDS`/`TRIGGER_KIND_INFO`
(`src/bot_modules/economy/quests.py:48`), a `fire_member_trigger` /
`fire_trigger_quests` hook at the feature's completion point, occurrence key,
spec table row (§4.5), tests. Pattern to copy: the `whisper`/`quote` additions.

**Tier A**

| kind | fires when | hook site | occurrence key |
|---|---|---|---|
| `chat_revive` | member responds to a chat-revive prompt | `services/chat_revive_service.py` response detection | `chat_revive:<prompt_id>` |
| `bump` | member bumps the server | `cogs/bump_tracker_cog.py` (after attribution fix — capture the bump interaction's invoker; today `_log_bump` has no user_id) | `bump:<bump_row_id>` |
| `voice_room_host` | member's Voice Master room reaches ≥2 other members (distinct, non-bot) | voice-state handling in `voice_master` (fire once per room lifetime on the crossing) | `voice_room_host:<channel_id>` |
| `pen_pal_complete` | Pen Pals session reaches its natural end (both fire) | `cogs/pen_pals_cog.py` session-end path | `pen_pal_complete:<session_id>` |
| `whisper_guess` | member correctly guesses a whisperer | `whisper/logic.py` guess resolution | `whisper_guess:<whisper_id>` |
| `guess_win` | member wins a Guess Who round | `cogs/guess_cog.py` round resolution | `guess_win:<round_id>` |
| `quoted` | someone else quote-cards the member's message | `cogs/quote_cog.py` beside the existing `quote` fire (credit the quoted author; self-quotes never fire) | `quoted:<quoted_message_id>` |
| `session_join` | member joins a scheduled game session | `cogs/games_session_cog.py` join path | `session_join:<session_id>` |

**Tier B**

| kind | fires when | hook site | occurrence key |
|---|---|---|---|
| `voice_message` | member posts a voice message (transcription path) | `cogs/voice_transcription_cog.py` | `voice_message:<message_id>` |
| `music_request` | member queues a song | `cogs/music_cog.py` enqueue | `music_request:<local_day>` (once/day by construction — bounded farm) |
| `birthday_set` | member sets their birthday | `cogs/birthday_cog.py` save | `birthday_set:set` (event = once ever, twin of `bio_set`) |
| `level_up` | member reaches a new level | XP award path (`cogs/xp_cog.py` / level service) | `level_up:<level>` (event kind: once per level) |
| `ama_answer` | hot-seat answers a question in their AMA | `cogs/games_ama_cog.py` answer path | `ama_answer:<game_id>:<q_idx>` |

**Kind activity ledger (feeds all dynamic sizing).** New table
`econ_kind_activity(guild_id, user_id, kind, local_day, count)` (PK on the
4-tuple), incremented inside `fire_trigger_quests`/`fire_trigger_inline` for
**every** occurrence — before/regardless of the personal-board filter, so it
records what members actually do, not just what paid. Pruned to a trailing
~10 weeks by the daily roll. This one table powers both personal dynamic
targets (per-user sums) and community auto-sizing (guild sums) — see
*Dynamic targets* below. Where cheap, backfill from `xp_events` for the kinds
it already mirrors (message_sent, reply_sent, reaction_given, voice_session,
qotd_reply) so those start warm.

Rejected (documented so we don't relitigate): `reaction_received`
(collusion-farmable; `starboard` covers the threshold version), `qa_verdict`
(already paid via `qa_reward` — double ledger), emoji stealer
(permission-gated), wellness/privacy (never incentivize), external games
(blocked on Gamebot parser sample).

## Stage 2 — seed + activate library quests (main guild)

Calibrated like the 2026-07-13 seeding: thresholds from real activity where
history exists (revive responses, bumps, voice rooms, music), sensible defaults
where it doesn't. Mix across daily/weekly/monthly; Gaussian bands where
counted. Activated immediately (user choice). Seed script in scratchpad like
last round.

## Stage 3 — community weeklies (auto-tracking)

- **Schema:** allow `trigger_kind` (+ nullable channel scope) on
  `qtype='community'` quests (migration — check for duplicate numeric prefixes
  first); per-quest tier payouts derived from `reward` (flat per tier) and
  `community_target`; contribution table
  `econ_community_contrib(quest_id, user_id, count)` for the top-contributor
  bonus + "N members contributed", fed by the same firing path.
- **Counting:** in `fire_trigger_quests`/`fire_trigger_inline`
  (`services/economy_quests_service.py:452,505`), after the personal-board
  pass, bump any active community quest of the same kind — **not** filtered by
  personal boards; every member's action counts. One action can advance both a
  personal quest and the community counter (intended).
- **Tiers:** 40/70/100% crossings stamp per-tier; each crossing pays flat to
  30d-active members via the `econ_community_payouts` exactly-once pattern with
  tier in the key. Resolution pays the top 3–5 contributors a small bonus.
- **Auto-sizing:** on activation, target = scaled guild-wide sum of the last
  4 full weeks of that kind's activity from `econ_kind_activity` (aim: typical
  week ≈ 70–80% of target, i.e. target ≈ weekly median ÷ 0.75). No manual
  override (user choice: fully automatic).
- **Scheduling:** economy loop ISO-week roll alternates on-week/off-week
  (`econ_day_marks`-style state). On-week roll activates the next community
  quest (rotation over a small library); next roll settles it.
- **Narration:** each beat (kickoff, tier crossed, final-24h, resolution)
  **DMs the owner** a ready-to-post beat sheet (progress numbers + suggested
  copy) instead of posting publicly. Owner hosts. Bot-posted narration is a
  possible later toggle.
- Manual (kind-less) community quests keep today's behavior untouched.

## Stage 4 — live tracker

- `GET /api/economy/quests/live` (manager-gated): community goal (progress, %,
  pace projection on daily buckets, time to roll, contributor count, tier
  states), per-quest completion aggregates for each cadence pool this period
  (assigned-count vs completed-count, counted-quest progress sums), event-kind
  counters, next-roll countdowns.
- Statistics page (`panels/economy-stats.js`) gains a top "Happening now"
  section, 30–60 s auto-refresh. **Anonymous aggregates only** — no member
  names, no confession rows in any breakdown.
- Leaderboard embed (`economy/leaderboard.py`): community bar gains tier
  markers + pace; tier crossings trigger an immediate in-place refresh (beside
  the hourly one).

## Stage 5 — add-ons

- **Weekly flip announcement:** at the ISO-week roll, post "this week's quests
  are up" (+ spotlight reveal) to the leaderboard/bank channel.
- **Daily free reroll:** one per member per day on the personal board; reroll
  swaps a board slot for a pool quest of a **different trigger kind** where
  possible; deterministic replacement recorded (small table — the pure-function
  board needs an override row).
- **Set bonus:** completing every daily on your board pays a bonus (ledger kind
  `quest_bonus`); weekly variant.
- **Weekly 2× spotlight:** one featured kind per week (rotating), quest payouts
  on that kind double; shown in flip announcement, embed, and `/quests`.

## Dynamic targets (user follow-up 2026-07-18)

Requirements scale to whoever they're issued to — the member for personal
quests, the guild for community quests. Both read `econ_kind_activity`.

**Personal (counted daily/weekly/monthly quests).** `effective_target` gains
an activity-aware path: a member's target for a period =
`clamp(round(median of their last 4 completed periods of that kind × 1.15),
target_min, target_max)` — a ~15% stretch over their own normal pace. Rules:

- Computed once per (user, quest, period) at first touch and **stored**
  (extend `econ_quest_progress` with the resolved target) so it never moves
  mid-period; `/quests` shows it like any target.
- **Fallback:** fewer than 2 periods of history for that kind → the existing
  deterministic Gaussian band draw. The band's `[min, max]` also stays as the
  hard clamp, so authors still control the sane range and sandbagging can't
  push a target below `target_min` (going quiet floors you at the band min,
  it never zeroes the quest).
- Fixed `target_count` quests (band unset) stay fixed — dynamic sizing is a
  band feature.
- **Reward stays flat.** Difficulty personalizes so *effort* is comparable;
  paying more for higher output would just re-reward the already-active.

**Community.** Covered in Stage 3 auto-sizing (guild-wide sums, same table,
same trailing-4-weeks window).

## Order & risks

Stages land 1→5; each is independently shippable. Risks: bump attribution
depends on what the bump interaction exposes (verify Disboard slash-command
payload in prod logs before promising per-member credit); `voice_room_host`
needs once-per-room-lifetime state (in-memory like Risky Rolls is fine —
restart forgiveness acceptable); reroll must not break counted-progress
(reroll blocked once progress > 0 on the slot); migration numbering — check
existing max prefix before adding (two 077s/078s exist already); dynamic
personal targets are cold until `econ_kind_activity` accrues ~2 periods per
kind (Gaussian fallback covers the gap; xp_events backfill warms the five
mirrored kinds); mild sandbagging is possible by going quiet but is floored
at `target_min` and self-defeating (less activity = less income anyway).
