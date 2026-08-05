# Community fixtures — battery review, 2026-08-05 (closes Wave 4)

events_cog (ingest hub), starboard, quotes, emoji_stealer, auto_react,
announcements, birthday, bump_tracker, chat_revive, invite_tracker,
inactive/prune, hidden_channels, todo, qa, docs cog, advisor.

## The held-over advisor question (economy-sources G1) — CLOSED

`build_system(guild_context)` sends: instructions + the public manual
(prompt-cached) + a **secret-filtered, admin-gated config summary**
(`advisor_context.build_config_summary` — `_can_see_config` gate, raw
`field = value` lines, char-capped). Member data sent to Anthropic is the
asker's own question text, nothing else. Remaining action from G1 is just
the privacy-notice line naming Anthropic. Downgraded medium → low.

## Highlights per fixture (findings only)

- **events_cog** is the ingest hub feeding message_store/XP/sentiment/
  interactions/greeting — its data semantics were reviewed in the
  privacy-core and per-feature bundles; the cog itself is dispatch glue. ✓
- **Birthday**: stores **month/day only, no year** — deliberate
  minimization ✓, plus a visibility `preference` column. `set_by` allows
  third-party entry (mods); fine with audit. Register: month/day
  downgrade from "PII needs decision" → purge-able, add to batch.
- **Quotes**: context-menu render of another member's message into an
  image; no consent from the quoted member, but `quote_audit_log` records
  every render (45 rows) and mods can act. U1 (low): note in manual.html
  that quotes are audited. Register: audit preserve.
- **Emoji stealer**: fetches only constructed Discord-CDN URLs
  (`emoji_cdn_url` + `is_https_url`) — SSRF-safe by construction; hand to
  the security sweep for confirmation, expected pass.
- **Invite tracker**: `invite_edges` (inviter↔invitee graph) feeds intake
  invited-by + effectiveness panel. Add both-direction rows to the purge
  batch (social graph, no audit role).
- **Inactive/prune**: verified day-based + exception list in the health
  bundle ✓. Starboard/auto-react/bump/chat-revive/todo/qa/docs/
  hidden_channels: config + counters, nothing sensitive, no findings.

## Verdict — Wave 4 complete

No highs. One closure (advisor context), several purge-batch additions
(birthdays, invite_edges, voice-master prefs from batch 1). Next: W4.5
horizontal sweeps — security, reliability, DB/perf, test-suite, logging,
deps — then W5 synthesis.
