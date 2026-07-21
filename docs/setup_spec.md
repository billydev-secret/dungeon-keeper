# Setup — Feature Spec

One-shot `/setup` wizard that walks a server admin through the six core configuration questions for the moderation system (mod/admin roles, jail and ticket categories, log and transcript channels). It creates nothing in the server — no channels, no roles — it only writes per-guild config rows that the jail/ticket features read (see `dungeon_keeper_jail_ticket_spec.md`).

> Not to be confused with `DUNGEON_KEEPER_TEST_ENV_SPEC.md`, which is an unrelated scope (a test environment with beta puppets), not this command.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/setup` | Slash | Administrator (default perms) + runtime admin check | Walk through the six-step role/category/channel wizard |

Guild-only. The runtime check (`ctx.is_admin`) passes for members with the Discord **Administrator** permission or any configured `admin_role_ids` role — so admins configured by a previous run can re-run it.

## Behavior

### Delivery: DM first, channel fallback

The command ACKs immediately (ephemeral defer), then tries to **DM the invoking admin** the wizard. If the DM fails (DMs closed, or any HTTP error), it falls back to an **ephemeral in-channel** wizard instead, prefixed with "⚠️ I couldn't DM you — your DMs may be closed. Let's do it here instead." On DM success the channel just shows "📬 Check your DMs — I've sent the setup questions there." Both wizards use the guild's branding accent color and share one source of truth for step wording (`jail/logic.py:_SETUP_STEPS`).

### The six steps

| Step | Question | Config key | Picker | Multi? |
|---|---|---|---|---|
| 1 | Moderator roles | `mod_role_ids` | role | yes |
| 2 | Admin/senior-staff roles (escalations, warning alerts) | `admin_role_ids` | role | yes |
| 3 | Category for jail channels | `jail_category_id` | category | no |
| 4 | Category for ticket channels | `ticket_category_id` | category | no |
| 5 | Audit-log channel | `log_channel_id` | text channel | no |
| 6 | Transcript channel (may equal the log channel) | `transcript_channel_id` | text channel | no |

Every step is skippable: advancing without a selection leaves any existing stored value untouched, so re-running `/setup` never clobbers config by accident. The final button reads **Finish** (earlier steps: **Next →**); completion shows a "Setup Complete — All settings saved. Use `/config` to adjust later." embed.

### DM wizard specifics

Native role/channel selects render empty in a DM, so the DM wizard builds plain string-selects by hand from the guild captured at `/setup` time:

- Options are paged 25 per select (Discord's cap) with ◀ / ▶ page buttons and a page counter; role picks accumulate **across pages** (deselecting on a page removes only that page's picks).
- Role steps are capped at **10** accumulated picks; `@everyone` is excluded; roles are listed high-to-low by position, channels/categories in positional order.
- Selections persist only when the admin presses Next/Finish for that step.
- If a step has no candidates (e.g. a server with no categories yet) a disabled "Nothing to choose — skip with Next →" placeholder is shown.
- The embed footer shows "Configuring: {server name}" since a DM has no guild context.
- View timeout: 10 minutes.

### In-channel wizard specifics

Uses Discord's native RoleSelect / ChannelSelect components. Unlike the DM wizard, each selection **saves immediately** (with an ephemeral "✅ Set **{key}** → …" confirmation); the Next button only advances the page. Role steps allow up to **5** picks here (native component limit as configured). View timeout: 5 minutes per step.

## User-visible errors

| When | The user sees |
|---|---|
| Invoker fails the runtime admin check | "Administrator only." |
| DM delivery fails | "⚠️ I couldn't DM you — your DMs may be closed. Let's do it here instead." + in-channel wizard |

There is no validation that the bot can actually see/post in the selected channels or create channels under the selected categories — a bad pick surfaces later when the jail/ticket features try to use it.

## Configuration

`/setup` **is** the configuration writer; it takes no options itself. Values it writes can also be adjusted afterwards via `/config`.

## Stored data

Rows in the `config` table (`guild_id`, `key`, `value`), upserted per guild:

- `mod_role_ids`, `admin_role_ids` — comma-joined role ID lists
- `jail_category_id`, `ticket_category_id` — single category ID
- `log_channel_id`, `transcript_channel_id` — single text-channel ID

Each write invalidates the in-memory per-guild config snapshot so readers see it immediately. No other state; the wizard views themselves are in-memory only.
