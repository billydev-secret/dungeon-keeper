# Game UX round — TTL + Rushmore + winner payouts (2026-07-20)

Source: live review of the two 2026-07-20 runs in the games channel
(games_game_history #117 ttl, #118 rushmore) plus the full channel transcript.
Player pain points were confirmed against the code before planning.

## Findings (transcript ↔ code)

1. **TTL join-mid-game corrupts the round embed (bug).** A late joiner's modal
   `on_submit` falls back to `interaction.message` (the active guess message)
   and `set_field_at(0, name="Players (N)")` overwrites statement 1's field.
   Seen live in round 6: voters couldn't read option 1️⃣.
2. **TTL prompt gets missed** (3/10 players answered off-prompt). Modal title
   truncates at 45 chars; guess embeds never repeat the prompt, so mid-game
   joiners never see it.
3. **TTL resubmit blocked after guessing starts** — the Join button rejects
   existing submitters, contradicting what the host told players.
4. **"Most Honest" award reads backwards** — it means "fooled the fewest".
5. **Rushmore pick button gets buried** — it lives only on the board message;
   turn pings carry no button. One player skipped all 4 picks after scrolling
   hunts; 7/24 picks timed out.
6. **Skips are permanent** and all-skip boards still get displayed (and roasted).
7. **Snake draft is slow** for casual topics — host floated parallel picking.
8. **Winners get nothing visible** — economy pays participation 5 / win 20, but
   only nhie/ttl/hottakes resolve winners, and no surface ever announces it.

## Decisions (locked with Billy 2026-07-20)

- Rushmore gains a **blitz mode** (synced rounds: everyone with an empty slot
  picks simultaneously each round, first-come wins duplicates); **snake stays
  the default**. Mode is a game option (dashboard + slash choice).
- Pick timer default stays **30s**; turn pings carry the **Make Your Pick**
  button; **60s backfill window** after the draft for skipped slots;
  **all-skip boards hidden** from FINAL BOARDS (already excluded from vote).
- TTL: resubmit allowed **until the player's round is revealed**; full prompt
  shown via **components-v2 modal** (TextDisplay + Label) and repeated on every
  guess embed; **🪞 Open Book** replaces "Most Honest" (with fooled count);
  optional **vote timer** knob (0 = host advances, default 0).
- Economy: **winner resolvers for every game with a genuine winner**
  (rushmore votes, clapback scores, mlt crowns, price if a clear winner exists;
  ttl extends to Best Liar + Best Guesser, ties included). Recaps get a
  **payout footer** ("🪙 +20 winners · +5 everyone", guild-configured values).
  Story/AMA/MFK/compliment/traditional stay participation-only; wyr stays
  excluded by design.

## Stages

1. TTL fixes: embed-corruption bug (failing test first), resubmit window,
   prompt visibility (modal v2 + guess embeds).
2. TTL: Open Book rename + vote-timer option.
3. Rushmore: ping-button, backfill window, all-skip board hiding.
4. Rushmore: blitz mode + join-embed how-to line.
5. Economy: resolver sweep + recap payout footer helper, applied to paying
   party-game recaps.
6. Docs (specs + INDEX.md, manual.html/help-sections, README) and tests ride
   in each stage's commit; merge to main when green.

Each behavior-changing commit ends with a `Testing:` checklist (QA cards).
