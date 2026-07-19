-- Social quest kinds (plan: quest-variety, social round).
--
-- Most social kinds need NO schema: "N distinct partners/channels/days"
-- quests fall out of the counted-quest marks table by keying occurrences to
-- the entity instead of the event. The one exception is
-- conversation_starter ("your message draws 3+ replies from distinct
-- humans"): message content is never stored (storage level "none"), so the
-- distinct-replier count per target message is derived at reply ingest into
-- this table — the starboard-crossing pattern. Rows are pruned to a
-- trailing 14 days on the economy day roll (a two-week-old message drawing
-- its third reply is conversation necromancy we're happy to miss).

CREATE TABLE IF NOT EXISTS econ_msg_replies (
    guild_id          INTEGER NOT NULL,
    target_message_id INTEGER NOT NULL,
    target_author_id  INTEGER NOT NULL,
    replier_id        INTEGER NOT NULL,
    created_at        REAL    NOT NULL,
    PRIMARY KEY (guild_id, target_message_id, replier_id)
) WITHOUT ROWID;
