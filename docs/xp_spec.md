# XP — Feature Spec

A leveling system for Discord activity. Four independent sources push positive XP into a single per-member ledger: text messages (with a reply bonus), voice-channel participation, reactions received on image posts, and reactions *given* to other members' messages. A handful of anti-grind multipliers attenuate text XP before it lands. Level is a quadratic function of total XP. XP only goes up — there is no decay.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/xp_leaderboards timescale:[hour\|day\|week\|month\|year\|alltime]` | Slash | Everyone (server only, ephemeral) | Per-source top-5 for the chosen window plus the caller's own rank. Defaults to `alltime` |
| `/xp_give member:<member>` | Slash | Mod, or a user in the per-guild grant-allowlist | Award a flat manual grant (default 20 XP) to a member; public confirmation |
| Message activity | Listener | n/a | Text messages earn XP; replies to a human earn a bonus. See [[events-spec]] |
| Image-reaction received | Listener | n/a | When a member's qualifying image post gets a reaction, the author earns a flat reaction-XP stipend. See [[events-spec]] |
| Reaction given | Listener | n/a | When a member reacts to *another* member's message, the reactor earns a flat stipend, once per message ever. See [[events-spec]] |
| Voice tick | Background loop | n/a | Members in a qualifying voice channel earn XP per completed interval |

The dashboard exposes the leaderboard and a time-to-level histogram (any level 2–100), plus an admin config panel for the XP coefficients and the role/channel ids. See [[reporting-spec]] for the dashboard wrapper.

## Behaviour

### Sources

- **Text message XP**: every qualified word in a non-bot, non-system message earns a small per-word amount. Replying to another human adds a flat reply bonus. Qualified words exclude URLs, custom Discord emoji, `:shortcode:` emoji, tokens shorter than 3 characters, and tokens with no alphanumerics. URL-only or emoji-only messages award nothing.
- **Voice XP**: every completed voice interval (default 60 seconds) in a qualifying channel pays a flat amount. A channel qualifies when it has at least 2 non-bot humans and is not the guild's AFK channel. The qualification clock resets to zero when the human count drops below the threshold.
- **Image-reaction XP**: when a non-bot reactor adds any reaction to a message whose author posted a non-spoilered image, the **author** receives a flat stipend. The reactor gets nothing. Self-reactions and bot reactions don't pay out.
- **Reaction-given XP**: when a non-bot member reacts to *another* member's message, the **reactor** receives a flat stipend (default 0.34, coeff `xp_coeff_reaction_given_xp` — double the image-react rate). Awarded at most **once per (message, reactor) ever** via a dedup table, so react/unreact can't farm. No self-reactions, no bots. This is the same event that feeds the economy's XP→currency conversion (see [[economy-spec]] §3.2).

### Anti-grind multipliers (text only)

Three multipliers stack on the base text+reply award before it lands:

1. **Cooldown** — three banded thresholds (default <10s → 0.35×, <30s → 0.6×, <60s → 0.85×; ≥60s → no penalty). The first message in a session is full credit.
2. **Duplicate-message** — if the normalised content matches the member's previous message, the award is multiplied by 0.2.
3. **Pair-streak** — when two members alternate in a channel for 4+ consecutive turns, subsequent messages from either are multiplied by 0.5. Resets when a third person speaks.

Voice and image-reaction XP are flat — no multipliers.

### Level model

XP required for level N is `factor × (N − 1)²`. Default factor 15.6 puts level 2 at ~16 XP, level 5 at ~250 XP, level 10 at ~1260 XP. The stored level is recomputed from total XP on every read — changing the curve factor takes effect on the next read, not retroactively for past announcements.

Crossing level 5 (the configured role-grant level) grants the level-5 role and posts an announcement to the level-5 log channel. Intermediate level-ups post to the level-up log channel (one embed per crossed level). Bulk backfill awards grant the role but skip the announcement embed.

**Announcing owed level-ups.** What has actually been *announced* is tracked separately from `level`, via an `announced_level` column that advances only when a level-up is genuinely announced (never by an award). This closes a gap where XP credited with no Discord handle in scope — quest payouts are the live case — moved a member up a level with nobody to announce it, and the next ordinary award then computed its start level from the already-credited total, saw `old == new`, and lost the level-up for good. Now a level won on a silent path stays *owed* and is delivered on the member's next ordinary award. Paths that credit XP but deliberately announce nothing — bulk backfill, the XP recompute script, and migration 075's one-time seed — catch `announced_level` up to `level` for the members they touch, so a deploy or replay never floods the channel with a member's whole level history.

### Leaderboards

`/xp_leaderboards` shows four separate top-5 boards — Text, Replies, Voice, Image Reacts — for the selected timescale, each with median / std dev and the caller's own standing. Manual grants are recorded for audit but **excluded** from leaderboards. If a guild has totals but no event ledger (data predating the ledger), the embed surfaces a note and continues.

## Permissions

- `/xp_leaderboards` — open to everyone; rejects DMs.
- `/xp_give` — caller must be a mod **or** their id is in the guild's grant-allowlist. Rejects DMs, bots, and self-grants.
- Dashboard XP routes require admin.
- The level-5 role grant requires the bot to have **Manage Roles**. Failures log a warning and skip the role assignment but don't reverse the XP.

## User-visible errors

| When | The user sees |
|---|---|
| `/xp_leaderboards` used in DMs | "This command only works in a server." |
| `/xp_leaderboards` caller not resolvable as a member | "Could not resolve your member record in this guild." |
| `/xp_leaderboards` with no XP recorded yet | Empty-state embed: "No tracked … XP yet" for each source |
| `/xp_leaderboards` when only legacy totals exist | "Existing XP totals predate the event ledger. New text and voice XP will appear here going forward." |
| `/xp_give` by non-authorised user | "You don't have permission to use this command." |
| `/xp_give` against a bot, self, or in DM | Per-case ephemeral guard message |

## Non-goals

- **No XP decay.** No time-based, inactivity-based, or moderation-based subtraction.
- **No XP loss on message delete or edit.** The award persists in the ledger.
- **No reaction-emoji weighting.** Every reaction on a qualifying image pays the same flat amount.
- **No leaderboard surface for manual grants.** Grants are recorded for audit only.
- **No DM XP.** Both sources require a guild context.
- **No bot XP, no self-reaction XP.** Bot authors and bot reactors never earn.
- **No retroactive level-5 announcement when a backfill crosses the threshold.** The role lands; the embed doesn't.
- **No per-channel rate caps beyond the channel-exclusion list.** A channel is either fully eligible or fully muted.

## Configuration

Per-guild settings, all editable from the dashboard XP panel:

- **Algorithm coefficients** — per-word XP, reply bonus, image-reaction stipend, reaction-given stipend, the three cooldown thresholds and their multipliers, duplicate-message multiplier, pair-streak threshold and multiplier, voice-award amount, voice-interval seconds, voice-minimum-humans, manual-grant amount, level-curve factor.
- **Level-5 role** — the role granted on reaching level 5.
- **Level-up log channel** — where per-level-up embeds post.
- **Level-5 log channel** — where the level-5 milestone embed posts (can match the level-up channel; the role-grant level then de-duplicates).
- **Grant-allowlist** — extra user ids (beyond mods) allowed to invoke `/xp_give`.
- **Channel exclusion list** — channels (and threads whose parent is in the list) where text and image-reaction XP are suppressed. The AFK voice channel is implicitly excluded.

Two internal knobs — the voice-tick poll period and the role-grant level itself — are pinned in code and not exposed.

## Stored data

Per-guild and per-member: a totals row (total XP, cached level, the highest level actually announced, last-message timestamp and fingerprint for the cooldown / duplicate multipliers), an append-only event ledger tagged by source (text, reply, voice, image-react, reaction-given, grant) with optional channel id, a per-(message, reactor) dedup table backing the once-ever reaction-given award, live voice-session state (current channel, qualifying-since timestamp, intervals already paid), a last-activity row for inactivity reports, a processed-messages ledger for backfill idempotency, and an append-only role-event audit (every grant and removal the bot sees, not just XP rewards). The pair-streak state lives only in memory and resets on bot restart. No DM data is ever stored.
