-- 146_no_contact_pairs.sql
-- No-contact list — pairs of members the bot must never put in contact.
--
-- This is a SAFETY feature, not a preference. The motivating case: a member
-- blocked someone in Discord, and he could still reach her through the bot's
-- anonymous features (whisper, AMA, confession replies), because a Discord
-- block does not travel through a bot that relays on his behalf.
--
-- WHY NOT dm_consent_pairs, whose (user_low, user_high) shape this copies:
--   1. dm_perms_service.normalize_request_type() coerces anything that isn't
--      'friend' to 'dm', so a rel_type='no_contact' insert would silently
--      store 'dm'.
--   2. load_consent_pairs() ignores rel_type entirely and adds every row to
--      the mutual-consent set in BOTH orderings. A no-contact row living
--      there would not merely fail to block — it would GRANT the blocked
--      party a DM consent pair. Exactly inverted.
--   3. Its PK is one row per pair, so the table cannot represent "these two
--      had a consent pair, and now they have a no-contact"; the insert would
--      overwrite the consent row via ON CONFLICT DO UPDATE and lose it.
-- So we reuse the *convention* (order-independent low/high, reason,
-- provenance) in a table of our own, with no shared row space.
--
-- WHY NOT pen_pals_blocks, which already has a symmetric source='admin' mode:
-- that table is feature-local by design and carries no notion of who is being
-- protected. A no-contact pair is cross-feature and its removal rule depends
-- on that distinction (see protected_user_id).

CREATE TABLE IF NOT EXISTS no_contact_pairs (
    guild_id          INTEGER NOT NULL,
    user_low          INTEGER NOT NULL,  -- order-independent: min(a, b)
    user_high         INTEGER NOT NULL,  -- max(a, b)
    -- Who the entry protects, and therefore the ONLY member who may lift it.
    -- NULL means a mutual separation (typically mod-added): neither party can
    -- remove it alone. Deliberately distinct from created_by — a member who
    -- adds an entry against themselves must not be able to quietly remove it
    -- later, and a mod adding one on someone's behalf has to say who it is for.
    protected_user_id INTEGER,
    created_by        INTEGER NOT NULL,  -- the member, or the mod who acted
    reason            TEXT    NOT NULL DEFAULT '',
    created_at        REAL    NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (guild_id, user_low, user_high)
);

-- Every gate asks "is this user under a no-contact with that user", and the
-- mention watcher asks "who is this author under a no-contact with" on the
-- message path. The PK covers lookups anchored on user_low; this covers the
-- user_high direction so neither ordering degrades to a scan.
CREATE INDEX IF NOT EXISTS idx_no_contact_pairs_high
    ON no_contact_pairs (guild_id, user_high);

-- Where alerts and blocked-attempt records are delivered. Both are server
-- config, so they are set on the dashboard rather than in Discord. A guild
-- with no row (or channel 0) simply gets no alerts — enforcement still
-- applies; alerting is the optional part.
CREATE TABLE IF NOT EXISTS no_contact_settings (
    guild_id         INTEGER PRIMARY KEY,
    alert_channel_id INTEGER NOT NULL DEFAULT 0,
    alert_role_id    INTEGER NOT NULL DEFAULT 0   -- pinged on alert; 0 = no ping
);

-- Blocked attempts, and mention/reply alerts, as a durable record for staff.
-- Deliberately NOT surfaced to the protected member: the sender already sees
-- a fake success and learns nothing, and notifying her every time he tries
-- would hand him an indirect channel to distress her. Mods get the trail that
-- justifies a kick or ban when attempts keep coming.
--
-- No message text is stored. messages.content is nullable and content
-- retention is off by default; widening what the bot keeps server-wide to
-- serve this feature would be a real privacy cost to everyone else, so an
-- alert carries provenance (channel, message) and the reader follows the
-- jump link for the rest.
CREATE TABLE IF NOT EXISTS no_contact_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    actor_id   INTEGER NOT NULL,  -- who attempted contact / authored the mention
    target_id  INTEGER NOT NULL,  -- the other member of the pair
    kind       TEXT    NOT NULL,  -- 'attempt' (blocked) | 'mention' | 'reply'
    surface    TEXT    NOT NULL DEFAULT '',  -- 'whisper', 'ama', 'guess', …
    channel_id INTEGER,
    message_id INTEGER,
    created_at REAL    NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_no_contact_events_guild
    ON no_contact_events (guild_id, created_at);
