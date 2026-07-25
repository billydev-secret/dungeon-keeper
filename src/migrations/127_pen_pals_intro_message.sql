-- Pen Pals: optional custom message shown in the session-opened embed.
--
-- Free text an admin can set on the dashboard; shown as the embed
-- description in _post_intro when non-empty. Blank (the default) means no
-- description is added, leaving the embed exactly as it was before this
-- column existed.
ALTER TABLE pen_pals_config ADD COLUMN intro_message TEXT NOT NULL DEFAULT '';
