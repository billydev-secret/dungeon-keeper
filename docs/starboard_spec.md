# Starboard & Quote — Feature Spec

Two related features that ride together. **Starboard** reposts highly-reacted messages to a dedicated channel. **Quote** is a right-click context menu that renders a message's text on top of the author's avatar and posts the card back to the channel.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/starboard channel <channel>` | Slash | Mod | Set the channel where starred messages get reposted |
| `/starboard threshold <count>` | Slash | Mod | Set the minimum star count to repost (1–100) |
| `/starboard emoji <emoji>` | Slash | Mod | Set the reaction emoji that triggers the board (unicode or custom) |
| `/starboard toggle` | Slash | Mod | Enable / disable the starboard |
| `/starboard exclude <channel>` | Slash | Mod | Add a channel to the exclusion list |
| `/starboard unexclude <channel>` | Slash | Mod | Remove a channel from the exclusion list |
| `/starboard status` | Slash | Mod | Show current config |
| `Quote` | Message context menu | Everyone | Generate a quote card from the clicked message |
| Web `/api/config/starboard` | Web (dashboard) | Admin | Edit channel, threshold, emoji, enabled flag, exclusion list |

Bot perms required: **Send Messages** + **Embed Links** in the starboard channel; **Read Message History** in source channels for embed building; **Attach Files** in the channel where Quote cards post.

## Behaviour

### Starboard: reaction → repost

When a member reacts with the configured emoji, the bot recounts effective stars on that message. Self-stars don't count — the author can react but isn't tallied. Once the count crosses the threshold, the bot posts an embed (author header, channel, message content truncated to 2 000 chars, first image attachment, jump link, footer `<emoji> <count>`) to the starboard channel.

Subsequent reactions just edit the existing embed's footer. Reaction removals decrement and re-edit. If the starboard message has been hand-deleted, the bot drops the stale record so the next reaction posts a fresh one.

**NSFW leak guard:** If the source channel is NSFW and the starboard channel is not, the repost is silently suppressed. This prevents the starboard from leaking age-gated content to members who lack access to the source.

Reactions in the exclusion list, reactions on the starboard's own posts, and reactions with non-matching emojis are ignored.

### Quote: context menu → card → post

Right-click a non-empty, non-system message and pick **Quote**. The bot shows an ephemeral picker (theme select, font select, **Generate** / **Cancel**) for 120 s. On Generate, the bot fetches the author's avatar, renders the quote card (text laid over a colour-graded avatar with vignette), and shows a preview with **Post** / **Cancel**. On Post, the card goes to the channel publicly, then the bot auto-reacts to its own post with the guild's starboard emoji — so a beloved quote can itself reach the starboard.

Each quote post writes an audit row (who quoted whom, where, theme/font used).

## Permissions

- All `/starboard` config commands require Mod.
- **Quote** has no user-side gate — anyone who can read the message can quote it. Discord's own visibility rules are the ACL.
- The web config endpoint requires the `admin` perm.

## User-visible errors

| When | The user sees |
|---|---|
| Bot lacks Send Messages or Embed Links in the chosen starboard channel | "I'm missing **X, Y** in #ch. Grant those permissions and try again." |
| `/starboard emoji` value is empty | "Emoji cannot be empty." |
| `/starboard emoji` value doesn't parse as an emoji | "That doesn't look like a reaction emoji…" |
| Quote on a system message or empty message | Ephemeral rejection |
| Avatar fetch fails | "Couldn't fetch the author's avatar." |
| Card renderer fails | "Failed to render the quote card." |

## Non-goals

- **Per-channel thresholds.** One threshold per guild. Channels are binary in/out via the exclusion list.
- **Multiple trigger emojis.** Exactly one per guild. Mods can swap; old reactions under the previous emoji become inert.
- **Stat leaderboards.** Starboard does not track "most-starred user" or "most-starred channel".
- **Custom quote themes.** Themes and fonts are fixed; mods cannot add new ones.
- **Quote dedup.** The same message can be quoted again — cards are creative artifacts, not records.

## Configuration

| Key | Default | Format |
|---|---|---|
| Starboard channel | unset | guild text channel |
| Threshold | `3` | int, 1–100 |
| Trigger emoji | `⭐` | unicode or custom emoji |
| Enabled | on | on / off |
| Excluded channels | empty | per-guild set |

## Stored data

Three starboard tables (config, per-message post records, per-reactor records) plus a quote audit log. All per-guild, all keyed on Discord IDs. Self-stars are tracked but excluded at count time. No filesystem cache — quote cards are produced in memory and uploaded directly.
