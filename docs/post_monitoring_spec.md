# Post Monitoring — Feature Spec

A side-effect layer that runs message-content checks as part of the normal `on_message` pipeline. Two checks live here, both backed by the shared image classifier ([[nsfw-classifier-spec]]): **spoiler enforcement**, which removes explicit images posted without a spoiler tag in designated channels, and **SFW nudity prevention**, which removes explicit images from channels Discord doesn't age-gate. Sections still carrying v1 scope are marked **v1**.

## Commands

Post monitoring has **no commands** of its own — no slash, no context menu, no web routes. It is invoked from gateway message listeners (see [[events-spec]]).

## Behavior

### Spoiler enforcement (v1)

In a channel designated as "images must be spoilered":

1. A member with a bypass role posts an image — nothing happens. Bypass roles are configured per-guild.
2. A member without a bypass role posts an image that's already marked spoiler — nothing happens.
3. A member without a bypass role posts a non-spoilered image — the bot classifies it and deletes the message only if it is **explicit**, posting an inline reminder ("Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler.") that self-destructs after 5 seconds.

The check only fires on image attachments (`.png/.jpg/.jpeg/.gif/.webp`). Non-image attachments and text-only messages are ignored.

### Content-awareness

Enforcement used to delete *every* unspoilered image, so a meme, a screenshot or a cat photo was removed exactly like explicit content. It now consults the shared classifier ([[nsfw-classifier-spec]]) and deletes only what actually qualifies:

| classifier says | outcome |
|---|---|
| explicit | deleted, as before |
| not explicit | **left alone** — the false positive this narrowing exists to fix |
| could not read it | deleted — unreadable is treated as maybe-explicit, so a CDN failure falls back to the old rule rather than opening a hole |

A spoilered image is never classified at all — it already satisfies the rule, so there is nothing to decide and no reason to fetch it.

On a message carrying several images, one innocent attachment does not clear the message: each unspoilered image is judged on its own, and any explicit one triggers deletion.

If no classifier is wired up, the original behavior applies unchanged — every unspoilered image goes. That is the deliberate fallback, not a degraded mode.

Note the timing this adds: a download plus inference sits between the image appearing and the bot acting, so an offending image is visible ~1–2s longer than before. This does not make Discord's push-notification preview any worse — deletion has always happened after the message posts, so that preview has always escaped — but it does not fix it either.

Webhooks, bots, and any author the bot can't resolve as a guild member skip enforcement entirely.

When a message is deleted by spoiler enforcement, the rest of the `on_message` pipeline short-circuits — no XP, no interaction tracking, no wellness checks fire on the deleted message.

### SFW nudity prevention

Removes explicit images posted in channels Discord does **not** age-gate. Where spoiler enforcement asks "was this tagged?", this asks "does this belong here at all?".

It ships **off**. The mode is a dashboard setting with three states, and deploying the code changes nothing until someone picks one:

| mode | what happens |
|---|---|
| `off` (default) | nothing — no download, no inference, no cost |
| `log` | reports what it *would* have removed to the mod-log channel, deletes nothing |
| `enforce` | removes the image, DMs it back to the poster, posts a brief public notice, and records the call to the mod-log channel |

`log` is the shakedown mode: it measures real accuracy against real traffic before anything is lost. Running it for a few days before switching to `enforce` is recommended.

**This check fails open.** An image the classifier could not read is left alone — the exact opposite of spoiler enforcement, and deliberately so. There, a failed read risks explicit content staying up in a channel that expects spoilers; here, acting on a failed read would delete an innocent member's photo. It also runs at the stricter SFW threshold ([[nsfw-classifier-spec]]), because it is the only check in this module that destroys content.

Exempt from it entirely:

- **Bots and webhooks.** The Guess game uploads explicit images itself (`SPOILER_guess_full.jpg` and friends); without this exemption the bot would delete its own game content in any Guess channel not marked NSFW.
- **Age-gated channels** — explicit content belongs there, and spoiler-required channels have their own rule.
- **Channels on the exemption list**, configured per guild.
- **Members holding a bypass role** — the same list spoiler enforcement uses.

On removal the image is read back and DM'd to the poster *before* the delete, while the attachment is still guaranteed fetchable: a wrong call should cost a member their post, not their file. Closed DMs are common and don't block removal. A mod-log failure is caught and logged — the audit trail failing must never change the outcome for the member or abort the rest of the `on_message` pipeline.

Messages with no image attachment never reach the policy load, so the DB isn't touched on the overwhelming majority of traffic.

## Permissions

- **User-side**: none. Enforcement is automatic.
- **Bot-side (v1)**: **Manage Messages** and **Send Messages** in every channel designated as spoiler-required. Without either, the offending image survives and the failure is logged silently. SFW nudity prevention needs the same two permissions in every channel it covers, plus the ability to DM the poster (optional — a closed DM only means the image isn't returned).

## User-visible errors

| When | The user sees |
|---|---|
| Non-spoilered image is deleted | Inline reply (auto-deletes after 5s): "Beep Boop - friendly bot helper: Images in this channel must be marked as spoiler." |
| Explicit image removed from a SFW channel | Inline reply (auto-deletes after 8s) explaining the removal and that a copy was DM'd back, plus the DM itself carrying the image |
| Bot lacks Manage Messages | No user-facing message — the image survives, the failure is logged operator-side |

## Non-goals

- **No embed / linked-image classification.** Only uploaded attachments are fetched and classified; images that live on external hosts are out of scope for the reasons given in [[nsfw-classifier-spec]].
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
