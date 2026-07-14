# Chat Revive — Product Spec (Dungeon Keeper)

**Codename:** Ember — it stirs the coals when the hearth goes quiet.

## What it is

Chat Revive notices when a channel has gone unusually quiet *for that channel, at that time of day* and gently restarts conversation by posting a question from a curated bank — optionally tagging an opt-in "chat revive" role of members who've said "yes, wake me when the room needs a spark."

It is not a timer bot. It never posts on a schedule, never spams, and never talks over an active room. Done right, members shouldn't think of it as a bot feature at all — it should feel like someone tossed a good question into a lull.

## Product principles

1. **It knows the room's rhythm.** A channel that's always sleepy at 3am should never get poked at 3am. A channel that normally buzzes at 8pm and has been silent for 90 minutes is a real lull worth catching. "Quiet" is always relative to that channel's own history.
2. **It never talks to itself.** If the last message in the channel is the bot's own revive, it stays silent until humans have spoken again. No chains, ever.
3. **Rare is powerful.** A revive that shows up a few times a week feels like a treat; one that shows up daily is noise people learn to ignore. Frequency limits are the heart of the product, not a setting buried at the bottom.
4. **Pings are earned, opt-in, and scarce.** Only members who took the chat-revive role get tagged, and never more than once a day per channel.
5. **It learns what works.** Every revive quietly measures whether conversation actually followed. Questions that spark chat come around more often; duds fade out on their own.

## Member experience

A quiet Tuesday evening in #general, normally lively at this hour. After a long unusual lull, this appears:

> 🔥 *stirring the coals…* @chat-revive What's a skill you learned entirely by accident?

That's the whole footprint. Plain text — no embed, no buttons, no follow-up nudge. If it lands, people answer each other, not the bot. If it doesn't land, the bot doesn't try again for hours.

Details of the moment:

- The little flourish line ("stirring the coals…") rotates and can be turned off per guild for a bone-dry delivery.
- The role tag appears only if that channel has pinging enabled *and* the role hasn't been tagged there in the last 24 hours. Otherwise the question posts un-pinged.
- Members join or leave the chat-revive role themselves through the existing self-role flow. Taking the role means "I like being summoned to restart conversation" — it should be pitched that way wherever roles are advertised.
- Questions never repeat within a month, and spicier questions only ever appear in channels marked for them. Each channel can have its own flavor mix — #deep-thoughts pulls from reflective prompts, #shitposting pulls from silly ones.

## When it fires (and when it refuses to)

**Fires when all of these are true:**

- The current silence is genuinely unusual for this channel at this time of day — several times longer than a normal gap, and longer than almost any lull this channel typically sees in this time band.
- The channel is normally alive right now. If this hour is typically dead for this channel, nothing happens.
- Humans have spoken since the last revive here.
- All the frequency protections below are clear.

**Refuses to fire when any of these hold (defaults, all adjustable):**

| Protection | Default behavior |
|---|---|
| Channel rest period | Once a revive fires in a channel, that channel rests 8 hours |
| Guild daily budget | No more than 3 revives per day across the whole server |
| Guild breathing room | At least 90 minutes between revives anywhere in the server |
| Quiet hours | Nothing fires overnight (midnight–8am server time by default) |
| Ping scarcity | Role tagged at most once per channel per day |
| Mods slowed the room | If a channel is in slowmode, the bot assumes that's intentional and stays out |
| Something's happening | No revives in a channel with an active event or ongoing game night |
| Not invited | Revive only operates in channels an admin explicitly enabled |

**New channels / new installs:** for the first couple of weeks, before the bot has learned a channel's rhythm, it runs in a conservative fallback mode — only firing after a long fixed silence, and only during daytime/evening hours. It quietly graduates to rhythm-aware behavior once it knows the room.

## The question bank

- Guild-owned and curated. Ships with a starter pack of ~60 questions across categories (general, deep, silly, spicy, photo, music, …) so it's useful the moment it's turned on.
- Admins and trusted members can add questions one at a time or in bulk, tag them by category, mark adult-only ones (which can only ever surface in NSFW channels), and retire anything that's gone stale.
- Every question carries a track record: how often it's been used and how much conversation it tends to generate. The picker favors proven sparkers and lets flops drift to the bottom — the bank self-tunes without anyone doing gardening.
- Attribution is kept (who contributed each question), opening the door to a light "your question revived chat" moment later if we want it.

## Admin experience

**First run:** a guided setup — pick the revive role (or create one), choose which channels are eligible and what flavor of questions each gets, confirm quiet hours and the daily budget. Five minutes, done.

**Day to day:** nothing. The steady state is that admins forget it exists and members occasionally enjoy a well-timed question.

**When they do check in, they can:**

- **Preview the brain.** A "would it fire right now?" check for any channel that explains, in plain language, the current lull, what the channel's normal rhythm looks like, which protection (if any) is holding it back, and which question it would choose. This is the trust-builder — admins can see exactly why it's quiet or why it spoke.
- **Fire it manually.** Post a revive on demand in any channel (respecting question selection but skipping the lull detection) — handy for kicking off an evening.
- **Tune the dials.** Everything in the protections table, plus per-channel overrides: rest period, question categories, ping on/off, a different role for a specific channel.
- **Review the scoreboard.** A stats view answering: how often are we reviving, how often does it actually work (did people start talking within the half hour?), which channels benefit most, and which questions are carrying the team vs. dead weight.

## How we'll know it's working

- **Revive success rate:** share of revives followed by real member conversation within 30 minutes. This is the headline metric; the feature exists to make this number high while firing rarely.
- **Scarcity held:** revives per channel per week stays low (roughly ≤3) without admins intervening.
- **Role health:** chat-revive role retention — people keep the role rather than shedding it, the clearest signal pings feel like an invitation rather than an interruption.
- **Zero embarrassments:** no double-posts, no overnight pings, no reviving a room mid-conversation. One violation of principle 2 or 4 costs more trust than fifty good revives earn.

## Explicitly out of scope for v1

- Web panel management screen (bank and settings are command-managed at launch; panel comes later)
- AI-generated or context-aware questions — v1 is bank-only so tone stays fully in the community's control
- Per-member ping preferences beyond the simple opt-in role
- Anything that replies to, reacts to, or follows up on its own revive

## Implementation notes (v1, as built)

- **Rhythm model:** 2-hour local bands; per-band median and p90 message gap
  learned from the last 60 days of `processed_messages` (human messages only).
  Auto-fire needs silence ≥ max(4× median, p90) for the current band **and**
  the band to be normally alive (≥ 20% of the channel's busiest band, min
  5 msgs/day). Bands with < 30 sampled gaps fall back to the whole-day profile.
- **Cold start:** < 14 days of channel history → fallback mode (fixed 6 h
  silence, fires 10:00–22:00 local only).
- **Success metric:** ≥ 3 human messages from ≥ 2 distinct people within
  30 minutes of the revive.
- **Ping scarcity** is a rolling 24 h per channel, not a calendar day.
- **Opt-in role:** the bot has no general self-role system, so
  `/revive optin-post` publishes a persistent join/leave button for the
  configured role (admins using another bot's role menu can skip it).
- **Manual `/revive fire`** skips every lull/frequency gate but keeps ping
  scarcity; manual revives still count toward the daily budget ledger.
- **Adult-only questions** are gated on Discord's channel age-restriction
  flag (`channel.is_nsfw()`), never on a bot-side toggle.
- Commands: `/revive setup · channel · check · fire · stats · flourish ·
  optin-post · question add|bulk|list|retire` (all `manage_guild` + mod-role).
