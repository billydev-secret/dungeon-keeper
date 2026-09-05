# Proposed wording — `privacy-tools` doc, retention section

**Status: PROPOSAL. Not applied.** The live `privacy-tools` doc is production
data, authored on the dashboard Docs panel and posted to two channels
(`1470847624704950395`, `1523516810685845636`). Changing it changes what members
are reading right now, so this session did not touch it. Apply it from the Docs
panel and re-post if you want it live.

## Why

The 2026-09-05 retention review's first finding: the live doc is candid about
*what* is kept and says **nothing at all** about *how long* — no durations, no
mention of the 7-, 30-, 90-day or 12-month sweeps. `manual.html` carries a
"How long" value on every row. So the surface most members actually read is the
one that answers the question least.

This adds one section. It changes nothing else — the existing text is good and
the carve-outs are well put.

## Where it goes

After **The carve-outs**, before **The tools**. It needs the carve-outs' honesty
about the message copy to have landed first, and it reads as the natural next
question.

## The text

The complete new body is in **`privacy-tools-body.md`** beside this file, ready
to paste into the Docs panel whole. It is the live doc as it stood on
2026-09-05 21:19 plus one new `## How long it's kept` section and one clause on
the message carve-out. Nothing else is altered.

## Two things that changed after this proposal was first written

**The doc was retitled and trimmed on 2026-09-05.** It is now
*"Privacy & Data Retention"* — a heading that promises retention information the
body did not contain. The hedged "on servers that archive message content"
wording is also no longer needed: this doc is guild-scoped and its guild is at
`message_storage_level=all`, so the 12-month rule can be stated plainly.

**The 7-day anonymity claim was false and is not reproduced.** See F9 in the
review. Confessions write to `anon_audit_log`, so the author link lives 90 days,
not 7. The draft says 90.

## Sequencing

`update_doc` re-renders every placement live, so saving from the Docs panel
updates both posted messages by itself — but a direct database write would not,
and would leave the channels showing the old text. **Save from the panel; do not
write the row directly.**

The 12-month and 180-day lines describe sweeps that ship in `ab775b0e` and are
**inert until the next restart**. Everything else in the list is already
enforced and verified against live data. Restarting before saving keeps the
document true on the day it is posted.
