-- Evidence of consent for the Guess pool (GDPR Art 7(1)).
--
-- The consent *view* shipped in 775d903d — the member reads a disclosure and
-- the role is granted only on the Join button. What was missing is the record:
-- Art 7(1) puts the burden on the controller to *demonstrate* that consent was
-- given, and a role on a Discord account demonstrates nothing about what the
-- person was shown or when.
--
-- disclosure_version pins which wording they agreed to. When the disclosure
-- changes materially the version bumps, and rows carrying an older version are
-- consent to something the member never read — which is the question anyone
-- auditing this would ask, and it cannot be reconstructed afterwards.
--
-- Rows survive optout on purpose: "did they ever consent, and to what" is
-- exactly the question that outlives the consent itself. A full erasure clears
-- them via purge_user_data (guild_id + user_id).
CREATE TABLE IF NOT EXISTS guess_consents (
    guild_id           INTEGER NOT NULL,
    user_id            INTEGER NOT NULL,
    consented_at       REAL    NOT NULL,
    disclosure_version INTEGER NOT NULL,
    withdrawn_at       REAL,
    PRIMARY KEY (guild_id, user_id, consented_at)
);

CREATE INDEX IF NOT EXISTS idx_guess_consents_user
    ON guess_consents (guild_id, user_id);
