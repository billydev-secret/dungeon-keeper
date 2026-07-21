# AI Advisor — grounded "how do I use Dungeon Keeper" assistant

**Status:** Stage 1 shipped (grounded Q&A, both surfaces). Stage 2 shipped
(Billy-bot rebrand; context-aware per-asker grounding; configurable model + a
dashboard config panel). See INDEX.md → Design spec.

## Goal

A Claude-backed assistant that answers members' and admins' "how do I use X"
questions about the bot and dashboard, grounded in the existing user manual so
it can't invent commands or promise unbuilt features.

## Decisions (locked)

- **Model:** configurable per-guild from the Config → Billy-bot panel
  (`advisor_model`), **default `claude-haiku-4-5`** — fast/cheap and plenty for
  grounded help; Sonnet 5 / Opus 4.8 are the higher-quality options. Thinking is
  **disabled** on all of them (help answers don't need multi-step reasoning;
  keeps latency low and the whole `max_tokens` budget available for the answer).
  No sampling params (the 4.x/5 models reject non-default `temperature`/`top_p`/
  `top_k`).
- **Server context is opt-in, default OFF** (`advisor_server_context`, admin
  toggle). When on, `advisor_context.build_asker_context` adds live per-server
  grounding — channel topics, pinned messages (snapshotted by `guild_pins_loop`,
  ~30 min), recent announcements, and dashboard `docs` — as an uncached block
  after the cached manual. **Privacy gate (enforced + tested):** every channel is
  filtered by `can_view(channel, asker)` (the asker's `view_channel`, or
  @everyone as the public fallback) and NSFW channels are always excluded, so an
  open `/ask` can't surface content the asker can't see. Answers are also
  tailored to the asker's permissions (`capability_summary`).
- **Config awareness (admins only):** `build_config_summary` adds a secret-filtered
  (drops `*token*`/`*secret*`/`*refresh*` etc.), id-resolved view of the shared
  `config` KV table so admins get correct "is X set up?" answers. It is *partial*
  by design — feature areas keep settings in ~40 own-tables (economy, wellness,
  games, …) with no reusable serializer, so the prompt tells the model to defer
  to the panel for anything not listed rather than guess (fixing the "says it's
  not configured when it is" bug). Deeper per-feature config is a follow-up.
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
  - Dashboard: an "Ask Billy-bot" box inside the existing Help panel
    (`help.js`), `POST /api/help/advisor`, gated to any authenticated user
    (`require_perms(set())`), rate-limited on the existing `ai` tier.
  - Discord: `/ask <question>` — ephemeral, per-user cooldown
    (`advisor_cog.py`).

## Architecture

```
                 answer_advisor(question, history)      ← shared logic layer
                 ┌───────────────┴───────────────┐        (advisor_service.py)
   POST /api/help/advisor                    /ask (ephemeral)
   (routes/advisor.py)                       (cogs/advisor_cog.py)
   → Help panel "Ask Billy-bot"              → Discord members
```

- `advisor_service.py` owns corpus extraction (mtime-cached), system-prompt
  assembly (instructions + cached corpus), input validation, and the Anthropic
  call. It is the tested unit.
- Both surfaces are thin glue over `answer_advisor`.

## Follow-ups (not yet built)

- **Token streaming** on the dashboard via the `logs.py` SSE precedent
  (perceived-latency win; still non-streaming POST — answers are short).
- **Member-scoped context on the web surface** currently resolves the asker via
  `guild.get_member(user_id)` and falls back to @everyone-public when the member
  isn't resolvable. Could tighten using the dashboard session's role set.
- **Agentic mode** — tools to look up live member state ("what's my balance") or
  take actions. Still pure grounded Q&A.
