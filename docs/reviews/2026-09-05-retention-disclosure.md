# Retention & disclosure review — 2026-09-05

**Scope.** Does what we tell members match what we actually keep, and for how
long? Retention and disclosure only. The *erasure* half was fixed on 2026-09-02
(`cc5e2a0b`, 8 silent failures, 108k rows) and is not revisited here.

**Method.** The three disclosure surfaces read against each other and against
the live schema; every bounded retention claim checked twice — is there a sweep,
and does the oldest live row honour it. All production queries read-only.

---

## Verdict

The documents are in better shape than the headline suggests, and the
enforcement is genuinely sound. **Every one of the ten bounded retention claims
has code behind it and is honoured in live data** — no stated period is a lie.
`scripts/privacy_coverage.py` reports **zero** tables missing from the register
across 183 tables that hold a member id. That is a real result and the 08-06 /
09-02 work earned it.

The weakness is not accuracy. It is that **"indefinite" appeared in 44 of the
78 rows**, and almost none of those recorded a decision. They recorded a default
that was never revisited. The register was honest about what we do; it did not
show that anyone chose it.

> **On the number.** The brief for this review said "46 indefinite, 2 bounded".
> 46 is a whole-file grep and catches two hits outside the Retention column;
> column-scoped it is 44 of 78 data rows, of which 30 cells were the bare word
> and nothing else. (78, not 79: the table ends with a `_(bundles append
> below)_` placeholder that parses as a row but names no table.)
> The "2 bounded" counted occurrences of the string "7 days" — the real figure
> was ~12 rows stating a period. Cross-checked with the parallel
> `gdpr-disclosure-report` session, which parses the column by position and
> independently got 44 — and which also caught the 79/78 error, and two rows
> this session had left unlabelled.

Three defects follow from that, and one is a genuine inconsistency between
surfaces rather than a matter of posture.

---

## F1 — The Discord-side notice states no retention at all *(highest impact)*

The live `privacy-tools` doc (3,318 chars, updated 2026-09-02, posted to two
channels) is well written and candid about the carve-outs. It contains **no
retention statement of any kind** — no durations, no mention of the 7/30/90-day
sweeps, nothing about how long message text is held.

`manual.html` §Your Data & Privacy, by contrast, is thorough: a prominent
warning box and ~20 table rows each carrying an explicit "How long" value.

So the two member-facing surfaces give materially different pictures, and the
one most members actually read is the thinner one. A member in Discord learns
*what* is kept and never *for how long*.

This is the finding worth fixing first, and it is a wording change, not a
behaviour change.

## F2 — The `messages` note is stale, and its framing overstates the age

The register's `messages` row says *"disclosure understates this — privacy-core
U1"*. **That is no longer true.** `manual.html` leads its privacy section with
the message-text warning and marks it "Indefinitely" in the table. If anything
the manual now over-discloses (see F3). The `U1` tag should be closed.

The age figure in circulation also conflates two things. Measured today:

| | rows | with text | oldest |
|---|---|---|---|
| All guilds | 804,691 | 578,233 | 2023-12-16 |
| Main guild (`…480666`, the only one at `all`) | 616,314 | 578,233 | **2026-02-07** |

**Message *text* goes back 211 days, not 2.7 years.** The 2023-12-16 row is
metadata-only in a different guild. Both numbers are real; only the second is
about stored content.

46,823 rows carry a `deleted_at` flag — messages a member deleted in Discord
that remain readable to moderators. That *is* disclosed, plainly, in the manual.

## F3 — `manual.html` states a per-guild dial as a flat fact

The warning box reads *"On this server it is set to store full message
content."* `message_storage_level` is per-guild, and **only 1 of 8 guilds is set
to `all`**; the other seven store metadata with no text.

`manual.html` is a single static file served to every guild's dashboard, so
members of seven guilds read a notice claiming their message text is stored when
it is not. The error is in the safe direction, but it is still inaccurate, and
it makes the notice useless as a statement about *this* server.

Fix: make the sentence conditional on the guild's actual level, the way
`/delete_me`'s confirmation prompt already does (`privacy_spec.md:43` — that
prompt has been honest per-guild since 2026-08-06; the manual never caught up).

Minor: three guilds nominally at the default hold a handful of text rows
(1, 1 and 4). Almost certainly pre-dial residue; worth a look, not alarming.

## F4 — Three stores absent from the privacy section

Present in the register, absent from `manual.html` §Your Data & Privacy:

- **`voice_follow_log`** (1,966 rows) — who followed whom into a voice channel.
  This is a *social-graph* record and the section discloses the message-side
  graph explicitly; the voice side is simply missing. Most substantive of the three.
- **`intake_cards` / `intake_card_steps`** (90 rows) — onboarding progress.
- **Backups.** `gdpr_runbook.md:135` binds off-device NAS copies to **14 days**
  (`RETENTION_DAYS`). This is one of only a few genuinely bounded periods in the
  system and it appears nowhere member-facing. It is also the window in which an
  erased member's data still exists.

`processed_messages` (608,585) and `known_users` (1,357) are dedup/roster
plumbing and are reasonably out of scope for a member-facing notice.

## F5 — The coverage sweep kept a stale second copy of a list *(fixed)*

Initially read as an export gap: `privacy_coverage.py` reported
`risky_pending_questions.questioners_asked` as invisible to the access export.
It is not. It has been in `privacy_service.LIST_VALUED_MEMBER_COLUMNS` since
`ae7766b9`.

The real defect is in the sweep. `scripts/privacy_coverage.py:88` restated the
list instead of importing it:

```python
_LIST_VALUED = frozenset({"participant_user_ids", "allowed_replier_ids"})
```

Two of the nine. So the sweep filed the other seven as newly-discovered gaps —
a false positive, in the one tool whose entire job is finding what a curated
list missed. Fixed by deriving it from `LIST_VALUED_MEMBER_COLUMNS`. The sweep
now reports **0 invisible, 0 unregistered**.

Worth noting as a near-miss: acting on the first reading would have "fixed" a
non-problem by adding a duplicate entry to a list that already had it.

## F6 — Two register notes are stale in the good direction

Both said a sweep had not yet had to do anything. Both are now working:

- `games_external_messages` — note says *"11,304 rows; oldest 2026-07-07, so the
  sweep has not had to"*. Now 18,112 rows, oldest **exactly 30d**.
- `greeting_watch` — note says *"352 resolved rows still present — the GC runs
  of…"*. Now 831 rows, oldest **29d**.

---

## Enforcement check — all ten bounded claims

Sweep present in code, *and* oldest live row within the claimed period.

| Table | Claim | Oldest | Age | |
|---|---|---|---|---|
| `anon_audit_log` | 90d | 2026-07-31 | 36d | ✓ |
| `confession_threads` | 7d | 2026-08-29 | 7d | ✓ |
| `confession_pending` | 7d | *(empty)* | — | ✓ |
| `games_external_messages` | 30d | 2026-08-06 | 30d | ✓ |
| `greeting_watch` | 30d | 2026-08-06 | 29d | ✓ |
| `risky_pending_questions` | 7d | 2026-08-29 | 7d | ✓ |
| `risky_posted_questions` | 7d | 2026-08-29 | 6d | ✓ |
| `rules_events` (dismissed) | 180d | 2026-06-21 | 76d | ✓ |
| `econ_login_digest_cards` | ~1d | 2026-09-05 | 0d | ✓ |
| `guess` originals | 90d | *(constant present)* | — | ✓ |

Constants confirmed: `ORIGINAL_MAX_AGE_DAYS = 90`,
`PARSE_BUFFER_RETENTION_DAYS = 30`, `DISMISSED_RETENTION_SECONDS = 180 * 86400`,
`DEFAULT_RETENTION_DAYS = 90` (anon audit), `THREAD_METADATA_TTL_SECONDS` and
`PENDING_TTL_SECONDS` both `7 * 24 * 60 * 60`.

**Nothing overruns its stated period.** The enforcement half of this review is clean.

---

## The one lever that needs no new code

`xp_events` — **1,331,711 rows, oldest 2026-02-07 (210 days)**. Migrations
186/187 already shipped a per-guild retention dial that rolls individual events
into `xp_daily` totals after 90 days. In production:

```
xp_retention_enabled = 0     (guild 1469491362444480666; unset elsewhere)
```

The dial is built, tested, disclosed in `manual.html` (correctly conditional —
*"if this server has turned on XP event retention"*), and **off**. Turning it on
is a dashboard toggle, not a commit.

---

## Where the 46 "indefinite" rows actually sit

Not all indefinites are equal. Sorted by whether the ground is already sound:

**Defensible and already grounded** — the five Art 17(3) categories
(`econ_ledger`, sanctions, `dm_audit_log`/`dm_consent_pairs`, no-contact,
admin-authored config), plus group-game history where deleting one player's row
corrupts another's. These should be *labelled* "permanent — Art 17(3)(e)"
rather than "indefinite", which reads like an oversight.

**Genuinely open questions** — the analytics and behavioural stores, where
nothing prevents a period and none has been set:

| Table | Rows | Oldest | Age |
|---|---|---|---|
| `messages` (text) | 578,233 | 2026-02-07 | 211d |
| `reaction_log` | 252,211 | 2026-04-05 | 153d |
| `user_interactions_log` | 327,694 | 2026-02-08 | 209d |
| `xp_events` | 1,331,711 | 2026-02-07 | 210d |
| `voice_follow_log` | 1,966 | 2026-07-23 | 44d |
| `member_events` | 1,497 | 2026-04-21 | 137d |
| `usage_events` | 3,524 | 2026-07-28 | 38d |
| `ping_events` | 576 | 2026-08-31 | 5d |

These eight are where a retention decision would actually change something. The
rest of the 46 are small, self-limiting, or already bounded in practice by the
feature's own lifecycle.

---

## F7 — One register row had no retention at all *(fixed)*

`audit_log, incident_events, role_events, role_prune_events` — 18,835 live rows
across four tables holding actor and target ids — had **`?`** in the Retention
column and a blank Notes cell, with three of the four not covered by the purge.

That is the register in violation of its own contract: CLAUDE.md requires a
preserved table to name its Art 17(3) ground. Nobody had ever filled it in.

Now recorded as **permanent — Art 17(3)(e)**, on the same ground as jails and
warnings: these are the record of moderation and role actions taken, and are
what explains a decision if it is challenged. `role_events` remains purged —
it records a member's own role history rather than a decision about them.

## F8 — The Processor column cannot answer "who else sees my data" *(documented)*

Raised by the parallel `gdpr-disclosure-report` session, which joins on that
column. It is "—" for 67 of 79 rows, and **nothing in it names Anthropic** —
`/ask` keeps its transcript in the Discord message, so it owns no table and has
no row to hang a processor on. A tool reading the column alone would tell a
member nothing leaves the box.

The Processors *section* is correct and does name Anthropic; only the column is
blind. Now flagged in the register above that section so the next tool to join
on it knows.

Separately: a memory note claimed the Anthropic disclosure was "written but
unshipped since 08-05" and that `manual.html` "still says nothing". **Stale** —
§Where your data goes names Anthropic, what is sent, and what is not. Corrected.

## F9 — "Anonymous for 7 days" was a false promise *(fixed)*

Found while drafting the Discord-side wording, by checking a claim rather than
copying it across.

`manual.html` said: *"the link between a confession and its author self-destructs
after 7 days, and the anonymous-games audit trail is swept after 90 days"* — two
facts presented as separate. They are not separate. **Confessions write to
`anon_audit_log`**: 59 `confession_posted` and 164 `reply_posted` rows live in
production, every one carrying both `actor_id` and `message_id`. So the
author↔confession link is reconstructable for **90 days**, not 7.

The 7-day figure is real but belongs to `confession_threads`, the routing record
that lets the bot notify an author of replies. Losing it does not anonymise
anything.

This is the worst class of defect this review could produce — a member deciding
whether to post something sensitive, told the link dies in a week when it lives
for three months. It was pre-existing in `manual.html`, and I had copied it
verbatim into the first draft of the Discord-side text before verifying it.

Precise position, from live data: the 90-day log covers **confessions, AMA,
compliments and WYR**. **Whisper and Guess are not in it at all** and are kept
until the member clears them. Both surfaces now say exactly that.

---

## What shipped

Periods were the owner's decision, taken 2026-09-05 against the measured fact
that **no report reads further back than 90 days**
(`contributors_service.WINDOW_DAYS`; `attention_report` uses 30).

| Store | Period | Day-one effect |
|---|---|---|
| `messages.content` (+ attachments, embeds) | **365 days**, redacted not deleted | **0 rows** — oldest text was 211d |
| `user_interactions_log` | 180 days | 26,465 rows |
| `reaction_log` | 180 days | 0 rows (oldest 153d) |
| `member_events`, `voice_follow_log`, `ping_events` | 180 days | 0 rows |
| `xp_events` | 90 days, **existing dial** | 651,462 rows when switched on |

Two decisions inside that deserve recording:

**Redaction, not deletion, for messages.** The row survives; only the text goes.
Deleting rows would have removed 24,417 *metadata-only* rows from the seven
guilds that never stored a byte of text — destroying their oldest activity
history to solve a problem only the eighth guild has. It also matches the
archive's own design: content is off by default precisely because the
derivations are the durable artefact.

**Enabled by default, with a per-guild off switch** — the inverse of
`xp_retention_enabled`. That one shipped dark on 2026-08-26 and was still off
210 days of events later. A rule whose first pass costs almost nothing and then
holds the line is worth more than one nobody switches on. The stored key is
negative (`data_retention_disabled`) so an absent row means the policy *applies*
and a new guild is covered from its first day.

### Delivered

- `services/retention_service.py` — periods, the switch, both sweep arms.
- `message_store.redact_message_content_older_than` — age-bounded sibling of
  the existing `purge_guild_message_content`.
- `cogs/retention_cog.py` — daily loop, per guild, WAL checkpoint after a pass.
- Dashboard dial on **Moderation & Privacy**, beside `message_storage_level`.
- `tests/test_retention_service.py` — 31 tests: both window boundaries, both
  guild-scoping directions, the chunked-delete drain, idempotency, and a case
  that fails if both arms ever read one constant.
- Register rows rewritten; the vocabulary note; F7 and F8.
- `manual.html`: the four changed periods, the per-guild rewrite, the intake
  row, the backup paragraph.
- `docs/proposals/privacy-tools-retention-wording.md` — F1, proposed only.

### Still open

- **`xp_events` is the owner's toggle**, on Moderation &amp; Privacy → XP
  settings. 651k rows go on the first pass. Not switched on by this session.
- **20 rows remain `undecided`** — the honest backlog, down from 44.
  Mostly small or self-limiting stores; none is a `messages`-scale question.
- **F1** needs applying from the Docs panel and re-posting to both channels.
