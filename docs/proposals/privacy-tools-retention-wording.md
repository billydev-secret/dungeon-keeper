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

---

## How long it's kept

Most of it, honestly: for as long as the server runs. A few things expire on a
clock, and those are worth knowing.

**Expires on its own:**

- **What you post, as text** — cleared after **12 months** on servers that
  archive message content. The message itself stays (who, where, when); the
  words go.
- **Who you interacted with** — reactions, replies, who followed whom into
  voice, role pings, joins and leaves: **180 days**.
- **The link between a confession and you** — **7 days**, then it's gone for
  good, including from the mod log.
- **A confession waiting for approval** — deleted the moment a mod approves or
  rejects it, or after **7 days** if neither happens.
- **Questions asked in Risky Rolls** — **7 days**.
- **The anonymous-games audit trail** — **90 days**.
- **Wellness records, once you opt out** — **30 days**.
- **An archived bio, once you leave** — **12 months**.
- **Off-site backups** — **14 days**. This is the one place your data can
  briefly outlive an erasure.

**Kept for as long as the server runs:** your coin balance and every ledger
entry, moderation records, XP and levels, game and casino history, bios and
birthdays you chose to add, and the record that you posted a message even after
its text has gone.

If you want to know how long something specific is kept, ask — there's a written
record of every one, and whoever answers will read it to you rather than guess.

---

## One caveat on the 12-month line

`message_storage_level` is **per guild**, and on 2026-09-05 only one of the eight
guilds was set to `all`. On the other seven there is no message text to clear and
the line reads as a promise about something that never happens. It is written
conditionally ("on servers that archive message content") for that reason. If
this doc is ever posted somewhere guild-specific, say plainly which applies there
instead.
