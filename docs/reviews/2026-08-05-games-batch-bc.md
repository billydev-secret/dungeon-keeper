# W3 batches B+C: 14 prompt games — review, 2026-08-05 (closes Wave 3)

price, external, mlt, ttl, story, traditional, nhie, wyr, hottakes,
fantasies, mfk, ffa, compliment, photo.

## Architecture

- Shape conformity is total: eleven `{logic,embeds}.py` modules + thin
  cogs on the shared platform (ffa is prompts-only by design). Batch A's
  verdict holds — no consolidation debt in the prompt-game family.
- **A1 — `games_external_messages` never empties.** It's the parse buffer
  for external game bots (content + embeds_json + author_id, 11,257 rows,
  `parse_status`/`parsed_at` present but nothing deletes parsed rows).
  Retention fix: sweep rows with `parse_status` terminal and
  `parsed_at < now-30d` — ledger effects (cat_catch payouts) are already
  booked; the buffer has no read-back use. **Priority: low-medium**
  (unbounded growth + stored content of a channel).

## GDPR

- **Anonymous games (FFA ToD, Hot Takes, Fantasies, AMA) share one
  deanonymization design**: name withheld in-channel, author recorded in
  `anon_audit_log` (90-day sweep, verified live), optional staff mirror
  channel with author visible — all documented in games_system_spec:116.
  Register: **DECIDED** as a family; the audit-channel mirror is the
  disclosure surface staff already know.
- Fantasies placement is mod-policed channels — same owner-decision class
  as Guess's 07-27 call; covered by that standing-exception note.
- Photo challenge: no image storage in the game path (message refs +
  econ_photo_* covered in economy bundles). ✓

## UX / Docs

- No findings. games_system_spec's anonymous-features paragraph is the
  single source and it's accurate.

## Verdict — Wave 3 complete

One retention fix (A1 external-buffer sweep). The 18-game family is
uniformly healthy; the review effort saved here was reinvested upstream.
