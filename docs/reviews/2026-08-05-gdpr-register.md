# GDPR data register — accumulating, 2026-08-05 staged review

Every bundle's GDPR pass appends rows here; Wave 5 dedupes and writes the gap
list. Columns: **Purge?** = is the table covered by the privacy purge path
(verify in code, cite file:line); **Processor** = data leaving the box.

| Table / store | Feature | Data class | Retention | Purge? | Processor | Notes |
|---|---|---|---|---|---|---|
| econ_wallets, econ_ledger, econ_* (48 tables) | Economy | user_id + amounts + timestamps; meta sampled clean (no PII) | indefinite | recommendation: preserve ledger (audit integrity), purge per-member state via econ_purge_user helper (economy-core G1) | — | |
| messages + attachments/mentions/embeds/reactions/sentiment | Message archive | **full content** (prod level=`all`, 452k of 631k rows have text) | indefinite | YES (keep_messages flag) | — | disclosure understates this — privacy-core U1 |
| processed_messages, known_users | Message archive | user_id dedup/roster | indefinite | YES | — | |
| member_xp, xp_events, voice_sessions, member_activity, member_events | XP/activity | activity timestamps, voice presence | indefinite | YES | — | **xp_reaction_awards (40k rows) MISSING from purge — games-platform A2** |
| member_gender | Gender service | mod-assigned gender (377 tagged) | indefinite | YES | — | **owner decision 2026-08-06: accepted as internal metrics, no change** (health-analytics G1 closed) |
| quality_score_leaves | Member quality | derived score | indefinite | YES | — | profiling — document logic |
| usage_events | Telemetry | command/panel usage per user | indefinite (no pruning, by design) | YES | — | spec discloses |
| anon_audit_log | Anon features | deanonymizing actor↔target map | 90d sweep (verified: 7 rows live) | YES — **DECIDED** | — | ✓ |
| user_interactions, user_interactions_log | Interaction graph | who-talks-to-whom edges | indefinite | YES | — | profiling |
| wellness_* (12 tables + partners) | Wellness | usage caps, streaks, blackouts, partner links | 30d post-optout GC (wired ✓) | YES | local Ollama only | model citizen; §8 reconciled |
| reaction_log | Reactions | reactor↔author pairs, 183k rows | indefinite | **NO** — attention-report evidence path; make preserve explicit (health G3) | — | biggest unpurged table |
| whispers, whisper_replies, whisper_guesses, whisper_reports, whisper_reply_reports | Whisper | anon message content + **sender identity**, report reasons | indefinite (30d age-lock ≠ delete) | self-service `/whisper forget-me` + deliberate audit preserve — **DECIDED** | — | forget-me has cross-guild over-delete bug (whisper A1, high); reporter/guessed rows survive forget-me (whisper D1); dashboard audit exposes ids not content ✓ |
| confession_threads, confession_emoji_assignments, confession_rate_limits | Confessions | deanonymizing author ids | **7-day TTL** (threads), verified live | **DECIDED** — TTL'd minimization, model pattern | — | best story in repo; propose same TTL for guess confession_text |
| bios, bio_answers, bio_field_values | Bios | self-disclosed profile text | self-service delete ✓; archived-on-leave kept forever | add to purge (penpals-bios G3); archive TTL decision (G2) | — | |
| pen_pals_sessions, pen_pals_pool, pen_pals_blocks | Pen Pals | pairing metadata ONLY (no letter content ✓), protective blocks | pool pruned; sessions kept for no-repeat | **DECIDED — preserve** (matchmaking memory + protective blocks) | — | ✓ |
| dm_audit_log, dm_consent_pairs, dm_requests | DM perms | consent records + transition audit | live map pruned on revoke; audit indefinite | **DECIDED** — forensic preserve (privacy_spec:111) | — | ✓ |
| member_birthdays | Birthday | **month/day only, no year** ✓ + visibility pref | until removed | add to purge batch | — | minimized by design |
| jails, warnings, tickets, ticket_participants, policy_tickets, role_grant audit | Mod actions | sanction history, mod-authored reasons (no transcripts ✓) | indefinite | **DECIDED — preserve** (canonical mod record) | — | ✓ |
| watched_users | Mod watch | watcher↔watched pairs (2 rows) | until unwatched | **NO** — add to purge (ai-moderation G3) | LAN LLM screening | covert moderation, mods-only ✓ |
| no_contact_pairs, no_contact_events | Mod safety | protective orders + attempt log (no text) | indefinite | **DECIDED** — deliberate preserve; add spec line (dmperms-nocontact G1) | — | ✓ |
| rules_events, rules_labels | Rules watch | 240-char content excerpts + matched phrase; mod-verdict training labels | indefinite | **NO** — decision: preserve as evidence + 180d sweep for dismissed events (ai-moderation G1) | LAN llama-server only | excerpt escapes storage-level dial — document |
| nsfw_classifications, nsfw_detections, nsfw_blocks | Image Guard | body-part tags (age-gated only), block records w/ author_id | indefinite (deliberate, spec'd) | **DECIDED — preserve**; revisit TTL on nsfw_detections in 6mo | — (all inference local) | observe mode ON in prod; log channels 0 |
| guess_rounds (+confession_text), guess_guesses, guess_audit_log, guess_cache/ files | Guess | intimate images on disk until solve; anon confession text + submitter id | unsolved rounds: indefinite | **UNDECIDED** — needs consent/opt-out package (review U1/G1/G2) | — (local) | consent below Whisper bar; no /guess optout |
| casino_* (bets, daily, weekly, member_stats, hands) | Casino | wagering history (behavioral) | indefinite | fold into econ_purge_user (casino review) | — | money code = repo's best |
| games_*, duels, hot_potato_*, pressure_*, quickdraw_*, mc_*, chicken_* | Games | participation, winners, nicks; anon games via anon_audit_log 90d | indefinite | **DECIDED** — anon family via audit sweep; per-user rows fold into econ_purge_user | — | ✓ |
| voice_follow, voice_follow_log | Voice follow | who-follows-whom | indefinite | **NO** — attention-report evidence; preserve explicitly w/ reaction_log | — | |
| voice_master_profiles, voice_master_trusted | Voice Master | member room prefs + trust lists | until changed | add to purge batch (modtools review) | — | |
| games_external_messages | External games | **content + embeds_json** parse buffer | indefinite, never swept | add 30d post-parse sweep (batch-bc A1) | — | 11k rows |
| intake_cards, intake_card_steps | Intake | newcomer progress, done_by greeter ids | indefinite (26 rows) | needs decision — low priority | — | intake-confessions G2 |
| greeting_watch | Greeting watch | ids+timestamp ONLY, no text (by design ✓) | verdicted rows kept (327) | needs 30d GC — low priority | — | intake-confessions G1 |
| audit_log, incident_events, role_events, role_prune_events | Mod/audit misc | actor/target ids | ? | role_events YES, others **NO** | — | |
| voice_transcription_config | Voice transcription | config only — transcripts never stored | n/a | n/a | local faster-whisper | ✓ no personal data |
| _(bundles append below)_ | | | | | | |

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

## Processors (running inventory)

- **Local/LAN only**: Marqo+NudeNet (ONNX in-process), VADER, faster-whisper,
  Ollama/llama-server (LAN, host-allowlisted) — moderation, wellness, guess.
- **Anthropic API (cloud)**: advisor (asker's question + secret-filtered admin-gated config summary — audited, fixtures review), quest-idea gen, game-prep. Remaining action: privacy-notice line. 
- Lavalink: local (127.0.0.1) ✓. **Spotify Web API (cloud)**: track queries only, no member identity; OAuth token storage → security sweep. Discord CDN: inherent.

## Known context (seeded from loose-ends audit)

- `Discord Messages/` in repo root is Billy's personal Discord export, not
  bot data — flagged for relocation in `2026-08-05-loose-ends.md` §6.
- Image Guard: web Blocked Images panel is the audit trail of record
  (verified wired 2026-08-06); Discord fan-out declined (§2 closed).

## Gap list (final, 2026-08-06 — see synthesis doc for the fix queue)

1. Erasure-path: placeholder blowout + missed tables (synthesis #1) — HIGH.
2. Disclosure vs reality: content retention ON while copy hedges (U1) — HIGH.
3. Undisclosed sensitive-attribute profiling: mod-assigned gender — HIGH policy.
4. Guess: consent/opt-out/retention package — HIGH.
5. Processors needing a privacy-notice line: Anthropic (advisor), Spotify.
6. TTL/sweep debt: rules_events (dismissed), games_external_messages,
   xp_events, bios archives, greeting_watch, guess confession_text.
7. No erasure runbook; no subject-access/export story (explicitly deferred).
8. Backups retain erased users — document retention window in runbook.

Decided & healthy: whisper, wellness, confessions, dm-perms, no-contact,
pen-pals, voice transcription, Image Guard, anon games family, sanctions,
economy ledger (preserve), birthdays (minimized).
