# Are the reviews right, and are the docs current? — 2026-08-06

A meta-audit of `docs/reviews/` and `docs/INDEX.md`. Six weeks of review
documents have accumulated and a lot of decisions are recorded in them;
nobody had checked whether they were still true.

**Method.** Every "fixed / shipped / resolved / decided" claim was checked
against the **code**, never against another doc. Every still-open item on the
synthesis fix queue, the loose-ends doc and the GDPR register was checked the
same way. Load-bearing numeric claims were re-measured against a read-only
prod snapshot taken with the sqlite3 backup API (never `cp`), and the economy
claims were re-run through the repo's own `scripts/economy_tuning_report.py`
rather than an ad-hoc query.

**Headline.** The reviews' *substance* is in good shape — every closed finding
I checked was genuinely closed, and both load-bearing numeric claims I could
independently re-measure came back correct. The problem is entirely
**bookkeeping**: ten fix-queue items that shipped still read as open work, in
three different documents. That is the failure mode this lane was created to
hunt, and it had already cost one session a near-re-implementation.

Doc corrections have been applied in this branch (see "Fixes applied").

---

## High

### H1. Ten shipped fix-queue items still read as open, in three documents

**Severity: High** — this is work someone will redo. It already nearly
happened once: a session started re-implementing a synthesis item before
noticing it had shipped. The cost is a whole wasted session per recurrence,
and the risk is a *second* implementation of a privacy-critical path.

**Evidence (verified by reading the code, 2026-08-06).** The synthesis doc
recorded the shipped status only in a header parenthetical
(`2026-08-06-review-synthesis.md:11-13`, added by `5d295017`) while every
numbered entry below it still read as open work. Two items were not even in
the header. The GDPR register's `Purge?` column and gap list carried the same
staleness independently.

| Item | Recorded as | Actually |
|---|---|---|
| Synthesis #1 erasure hardening | open | `6ac71558` — `privacy_service.py:14` `_ID_CHUNK=500`, `:77` chunked delete, `:120-125` the missed tables, `:156-158` pair tables both sides, `:201` → `economy_service.py:1304` `econ_purge_user` |
| Synthesis #2 whisper cross-guild forget-me | open | `2b3094aa` |
| Synthesis #3 `/delete_me` disclosure | open | `1dfa8fea` — `privacy/logic.py:188,218`, `privacy_cog.py:482,554` |
| Synthesis #4 guess consent package | open | `775d903d` — `guess_cog.py:1603` consent copy, `:1642` consent view, `:2032` `/guess optout` |
| Synthesis #5 aiohttp CVEs | open | `6639dbc0` — both locks pin `aiohttp==3.14.3` |
| Synthesis #8 perk-renewal price DM | open | `49a02867` |
| Synthesis #10 `rules_events` 180d sweep | open | `49a02867` — `rules_watch/ledger.py:442` |
| Synthesis #11 `games_external_messages` 30d sweep | open | `49a02867` — `games_external/logic.py:231` |
| Synthesis #12 bios archive TTL | open (**not in the header at all**) | `9374c306` — migration `149_bios_archived_at.sql`, applied in prod |
| Synthesis #13 low/housekeeping | open (**not in the header at all**) | `49a02867` + `bf02ce1f` |

The register repeated the same staleness in rows for `econ_*`,
`xp_reaction_awards`, `bios`, `member_birthdays`, `watched_users`,
`voice_master_*`, `games_external_messages`, `greeting_watch`, `rules_events`
and `casino_*`, plus gap-list entries 1, 2, 3, 4, 6 and 7.

**Fix (applied).** Every item struck through **in place** with its commit
hash, in the synthesis doc, the register table and the register gap list. A
note at the top of the synthesis fix queue makes the convention explicit:
strike the item where it lives, not in a header. Same convention added to the
register's gap list.

---

## Medium

### M2. INDEX.md contradicts the novel-hunt review on four S1 security findings

**Severity: Medium** — INDEX is the doc people are told to read *first*, and
it said two cross-guild/authz S1s were still open. A reader trusting it would
either redo fixed work or, worse, believe live security holes exist.

**Evidence (read the code).** `docs/INDEX.md:168` said "4 new S1s; 2 fixed, 2
open". `reviews/2026-07-23-novel-hunt.md:61` says "All four S1s are now
fixed." The code agrees with the review, not with INDEX — all four verified:

- `PUT /config/confessions` / `/config/whisper` — now `require_perms({"admin"})`
  (`routes/config.py:3850-3856`, `:4585-4591`), each with a comment naming the
  de-anonymisation risk.
- `GET /moderation/transcript` — binds and passes the guild id
  (`routes/moderation.py:1086,1091`).
- Docs placement cross-guild post — the guard moved into the resolver,
  `bot_modules/docs/sync.py:94` `if channel.guild.id != guild.id`.
- `GET /config/channels` — emits `str(r[0])` with a comment on the 2^53
  rounding (`routes/games.py:1301-1304`).

**Fix (applied).** INDEX row corrected, with the four code citations.

### M3. The 2026-08 review corpus is invisible from INDEX.md

**Severity: Medium** — 25 review docs plus the synthesis, the GDPR register
and the loose-ends audit are unlisted in INDEX's Audits table, which stops at
2026-07-30. A future session told to "read INDEX.md first" would not discover
the fix queue at all — the exact discovery failure behind H1.

**Evidence.** Script-checked: 42 `docs/**/*.md` files are unlinked from
INDEX.md, of which 25 are the `reviews/2026-08-0*` corpus. (The other 17 are
`plans/` docs — see L6.)

**Fix (applied).** Added a "start here" callout pointing at the synthesis doc
and explaining the strike-through convention, plus rows for the synthesis,
the GDPR register, the loose-ends audit, the retune proposal and this
document. The 25 per-bundle docs are deliberately summarized as a group
rather than listed row-by-row — they are evidence for the synthesis, not
entry points.

### M4. Ten headline findings in the 07-22 deep review were quietly fixed

**Severity: Medium** — a 61k-line report presenting fixed work as open. Lower
than H1 only because it is a dated snapshot rather than a live queue.

**Evidence (all read in code, 2026-08-06).** Full table is now in the doc's
own status header. Highlights: the S1 Truth-or-Dare NSFW gate now exists
(`games_traditional_cog.py:170,258`); the S1 snowflake `parseInt` is gone
(`economy-bank-manager.js:263`); frontend lint is now blocking
(`.github/workflows/test.yml:46`); `gate.py:130` now matches bare
`logic.py`/`store.py`; the QA Tracker rows are keyboard-reachable
(`qa-tracker.js:172,239`); the merge-conflict markers are out of
`economy_spec.md`; the decorative game-enable toggle is enforced.

Spot-checked and **still genuinely open**: `role="tab"` / `makeTabStrip`
(neither string exists anywhere under `static/js/`) and `/logout` as a
CSRF-able plain GET (`routes/oauth.py:311`).

**Fix (applied).** Dated status header at the top of the 07-22 review listing
what was re-verified and, explicitly, that everything else in the body was
*not* re-checked and should be treated as unknown rather than open.

### M5. Two dev-session loose ends were resolved; one command is still outstanding

**Severity: Medium** — the doc's "still unmerged, units correctly off" reads
as a pending ship decision, when the real remaining action is a single
`loginctl` command that the feature does not work without.

**Evidence.** `git log` shows the branch merged as `81035491` (`25ac6e32`).
Verified with `systemctl --user is-enabled`: `dk-snapshot.timer` **enabled**,
`dk-restore.service` disabled (correct — it is oneshot-on-demand).
`loginctl show-user ben -p Linger` returns **`Linger=no`**, so the user
manager stops with the last session and the snapshot timer does not run
across the reboot the feature exists to survive.

Likewise loose-ends §7: four of the five branches merged
(`78053cf4`, `cdb73a17`, `011c6a2f`, and the testing-cards worktree never had
a branch); only quest-ideas remains, confirmed via `git branch --no-merged main`.

**Fix (applied) + remaining owner action.** Both sections rewritten with the
verified state. **BEN: run `loginctl enable-linger ben`.**

### M6. INDEX and event_echo_spec call Pools and bounties dormant; both are live

**Severity: Medium** — a Reference-table entry stating that two features
cannot fire when they demonstrably do.

**Evidence (read-only prod snapshot).** `config` holds
`casino_pools_enabled='1'` with a channel set; `casino_pools_rounds` has 9
rounds from 2026-07-28 to 2026-08-05 and `casino_pools_bets` has 37 bets.
`econ_bounty_channel_id` is a real channel on the main guild and
`econ_bounties` has 5 rows. `event_echo_log` grouped by source returns
`pools_closing` 1, `bounty` 1, `party_game` 1 — both "dormant" sources have
actually fired. Only the raffle is still dormant (2 draws, **0 tickets**).

The spec's bullet (`event_echo_spec.md:331`) is honestly dated "as of
2026-07-28", so the spec is stale rather than wrong; the INDEX row stated it
flatly with no date, which is the worse form.

**Fix (applied).** INDEX row corrected; a dated correction paragraph added
under the spec's own bullet rather than editing the historical observation.

---

## Low

### L7. `docs/plans/` is 50% unlisted in INDEX.md

**Severity: Low** — plans carry their own dated status headers and INDEX says
to trust those over its table, so the cost is discoverability rather than
wrong information. Worth knowing that the table is a sample, not an index.

**Evidence.** 17 plan docs are unlinked, including
`plans/staged-review-2026-08.md` — the plan the synthesis doc closes — plus
`plans/website-ux-cleanup.md`, `plans/test-suite-slim-and-remote-resilience.md`
(cited by name in CLAUDE.md), `plans/casino.md`, `plans/intake-cards.md`,
`plans/promotion-review-cards.md` and eleven others.

**Fix: not applied.** Adding 17 rows is a judgment call about what the table
is *for*, and the table's framing ("Stage-by-stage build plans") may be
deliberately selective. Flagging for Ben rather than deciding it here.

### L8. manual.html never learned about the perk price-change DM

**Severity: Low** — a member-facing DM shipped without the user-facing
manual entry the per-commit contract asks for. Small, but exactly the drift
CLAUDE.md warns lags behind `docs/`.

**Evidence.** `49a02867` added a renewal DM on a price change and touched
`docs/economy_spec.md` but not `manual.html`. The manual's Perk Shop section
had a callout for the *unpayable* renewal (`manual.html:966-972`) and nothing
for the repriced one, while the body text says perks bill "at the server's
current price" — a member would not know they get told.

**Fix (applied).** Added an "If the price changes" callout beside the
existing grace-window one.

---

## Info — claims I re-measured and found correct

Recording these so they are not re-derived. All from a read-only snapshot
(sqlite3 backup API) taken 2026-08-06.

| Claim | Source | Measured |
|---|---|---|
| Float still growing ~+5,000/day | loose-ends §1 | **Confirmed** — `economy_tuning_report.py --days 5` gives a Pools line of **+4,993.5/day**; last five days +4,275 / +4,993 / +2,708 / +4,997 / +3,905. Median balance 194 (doc said 206 at the time; still above the 186 floor the retune had to protect) |
| prod runs `message_storage_level='all'`, 452k of 631k rows have text | register row 2 | **Confirmed**, now 455,136 of 635,625 |
| `xp_events` ~1M rows, largest table | dbperf P1 / synthesis #9 | **Confirmed** — 1,022,853, next is `messages` at 635,625 |
| `xp_reaction_awards` 40k rows | register row 3 | **Confirmed** — 42,518 |
| `member_gender` 377 tagged by 3 mods | register / health G1 | **Confirmed exactly** — 377 rows, 3 distinct setters |
| `anon_audit_log` 90d sweep working, 7 rows | register | **Confirmed** — still 7 |
| `games_external_messages` 11k rows | register | **Confirmed** — 11,304; oldest `collected_at` 2026-07-07, so the new 30d sweep has not had to bite yet. The `collected_at` TEXT format (`YYYY-MM-DD HH:MM:SS`) does compare correctly against `datetime('now', '-30 days')` — the sweep is not a silent no-op |
| Wellness "no code path provisions `role_id`/`channel_id`" | INDEX caveat | **Confirmed still true** — `upsert_wellness_config` accepts both (`wellness_service.py:745-746`) but no route or scheduler passes either; the only web caller sets `default_enforcement` (`wellness_routes/admin.py:122`) |
| `survey_spec.md` is "Zero code" | INDEX Design table | **Confirmed** — no cog, no launcher; the three `survey` hits in `src/` are unrelated word usage |
| `todo_spec.md`'s removed context menu | INDEX note | **Confirmed** — no `context_menu` in `todo_cog.py` |
| Image Guard: web panel is the audit trail | loose-ends §2 | **Confirmed and strengthened** — `nsfw_blocks` now holds **3 real gate removals** (it was empty when §2 was written, which was the basis of the original "flying blind" finding), and 82 classifications |
| `game_host` payouts flowing | loose-ends §3 | **Confirmed** — now 52 payouts / 7,450 total (was 51 / 7,270) |

### One methodology note worth carrying

My first pass at the float claim summed `econ_ledger` directly and got
**+20,000/day**, which looked like a 4× contradiction of loose-ends §1. It
was not: the tuning report deliberately nets casino money (a stake is not a
burn, a payout is not income) and excludes non-faucet kinds, and running
`scripts/economy_tuning_report.py` reproduced the doc's number to the digit.
**When a review cites a number a script produced, re-run the script — an
ad-hoc query against the same table is a different metric, not a check.**

---

## Nothing found in

- **Command-surface drift.** Enumerated every `app_commands` command and group
  in `src/bot_modules` and diffed against `manual.html`. No orphans: the
  apparent top-level `/ama`, `/wyr`, `/price` etc. are re-parented under
  `/games play` in each cog's `setup()` (e.g. `games_ama_cog.py:1620`
  `play.add_command(cog.ama)`), and the apparent gaps `/voice trusted remove`,
  `/voice blocked list`, `/voice profile reset` are documented under their
  group headings (`manual.html:1071-1074`).
- **INDEX Reference/Design/Aspirational labels.** Beyond M6, the labels I
  checked hold — including the two most at risk of rotting (`survey_spec.md`
  as Design-with-zero-code, and the wellness provisioning caveat), both
  re-verified above.
- **Contradictions between reviews.** Only one found (M2, INDEX vs
  novel-hunt); the per-bundle 2026-08-05 docs agree with each other and with
  the synthesis.
- **Owner decisions.** None were changed. The two recorded decisions
  (mod-assigned gender accepted as internal metrics; Image Guard Discord
  fan-out declined) are left exactly as written; the register's Image Guard
  note is only annotated with the fact that the panel has since recorded real
  blocks, which supports the decision rather than reopening it.
- **The per-commit docs contract, broadly.** Sampled 30 recent
  behavior-changing commits touching cogs / routes / panels. All but one
  updated a spec, and manual.html rode along wherever member-facing copy
  changed. The single gap is L8. `98395fdb` (Cat Bot tier dials) touched no
  manual.html, but the Income Sources panel is documented generically at
  `manual.html:1047` — that reads as adequate, not as a miss.

---

## Fixes applied in this branch

| File | Change |
|---|---|
| `reviews/2026-08-06-review-synthesis.md` | All 10 shipped items struck through in place with commit hashes; item 7 marked as the last open High with its grep evidence; item 9 annotated with a fresh row count; loose-ends section rewritten against verified state; convention note added |
| `data_register.md` | 10 table rows and 6 gap-list entries updated to shipped, each citing the commit and `file:line`; guess row split into what shipped and what is still open |
| `reviews/2026-08-05-loose-ends.md` | §1 re-verified with fresh numbers; §4 rewritten (merged, timer enabled, linger outstanding); §7 rewritten (4 of 5 merged) |
| `docs/INDEX.md` | novel-hunt row corrected (4/4 S1s fixed); event_echo row corrected; Audits table gains a "start here" callout and rows for the 2026-08 corpus |
| `docs/event_echo_spec.md` | Dated correction under the "three sources are dormant" bullet |
| `reviews/2026-07-22-deep-review.md` | Dated status header: 11 findings re-verified as fixed, 2 confirmed still open, rest explicitly marked un-rechecked |
| `src/web_server/static/manual.html` | "If the price changes" callout in the Perk Shop section (L8) |

## Left for Ben

1. **`loginctl enable-linger ben`** — the dev-session snapshot timer does not
   survive a reboot without it (M5).
2. **Synthesis #7, the transparency package** — a privacy-notice line naming
   Anthropic (advisor questions) and Spotify (track queries). The last
   unshipped High-tier item; neither name appears in any user-facing surface.
3. **Whether `docs/plans/` should be listed exhaustively in INDEX.md** (L7).
4. **quest-ideas branch** — ship or reap; the last unmerged session branch.
