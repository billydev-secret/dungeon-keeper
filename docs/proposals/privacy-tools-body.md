# Privacy & Data Retention

## The promise

Everything the bot does with your data stays on hardware in this server's own home — the machine the bot runs on, or another on the same private network. Image checks, voice transcription, sentiment scoring and the moderation model are all handled there, never sent to a cloud service. Nothing is sold, nothing is shared.

Two things leave the box, and only these two. A question you put to the in-server AI assistant goes to Anthropic to be answered, without your name or your ID attached. Music you queue or post gets looked up on Spotify by track name — Spotify never learns who asked for it.

You can have a copy of everything held about you, or all of it erased, whenever you want. You don't have to give a reason, and asking isn't a complaint.

## The carve-outs

Three places where that promise has an honest limit. You should know all three.

**The bot keeps its own copy of your messages, text included.** Deleting a message in Discord removes it from Discord — the server's copy stays, and moderators can still read it. `/delete_me` clears the Discord side only. The text is cleared after a year; the record that you posted is kept.

**"Anonymous" means anonymous to other members, not to the admin.** Whispers, confessions, anonymous game submissions and Guess rounds all carry your name in a log only the admin can see. That's deliberate. An anonymous channel nobody is accountable in turns into a harassment tool, and this is the thing that stops it.

**An erasure keeps a few things**, and whoever runs it will tell you exactly which:

- The coin ledger. It's double-entry — deleting your side of a transfer would corrupt the balance of whoever you traded with.
- Moderation records, so a decision can still be explained if it's ever challenged.
- A no-contact order protecting someone else, and any voice-room block someone has placed on you. Your own no-contact entries and your own block list go with everything else; theirs are their protection, and yours to respect.
- If you're a mod or admin: your name against settings you set up, like a doc you wrote or an announcement you scheduled. That's a record about the server, not about you. It still appears in your copy.

## How long it's kept

Most of it, honestly: for as long as the server runs. Some of it goes on a clock, and those are the ones worth knowing.

**Cleared automatically:**

- **The text of what you post** — after **12 months**, once the server switches that on. The message itself stays (who posted it, where, and when); the words go, and so do any attachments.
- **Who you interacted with** — reactions, replies, who followed whom into a voice channel, role pings: **180 days**, once the server switches that on. Joins and leaves are kept for good; they are how the bot knows how long you have been here.
- **The link between you and something you posted anonymously** — a confession, an AMA question, a Would-You-Rather vote, a compliment pairing: the admin's log keeps that link for **90 days**, then it is gone. The separate record that routes replies back to your confession goes sooner, after **7 days**.
- **A confession waiting to be approved** — deleted the moment it is approved or rejected, or after **7 days** if neither happens. It is the only place the bot holds a confession's text next to your name.
- **A question you asked in Risky Rolls** — **7 days**.
- **Wellness records, once you opt out** — **30 days**.
- **Your bio, if you leave the server** — **12 months**.
- **Off-site backups** — **14 days**. This is the one place your data can briefly outlive an erasure.

**Kept for as long as the server runs:** your coin balance and every ledger entry, moderation records, your XP and levels, game and casino history, a bio or birthday you added yourself, and the record that you posted a message even after its text has gone. **Whispers and Guess rounds are also in this list, not the one above** — they are kept until you clear them yourself with `/whisper forget-me` or `/guess optout`.

If you want to know how long something specific is kept, ask. There is a written record covering every place the bot stores anything, and whoever answers will read it to you rather than guess.

## The tools

- `/delete_me` — clears your messages from the server, all of them or just the images.
- **Leave one feature instead of all of it:** `/whisper forget-me`, `/guess optout`, and the opt-out controls in `/bank shop` and Wellness. All work on their own, no need to ask.
- **A copy of everything held about you, or a full erasure** — open a ticket in <#1469781592854630580>. You don't have to give a reason, and asking isn't a complaint. Whoever runs an erasure will tell you exactly what was kept and why.

Protecting yourself from another member — blocking, `/nocontact`, DM permissions — is a separate subject with its own doc: see **Safety Tools**.
