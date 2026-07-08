-- Pin a doc's messages in the channel it's placed in.
--
-- A placement can be marked `pinned`: after `sync` reconciles the doc's
-- messages, it pins every one of them (in reading order) so the whole doc lives
-- in the channel's pinned list, and unpins them when the flag is cleared. The
-- pin pass is delta-only — it never re-pins an already-pinned message — because
-- pinning emits a "pinned a message" system notice that would otherwise spam on
-- every edit/sync.

ALTER TABLE doc_placements ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
