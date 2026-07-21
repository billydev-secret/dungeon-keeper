# Embed style-guide conformance sweep

**Status:** in progress (started 2026-07-21)
**Spec:** [`../embed_style_guide.md`](../embed_style_guide.md) — updated 2026-07-21 with
the color rulings, currency vocabulary, ledger-row, footer, and title-glyph
sections this sweep enforces.

A five-agent audit of all 272 `discord.Embed(` constructions found the codebase
**mostly conforming**, with drift concentrated in a few surfaces the economy
currency pass never reached. This tracks the fixes. Rulings (2026-07-21, user):
**games go full-accent** (keep only win=green / loss=red); the **per-domain
identity palettes** in `services/embeds.py` (bios, wellness, mod, starboard,
dm-perms) are **blessed** — documented, not changed.

Each stage ships with tests in the same commit and its own `Testing:` section.

## Stage 0 — spec — DONE
- Update `embed_style_guide.md` (color rulings + currency/ledger/footer/title
  sections). Add this tracker. Reclassify nothing (still Reference).

## Stage 1 — XP surfaces → accent
- `cogs/xp_cog.py:70-95` — drop the per-time-window decorative palette; pass
  `resolve_accent_color` like the sibling embed at `:249`. (HIGH)
- `services/xp_service.py:259,374` — level-up embeds hard-code gold/blue → accent.
  (HIGH) Thread the accent from the caller.
- `xp_cog.py:144`, `xp_service.py:257,372` — lead titles with a glyph. (LOW)

## Stage 2 — Welcome → accent
- `services/welcome_service.py:63` — `build_welcome_embed` hard-codes blurple, no
  color param → add `color` param, thread `resolve_accent_color` from the caller.
  (HIGH)
- `:93` — `build_leave_embed` dark-gray → accent (or keep gray as a commented
  "departure" semantic — decide in-stage). (MED)

## Stage 3 — Duels → accent lobby + currency vocabulary
- `duels/base_game.py:551` — lobby hard-codes gold, no param → add `color`, thread
  accent (sibling `base_duel` already does at `:147-149`). (HIGH)
- `duels/base_duel.py:189`, `base_game.py:564,1130` — wager pots miss
  `currency_emoji`/unit; `:1130` always-plural → `{emoji} **{n:,}** {unit}`. (HIGH)
- `base_game.py:1097`, `base_duel.py:222` — bare wager numbers → currency vocab. (MED)

## Stage 4 — Footer / custom-emoji risk (rule 7)
- `economy/register.py:452` — currency emoji in footer → drop it (use the wallet
  word); safe against a custom `<:coin:id>`. (LOW/latent-MED)
- `economy/game_rewards.py:463` (`append_payout_footer`) — bare currency in a
  footer, feeds the whole games payout line → move the payout line into a
  field/description with proper `{emoji} **{n:,}** {unit}`, or drop the emoji. (MED)
- `starboard/embeds.py:39` — custom reaction emoji in footer → unicode-only or
  move the count out of the footer. (MED)

## Stage 5 — Games → full accent (the big one)
Retire the phase palette; thread the guild accent, keep only win=green/loss=red.
- **Camp B (no `color` param today):** `games_wyr`, `games_hottakes`, `games_mlt`,
  `games_nhie`, `games_ttl`, `games_price`, `games_rushmore` `/embeds.py` — add a
  `color` param to every builder; resolve accent at the cog call site; keep only
  the win/loss semantic branches.
- **Clapback** (`games_clapback/embeds.py`) — the lone outlier: retire its private
  palette, thread accent, and fix the off-pattern `⚡ C L A P B A C K ⚡` title to
  the `⚔️` game icon. (HIGH-consistency)
- **Camp A gray recap fallbacks:** `games_fantasies:20`, `games_traditional:22`
  default recap to gray → BRAND/accent. `games_traditional:27-33` per-category
  card colors → decide (category-coding vs accent). (LOW-MED)
- Engine games already semantic (quickdraw/chicken/musical_chairs/hot_potato/
  pressure_cooker/legitlibs) — audit says CONFORMS; leave unless a specific
  hard-coded non-win/loss color surfaces (e.g. legitlibs cancelled greyple).

## Stage 6 — Wellness literal cleanup (blessed, no color change)
- `services/wellness_scheduler.py`, `wellness_enforcement.py`, `wellness_partners.py`
  — replace raw `"#7BC97B"` literals with the `WELLNESS_PRIMARY` constant. (LOW)

## Stage 7 — Cosmetic title-glyph sweep (rule 4, LOW)
Lead sibling titles with a glyph where the family already does:
- economy: `economy_cog.py:1492` ("Notifications muted" → 🔔), `quest_views.py`
  resolution/DM titles, `sponsor_views.py` card titles.
- `whisper/embeds.py:52,83,116` (reply/report titles), `voice_master/embeds.py`
  (profile/panel/controls/ready titles), `jail/embeds.py` (policy/ticket/setup),
  `jail/apply.py:370` ("Moderation Hold" → 🔒), `guess_cog.py:315,1682,1901`,
  `services/role_grant_audit_service.py:510`, `services/confessions_service.py`,
  `music/embeds.py`.
- `events_cog.py:1043` — prefix the currency emoji on the digest amount. (LOW-MED)

## Non-goals / confirmed clean
- Rule 3 (raw ledger kinds): clean everywhere post-economy-pass.
- Moderation/jail/rules-watch red/green: semantic, correct.
- `services/embeds.py` per-domain palette: blessed (Stage 6 only tidies literals).
- `games_ffa_cog.py` TRUTH/DARE themed colors: documented content-semantic; left
  as-is unless the ruling is read to forbid them (revisit if so).
