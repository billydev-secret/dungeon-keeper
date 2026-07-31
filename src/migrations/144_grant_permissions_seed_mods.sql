-- /grant per-grant permissions: seed the configured mod roles so removing the
-- moderator bypass is behavior-neutral on the day it ships.
--
-- can_use_grant_role used to short-circuit on is_mod, which meant the
-- grant_role_permissions allow-list could only ever *add* people: every
-- moderator could hand out every configured grant and no list could take that
-- away. "Golden Girl is one keeper's to give" was unexpressable. The gate is
-- now is_admin, so the list actually decides.
--
-- Without this seed that flip would silently revoke access. In prod, 5 of the
-- 6 grants (goldengirl, kink, nsfw, theboys, veteran) have EMPTY permission
-- rows and work purely through the old bypass; denizen lists only the greeter
-- role. Flipping the gate cold would leave those 5 admin-only and drop
-- denizen from moderators the moment the bot restarted.
--
-- So: copy each guild's mod_role_ids into every grant it has configured. Day
-- one behaves exactly like today, and pruning a grant back to its real keeper
-- becomes a dashboard edit rather than an outage.
--
-- mod_role_ids is a CSV in the config table, hence the recursive split. The
-- join is strict on guild_id — no guild_id=0 legacy fallback — because that
-- row duplicates the home guild's and would otherwise leak the home guild's
-- mod role into other guilds' grants. Admin roles are deliberately NOT seeded:
-- admins bypass the list anyway, so a row for them would be dead weight the
-- user then has to look at and wonder about.
--
-- INSERT OR IGNORE against the (guild_id, grant_name, entity_type, entity_id)
-- primary key makes this idempotent and preserves any list already set by
-- hand (denizen's greeter role survives and gains the mod role alongside it).

INSERT OR IGNORE INTO grant_role_permissions
    (guild_id, grant_name, entity_type, entity_id)
WITH RECURSIVE mod_role_csv(guild_id, rest, part) AS (
    SELECT guild_id, value || ',', ''
    FROM config
    WHERE key = 'mod_role_ids' AND guild_id <> 0 AND TRIM(value) <> ''
    UNION ALL
    SELECT guild_id,
           SUBSTR(rest, INSTR(rest, ',') + 1),
           SUBSTR(rest, 1, INSTR(rest, ',') - 1)
    FROM mod_role_csv
    WHERE rest <> ''
)
SELECT gr.guild_id, gr.grant_name, 'role', CAST(TRIM(m.part) AS INTEGER)
FROM grant_roles gr
JOIN mod_role_csv m ON m.guild_id = gr.guild_id
WHERE TRIM(m.part) <> ''
  AND CAST(TRIM(m.part) AS INTEGER) > 0;
