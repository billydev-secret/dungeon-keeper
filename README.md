# Dungeon Keeper

A Discord bot for communities that have outgrown a handful of utilities — one
bot covering moderation, member safety, XP and analytics, voice rooms, music, a
coin economy, a large social-games suite, and the everyday rituals that keep a
server feeling alive.

The organizing idea: **configuration lives on a web dashboard, not in Discord.**
Admins get over 130 dashboard pages instead of a swamp of config subcommands,
and the Discord surface stays what members actually want it for — playing,
opting in, and customizing their own corner of the server.

## What it does

**Moderation with a memory.** Jail a member and their roles come back on
release; warn them and it lands in one profile beside their jail history and
tickets. An AI reviewer and a passive rules watcher pre-screen public chat into
a human-reviewed queue rather than acting on their own.

**Safety that holds up between members.** Discord's block stops someone
messaging you directly, but not reaching you through a bot. The no-contact list
closes that gap across every surface that can carry a message between two
people — and does it invisibly, so the blocked party can't tell the refusal
from an ordinary outcome. Age-gated content keys off Discord's own channel
age-gate, never a bot-side toggle, and message content is not stored by
default.

**A games night that runs itself.** Seventeen party games (Would You Rather,
Never Have I Ever, Truth or Dare, Story Builder, anonymous AMA…), five
head-to-head and elimination duels played for auto-reverting nickname stakes,
American-style mahjong, and a season-long NFL survival pool where picks stay
secret until the weekly reveal.

**An economy with somewhere for the money to go.** Members earn coins from
chatting, voice, games and daily quests, then spend them renting role colors,
emoji slots and server-defined perks, or lose them in a nine-game casino and a
daily prediction market on the server's own statistics.

**Rituals that fill quiet rooms.** Anonymous whispers, confessions with
anonymous replies, scheduled pen-pal pairings in private channels, a lull
watcher that drops a conversation starter when a channel goes quiet, a photo
challenge on its own schedule, and rotating activity rooms that take turns so a
server of thirty channels doesn't feel like a ghost town.

**Analytics that answer real questions.** Twenty-seven cached report panels:
retention curves, activity heatmaps, churn risk, a drag-and-zoom connection
graph you can replay week by week to watch friend groups form — and honest ones
like whether anybody actually turns up when you ping a role.

## Getting started

- **Install and deploy** — [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) covers
  prerequisites, the Discord application, `.env`, running as a service, and the
  optional music, dashboard and LLM stacks. First-time server configuration is
  done on the dashboard once the bot is up.
- **Every feature, in full** — [docs/features.md](docs/features.md).
- **Day-to-day reference** — `/help` inside Discord is permission-aware, and the
  dashboard's Help panel carries the illustrated manual. Both stay current
  automatically; the docs here don't try to compete with them.

## Development

Contributor guide: [docs/design_guide.md](docs/design_guide.md) — how a feature
gets built here, the coding standards, what tests ship with it, and the
pre-commit gate (`python scripts/gate.py`). [docs/INDEX.md](docs/INDEX.md)
classifies every spec, because not all of them describe features that exist.
