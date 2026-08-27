# Staged review synthesis — 2026-08-06

Closes the 2026-08-05 staged review (plan:
`docs/plans/staged-review-2026-08.md`). 21 feature bundles + 6 horizontal
sweeps + loose-ends audit; per-bundle findings in the sibling
`2026-08-05-*` / `2026-08-06-sweep-*` docs; data decisions in
`docs/data_register.md`.

## Fix queue (deduped, priority order)

### High — code — ALL SHIPPED 2026-08-06

> Each item below is struck through with the commit that closed it, and every
> one was re-verified against the code on 2026-08-06 (see
> `2026-08-06-review-docs-accuracy-audit.md`). Strike an item **here**, in
> place, when it lands — a shipped-status note in this header alone was read
> as "still open" and nearly caused a re-implementation.

1. ~~**Erasure-path hardening** (one work package, privacy-core A1/A2/A3 +
   gap rollup): chunk the `IN (…)` id list, one transaction, uniform
   schema tolerance, WAL-checkpoint note in a new erasure runbook; add
   the missed tables — `xp_reaction_awards`, `watched_users`, bios×3,
   `invite_edges` (both directions), voice-master prefs,
   `member_birthdays` — and a new `econ_purge_user` helper (economy +
   casino per-member state; ledger deliberately preserved). Ship with a
   >32k-message failing test first.~~ — **shipped `6ac71558`**
   (`privacy_service.py:14,77,120-125,156-158,201`,
   `economy_service.py:1304`, `docs/gdpr_runbook.md`).
2. ~~**Whisper cross-guild forget-me over-delete** (whisper A1) + two-guild
   repro test; tidy D1 spec drift and dead `decrement_guesses_left` in
   the same commit.~~ — **shipped `2b3094aa`**.
3. ~~**`/delete_me` disclosure truth** (privacy U1): branch the confirm
   prompt on `guild_retains_content` — prod stores full text for 452k
   messages while the copy says "mostly metadata".~~ — **shipped
   `1dfa8fea`** (`privacy/logic.py:188,218`, `privacy_cog.py:482,554`).
   Prod re-checked 2026-08-06: `message_storage_level='all'`, 455,136 of
   635,625 message rows carry text.
4. ~~**Guess consent package** (image-guard-guess U1/G1/G2): consent view
   with retention disclosure, `/guess optout`, 90-day age-out for
   unsolved originals~~ — **shipped `775d903d`** (`guess_cog.py:1603,1642,
   2032`). **Still open:** the guess confession-text TTL was only ever a
   "consider", and no `guess_*` table is in `purge_user_data` — see the
   register's row 31.
5. ~~**aiohttp 3.14.1 → 3.14.3** (security D1): three published CVEs in
   discord.py's HTTP layer; bump + recompile locks, CI proves it.~~ —
   **shipped `6639dbc0`** (both locks pin `aiohttp==3.14.3`).

### High — policy (Ben decides, then a small commit documents)

6. ~~Mod-assigned gender~~ — **owner decision 2026-08-06: accepted as
   internal metrics, no change.** (Register row updated.)
7. **Transparency package**: privacy-notice line naming Anthropic
   (advisor questions) + Spotify (track queries). **Still open, verified
   2026-08-06** — neither name appears in `manual.html`,
   `docs/privacy_spec.md`, or `src/bot_modules/privacy/`. This is the
   only unshipped item left in the High tier. Image Guard log
   channels: ~~wire them~~ — resolved; the web Blocked Images panel is
   the trail of record (loose-ends §2 corrected), Discord fan-out
   declined. The panel has since proved itself: `nsfw_blocks` holds 3
   real gate removals (was empty when §2 was written), 82 classifications.

### Medium

8. ~~Perk-renewal notice DM, at least on price change (economy A2).~~ —
   **shipped `49a02867`** (`BillingResult.previous_price`).
9. ~~`xp_events` 90-day retention + rollup (dbperf P1 — 1M rows, largest
   table).~~ — **shipped 2026-08-26**, four stages on branch
   `xp-events-retention`, plan in
   `docs/plans/xp-events-retention-and-rollup.md`. An `xp_daily` rollup
   the all-time readers union, so pruning changes what is stored and not
   what anyone sees. The deferral's "leaderboards by source" undercounted:
   there were **seven** readers reaching past any usable window, including
   the inactive report's unfiltered `MAX(created_at)` and — found while
   building, not while designing — the XP hour-of-day histogram, which has
   no time filter at all and which a *daily* rollup cannot answer, so it
   was windowed to the horizon instead. Deletion **ships off** behind a
   per-guild dial and four fail-closed guards, with
   `scripts/verify_xp_retention.py` to re-prove the no-op on a prod
   snapshot before it is turned on.
10. ~~`rules_events`: dismissed-event 180d sweep + spec preserve lines
    (ai-mod G1/G2).~~ — **shipped `49a02867`**
    (`rules_watch/ledger.py:442` `purge_old_dismissed_events`).
11. ~~`games_external_messages` post-parse 30d sweep (batch-bc A1).~~ —
    **shipped `49a02867`** (`games_external/logic.py:231`
    `sweep_old_buffer_rows`).
12. ~~Bios archived-on-leave 12-month TTL decision (penpals-bios G2).~~ —
    **decided + shipped `9374c306`** (migration `149_bios_archived_at.sql`,
    applied in prod).

### Low / housekeeping

13. ~~Dead structure: `games_consent/` dir now; greeting_watch 30d GC;
    quotes-are-audited line in manual.html; journald line in
    DEPLOYMENT.md; keep ollama prompt-log debug-only (comment).~~ —
    **shipped `49a02867` (dir + GC) and `bf02ce1f` (the four doc/comment
    touches)**. **Still open:** the orphan `games_consent` table + guess
    reuse columns still want dropping in the next games migration, and
    the intake purge decision is still unmade.

### Loose ends still awaiting Ben (from `2026-08-05-loose-ends.md`)

Re-checked 2026-08-06:

- **Economy retune round 2** — still awaiting sign-off, and the number
  still holds: the Pools line reads **+4,993/day** on a fresh read-only
  snapshot (`economy_tuning_report.py --days 5`), unchanged from the
  figure loose-ends §1 quoted. ~~cat_catch needs to become a dial~~ —
  done in `98395fdb`; the round-2 proposal is
  `2026-08-06-economy-retune-round2-proposal.md`, **not applied**.
- ~~Snapshot/restore branch ship-or-reap (+3 more branches)~~ — the
  snapshot/restore branch **merged** as `81035491`, and three of the four
  other branches merged too (jail `cdb73a17`, pen-pals `011c6a2f`,
  confessions `78053cf4`). Only the quest-ideas branch is still unmerged.
- ~~**Rollback SQL + `Discord Messages/` relocation**~~ — **done
  2026-08-06.** Both moved out of the production checkout root to
  `/home/ben/discord-bots/archive/` (`db-rollbacks/` for the four `.sql`
  files, `discord-export-billy/` for the 100 MB export), so the root is
  clean and `git status` is empty. Moved rather than deleted: the
  rollback scripts are still the only undo path for the July economy
  retune, which is why they were kept on disk in the first place.

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
