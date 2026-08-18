# Data register — record of processing activities

Every table holding personal data: what it is, how long it is kept, whether the
erasure path clears it, and what leaves the box. Built by the 2026-08-05 staged
review's per-bundle GDPR passes; **promoted out of `docs/reviews/` on 2026-08-06
to a maintained document**, because a record of processing that is only a dated
audit snapshot goes stale — this one had drifted within a day of being written.

> **On the per-commit docs contract.** A new table holding per-user data needs a
> row here in the same commit, with an explicit purge/preserve decision. See
> `CLAUDE.md`. A preserved table must name the ground it is preserved on, not
> just the engineering reason.

**Related:** [gdpr_runbook.md](gdpr_runbook.md) is the operator procedure for
access, erasure and breach. `scripts/export_user_data.py` answers "what do we
hold on this person" from the live schema — run it rather than reading this
table when the question is about one member.

Columns: **Purge?** = is the table covered by `purge_user_data` (verify in code,
cite `file:line`); where a table is deliberately preserved, name the **Art 17(3)
ground**. **Processor** = data leaving the box.

## Preserved categories and their grounds

Erasure that quietly retains categories is worse than a documented partial
erasure. These are the five the purge deliberately keeps:

| Preserved | Art 17(3) ground | Strength |
|---|---|---|
| `econ_ledger` | (e) legal claims — and integrity of a double-entry record where deleting one side corrupts the counterparty's balance | Solid |
| Sanctions: jails, warnings, tickets, policy tickets | (e) legal claims — the canonical record if a moderation decision is challenged | Solid |
| `dm_audit_log`, `dm_consent_pairs` | (e) legal claims — the record exists precisely to answer "did they agree" | Solid |
| `no_contact_pairs`, `no_contact_events` | (e) legal claims + the **other** party's rights — erasing a protective order at the request of the restrained party defeats it | Solid |
| `reaction_log`, `voice_follow_log` | Attention-report evidence path | **Weak** — an internal-analytics rationale, not a clean statutory ground. Revisit if a request is contested |

Everything preserved is still **exported** on an access request: Art 17(3)
exempts deletion, not disclosure.

| Table / store | Feature | Data class | Retention | Purge? | Processor | Notes |
|---|---|---|---|---|---|---|
| econ_wallets, econ_ledger, econ_* (48 tables) | Economy | user_id + amounts + timestamps; meta sampled clean (no PII) | indefinite | YES — **`econ_purge_user` shipped `6ac71558`** (`economy_service.py:1304`, called at `privacy_service.py:201`); `econ_ledger` deliberately preserved | — | G1 closed |
| messages + attachments/mentions/embeds/reactions/sentiment | Message archive | **full content** (prod level=`all`, 452k of 631k rows have text); plus `deleted_at`/`deleted_source` — *when* a message was deleted from Discord and whether it was a plain deletion or an auto-delete sweep | indefinite | YES (keep_messages flag) — the deletion columns are part of the row and go with it | — | disclosure understates this — privacy-core U1. Deletion marking added `155_messages_deleted.sql`: the archive already survived Discord deletions, this records that one happened so mod surfaces stop offering a dead deep link. **A member's privacy-panel purge is deliberately not distinguished** — it lands as `discord` like any other deletion, so the archive does not record that a member exercised a privacy control. Exported via the existing `author_id` in `SUBJECT_ID_COLUMNS`; no new table |
| processed_messages, known_users | Message archive | user_id dedup/roster | indefinite | YES | — | |
| member_xp, xp_events, voice_sessions, member_activity, member_events | XP/activity | activity timestamps, voice presence | indefinite | YES | — | ~~xp_reaction_awards MISSING from purge~~ **added `6ac71558`** (`privacy_service.py:120`); 42,518 rows |
| member_gender | Gender service | mod-assigned gender (377 tagged) | indefinite | YES | — | **owner decision 2026-08-06: accepted as internal metrics, no change** (health-analytics G1 closed) |
| quality_score_leaves | Member quality | derived score | indefinite | YES | — | profiling — document logic |
| usage_events | Telemetry | command/panel usage per user | indefinite (no pruning, by design) | YES | — | spec discloses |
| anon_audit_log | Anon features | deanonymizing actor↔target map | 90d sweep (verified: 7 rows live) | YES — **DECIDED** | — | ✓ |
| user_interactions, user_interactions_log | Interaction graph | who-talks-to-whom edges | indefinite | YES | — | profiling |
| wellness_* (12 tables + partners) | Wellness | usage caps, streaks, blackouts, partner links | 30d post-optout GC (wired ✓) | YES | local Ollama only | model citizen; §8 reconciled |
| reaction_log | Reactions | reactor↔author pairs, 183k rows | indefinite | **NO** — attention-report evidence path; make preserve explicit (health G3) | — | biggest unpurged table |
| whispers, whisper_replies, whisper_guesses, whisper_reports, whisper_reply_reports | Whisper | anon message content + **sender identity**, report reasons | indefinite (30d age-lock ≠ delete) | self-service `/whisper forget-me` + deliberate audit preserve — **DECIDED** | — | forget-me has cross-guild over-delete bug (whisper A1, high); reporter/guessed rows survive forget-me (whisper D1); dashboard audit exposes ids not content ✓ |
| confession_threads, confession_emoji_assignments, confession_rate_limits | Confessions | deanonymizing author ids | **7-day TTL** (threads), verified live | **DECIDED** — TTL'd minimization, model pattern | — | best story in repo; propose same TTL for guess confession_text |
| bios, bio_answers, bio_field_values | Bios | self-disclosed profile text | self-service delete ✓; archived-on-leave **12-month TTL** (`9374c306`, migration 149) | YES — **added `6ac71558`** (`privacy_service.py:123-125`) | — | both G2 and G3 closed |
| pen_pals_sessions, pen_pals_pool, pen_pals_blocks | Pen Pals | pairing metadata ONLY (no letter content ✓), protective blocks | pool pruned; sessions kept for no-repeat | **DECIDED — preserve** (matchmaking memory + protective blocks) | — | ✓ |
| pen_pals_pool_events | Pen Pals | who joined/left the matching pool and why (`join`/`leave`/`skip` + reason); no content | indefinite | **YES — purge** (migration 160). Operational history only: nothing downstream reads it, the no-repeat matchmaking memory lives in `pen_pals_sessions`, and the protective blocks live in `pen_pals_blocks` — so there is no Art 17(3) ground to hold it against an erasure request | — | `user_id` already in `SUBJECT_ID_COLUMNS`, so export sees it ✓ |
| dm_audit_log, dm_consent_pairs, dm_requests | DM perms | consent records + transition audit | live map pruned on revoke; audit indefinite | **DECIDED** — forensic preserve (privacy_spec:111) | — | ✓ |
| member_birthdays | Birthday | **month/day only, no year** ✓ + visibility pref | until removed | YES — **added `6ac71558`** (`privacy_service.py:121`) | — | minimized by design |
| jails, warnings, tickets, ticket_participants, policy_tickets, role_grant audit | Mod actions | sanction history, mod-authored reasons (no transcripts ✓) | indefinite | **DECIDED — preserve** (canonical mod record) | — | ✓ |
| watched_users | Mod watch | watcher↔watched pairs (2 rows) | until unwatched | YES — **added `6ac71558`, both sides** (`privacy_service.py:156`) | LAN LLM screening | covert moderation, mods-only ✓ |
| no_contact_pairs, no_contact_events | Mod safety | protective orders + attempt log (no text) | indefinite | **DECIDED** — deliberate preserve; add spec line (dmperms-nocontact G1) | — | ✓ |
| rules_events, rules_labels | Rules watch | 240-char content excerpts + matched phrase; mod-verdict training labels | preserve as evidence + **180d sweep for dismissed events** (`49a02867`, `rules_watch/ledger.py:442`) | **NO** — deliberate evidentiary preserve | LAN llama-server only | G1 closed; excerpt escapes storage-level dial — document |
| nsfw_classifications, nsfw_detections, nsfw_blocks | Image Guard | body-part tags (**age-gated + spoiler-required channels** — widened 2026-08-15, see below), block records w/ author_id | indefinite (deliberate, spec'd) | **DECIDED — preserve**; revisit TTL on nsfw_detections in 6mo | — (all inference local) | observe mode ON in prod; log channels 0. ⚠️ scope widened — owner-decided, see note |
| guess_consents | Guess | consent evidence: when, and which disclosure version | kept through optout (`withdrawn_at` stamped); cleared by full erasure | YES — `privacy_service.py` simple-table list | — | Art 7(1) evidence, migration 154 |
| guess_rounds (+confession_text), guess_guesses, guess_audit_log, guess_cache/ files | Guess | intimate images on disk until solve; anon confession text + submitter id | originals of unsolved rounds: **90d age-out** (`775d903d`); rows themselves indefinite | **STILL NO** — no `guess_*` table is in `purge_user_data` (grepped 2026-08-06) | — (local) | consent view + `/guess optout` shipped `775d903d` (U1/G1/G2 closed). **Open:** purge coverage, and the confession-text TTL that was only ever a "consider" |
| casino_* (bets, daily, weekly, member_stats, hands incl. **mines**, **rounds**) | Casino | wagering history (behavioral) | indefinite | YES — folded into `econ_purge_user` (`6ac71558`); the five `*_rounds` tables added there with migration 158, when a round stopped belonging to the channel and started naming the player who opened it, and `casino_mines_hands` with migration 164 (2026-08-16) — the hand-maintained list is why a new per-member casino table is invisible to erasure until someone adds it. `casino_pools_rounds` stays out: the daily market has no owner, and its per-member data is all in `casino_pools_bets` | — | money code = repo's best |
| games_*, duels, hot_potato_*, pressure_*, quickdraw_*, mc_*, chicken_* | Games | participation, winners, nicks; anon games via anon_audit_log 90d | indefinite | **DECIDED** — anon family via audit sweep; per-user rows fold into econ_purge_user | — | ✓ |
| voice_follow, voice_follow_log | Voice follow | who-follows-whom | indefinite | **NO** — attention-report evidence; preserve explicitly w/ reaction_log | — | |
| voice_master_profiles, voice_master_trusted | Voice Master | member room prefs + trust lists | until changed | YES — **added `6ac71558`** (`privacy_service.py:122,157`; trusted both sides) | — | |
| games_external_messages | External games | **content + embeds_json** parse buffer | **30d post-parse sweep** (`49a02867`, `games_external/logic.py:231`) | — (buffer, not a purge target) | — | 11,304 rows; oldest 2026-07-07, so the sweep has not had to bite yet |
| intake_cards, intake_card_steps | Intake | newcomer progress, done_by greeter ids | indefinite (26 rows) | needs decision — low priority | — | intake-confessions G2 |
| greeting_watch | Greeting watch | ids+timestamp ONLY, no text (by design ✓) | **30d GC on verdicted rows** (`49a02867`, `greeting_watch_service.py:303`) | — | — | G1 closed. 352 resolved rows still present — the GC runs off the 60s loop, so it clears on the next bot restart |
| audit_log, incident_events, role_events, role_prune_events | Mod/audit misc | actor/target ids | ? | role_events YES, others **NO** | — | |
| voice_transcription_config | Voice transcription | config only — transcripts never stored | n/a | n/a | local faster-whisper | ✓ no personal data |
| mention_award_rules | Mention Awards | guild config (channel, amount, conditions JSON) + `created_by` admin id; **a `from_user` chip names a member** inside the JSON (migration 157) | until deleted by an admin | **SPLIT.** `from_user` chips: **YES** — purge strips the erased member's chips, deleting a rule left empty (`privacy_service.py`, mention_award_rules step); also in `LIST_VALUED_MEMBER_COLUMNS` so the export discloses the JSON blind spot. `created_by`: preserved, Art 17(3)(e) — the record of who opened a currency faucet, counterpart to the `econ_ledger` rows it produces | — | Awards land in `econ_ledger` (preserved, registered) and dedupe via `games_external_payouts`. **Message content is never stored**: chips are matched live off the gateway and discarded |
| music_playlist_tracks | Music Playlist | `added_by` poster id + track/title/artist + source link + message pointer (migration 165) | indefinite by design — rolled-off rows are the "what did we listen to in July" history (kept, no ageing-out yet) | **YES — purge** (decided in the spec: no Art 17(3) ground — "who posted a song" carries no legal-claims or integrity weight, and the Spotify playlist is keyed by track id, not poster). Deleted by `music_playlist_store.purge_member_rows`, called from `privacy_service.purge_user_data` | Spotify Web API (track ids only, no member identity) | Member column is `added_by`, already in `privacy_service.SUBJECT_ID_COLUMNS` (`privacy_service.py:272`), so the access export sees the table with no code change |
| music_playlist_unmatched | Music Playlist | `added_by` poster id + posted link, extracted title, best-candidate track + score; `reviewed_by` mod id | indefinite (reviewed rows kept as queue history) | **YES — purge**, same decision and same function (called from `privacy_service.purge_user_data`): the member's rows are deleted; rows they merely *reviewed* survive with `reviewed_by` nulled (a review is a mod action, not the reviewer's personal record) | — (queue rows never leave the box; only an approval's playlist add reaches Spotify) | `added_by` deliberately chosen over the spec's `posted_by` so `SUBJECT_ID_COLUMNS` covers it for free |
| todos | Todo / Mod Chores | `added_by` and `completed_by` Discord ids against a task the mod team shares; task/description text is server work product, not member disclosure. `missed_at` (migration 166) names nobody — it records that a recurring chore's day passed undone | indefinite | **YES — anonymised, not deleted** (`privacy_service.py:_scrub`, `added_by`→0, `completed_by`→NULL). The row is two things at once: the team's outstanding work and two ids naming a person. Deleting it to reach the ids would take real work off other people's list — a task someone else is part-way through vanishing because an unrelated member left — so the ids are cleared and the work stands. Nothing identifying survives, which is what the erasure right asks for; the blanked values are the same "unknown" every surface already renders for an unresolvable member | — | **Was missing from this register entirely until 2026-08-17** — the table predates the register and no row was ever written, so it was invisible to an access or erasure request. `completed_by` was likewise absent from `SUBJECT_ID_COLUMNS`, so an export showed the tasks a member *added* and silently omitted the ones they *did*; added in the same commit. Found while building the mod chore board, which surfaces "who ticked it" into a Discord channel |
| todo_board, todo_recurring | Todo / Mod Chores | **no member data**: channel/message ids and cadence config. `todo_recurring.created_by` is the admin who defined the chore | indefinite | n/a — config, not personal data (the `created_by` admin id rides along and is not swept) | — | listed so the feature's three tables are all accounted for rather than two of them reading as an omission |
| _(bundles append below)_ | | | | | | |

### Note — `nsfw_detections` scope widening, 2026-08-15

The tagger's scope changed from "Discord-age-gated channels" to "age-gated **or** spoiler-required channels". No new table and no new column; the same rows now cover more channels.

**Why.** The spoiler guard was letting bare male chests through (todo #99). Measured on 869 production classifications, Marqo scores a bare male chest under the 0.5 threshold in 14% of chest-only images against 8% for female, and the misses sit at 0.05–0.32 — low enough that no usable threshold catches them (0.05 flags 98.3% of all traffic). The fix is a rule keyed on NudeNet's chest labels, which requires labels; 10 of the 17 spoiler-required channels in production are not age-gated, so without the widening the rule is unenforceable where it is most needed.

**What it costs.** Body-part labels and bounding boxes are now stored for uploads in channels Discord does not age-gate. Those images were already being downloaded and scored by Marqo; what is new is the per-part inventory. The privacy boundary is no longer enforced by Discord's own age gate alone — adding a channel to the spoiler-required list is now also a decision to record labels for its uploads.

**Decision.** Taken by the server owner on 2026-08-15, against two rejected alternatives: tag-without-persisting (preserves "never stored", loses the data needed to tune the floor) and age-gated-only (preserves the structural guarantee, leaves the bug live in 10 channels).

**Erasure/export unchanged.** Neither table has an `author_id`; authorship joins through `messages`, so the existing `purge_user_data` behaviour and the preserve decision above are unaffected. Retention is still indefinite, and the 6-month TTL review on `nsfw_detections` now matters more, since the table covers more channels than when that review was scheduled.

## Cross-cutting questions each bundle must answer

1. Tables touched and their personal-data columns (user_id alone is personal
   data under GDPR — pseudonymous, not anonymous).
2. Lawful basis: opt-in surface? NSFW behind `channel.is_nsfw()`?
3. Retention: any TTL/cleanup loop, or grows forever?
4. Purge coverage: does the privacy purge path delete this feature's rows for
   a requesting user? (The authoritative list lives in the privacy service —
   Wave 1 bundle 1 documents it here.)
5. Minimization: content stored where metadata would do?
6. Processors: Ollama, Marqo, Lavalink, Spotify, LLM APIs, anything else.
7. Mod visibility: which audit panels expose this data, gated how?
8. Exportability: could we answer a subject-access request for this feature?

## Deliberately not personal data

Tables that look like they belong in the register and are absent on purpose.
Recorded here so their absence reads as a decision rather than an oversight —
and so that adding a member-identifying column to one is understood as putting
it *into* the register, with a purge decision, rather than a routine schema
tweak.

| Table | What it holds | Why it stays out |
|---|---|---|
| `casino_win_history` (migration 162, 2026-08-15) | `(guild_id, payout, ts)` — a rolling `WIN_HISTORY_KEEP`-row window of the payouts this guild has publicly announced, powering the big-win broadcast's top-3% `@here` tier | No `user_id` and no other member identifier. It answers only "how big is a big win in this guild lately", which never needs to know who won. The cheaper option was raising `casino_ticker`'s retention 25 → 500, and it was rejected precisely because those rows *do* carry `user_id`: it would have retained 20× more per-member play history to power a header. Pinned by a schema assertion in `tests/test_casino_service.py` |
| `music_playlist_messages` (migration 165, 2026-08-17) | `(guild_id, message_id, channel_id, status, processed_at)` — the processed-message ledger that makes a restart or a channel re-scan idempotent | No member column. Authorship joins through `messages` (already registered, purged with `keep_messages` semantics); the ledger itself records only that a message id was seen, which must survive erasure or a re-scan would re-process — and re-credit — messages the purge just cleared |

## Processors (running inventory)

- **Local/LAN only**: Marqo+NudeNet (ONNX in-process), VADER, faster-whisper,
  Ollama/llama-server (LAN, host-allowlisted) — moderation, wellness, guess.
- **Anthropic API (cloud)**: advisor, quest-idea gen, game-prep.
  What the advisor sends, as of 2026-08-16 (todo #100): the asker's question;
  the cached manual; a secret-filtered, admin-gated config summary; and — only
  when `advisor_server_context` is on — names/topics of the **shared** channels
  the asker can see, staff-authored dashboard `docs`, recent sent
  announcements, and the asker's **role names and permissions**. Per-member
  rooms (jail, tickets, Pen Pals, bios wizard) are excluded for every asker
  including staff: their channel names identify their occupants. No Discord identity: no user id, and the
  asker's display name was removed with the rest.
  **No member content, by construction.** Until 2026-08-16 a background loop
  (`guild_pins_loop`) snapshotted the first five pins of every shared channel
  and fed them into each ask, taking an embed's title+description when the
  message had no content — which is the shape of a bio, a starboard entry, or
  any other embed. That loop, its snapshot, and its `is_private_room` gate are
  deleted, not merely disabled, and `tests/test_advisor_context.py` fails if any
  of them reappear. Nothing else in the AI path reads `bio_answers`,
  `bio_field_values`, `messages`, or any other per-member store — this remains
  true only as long as no new source is added to
  `advisor_context.build_asker_context`, which is why that function carries a
  pointer back to this register.
  Privacy-notice line: **shipped** — `manual.html` §Where your data goes now
  itemises what is and is not sent.
- Lavalink: local (127.0.0.1) ✓. **Spotify Web API (cloud)**: track queries,
  and — since Music Playlist (2026-08-17) — playlist add/remove calls carrying
  track ids only. No member identity in either direction; Spotify never learns
  who posted a song. OAuth token storage → security sweep. Discord CDN:
  inherent.

## Known context (seeded from loose-ends audit)

- `Discord Messages/` in repo root is Billy's personal Discord export, not
  bot data — flagged for relocation in `2026-08-05-loose-ends.md` §6.
- Image Guard: web Blocked Images panel is the audit trail of record
  (verified wired 2026-08-06); Discord fan-out declined (§2 closed).

## Gap list (final, 2026-08-06 — see synthesis doc for the fix queue)

Status re-verified against the code 2026-08-06
(`2026-08-06-review-docs-accuracy-audit.md`). Strike items **here** when
they land, not only in the synthesis header.

1. ~~Erasure-path: placeholder blowout + missed tables (synthesis #1)~~ —
   **shipped `6ac71558`**.
2. ~~Disclosure vs reality: content retention ON while copy hedges (U1)~~ —
   **shipped `1dfa8fea`**.
3. ~~Undisclosed sensitive-attribute profiling: mod-assigned gender~~ —
   **owner decision 2026-08-06: accepted, no change.**
4. ~~Guess: consent/opt-out package~~ — **shipped `775d903d`**. Retention
   half is partly open: 90d original age-out shipped, `guess_*` purge
   coverage and the confession-text TTL did not.
5. ~~Processors needing a privacy-notice line: Anthropic (advisor),
   Spotify~~ — **closed 2026-08-16.** Both are named in `manual.html`
   §Your Data & Privacy → "Where your data goes"; the item's claim that
   neither appears in a user-facing surface was already stale when
   re-read. The Anthropic entry was expanded the same day (todo #100) to
   itemise what is and is not sent.
6. TTL/sweep debt — ~~rules_events (dismissed)~~ `49a02867`,
   ~~games_external_messages~~ `49a02867`, ~~bios archives~~ `9374c306`,
   ~~greeting_watch~~ `49a02867`. **Still open:** `xp_events` (deferred
   with a design note — needs a rollup table, see synthesis #9) and
   guess `confession_text`.
7. ~~No erasure runbook~~ — `docs/gdpr_runbook.md` shipped with
   `6ac71558` and is now listed in INDEX.md. **Still open:** no
   subject-access/export story (explicitly deferred).
8. **OPEN** — backups retain erased users; document the retention window
   in the runbook.

Decided & healthy: whisper, wellness, confessions, dm-perms, no-contact,
pen-pals, voice transcription, Image Guard, anon games family, sanctions,
economy ledger (preserve), birthdays (minimized).
