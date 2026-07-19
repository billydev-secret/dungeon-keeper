# AI Moderation — Feature Spec

LLM-assisted moderation backed by a local model. Three systems share the same model infrastructure and message archive:

- **`/ai review | scan | channel | query`** — on-demand inspection commands. Reads the local message archive, assembles a tagged log with context, runs it through the configured system prompt, and returns the analysis ephemerally.
- **`/watch add | remove | list`** — opt-in per-user subscription. Every public message from a watched member is evaluated by the LLM; only messages tagged as rule violations are DM'd to the watcher. When the LLM is unavailable, every public message is relayed unfiltered.
- **Rules Watch** — passive all-channel monitor. Every public message is pre-screened by cheap heuristics; those that pass are evaluated by a recall-leaning guard model and scored against layered context signals. Flags are routed to a human-reviewed priority queue. Every confirm or dismiss a moderator makes becomes a labeled training example.

## Commands

| Command | Type | Permission | Purpose |
|---|---|---|---|
| `/ai review user:<member> days:[1-30]=7` | Slash | Admin | Flag violations and patterns in a member's recent messages |
| `/ai scan count:[10-200]=50` | Slash | Admin | Scan the last N archived messages in the current channel / thread |
| `/ai channel question:<text> minutes:[1-1440]=60 channel:[#channel]` | Slash | Admin | Free-form Q&A over a channel's recent archive window |
| `/ai query user:<member> question:<text> days:[1-30]=14` | Slash | Admin | Free-form Q&A about one member's archive |
| `/watch add user:<member>` | Slash | Mod | Subscribe yourself to that member's posts |
| `/watch remove user:<member>` | Slash | Mod | Unsubscribe yourself |
| `/watch list` | Slash | Mod | Show everyone you're currently watching |
| `/rules-watch digest` | Slash | Mod | Post a summary of all unlabeled digest-tier events |
| `/rules-watch stats` | Slash | Mod | Show event counts, false-positive rate, and signal firing rates |
| `/rules-watch label <event_id> <verdict>` | Slash | Mod | Manually label a digest-tier event |
| Rules Watch enable/disable + alert channel | Web | Admin | Toggle passive monitoring and set the immediate-alert channel (dashboard's Rules Watch config panel — replaced the retired `/rules-watch enable`/`disable`/`set-channel` commands) |
| AI config (models / prompts / clear) | Web | Admin | Read or override the per-guild model and system prompt for each command |
| AI prompt test | Web | Admin | Run the current prompt + model against arbitrary input |
| Model status / source / reload | Web | Admin | Inspect or change the loaded model file |
| Guild-wide message query | Web | Moderator | Free-form question against the local archive with optional filters |
| Rules Watch alert queue | Web | Moderator | Review flagged events; Confirm / Dismiss with inline label buttons |
| Rules Watch label stats | Web | Moderator | Label counts, false-positive rate, events by tier and rule |

---

## Behavior

### `/ai review`
Loads the target's last N days of activity from the local message archive. For each channel where the target posted, a window of messages is fetched and each line is tagged:

- `[TARGET]` — the target wrote it.
- `[REPLY→TARGET]` — someone replied to the target.
- `[TARGET REPLIED TO]` — the message the target replied to.
- `[CONTEXT]` — surrounding messages (a symmetric four-message window around each target line).

The total target-line count is capped (~200). Attachments and mentions are appended inline as ` [📎 ext]` and ` [@Name]`. The tagged log goes to the LLM with the review system prompt; the response is replied ephemerally, split across multiple messages if needed.

### `/ai scan`
Same reader shape but scoped to the current text channel or thread and the most recent N messages (oldest-first after fetching). Uses the scan system prompt.

### `/ai channel`
Free-form question against a channel's recent archive window (in minutes rather than days). The moderator's question is prepended to the log. Uses the channel-query prompt.

### `/ai query`
Like `/ai review` but with a free-form question prepended. Uses the user-query prompt.

### `/watch add | remove | list`
- **add** — rejects bots, self, and DM-context invocations. Multiple mods can independently watch the same member. The reply explicitly warns when the LLM is unavailable.
- **remove** — drops the (watched, watcher) pair.
- **list** — shows every member the caller is currently watching; departed members appear as bare IDs.

On every public guild message from a watched member:

- **LLM available** — the message is evaluated by the watch-check prompt (400-character truncation, newlines collapsed, channel name and NSFW tag included). Only messages the model tags as a rule violation are DM'd. Any error during the check falls through to "treat as violation" — better to over-notify than silently drop a flag.
- **LLM unavailable** — every public message is relayed unconditionally.

The DM carries the message content, attachment URLs, an optional `⚠️ Rule concern: {reason}` line, and a footer with author, guild, channel, and jump URL. Watchers with closed DMs are silently dropped. The watch-check prompt has explicit high-threshold guidance because every public post from a watched member is evaluated — a chatty false-positive rate would spam DMs.

### Rules Watch — passive monitor

Enabled per guild from the web dashboard's Rules Watch config panel. Fires on every public guild message from a non-bot user in any text channel or thread.

#### Pre-filter gate
The LLM is only called when at least one cheap heuristic fires:
- VADER compound score (already computed by the events listener) < −0.25
- Message content contains a boundary-token keyword
- A slur or identity attack is detected lexically
- The author has sent 3+ consecutive directed messages to the same user with no reply

This gate is hardcoded in the first implementation and will be tuned against historical data in a future pass (see §5a of `rules_watch_cog.md`).

#### Guard model
A recall-leaning conversation window prompt — the opposite disposition from the watch-check prompt. The guard model is *meant* to flag generously; false positives cost a moderator glance, false negatives cost a missed violation. It receives the last 8 messages in the channel (oldest first) and returns structured JSON:

```json
{"verdict": "flag", "rule": "2", "reason": "brief reason", "confidence": 0.87}
```

On any parse failure the result is treated as `ok`.

#### Context signals
When the guard flags a message, the scorer computes up to eight context signals before assigning a priority. Signals never suppress a flag — they can only move it up or down the queue.

**Down-weights (multiplicative):**
- Mutual interaction history — established, reciprocal rapport reduces priority
- Active DM-consent pairing — deliberate bilateral consent act
- Balanced reciprocity — roughly equal message exchange between author and target

**Up-weights (additive):**
- Slur or identity attack detected lexically
- Boundary token in message (`stop`, `no`, `not interested`, recognized safewords)
- DM-consent pairing recently revoked (within 72 h) followed by directed content
- Persistence — consecutive directed messages to a target with no reply
- Target withdrawal — target goes quiet or leaves after the flagged exchange
- One-sided thread — low target-to-author message ratio with sustained persistence
- DM tier mismatch — author's DM stance more open than the target's
- New account — tenure under 7 days

The floor for any flag is `priority_score = 1.0` regardless of down-weights — nothing is ever silently discarded.

**Target identification** — in priority order: (1) reply chain, (2) first @mention, (3) most-mentioned non-author in recent window, (4) most recent reply target in channel history. If no target can be identified, context signals that depend on a target are zeroed and a penalty is applied to the confidence of those that aren't.

#### Priority tiers

| Tier | Condition | Action |
|---|---|---|
| `immediate` | priority_score ≥ 7 | Embed posted to the configured alert channel with Confirm / Dismiss buttons |
| `digest` | priority_score 3–6.9 | Stored; surfaced by `/rules-watch digest` or the web queue |
| `logged` | priority_score < 3 | Stored only; no notification |

Every event is stored regardless of tier, so no information is lost.

#### Withdrawal check
For `immediate` and `digest` events with an identified target, a background task re-checks 30 minutes later whether the target has posted in that channel. Silence upgrades the `target_withdrew` flag. If the re-score escalates the tier to `immediate`, a brief follow-up note is posted to the alert channel.

#### Label capture
When a moderator clicks **✅ Confirmed violation** or **❌ False positive** on an alert embed, or runs `/rules-watch label`, or confirms/dismisses through the web dashboard:
- A row is written to `rules_labels` with the verdict, the labeling moderator's ID, and a timestamp.
- Corrected rule numbers can be supplied via the web dashboard.
- The alert embed's buttons are disabled after labeling.

These labels are the primary long-term output of the system. As confirmed and dismissed events accumulate, the label set describes *this* community's consent norms in a form no public dataset can provide.

---

## LLM lifetime

Two backends, selected by whether `LLAMA_SERVER_URL` is set.

**In-process** (default). Model loading is asynchronous and singleton: at most one model is loaded at a time. Phases are `idle → downloading → loading → ready` (or `error`). The model file is downloaded from HuggingFace on first start and cached locally. Inference is serialised through a single worker thread — concurrent calls queue rather than race, and Rules Watch guard calls share that queue with `/ai` commands, so a slow manual review blocks passive monitoring.

**Remote.** `LLAMA_SERVER_URL` points at a llama.cpp `llama-server` on another machine — typically one with a GPU. Nothing is downloaded or loaded locally; the phase goes straight to `ready`. Requests use the server's OpenAI-compatible `POST /v1/chat/completions`. Concurrency is the server's business (continuous batching), so guard calls no longer queue behind `/ai` commands. `status()` reports which backend is active.

### Endpoint privacy gate

The guard model sees raw conversation windows, which is exactly the content the local-inference design exists to keep off third-party services. So the remote backend **only accepts an endpoint it can prove is local**: loopback, RFC1918 private, link-local, or unique-local v6 addresses; `localhost`; or a hostname ending in `.local` / `.lan` / `.home` / `.internal` / `.localdomain`. A bare hostname cannot be classified without a DNS lookup and is refused rather than guessed at.

A non-private URL is **refused with an error log and the bot falls back to in-process inference** — it does not silently send content off-network. `LLAMA_SERVER_ALLOW_PUBLIC=1` overrides this deliberately; unrecognised values for that flag fail closed.

The `model` argument threaded through `chat()` remains ignored on both backends and is **never forwarded to the remote server**. Some guild rows still carry hosted model IDs from an abandoned cloud switch; honouring them would route moderation content off-box.

---

## Permissions

- The bot needs **Read Message History** wherever the archive is populated.
- `/ai *` requires the bot's **admin** check.
- `/watch *` and `/rules-watch *` require the bot's **mod** check; add/remove/enable reject DMs.
- The DM relay needs each watcher to allow DMs from server members. Failures are logged and dropped.
- Dashboard endpoints require **admin** except the guild-wide message query and the Rules Watch queue/stats, which require **moderator**.

---

## User-visible errors

| When | The user sees |
|---|---|
| Non-admin runs `/ai *` | "You don't have permission to use this command." |
| Non-mod runs `/watch *` or `/rules-watch *` | "Permission denied." |
| LLM not configured | "OLLAMA_BASE_URL is not set — AI features require a local Ollama instance." |
| `/ai *` invoked in DMs | "This command only works in a server." |
| `/ai scan` / `/ai channel` in voice/category/forum channel | "This command only works in text channels and threads." |
| `/watch add` on a bot | "You cannot watch bots." |
| `/watch add` on self | "You cannot watch yourself." |
| `/watch add | remove` in DMs | "This command must be used in a server." |
| Archive returned no rows | "No messages found for {name} in the last {N} days." |
| LLM call returned an empty string | "No analysis returned." |
| Dashboard guild-wide query, LLM not configured | "LLM is not configured." |
| Dashboard guild-wide query, guild unavailable | "Guild not available" |
| Dashboard prompt update, unknown key | "Unknown prompt key: {key}" |
| Dashboard model reload, no source set | "No model source configured." |
| `/rules-watch label`, event not found | "Event #{id} not found." |
| Dashboard label, event not found | 404 |

The "OLLAMA_BASE_URL" wording is legacy — the check is whether a model file or HuggingFace source has been configured.

---

## Non-goals

- **No live Discord history fetch.** Every command reads the local archive. A gap in the archive is a gap in `/ai` output.
- **No multi-model serving.** Per-command model overrides change which model name is requested; exactly one model is loaded at a time.
- **No streaming.** Each inference call returns the whole completion.
- **No prompt-injection mitigation** beyond 400-character per-message truncation.
- **No DM inspection.** Rules Watch observes public chat only. The DM-consent registry contributes only as a relationship-confidence signal; it never implies reading private messages.
- **No auto-action.** Rules Watch is alert-only. Nothing is kicked, banned, or muted automatically.
- **No Rule 5 enforcement.** DM-consent violations are handled entirely by human-reviewed user reports. The pairing registry is used only to reduce false positives in public-chat scoring.
- **No per-channel watch scoping.** A `/watch` subscription fires across every guild channel.
- **No conversational memory.** Each AI call is one-shot; no follow-ups, no transcripts.

---

## Configuration

Per-guild keys an admin sets via the dashboard:

- **Mod model / wellness model** — default model for moderation and wellness commands.
- **Per-command model override** — review, scan, user query, channel query, watch check, rules watch guard. Empty falls through to the mod model default.
- **Per-command system prompt** — same keys. Empty falls through to the hard-coded default.
- **`rules_watch_enabled`** — whether the passive monitor is running (set via the web dashboard's Rules Watch config panel).
- **`rules_watch_channel_id`** — Discord channel ID where `immediate`-tier alerts are posted (set via the same panel).

Global-only (host-level, not per-guild):

- **Model file path** — the local GGUF file.
- **HuggingFace repo + filename** — used to download the model on first start if local file is missing.

Runtime tuning (context length, GPU layers, threads, batch size) is set once via environment variables — not user-tunable through the dashboard.

Backend selection is deployment topology, so it lives in the environment alongside `LAVALINK_HOST` rather than on the dashboard:

| Variable | Meaning |
|---|---|
| `LLAMA_SERVER_URL` | Base URL of a llama.cpp `llama-server`, e.g. `http://192.168.1.20:8080`. Unset ⇒ in-process. |
| `LLAMA_SERVER_TIMEOUT` | Per-request timeout in seconds (default 120). |
| `LLAMA_SERVER_ALLOW_PUBLIC` | `1` to permit a non-private endpoint. Off by default; see the privacy gate above. |

Note that `LLAMA_N_CTX` must accommodate the largest prompt any command can build. `/ai scan` accepts up to 200 messages at 400 characters each (~20k tokens), which is what the current 32768 setting is sized for — lowering it without also lowering that ceiling will truncate or fail those calls. On the remote backend this is the *server's* `-c` flag, not the bot's env var.

---

## Stored data

**Per guild:**

| Table | Contents |
|---|---|
| `watched_users` | (guild_id, watched_user_id, watcher_user_id) — one row per mod/member pair. Persists when watchers leave the guild. |
| `rules_events` | One row per message that passed the pre-filter and was stored. Columns: all content signals (guard verdict, rule, reason, confidence; slur flag; VADER compound and trajectory), all context signals (mutual count, reciprocity, consent state, DM tier mismatch, thread reciprocity, persistence count, boundary-token flag, withdrawal flag, tenure), priority score, tier, human-readable reason, and the Discord message ID of any posted alert. |
| `rules_labels` | One row per labeled event: is_violation (bool), corrected rule, labeling mod ID, timestamp, optional notes. |

**Per guild, in the shared config table:** model defaults, per-command model overrides, per-command prompt overrides, `rules_watch_enabled`, `rules_watch_channel_id`. The wellness prompt/model entries are co-owned with [[wellness-guardian-spec]].

**Global, in the shared config table:** model file path and HuggingFace source.

**Read-only consumers:** the message archive (messages, message_attachments, message_mentions, message_sentiment) populated by [[events-spec]]. The AI cluster never writes to those tables.

**On disk:** the model file, downloaded from HuggingFace on first start and cached across restarts.

**In-memory only:** the watched-users map (hot-updated by `/watch add | remove`); the loaded model singleton; the Rules Watch monitor's in-flight asyncio tasks.

**Retention:** `rules_events` rows are retained indefinitely — the label set is the primary long-term output of Rules Watch and must not be purged. Message text in `window_json` follows the same retention as the messages archive (purged after the configurable archive window). Labels survive that purge because they reference events, not messages.
