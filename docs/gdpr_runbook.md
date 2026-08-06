# GDPR operator runbook

Procedures for the three things that arrive with a clock attached: a **subject
access request** (Art 15/20, one month), an **erasure request** (Art 17, one
month), and a **personal-data breach** (Art 33, seventy-two hours).

The bot deliberately has **no command** for any of them — `/delete_me` and
`/delete_user` clear Discord messages only (see
[privacy_spec.md](privacy_spec.md)). Everything here is an operator running a
script against the database.

> **Scope caveat, read once.** Whether GDPR binds this deployment at all is a
> legal question that has not been answered — see
> [reviews/2026-08-06-gdpr-compliance-assessment.md](reviews/2026-08-06-gdpr-compliance-assessment.md).
> These procedures are written as if it does, because the cost of having them
> and not needing them is a few hours.

---

## 1. Subject access / portability request (Art 15, Art 20)

**Clock: one month** from receipt, extendable by two further months for complex
requests if you tell the requester within the first month.

```bash
# What would be disclosed, by table — no file written, nothing sensitive printed
.venv/bin/python scripts/export_user_data.py --guild <gid> --user <uid> --summary

# The actual export
.venv/bin/python scripts/export_user_data.py --guild <gid> --user <uid> --out sar.json
```

The database is opened `mode=ro`, so this is safe to run against the live bot.

**The export is deliberately wider than the erasure path.** Data the server
keeps under Art 17(3) — the `econ_ledger` double-entry record, sanction
history, consent audit, no-contact orders — is still the subject's personal
data and still has to be disclosed. Retention is not an access exemption.

### Before you send it — three checks

1. **Art 15(4) third-party review.** The script prints a `review_required`
   list: tables whose rows name a *second* member, where that person's identity
   is the payload (whispers, guesses, no-contact orders, DM consent pairs, the
   interaction graph, moderation records). An access request must not adversely
   affect others' rights. Decide per table whether to redact the counterparty
   id, and record what you decided.
2. **The list-column blind spot.** A few columns store member ids as a JSON or
   CSV *list*, which an equality match cannot find. The script names them in
   its `notes`. Grep them by hand:
   ```bash
   sqlite3 dungeonkeeper.db "SELECT * FROM risky_pending_questions WHERE participant_user_ids LIKE '%<uid>%';"
   ```
   Same for `risky_pending_questions.lowest_tie_user_ids`,
   `risky_active_rounds.reroll_user_ids`, `econ_demurrage_sweeps.taxed_members`,
   `confession_config.blocked_user_ids`, `revive_events.follow_authors`.
3. **Unscoped tables.** Some tables have no `guild_id`; the script notes which
   ones actually returned rows. Their rows span every guild the bot is in, so
   for a multi-guild subject the export may include data from a guild the
   request did not cover.

### What is *not* in the export

- **Discord's own copy** of anything. The subject's messages as Discord holds
  them are Discord's to disclose, not ours.
- **Files on disk.** Guess round originals live in the cache directory, not the
  DB. Check `guess_rounds.original_path` in the export and attach the files if
  the request covers them.
- **Backups.** See the erasure section's note.

---

## 2. Erasure request (Art 17)

1. **Discord side** — run `/delete_user member:<user>` in the guild (mode
   omitted = all messages). Works for users who have left. Wait for the
   summary; re-run after fixing perms if it reports failures.
2. **DB side** — from the checkout, with the bot either running or stopped
   (WAL handles the concurrent writer; the purge is one transaction):

   ```bash
   .venv/bin/python - <<'PY'
   from pathlib import Path
   from bot_modules.core.db_utils import open_db
   from bot_modules.services.privacy_service import purge_user_data

   GUILD_ID = 0   # ← fill in
   USER_ID = 0    # ← fill in

   with open_db(Path("dungeonkeeper.db")) as conn:
       n = purge_user_data(conn, GUILD_ID, USER_ID)
   print(f"purged; {n} message rows existed")
   PY
   ```

   The purge covers messages+children, XP/activity/events, gender, telemetry,
   interaction graph (both directions), wellness, watched/trusted/invite pair
   tables, bios, birthdays, voice-master prefs, and all per-member
   economy/casino state (`economy_service._PURGE_USER_ID_TABLES`).

   **Deliberately preserved, and the Art 17(3) ground relied on** — the full
   table is in [data_register.md](data_register.md):

   | Preserved | Ground |
   |---|---|
   | `econ_ledger` | Art 17(3)(e) — establishment/exercise/defence of legal claims; also integrity of a double-entry record where deleting one side corrupts the other party's balance |
   | Sanction history (jails, warnings, tickets, policy tickets) | Art 17(3)(e) — the canonical record of moderation decisions, needed if a decision is challenged |
   | `dm_audit_log`, `dm_consent_pairs` | Art 17(3)(e) — consent forensics; the record exists precisely to answer "did they agree" |
   | `no_contact_pairs`, `no_contact_events` | Art 17(3)(e) + protection of the **other** party's rights and freedoms — erasing a protective order at the request of the restrained party would defeat it |
   | `reaction_log`, `voice_follow_log` | Attention-report evidence path. **Weakest of the five** — this is an internal-analytics justification, not a clean Art 17(3) ground. Revisit if a request is ever actually contested |

   **Tell the subject what was kept and why.** Art 17 erasure that silently
   retains categories is worse than a documented partial erasure.
3. **Per-feature self-service paths**, run *as* or *for* the user where the
   request demands them: `/whisper forget-me` (whisper data), bios delete (also
   covered by step 2), `/guess delete` per round, `/guess optout`.
4. **Files on disk.** `purge_user_data` clears DB rows only. Guess round
   originals in the cache directory need deleting separately — take the paths
   from an export run *before* the purge.
5. **Reclaim the bytes** — deleted rows persist in the WAL and freelist:

   ```bash
   sqlite3 dungeonkeeper.db "PRAGMA wal_checkpoint(TRUNCATE);"
   # optional, rewrites the file: sqlite3 dungeonkeeper.db "VACUUM;"
   ```
6. **Backups** — nightly backups retain pre-erasure copies until they age out
   of the retention window. Note the erasure date so an accidental restore can
   be re-purged, and **do not restore past backups over an erasure** without
   re-running step 2. If the requester asks, the honest answer is that backups
   are erased on rotation, not on request — which is an accepted position
   provided the window is stated.

### Multi-guild note

The purge is per-guild. A user present in both guilds needs one run per guild
id. The export has the same shape.

---

## 3. Personal-data breach (Art 33 / Art 34)

**Clock: seventy-two hours** from *becoming aware*, to the supervisory
authority. "Aware" starts at reasonable certainty that a breach occurred, not
at the end of the investigation — an incomplete notification on time beats a
complete one late (Art 33(4) explicitly allows phased notification).

### What counts

A breach is any accidental or unlawful destruction, loss, alteration,
unauthorised disclosure of, or access to personal data. For this deployment the
realistic shapes are:

- Dashboard session compromise (stolen cookie, leaked `SESSION_SECRET`).
- The database file or a backup leaving the host.
- A bot token leak allowing message reads across the server.
- A bug exposing one member's data to another — the cross-guild IDOR class the
  2026-07-23 review found four of.
- Accidental publication of an export file from section 1.

### Procedure

1. **Contain first, document as you go.** Rotate the credential, revoke the
   token, take the dashboard down if it is the vector. Write timestamps down
   while you do it — the 72 hours is evidenced by your own notes.
2. **Establish scope** using the tools already here: `scripts/export_user_data.py
   --summary` tells you what a given account's exposure actually contains, and
   the [data register](data_register.md) tells you which categories are
   involved. Note that `message_storage_level='all'` on the main guild means a
   database compromise is a **content** breach, not a metadata one.
3. **Assess risk to the people involved.** Low risk → record it and do not
   notify (Art 33(1) allows this, but the reasoning must be written down).
   Risk → notify the supervisory authority within 72h. **High** risk → also
   notify the affected members directly, without undue delay (Art 34).
   Anonymous-feature data (whispers, confessions, Guess) raises severity
   sharply: deanonymisation is the harm those features exist to prevent.
4. **Record it either way.** Art 33(5) requires an internal record of *every*
   breach including ones you decided not to notify — facts, effects, remedial
   action. Append to the register below.
5. **Members notified in Discord** should get it in writing in the server, not
   only by DM — DMs are closed by default for many members.

### Breach register

| Date aware | What happened | Categories | People affected | Notified? | Ground if not |
|---|---|---|---|---|---|
| — | *(no breaches recorded)* | | | | |

---

## Where the data itself is catalogued

[data_register.md](data_register.md) — every table holding personal data, its
class, retention, purge coverage, and the processor it reaches. Keep it current:
a new user-data table needs a row there in the same commit.
