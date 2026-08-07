-- Migration 157: Mention Awards — generalize the phrase/announcer levers into
-- a conditions list ("chips").
--
-- WHY: the trigger phrase and the announcer role were two hardcoded instances
-- of a general idea — conditions a message must meet before it awards. Making
-- the list first-class adds "mentions role" (the only robust way to key on a
-- role ping: `@Hot Seat` is `<@&id>` in raw content, so a *text* match on the
-- rendered name can never see it), "from user", and regex text matching,
-- without a schema change per new condition kind.
--
-- `conditions` is a JSON array of chips, every one of which must match (AND):
--   {"kind":"contains_text",  "value":"your turn", "regex":false}
--   {"kind":"mentions_role",  "value":"<role id as string>"}
--   {"kind":"from_user",      "value":"<user id as string>"}
--   {"kind":"author_has_role","value":"<role id as string>"}
--
-- Ids are JSON *strings*: the panel reads this JSON, and a bare snowflake
-- past 2^53 loses precision in JavaScript.
--
-- Existing rows convert losslessly — phrase becomes a contains_text chip,
-- a non-zero announcer_role_id becomes an author_has_role chip — and the two
-- old columns are then dropped so there is exactly one source of truth. A
-- rule with an empty conditions list matches nothing (fail closed, enforced
-- in logic.py); the conversion cannot produce one, since phrase was NOT NULL
-- and validated non-empty.

ALTER TABLE mention_award_rules ADD COLUMN conditions TEXT NOT NULL DEFAULT '[]';

UPDATE mention_award_rules
   SET conditions = json_array(
        json_object('kind', 'contains_text', 'value', phrase, 'regex', json('false'))
   );

UPDATE mention_award_rules
   SET conditions = json_insert(
        conditions, '$[#]',
        json_object('kind', 'author_has_role', 'value', CAST(announcer_role_id AS TEXT))
   )
 WHERE announcer_role_id != 0;

ALTER TABLE mention_award_rules DROP COLUMN phrase;
ALTER TABLE mention_award_rules DROP COLUMN announcer_role_id;
