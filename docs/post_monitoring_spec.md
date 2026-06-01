# Post Monitoring — Feature Spec

A side-effect layer that runs message-content checks as part of the normal `on_message` pipeline. v1 ships **spoiler enforcement only** — admins designate channels where every image attachment must be marked as a spoiler, and the bot deletes any non-spoilered image with a brief inline reminder. The module is structured to grow as more realtime content checks come online; sections that will expand are marked **v1**.

## Commands

Post monitoring has **no commands** of its own — no slash, no context menu, no web routes. It is invoked from gateway message listeners (see [[events-spec]]).

## Behaviour

### Spoiler enforcement (v1)

In a channel designated as "images must be spoilered":

1. A member with a bypass role posts an image — nothing happens. Bypass roles are configured per-guild.
2. A member without a bypass role posts an image that's already marked spoiler — nothing happens.
3. A member without a bypass role posts a non-spoilered image — the bot deletes the message and posts an inline reminder ("Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler.") that self-destructs after 5 seconds.

The check only fires on image attachments (`.png/.jpg/.jpeg/.gif/.webp`). Non-image attachments and text-only messages are ignored.

Webhooks, bots, and any author the bot can't resolve as a guild member skip enforcement entirely.

When a message is deleted by spoiler enforcement, the rest of the `on_message` pipeline short-circuits — no XP, no interaction tracking, no wellness checks fire on the deleted message.

## Permissions

- **User-side**: none. Enforcement is automatic.
- **Bot-side (v1)**: **Manage Messages** and **Send Messages** in every channel designated as spoiler-required. Without either, the offending image survives and the failure is logged silently.

## User-visible errors

| When | The user sees |
|---|---|
| Non-spoilered image is deleted | Inline reply (auto-deletes after 5s): "Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler." |
| Bot lacks Manage Messages | No user-facing message — the image survives, the failure is logged operator-side |

## Non-goals

- **No NSFW image classification.** Spoiler enforcement checks Discord's user-set spoiler flag, not pixel content.
- **No link / URL scanning (v1).** Wellness Guardian has a separate keyword pipeline — see [[wellness-guardian-spec]].
- **No edit handling.** A non-spoilered image edited after posting isn't re-evaluated.
- **No file scanning of non-image attachments.** PDFs, archives, executables pass through untouched.
- **No incident audit log.** Deletions are logged operator-side only, not into the incident pipeline ([[reporting-spec]]).

## Configuration

Post monitoring owns no per-guild config keys. The two collections it consumes are owned by [[events-spec]]:

| Key | Purpose |
|---|---|
| Spoiler-required channels | Channels where images must be spoilered |
| Bypass role ids | Roles exempt from spoiler enforcement |

Future content checks may introduce their own keys; until then admins manage these from the events config surface.

## Stored data

None (v1). Spoiler enforcement is stateless — deleted messages are not recorded. Future content checks that need an audit trail will document their persistence here.
