# Duels + party games — battery review, 2026-08-05 (closes Wave 2)

Bundle: `duels/` (2.4k: base_game/base_duel/lobby/db), party packages
`hot_potato`, `hot_potato_group`, `musical_chairs`, `pressure_cooker`,
`quickdraw`, `chicken` (~660-770 each), `needle_cog`, `services/risky_roll/`.
Specs: `dk_pvp_games_suite_spec.md` (Reference since 07-15 correction),
`pressure_cooker_spec.md`, `needle_spec.md`.

## Architecture — consolidation already done

- The worry that motivated this bundle (copy-evolved duplication) is
  largely answered: **all five party packages import the shared games
  platform** (`from bot_modules.games…`), each follows the same
  cog/db/game/views shape, and duels provides `base_game`/`base_duel`
  inheritance. W3's prompt-game batches should be measured against this
  bar, not the other way round.
- Nick sentences (loser renames) have a proper lifecycle: `duel_nicks`
  rows carry `reverted_at` + `revert_reason`, and `base_game.py:261`
  reverts on expiry with an audit-reason string. ✓
- No architecture findings worth a code change.

## UX

- Party games are lobby-opt-in; a nick sentence is a stake accepted by
  joining. Games permission fix from memory (Use Activities on 5 channels,
  2026-08-05) is Discord-side state, noted only. No findings.

## GDPR

- Tables are participation metadata (host/target/winner ids, style prefs,
  nick history). `risky_pending_questions.participant_user_ids` is a
  participant list for a ToD-style game — gameplay data, posted publicly
  at the time. Register: all party-game tables → **preserve as history or
  fold into econ_purge_user's sweep** — either is defensible; recommend
  folding the per-user ones (nicks, styles) and preserving round history.
  Wager money paths ride the verified economy funnel (rake checked in the
  07-30 retune context).

## Verdict — Wave 2 complete

No high findings in W2 beyond economy A2 (silent renewals) and the
economy/casino purge-helper package. Cumulative high-priority list
unchanged from W1's four. Next: W3 prompt-game batches (measured against
the platform consolidation bar), then W4/W4.5.
