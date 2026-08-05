# W3 batch A: AMA / Rushmore / Clapback / LegitLibs — review, 2026-08-05

## Architecture — the copy-evolution fear doesn't hold

- All four consume the shared games platform (`from bot_modules.games…`);
  the three prompt games follow an identical `<game>/{logic,embeds}.py` +
  thin-cog shape with 300-385-line logic modules that are *game rules*,
  not duplicated plumbing. Embeds go through the accent contract table.
  LegitLibs is structurally richer (modes/, quiplash+classic split) but
  self-contained. **No consolidation work recommended** — the W3 premise
  ("evolved by copy") is answered: the platform extraction already
  happened, and what remains per-game is legitimately per-game.
- No dedicated prod tables for AMA/Rushmore/Clapback rounds — game state
  is in-memory via the platform's game manager; only
  `games_game_history` metadata persists. Restart resilience of in-flight
  rounds is therefore the reliability sweep's question (W4.5), not a
  per-game one.

## UX / Docs

- Member play in Discord, panels for question banks (games-* panels),
  specs under the corrected games_system umbrella. No findings.

## GDPR

- **AMA anonymity verified at the surfaces**: public recap aggregates
  ("N questions by M people"), the asker's identity appears only in their
  own DM; `asker_id` lives in transient game state, not a table. ✓
  (Matches the ToD-MCP rule: never attribute AMA questions.)
- Clapback/Rushmore/LegitLibs: submissions are public-by-design gameplay;
  `legitlibs_templates` author credit noted in games-platform review.
  Nothing to add to the register beyond the existing games row.

## Verdict

Clean batch. Batches B and C will get one combined pass next — same
checklist, expecting the same shape.
