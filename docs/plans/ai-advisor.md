# AI Advisor — grounded "how do I use Dungeon Keeper" assistant

**Status:** Stage 1 shipped (grounded Q&A, both surfaces). Stage 2 shipped
(Billy-bot rebrand; context-aware per-asker grounding; configurable model + a
dashboard config panel). Stage 2b shipped: the **name is per-guild branding**
(`branding_config.assistant_name`, `branding_service.resolve_assistant_name*`,
default `DEFAULT_ASSISTANT_NAME = "Billy-bot"`), edited on Config → Branding.
Both surfaces resolve it and pass `assistant_name=` into `answer_advisor`,
which threads it through the system prompt (`system_instructions(name)`) and
the failure message (`error_msg(name)`). See INDEX.md → Design spec.

## Goal

A Claude-backed assistant that answers members' and admins' "how do I use X"
questions about the bot and dashboard, grounded in the existing user manual so
it can't invent commands or promise unbuilt features.

## Decisions (locked)

- **Model:** configurable per-guild from the assistant's Config panel
  (`advisor_model`), **default `claude-haiku-4-5`** — fast/cheap and plenty for
  grounded help; Sonnet 5 / Opus 4.8 are the higher-quality options. Thinking is
  **disabled** on all of them (help answers don't need multi-step reasoning;
  keeps latency low and the whole `max_tokens` budget available for the answer).
  No sampling params (the 4.x/5 models reject non-default `temperature`/`top_p`/
  `top_k`).
- **Server context is opt-in, default OFF** (`advisor_server_context`, admin
  toggle). When on, `advisor_context.build_asker_context` adds live per-server
  grounding — channel names/topics, recent announcements, and dashboard `docs`
  — as an uncached block after the cached manual. **Privacy gate (enforced +
  tested):** every channel is filtered by `can_view(channel, asker)` (the
  asker's `view_channel`, or @everyone as the public fallback) and NSFW channels
  are always excluded, so an open `/ask` can't surface content the asker can't
  see. Answers are also tailored to the asker's permissions and role names
  (`capability_summary`).
- **No member content, and no member identity** (todo #100, 2026-08-16). Pinned
  messages used to be part of that grounding, snapshotted from every shared
  channel by a `guild_pins_loop` background task; the snapshot took an embed's
  title+description when a message had no content, so any member-authored embed
  — a bio among them — could land in front of the model. The loop, the
  snapshot, and the `is_private_room` gate that existed only to serve it are
  **deleted**. `is_private_room` came back in a different place: it now filters
  the *channel list*, so jail rooms, tickets, Pen Pals rooms and bios wizard
  channels are omitted even for a staff asker who can see them — those channels
  are named after their occupants (`penpals-alice-bob`, `jail-alice-1723`), so
  listing them handed over a roster. `visible_ids` drives the announcement
  filter too, so announcements sent into such a room drop out with it. The
  asker's display name went as well; their roles stayed, because
  permission-tailoring depends on them. The system prompt now carries an
  explicit rule that the assistant has no access to any member's bio, birthday,
  confessions, wellness data, DMs, messages, balance or stats and must say so
  rather than answer — the anti-fabrication rule above it only ever forbade
  inventing commands, channels, rules and features, never facts about a person.
  See `docs/data_register.md` § Processors before adding any new source.
- **Config awareness (admins only):** `build_config_summary` gives admins a
  secret-filtered (drops `*token*`/`*secret*`/`*refresh*` etc.), id-resolved view
  of the guild's settings so they get correct "is X set up?" answers. It combines
  the shared `config` KV table with a **getter registry** (`_FEATURE_LOADERS`)
  over the per-feature service loaders (`load_econ_settings`, `load_xp_settings`,
  `load_voice_master_config`, `get_wellness_config`, …) — Strategy B from the
  investigation, since there's no reusable full-config serializer and the web
  layer's `_section` helpers would drag FastAPI+cogs into the bot path and miss
  the headline features. A generic `_to_flat_dict` + `_fmt_value` serializes any
  dataclass/Row/dict/list; each loader is failure-isolated (a bad/missing one
  just drops its section), overall size is capped. The prompt says: answer from
  the listed settings; if something isn't listed, say you can't see it and point
  to the panel — never invent a value. Still-uncovered: the 6 async per-game
  configs (need the `GamesDb` aiosqlite wrapper) and a few private-cog loaders
  (needle/bump/pen-pals) — add getters as needed.
- **Linking:** the context lists channels as `#name (<#id>)` and the (env)
  `DASHBOARD_BASE_URL`; the prompt tells the model to emit `<#id>` mentions and
  the dashboard URL. Discord renders both natively; the web Ask box converts
  `<#id>` → `discord.com/channels/...` links (via a visible-channel map the
  route returns) and auto-links URLs.
- **Provider:** Anthropic (off-box), reusing the existing
  `ANTHROPIC_API_KEY` + `bot_modules.games.utils.ai_client.get_client()`
  singleton. The on-box/LAN llama stack is reserved for moderation (privacy
  fence) and is too slow (~68s/check) for interactive help.
- **Grounding corpus (MVP):** `src/web_server/static/manual.html` only — the
  canonical user-facing guide, the same source the dashboard Help panel
  renders. Extracted to section-anchored plain text, prompt-cached (`cache_control`
  ephemeral) so repeat calls bill the corpus at ~0.1x. Grounding on shipped-only
  docs structurally prevents promising the Aspirational specs INDEX.md warns of.
- **Two surfaces, one brain:**
  - Dashboard: an ask box inside the existing Help panel (`help.js`),
    `POST /api/help/advisor`, gated to any authenticated user
    (`require_perms(set())`), rate-limited on the existing `ai` tier. It labels
    itself from `GET /api/help/advisor/name` (same auth), so a member sees the
    guild's own name for the assistant.
  - Discord: `/ask <question>` — ephemeral, per-user cooldown
    (`advisor_cog.py`).

## Architecture

```
                 answer_advisor(question, history)      ← shared logic layer
                 ┌───────────────┴───────────────┐        (advisor_service.py)
   POST /api/help/advisor                    /ask (ephemeral)
   (routes/advisor.py)                       (cogs/advisor_cog.py)
   → Help panel ask box                     → Discord members
```

- `advisor_service.py` owns corpus extraction (mtime-cached), system-prompt
  assembly (instructions + cached corpus), input validation, and the Anthropic
  call. It is the tested unit.
- Both surfaces are thin glue over `answer_advisor`.

### The config-tool loop and the event loop

For admin askers, `answer_advisor` runs a bounded tool loop (`MAX_TOOL_ROUNDS`;
the last round forces a text answer) over the callbacks the surface wires up in
`AdvisorTools`. Every one of those callbacks blocks — it opens the DB and walks
the guild cache — and **the dashboard's uvicorn runs on the bot's own event
loop**, so a blocking tool call stalls the Discord gateway, not just one request.

The contract is therefore: **`AdvisorTools` callbacks are plain sync functions,
and `_run_tool` (async) dispatches each one through `asyncio.to_thread`** — the
same convention `web_server/deps.run_query` uses for route DB reads. The
off-loop hop lives in one place instead of being re-implemented per surface, and
a coroutine function passed in is a pyright error at the call site. Calls within
a round are awaited **in order, never gathered**: the propose tools mutate the
surface's shared proposal list (dedupe, then a `_MAX_PROPOSALS` cap), so
ordering is part of the behaviour. Guarded by
`test_every_db_backed_tool_runs_off_the_event_loop` (service) and
`test_advisor_config_tools_run_off_the_event_loop` (route wiring).

## Follow-ups (not yet built)

- **Token streaming** on the dashboard via the `logs.py` SSE precedent
  (perceived-latency win; still non-streaming POST — answers are short).
- **Member-scoped context on the web surface** currently resolves the asker via
  `guild.get_member(user_id)` and falls back to @everyone-public when the member
  isn't resolvable. Could tighten using the dashboard session's role set.
- **Agentic mode** — partly shipped: admin askers get config tools
  (`get_server_settings`, `find_setup_gaps`, and the propose-a-change pair
  rendered as Apply buttons on Discord). Still missing are tools over live
  *member* state ("what's my balance").
