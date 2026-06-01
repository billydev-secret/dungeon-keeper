# AI Moderation — Feature Spec

LLM-assisted moderation backed by a local model. Two command groups share the same model and message-archive reader:

- **`/ai review | scan | channel | query`** — admin-only inspection commands. Reads the local message archive (no live Discord history fetch), assembles a tagged log with context around the target, runs it through the configured system prompt, and returns the analysis as an ephemeral reply.
- **`/watch add | remove | list`** — mod-level subscription to a member's posts. Every public message from a watched user is evaluated by the LLM; only messages tagged as rule violations are DM'd to the watchers. When the LLM is unavailable, **every** public message is relayed unfiltered.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/ai review user:<member> days:[1-30]=7` | Slash | Admin | Flag rule violations and concerning patterns in the member's recent messages |
| `/ai scan count:[10-200]=50` | Slash | Admin | Scan the last N archived messages in the current text channel / thread |
| `/ai channel question:<text> minutes:[1-1440]=60 channel:[#channel]` | Slash | Admin | Free-form Q&A over a channel's recent archive window |
| `/ai query user:<member> question:<text> days:[1-30]=14` | Slash | Admin | Free-form Q&A about one member's archive |
| `/watch add user:<member>` | Slash | Mod | Subscribe yourself to that member's posts |
| `/watch remove user:<member>` | Slash | Mod | Unsubscribe yourself |
| `/watch list` | Slash | Mod | Show everyone you're currently watching |
| AI config (read / write models / write prompts / clear prompt) | Web (dashboard) | Admin | Read or override the per-guild model and prompt for each command |
| AI prompt test | Web (dashboard) | Admin | Run the current prompt + model against arbitrary test input |
| Model status / source / reload | Web (dashboard) | Admin | Inspect or change the loaded model file |
| Guild-wide message query | Web (dashboard) | Moderator | Free-form question against the local archive, with optional author / channel / day filters |

The `/ai` group's `Manage Server` default-permission is a Discord client-side hint only — the real gate is the bot's admin check.

## Behaviour

### `/ai review`
Loads the target's last N days of activity from the local message archive. For each channel where the target posted, the reader pulls a window of messages and tags each one:

- `[TARGET]` — the target wrote it.
- `[REPLY→TARGET]` — someone replied to the target.
- `[TARGET REPLIED TO]` — the message the target replied to.
- `[CONTEXT]` — surrounding messages (a symmetric four-message window around each target line).

The total target-line count is capped (~200) so the context budget isn't blown out. Attachments and mentions are appended inline as ` [📎 ext]` and ` [@Name]` notes. The full tagged log goes to the LLM with the review system prompt; the response is replied ephemerally, split across multiple messages if it exceeds Discord's single-message cap.

If the target has no archived activity in the window, the reply still posts: "No messages found for {name} in the last {N} days."

### `/ai scan`
Same reader shape but scoped to the current text channel or thread and the most recent N messages (oldest-first after fetching). Uses the scan system prompt.

### `/ai channel`
Free-form question against a channel's recent archive window (in minutes rather than days). The user's question is prepended to the log as `Moderator question: …`. Uses the channel-query prompt.

### `/ai query`
Like `/ai review` but with a free-form question prepended. Uses the user-query prompt.

### `/watch add | remove | list`
- **add** — rejects bots, self, and DM-context invocations. Multiple mods can independently watch the same member. The reply explicitly warns when the LLM is unavailable: every public post will be relayed unfiltered until the model comes back.
- **remove** — drops the (watched, watcher) pair.
- **list** — shows every member the caller is currently watching, with departed-from-guild members rendered as bare ids.

Add and remove take effect immediately — no restart or delay.

### What watchers receive
On every public guild message from a watched user:

- **If the LLM is available**: the message is evaluated against the watch-check prompt (truncated to 400 characters, with the channel name and any NSFW-designation tag). Only messages the model tags as a rule violation are DM'd to the watchers, with the model's reason quoted. Any error during this check falls through to "treat as violation" — better to over-notify than to silently drop a flag.
- **If the LLM is unavailable**: every public message is relayed unconditionally. The `/watch add` reply explicitly warned the watcher about this fallback.

The DM carries the message content (or "[no text content]" if empty), any attachment URLs, an optional `⚠️ Rule concern: {reason}` line, and a footer with the author, guild, channel, and jump URL. Watchers with closed DMs are silently dropped.

The watch-check prompt has explicit high-threshold guidance ("the vast majority of messages are completely normal and friendly … your threshold must be HIGH") because every public message a watched member sends gets evaluated — a chatty false-positive rate would spam DMs.

### Web behaviour
The dashboard's prompt editor offers a **test** action that runs the current prompt + model against arbitrary input so admins can preview behaviour before saving. The moderator-facing guild-wide query is the same archive reader exposed as a one-off question with optional author and channel filters and a 500-message cap.

### LLM lifetime
Model loading is asynchronous and singleton: at most one model is loaded at a time. Phases are `idle → downloading → loading → ready` (or `error`). The model file is downloaded from HuggingFace on first start and cached locally; subsequent starts use the cache. Inference is serialised — concurrent commands queue behind each other rather than racing the model.

## Permissions

- The bot needs **Read Message History** wherever the archive is populated (the archive is filled live by [[events-spec]] — no extra Discord API call happens at command time).
- `/ai *` requires the bot's **admin** check.
- `/watch *` requires the bot's **mod** check; add and remove reject DMs.
- The DM relay needs each watcher to allow DMs from server members. Failures are logged and dropped.
- All dashboard endpoints require **admin** except the guild-wide query, which is **moderator**.

## User-visible errors

| When | The user sees |
|---|---|
| Non-admin runs `/ai *` | "You don't have permission to use this command." |
| Non-mod runs `/watch *` | "You don't have permission to use this command." |
| LLM not configured | "OLLAMA_BASE_URL is not set — AI features require a local Ollama instance." |
| `/ai *` invoked in DMs | "This command only works in a server." |
| `/ai scan` or `/ai channel` invoked in a voice / category / forum-root channel | "This command only works in text channels and threads." |
| `/watch add` on a bot | "You cannot watch bots." |
| `/watch add` on self | "You cannot watch yourself." |
| `/watch add | remove` in DMs | "This command must be used in a server." |
| Archive returned no rows | "No messages found for {name} in the last {N} days." (or equivalent) |
| LLM call returned an empty string | "No analysis returned." |
| Dashboard guild-wide query, LLM not configured | "LLM is not configured." |
| Dashboard guild-wide query, bot can't resolve the guild | "Guild not available" |
| Dashboard prompt update, unknown key | "Unknown prompt key: {key}" |
| Dashboard model reload, no source set | "No model source configured — set model path and HuggingFace details first." |

The "OLLAMA_BASE_URL" wording is legacy — there is no such environment variable. The check is whether a model file or HuggingFace source has been configured.

## Non-goals

- **No live Discord history fetch.** Every command reads the local archive. A gap in the archive is a gap in `/ai` output; no extra API calls happen at command time.
- **No multi-model serving.** Per-command "model" overrides change which model name the dashboard reads back; they do not swap weights at inference time. Exactly one model is loaded at a time.
- **No streaming.** Each call is one blocking inference returning the whole completion.
- **No prompt-injection mitigation** beyond a 400-character per-message truncation (newlines collapsed to spaces). Message content is passed verbatim into the user role.
- **No public-channel auto-flagging.** The watch check is opt-in via `/watch`. Server-wide auto-moderation lives elsewhere ([[post-monitoring-spec]] for spoiler enforcement, [[wellness-guardian-spec]] for wellness-tagged users).
- **No per-channel watch scoping.** A watch fires across every guild channel the watcher can read.
- **No conversational memory.** Each call is one-shot; no follow-ups, no transcripts, no tool calling.

## Configuration

Per-guild keys an admin sets via the dashboard:

- **Mod model / wellness model** — the default model for moderation commands and for the wellness encouragement prompt respectively.
- **Per-command model override** — review, scan, user query, channel query, watch check, and wellness encouragement. Empty means fall through to the matching default above.
- **Per-command system prompt** — same six keys. Empty means fall through to the hard-coded default.

`ai_config` readers all take the active guild id, so dashboard writes round-trip to dashboard reads (without this, edits silently disappeared because writes landed at the guild and reads landed at the global slot).

Global-only (not per-guild) — admins set these once for the host:

- **Model file path** — the local GGUF file.
- **HuggingFace repo + filename** — used to download the model on first start if the local file is missing.

Runtime tuning (context length, GPU layers, threads, batch size) is read once at load time from environment variables — not user-tunable via the dashboard.

## Stored data

Per guild: the set of (watched, watcher) subscriptions — one row per pair, so multiple mods can independently watch the same member. There is no purge job; entries persist across watchers leaving the guild (they just stop receiving DMs).

Per guild, in the shared config table: the model defaults, the per-command model overrides, and the per-command prompt overrides. The wellness prompt and wellness model entries are co-owned with [[wellness-guardian-spec]] — the registry lives here, the consumer lives there.

Globally, in the shared config table: the model path and HuggingFace source.

Read-only consumers: the message archive (and its attachment + mention children) populated by [[events-spec]]. The AI cluster never writes to those tables — every `/ai *` command and the dashboard's guild-wide query read from them.

On disk: the model file itself, downloaded from HuggingFace on first start and persisted across restarts. The file is cache-grade — re-downloadable from the configured source.

In-memory only: the watched-users map (populated once at startup, hot-updated by `/watch add | remove`), and the loaded model singleton.
