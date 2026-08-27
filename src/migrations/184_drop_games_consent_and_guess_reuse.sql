-- Migration 184: drop two orphans the 2026-08-06 review synthesis left open
-- (item 13, "the orphan `games_consent` table + guess reuse columns still
-- want dropping in the next games migration").
--
-- 1. `games_consent` (migration 019). The per-user Truth-or-Dare consent flag
--    for a gate that was never enforced: the `/consent` command family and the
--    `games_consent/` module were deleted long ago (dir + scaffolding in
--    `49a02867`), leaving a table with zero code references and one prod row
--    from 2026-06-13. It was never in `docs/data_register.md` either, so it is
--    personal data nobody was accounting for — dropping it is the fix, not a
--    loss. No purge or export helper names the table, so nothing starts
--    raising when it goes; the export's generic `user_id` sweep simply has one
--    fewer table to walk. If per-user game consent is ever revived it is a new
--    build (games_system_spec:296), not a re-enable of this row.
--
-- 2. `guess_rounds.{allow_reuse, is_reuse, original_round_id, reuse_blocked}`
--    (migration 009, as `veil_rounds`). The round-reuse / throwback feature was
--    cut before release (guess_spec:111 lists it under non-goals) and the
--    columns became write-only ballast: every insert passed a hardcoded False
--    and the one reader, `guess_repo.get_reusable_rounds`, had no caller
--    outside its own unit tests. All 408 prod rounds carry 0 / 0 / NULL / 0, so
--    the drop discards no information. The surviving `idx_guess_rounds_reuse`
--    keeps its historical name but has indexed (guild_id, submitter_id,
--    image_hash) since migration 020 — it is the duplicate-image guard, not
--    reuse, and no index references the dropped columns, so DROP COLUMN is
--    legal here. The vestigial dataclass fields, insert parameters and the dead
--    reader go in the same commit.

DROP TABLE IF EXISTS games_consent;

ALTER TABLE guess_rounds DROP COLUMN allow_reuse;
ALTER TABLE guess_rounds DROP COLUMN is_reuse;
ALTER TABLE guess_rounds DROP COLUMN original_round_id;
ALTER TABLE guess_rounds DROP COLUMN reuse_blocked;
