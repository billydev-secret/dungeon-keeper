# GDPR erasure runbook

Operator procedure for a genuine legal erasure request. The bot deliberately
has **no command** for this — `/delete_me` / `/delete_user` clear Discord
messages only (see [privacy_spec.md](privacy_spec.md)).

## Procedure

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
   **Deliberately preserved**: `econ_ledger` (pseudonymous double-entry
   record), sanction history (jails/warnings/tickets), dm-perms consent
   audit, no-contact records, whisper/confession audit surfaces (each has
   its own per-feature erasure or TTL — see
   `docs/reviews/2026-08-05-gdpr-register.md`).
3. **Per-feature self-service paths**, run *as* or *for* the user where the
   request demands them: `/whisper forget-me` (whisper data), bios delete
   (also covered by step 2), `/guess delete` per round.
4. **Reclaim the bytes** — deleted rows persist in the WAL and freelist:

   ```bash
   sqlite3 dungeonkeeper.db "PRAGMA wal_checkpoint(TRUNCATE);"
   # optional, rewrites the file: sqlite3 dungeonkeeper.db "VACUUM;"
   ```
5. **Backups** — nightly backups retain pre-erasure copies until they age
   out of the retention window; note the erasure date so an accidental
   restore can be re-purged. Do not restore past backups over an erasure
   without re-running step 2.

## Multi-guild note

The purge is per-guild. A user present in both guilds needs one run per
guild id.
