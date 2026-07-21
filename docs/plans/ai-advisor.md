# AI Advisor — grounded "how do I use Dungeon Keeper" assistant

**Status:** Stage 1 shipped (grounded Q&A, both surfaces). See INDEX.md → Design spec.

## Goal

A Claude-backed assistant that answers members' and admins' "how do I use X"
questions about the bot and dashboard, grounded in the existing user manual so
it can't invent commands or promise unbuilt features.

## Decisions (locked)

- **Model:** `claude-sonnet-5` — near-Opus quality on grounded doc Q&A at ~half
  the cost, sized for member-facing volume. Thinking is **disabled** (help
  answers don't need multi-step reasoning; keeps latency low and the whole
  `max_tokens` budget available for the answer). No sampling params (Sonnet 5
  rejects non-default `temperature`/`top_p`/`top_k`).
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
  - Dashboard: an "Ask the Guide" box inside the existing Help panel
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
   → Help panel "Ask the Guide"              → Discord members
```

- `advisor_service.py` owns corpus extraction (mtime-cached), system-prompt
  assembly (instructions + cached corpus), input validation, and the Anthropic
  call. It is the tested unit.
- Both surfaces are thin glue over `answer_advisor`.

## Follow-ups (not in stage 1)

- **Token streaming** on the dashboard via the `logs.py` SSE precedent
  (perceived-latency win; MVP is non-streaming POST — answers are short).
- **README + docs/ grounding** alongside the manual (broader coverage; MVP is
  manual-only for a single non-drifting source).
- **Per-guild model override** via a dashboard AI-config knob (MVP is a module
  constant).
- **Agentic mode** — tools to look up live member state ("what's my balance")
  or take actions. Stage 1 is pure grounded Q&A.
