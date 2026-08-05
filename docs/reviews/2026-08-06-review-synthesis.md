# Staged review synthesis — 2026-08-06

Closes the 2026-08-05 staged review (plan:
`docs/plans/staged-review-2026-08.md`). 21 feature bundles + 6 horizontal
sweeps + loose-ends audit; per-bundle findings in the sibling
`2026-08-05-*` / `2026-08-06-sweep-*` docs; data decisions in
`2026-08-05-gdpr-register.md`.

## Fix queue (deduped, priority order)

### High — code

1. **Erasure-path hardening** (one work package, privacy-core A1/A2/A3 +
   gap rollup): chunk the `IN (…)` id list, one transaction, uniform
   schema tolerance, WAL-checkpoint note in a new erasure runbook; add
   the missed tables — `xp_reaction_awards`, `watched_users`, bios×3,
   `invite_edges` (both directions), voice-master prefs,
   `member_birthdays` — and a new `econ_purge_user` helper (economy +
   casino per-member state; ledger deliberately preserved). Ship with a
   >32k-message failing test first.
2. **Whisper cross-guild forget-me over-delete** (whisper A1) + two-guild
   repro test; tidy D1 spec drift and dead `decrement_guesses_left` in
   the same commit.
3. **`/delete_me` disclosure truth** (privacy U1): branch the confirm
   prompt on `guild_retains_content` — prod stores full text for 452k
   messages while the copy says "mostly metadata".
4. **Guess consent package** (image-guard-guess U1/G1/G2): consent view
   with retention disclosure, `/guess optout`, 90-day age-out for
   unsolved originals; consider confession-text TTL (pattern exists in
   confessions' 7-day purge).
5. **aiohttp 3.14.1 → 3.14.3** (security D1): three published CVEs in
   discord.py's HTTP layer; bump + recompile locks, CI proves it.

### High — policy (Ben decides, then a small commit documents)

6. **Mod-assigned gender** (health G1): self-declared roles / transparency
   line / drop the dimension — pick one; 377 members are tagged today.
7. **Transparency package**: privacy-notice line naming Anthropic
   (advisor questions) + Spotify (track queries); wire the two Image
   Guard log channels (loose-ends §2 — enforcement currently trail-less).

### Medium

8. Perk-renewal notice DM, at least on price change (economy A2).
9. `xp_events` 90-day retention + rollup (dbperf P1 — 1M rows, largest
   table).
10. `rules_events`: dismissed-event 180d sweep + spec preserve lines
    (ai-mod G1/G2).
11. `games_external_messages` post-parse 30d sweep (batch-bc A1).
12. Bios archived-on-leave 12-month TTL decision (penpals-bios G2).

### Low / housekeeping

13. Dead structure: `games_consent/` dir now; orphan table + guess reuse
    columns in the next games migration. greeting_watch 30d GC; intake
    purge decision; quotes-are-audited line in manual.html; journald
    line in DEPLOYMENT.md; keep ollama prompt-log debug-only (comment).

### Loose ends still awaiting Ben (from `2026-08-05-loose-ends.md`)

Economy retune round 2 (float still +5k/day; cat_catch needs to become a
dial), snapshot/restore branch ship-or-reap (+3 more branches), rollback
SQL + `Discord Messages/` relocation out of the repo root.

## House patterns (cite these in future reviews)

Whisper's consent ceremony + forget-me · Confessions' 7-day
deanonymization TTL · Wellness' wired GC + session-derived route identity
· attention_report's flag-not-verdict restraint · Casino's
cap-debit-rollback money discipline · the avatar fetcher's per-hop SSRF
defense · ollama's host allowlist · no-contact's identical-confirmation
no-tell property · Image Guard's three-valued verdict.

## Standing exceptions (stop re-flagging)

Guess/fantasies channel placement is mod-policed by owner decision
(2026-07-27); no-contact's Discord commands are the documented
member-self-service exception to config-on-web.

## Docs commit assessment

No INDEX.md misclassifications found (all specs checked matched their
flavor). manual.html changes ride their code fixes (U1, quotes note,
guess package) per the working agreement — no standalone docs commit
needed.
