-- Externally-detected completion marks for one-time setup quests.
--
-- The setup done-checks in economy_quests_service read the owning feature's
-- own tables (bios, member_birthdays, role_menu_grants, econ_ledger), but
-- some completing actions happen where the bot has no table of record:
-- picking roles through Discord's native onboarding ("Channels & Roles →
-- Customize") assigns roles without any bot interaction, so those members
-- looked forever-pending and the role_pick quest stayed pinned to their
-- daily board.
--
-- A row here says "this member has already done this setup kind" — it clears
-- the quest from their board exactly like the feature-table checks do, but
-- deliberately does NOT pay the quest (no claim row): detection is not the
-- member acting on the quest, and the first sweep would otherwise mass-mint.
-- Rows are written by the hourly economy-loop sweep (guild onboarding roles ∩
-- member roles) and are additive-only; kind is a quests.SETUP_QUEST_KINDS
-- value ('role_pick' today, the column is generic for future detectors).
CREATE TABLE IF NOT EXISTS econ_setup_marks (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    kind      TEXT    NOT NULL,
    marked_at REAL    NOT NULL,
    PRIMARY KEY (guild_id, user_id, kind)
) WITHOUT ROWID;
