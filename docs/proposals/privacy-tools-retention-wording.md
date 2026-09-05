# Proposed wording — the `privacy-tools` doc, split in two

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


---

# The split (decided 2026-09-05)

The live doc was retitled *"Privacy & Data Retention"* and trimmed earlier that
day, which cut the safety tools out. Rather than restore them into a doc whose
own title no longer covers them, they become a second doc. Two subjects, two
docs:

| Doc | `doc_key` | Covers |
|---|---|---|
| **Privacy & Data Retention** | `privacy-tools` *(existing — key stays)* | The promise, the carve-outs, how long things are kept, and the tools that change what is kept |
| **Safety Tools** | `safety-tools` *(new)* | Blocking, `/nocontact`, DM permissions, feature opt-outs, and the ticket route |

Bodies: `privacy-tools-body.md` and `safety-tools-body.md`, both ready to paste.

**The access route is restored deliberately.** The trim had removed the only
text telling a member how to request a copy or an erasure — an Art 15/17 route,
not a nicety. It now appears in both docs, framed for each: "a copy of
everything held about you, or a full erasure" under retention, "someone who is
bothering you, or a copy / erasure / correction" under safety. A member landing
on either finds the way through.

**Every reference verified against production**, because the trimmed text was
old enough to have rotted:

- `<#1469766598800838736>` — the DM-permissions panel channel, confirmed live in
  `dm_panel_settings` for this guild, with a panel message posted.
- `<#1469781592854630580>` — the ticket channel, confirmed live: the intake
  reference blocks still direct newcomers to it.
- `/nocontact`, `/delete_me`, `/whisper forget-me`, `/whisper optout`,
  `/guess optout`, `/bank shop` — all still exist in source. `/bank shop`
  survived the shop reorganisation.

**Placement.** `privacy-tools` currently sits in `1470847624704950395` and
`1523516810685845636` (the rules/FAQ channel). `safety-tools` most naturally
goes to the same two, so a member scrolling either channel meets both halves —
but that is a placement decision, not a wording one.
