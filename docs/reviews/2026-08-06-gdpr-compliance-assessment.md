# GDPR compliance assessment — 2026-08-06

A pass over the bot's data-protection posture, article by article, with the
gaps that were closed the same night and the ones that need a decision or a
lawyer. Follows the docs-accuracy audit
([2026-08-06-review-docs-accuracy-audit.md](2026-08-06-review-docs-accuracy-audit.md))
and reads the 2026-08-05 staged review's GDPR pass forward into "is this
actually compliant", which the register never claimed to answer.

**Not legal advice.** This is an engineer reading the regulation against the
code. Two questions below need a real lawyer and are flagged as such.

---

## 0. The threshold question — does GDPR apply here at all?

**Unanswered, and it determines how much of the rest matters.**

The operator appears to be US-based (the economy tuning report hardcodes a
`-7` guild offset). GDPR reaches a non-EU controller only through Art 3(2):

- **Art 3(2)(a), offering goods or services to people in the Union** — probably
  not met. A Discord community is not "targeting" the EU in the Recital 23
  sense; there is no currency, language or marketing directed there.
- **Art 3(2)(b), monitoring behaviour that takes place in the Union** — this is
  the live hook, and it is the bot's own feature set that creates it. Sentiment
  scored on every message, a who-talks-to-whom interaction graph, attention
  reports, per-member quality scoring, activity profiling, inactivity
  prediction. Recital 24 describes exactly this: tracking on the internet,
  including profiling to analyse or predict preferences and behaviour. If any
  member is in the EU or UK, this is arguable.

The **household exemption** (Art 2(2)(c)) is not a way out: the CJEU reads it
narrowly (*Ryneš*), and a public community server with hundreds of members is
not purely personal activity.

**Recommendation: get this answered before spending more effort.** If the
answer is "out of scope", everything below becomes optional good practice and
the work already shipped is still worth having. If it is "in scope", §4's
remaining items become obligations with deadlines attached.

---

## 1. What was already strong

Verified by reading the code, not from the register:

- **Erasure genuinely works.** `purge_user_data` covers ~60 tables, chunks the
  id list at 500 so a heavy poster cannot blow SQLite's variable cap, runs in
  one transaction the caller owns, and tolerates schema drift per table.
- **Minimisation is a real design rule, not a slogan.** `message_storage_level`
  defaults to `none`; birthdays store day and month with **no year**;
  `greeting_watch` stores ids and timestamps with no text; Pen Pals stores
  pairing metadata and never letter content; voice transcription stores no
  transcripts at all.
- **Inference is local by default.** Marqo, NudeNet, VADER, faster-whisper and
  Ollama all run on-box or LAN-allowlisted; `ai_moderation_spec.md` refuses any
  endpoint it cannot *prove* is local rather than trusting configuration.
- **Confessions' 7-day de-anonymisation TTL** is the best pattern in the repo —
  time-limited linkability instead of permanent, and the model the anonymous
  games audit copied.
- **Purpose-built retention sweeps exist** and were verified running:
  `anon_audit_log` 90d, `rules_events` dismissed-180d, `games_external_messages`
  30d, `greeting_watch` 30d, bios archive 12mo.

## 2. What shipped tonight

| Gap | Article | Shipped |
|---|---|---|
| No subject-access or portability path at all | 15, 20 | `export_user_data` + `scripts/export_user_data.py` (`0a57c581`) |
| No privacy notice anywhere | 13 | manual.html § *Your Data & Privacy* (`bd017757`) |
| Consent taken but not evidenced | 7(1), 7(3) | `guess_consents`, migration 154 (`b16fcf08`) |
| Register was a dated audit artifact, already stale | 30 | promoted to `docs/data_register.md` (`bd017757`) |
| Preserved categories justified in engineering terms only | 17(3) | ground named per category in the register + runbook (`bd017757`) |
| No breach procedure | 33, 34 | `gdpr_runbook.md` §3 with a breach register (`0a57c581`) |
| `SESSION_SECRET` unvalidated | 32 | boot-time strength gate (`0a57c581`) |

### Notes on two of them

**The access export is a superset of the erasure path, deliberately.** Art 17(3)
exempts categories from *deletion*, not from *disclosure* — the ledger, sanction
history and consent audit are all exported even though they are never purged.
Getting this backwards is the obvious failure mode and there is a test pinning
the direction (`test_export_covers_every_table_the_purge_deletes`), which caught
a real miss (`econ_msg_replies`) on its first run.

**The export finds tables by column discovery, not a curated list.** Building
the column set against a read-only prod snapshot surfaced ~30 member columns a
hand-written list would have missed — including `reactor_id` (185k rows) and
`confession_threads.original_author_id`, which is de-anonymising. A curated list
is precisely the thing that goes stale, and a stale access export is an
incomplete answer to a statutory request.

## 3. Special-category data (Art 9) — the judgement call

**Needs a lawyer, not an engineer.** Art 9 covers, among other things, data
concerning a person's **sex life or sexual orientation**. This deployment holds:

- **Guess** — intimate/NSFW images of identifiable members cached on disk, plus
  confession text bound to a user id.
- **The anonymous games family** — Fantasies & Dealbreakers, NSFW Truth or
  Dare, anonymous AMA — all de-anonymisable through `anon_audit_log`.
- **DM permission consent pairs**, which record who agreed to talk to whom.

If that is Art 9 data, processing needs *explicit* consent under 9(2)(a) — a
higher bar than ordinary consent. The good news is that the shape is now right:
a disclosure the member reads, a role granted only on an affirmative click, and
since tonight a stored record of when they consented and to which wording. That
is close to what 9(2)(a) asks for. What a lawyer needs to rule on is whether the
disclosure *content* is specific enough.

**On `member_gender`, for accuracy:** gender identity is **not** enumerated in
Art 9, so this is probably *not* special-category data, and the owner decision
of 2026-08-06 to keep it as an internal metric is more defensible than the
original finding implied. The real exposure was never sensitivity — it was that
377 people were classified by three moderators with no notice and no route to
correct it (Art 16). The privacy notice now names it and points at the fix, so
the remaining risk is small.

## 4. What is still open

| # | Item | Article | Severity | Note |
|---|---|---|---|---|
| 1 | **Art 3 scope question** | 3 | **Blocking everything else** | See §0. One lawyer-hour. |
| 2 | **Art 9 classification** of Guess / anonymous games | 9 | High if in scope | See §3. Same lawyer-hour. |
| 3 | **No processor contracts recorded** | 28(3) | Medium | Anthropic receives member-authored question text; Spotify receives track queries. Both publish DPAs; nothing records whether they were accepted. This is paperwork, not engineering. |
| 4 | **No age assurance beyond Discord's** | 8 | Medium | NSFW gates on `channel.is_nsfw()` — a channel flag on a 13+ platform, not age verification, while Guess collects intimate imagery. More a safeguarding concern than a compliance one; they point the same way. |
| 5 | **`reaction_log` / `voice_follow_log` preserve** | 17(3) | Low–Medium | The one preserved category without a clean statutory ground — internal analytics. Documented as weak in the register rather than papered over. Revisit if a request is contested. |
| 6 | **Backups retain erased users** | 17 | Low | Erased on rotation, not on request. An accepted position *provided the window is stated* — the runbook now states it. |
| 7 | **`xp_events` has no retention** | 5(1)(e) | Low | 1,022,853 rows, the largest table, kept forever. Deferred with a design note: deletion needs a rollup the leaderboards union. Storage limitation says data shouldn't be kept longer than necessary for the purpose — "all-time leaderboards" is a real purpose, so this is defensible but worth revisiting. |
| 8 | **List-valued member columns** invisible to the export | 15 | Low | Six columns store ids as JSON/CSV. The export names them and the runbook says to grep by hand. A gap that is disclosed is not the same as one that is hidden. |
| 9 | **No DPIA** | 35 | Low if out of scope | Large-scale profiling + special-category data would normally trigger one. Follows entirely from §0 and §3. |

## 5. What I would not bother with

Recording these so they are not re-raised:

- **A cookie banner.** The dashboard sets one strictly-necessary session cookie
  and no analytics or tracking cookies. ePrivacy consent is not required for
  strictly-necessary cookies.
- **A DPO.** Art 37 triggers do not plausibly apply to a hobby server at this
  scale.
- **Automated-decision-making safeguards (Art 22).** Rules Watch raises alerts
  to a human, promotion review produces cards a mod clicks, and the inactivity
  sweep was verified **not** to auto-kick. Nothing here decides anything about
  a member without a human in the loop. Worth re-checking if any of those ever
  gets an auto-action.

## 6. Verification method

- Every claim about the code was read in the source; no claim was carried over
  from the register or a prior review without re-reading.
- Prod facts came from a read-only snapshot taken with the sqlite3 backup API
  (never `cp`), queried read-only.
- The export's coverage was checked against the live schema — 166 tables
  reference a member — and smoke-tested against the snapshot for one real
  member: 78,940 rows across 60 tables, which is what a SAR answer here
  actually looks like.
- `SESSION_SECRET`'s live length was checked (43 chars, 33 distinct) **before**
  adding a boot gate that could otherwise have blocked a restart. Its value was
  never printed.
