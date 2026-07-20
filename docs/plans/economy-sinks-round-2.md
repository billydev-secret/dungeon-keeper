# Economy sinks, round 2

**Status:** all stages built (4b landed 2026-07-20) · **Owner:** economy · **Spec:** `docs/economy_spec.md` §6

## Why

Live numbers from the main guild, 28 days to 2026-07-19:

| | |
|---|---|
| Minted | **25,820** across 171 earners (~151/member/month, **~38/week**) |
| Absorbed by sinks | **650** — **2.5%** of the faucet |
| Active rentals | **9** (~5% of earners) |
| Balances | p50 **53** · p75 179 · p90 332 · p99 1,699 · max 1,998 |

Two failures, needing opposite fixes:

1. **The median member can't afford the shop.** They earn ~38/wk; the cheapest
   perk is `role_name` at 35/wk. A rental is ~100% of their income — a lifestyle
   commitment, not a purchase. Nothing exists in the impulse range.
2. **The top end has nothing to buy.** The ceiling is 120/wk. A member holding
   1,998 coins has bought everything that exists and is still gaining.

Structurally, all five rentable perks are the same product — *change how your
name looks*, billed weekly. A member who doesn't care about name colour has no
reason to earn at all. Healthy economies absorb ~20–30% of the faucet across
several unrelated categories; we have one category.

This round adds the missing **consumable** tier (cheap, one-shot, impulse) and
the missing **status** surface, and puts the wallet in front of the PvP games.

## Locked decisions (user Q&A 2026-07-19)

| Decision | Choice |
|---|---|
| Scope this round | Quest rerolls · sponsor-a-QOTD (mod-approved) · burn list · PvP wagers |
| PvP wager approach | **Funnel first, then all 6 duel games** — collapse payout into the shared resolvers before escrow goes near it |
| PvP rake | **None.** Winner takes the full pot |
| Reroll model | Keep the existing 1 free/day; **charge for additional** rerolls (never take away something already free) |
| QOTD charge timing | **Debit on submit**, auto-refund on deny/expire — a free queue invites spam |
| QOTD approval surface | **Both** Discord card and dashboard, matching the quest sign-off idiom |

### Consequence of the no-rake decision, stated plainly

Winner-takes-all wagering is a **transfer, not a sink**. Coins move sideways
between members; nothing is removed from the economy. Net absorption from
Stage 4 is **zero by design**.

That is a deliberate trade — wagers are here to make the games matter and to
give coins a *use*, and the friction of a house cut would undercut that. But it
means the absorption target rests entirely on Stages 1–3, and Stage 4 must not
be counted as progress against the 2.5% figure. If absorption hasn't moved once
Stages 1–3 are live, the answer is more consumables or a scarcity sink
(spotlight slot, rotating icon drops), **not** revisiting the wager maths.

## Stage 1 — paid quest rerolls

The cheapest win: `reroll_board_slot`
(`services/economy_quests_service.py:526`) already does the entire swap —
validation, candidate selection preferring a different `trigger_kind`, override
persistence. The only gate is a once-per-guild-local-day free allowance.

- **Seam:** the free-reroll burn block at `economy_quests_service.py:589-596`.
  Free stays first (`INSERT OR IGNORE` into `econ_rerolls`); when `rowcount == 0`
  the reroll is *paid* — check the daily cap, `apply_debit`, increment.
- **Migration 089:** `econ_rerolls.paid_count INTEGER NOT NULL DEFAULT 0`. The
  table's PK is `(guild_id, user_id, local_day)` with no counter today.
- **Settings:** `price_quest_reroll` (default 10) and `quest_reroll_daily_cap`
  (default 3) on `EconSettings`. A cap matters — unlimited paid rerolls let a
  wealthy member cycle the board hunting for the easiest quests.
- **Charge order:** validation → free attempt → cap check → debit → override
  write. A failed debit must consume nothing.
- **Surfaces:** `reroll_ok` in `_load_quests_state`
  (`cogs/economy_cog.py:2082`) becomes "free left **or** balance ≥ price";
  select placeholder copy (`economy/quest_views.py:802`) shows the price once
  the free one is spent.
- **Also touch:** `register.py` memo branch for the `quest_reroll` ledger kind,
  `routes/economy.py` price field, `panels/economy-sinks.js` `PRICE_FIELDS`,
  `economy/metrics.py` `PRICING_FACTORS`, `economy/stats.py` `PRICE_FIELDS`.
- **Tests:** free-then-paid ordering; cap enforcement; insufficient funds
  consumes nothing and leaves the board untouched; validation failure before
  debit; existing reroll tests (`tests/test_economy_quests_service.py:1896`)
  still pass.
- **Docs:** `economy_spec.md:465-474`, `manual.html:870`.

## Stage 2 — burn list on the Statistics page

Lifetime coins **spent** as a ranked leaderboard. Cheap to build, and it turns
spending itself into status — which makes every other sink more attractive.

- Member table already carries `spent_7d`; add lifetime burn alongside.
- Pure math in `economy/stats.py`, assembly in
  `services/economy_stats_service`, render in `panels/economy-stats.js`.
- Read-only, manager-gated like the rest of the page.

## Stage 3 — sponsor a QOTD (mod-approved)

Member spends coins to submit a question; it lands in a pending queue; a mod
approves in Discord or on the dashboard; the approved question runs as a QOTD
credited to the sponsor.

The idiom already exists — **clone the quest sign-off claim**, which is exactly
this shape:

- State machine + partial unique indexes: `migrations/064_economy_quests.sql:47-68`
- Service: `claim_quest:1265`, `resolve_claim:1396`, `expire_stale_claims:1466`
- Discord cards: `economy/quest_views.py` — `render_signoff_card_embed:118`,
  `QuestApproveButton:167` (DynamicItem), `post_signoff_card:524`
- Dashboard: `routes/economy_manager.py` `_resolve_and_notify:515`,
  approve/deny endpoints `:640`, panel `panels/economy-claims.js`

Specifics:

- **Migration 089:** `econ_qotd_submissions` (pending/approved/denied/expired)
  plus `econ_qotd.sponsor_user_id` — `posted_by` is the mod who ran the command
  and must stay that way.
- **Money:** `apply_debit(kind="qotd_sponsor")` on submit; refund via
  `apply_credit` on deny **and** on expiry. Denial reason flows back like the
  quest deny modal.
- **Note:** `open_qotd_for` is channel+day scoped and "latest wins" — multiple
  QOTDs already coexist in a day. Sponsored questions must decide explicitly
  whether they can stack; don't inherit that by accident.
- **Not built and out of scope:** there is no QOTD scheduler. Approved
  questions queue for a mod to post, they don't auto-post.

## Stage 4a — funnel game payout (prerequisite)

Today `pay_game_rewards` is called from **9 scattered sites**, and it sits
*beside* rather than *inside* the two shared resolvers. Escrow cannot land on
that safely — nothing would enforce that we found every path.

### What the first survey said to do, and why it isn't enough

The obvious move is: put the payout inside `BaseDuel._finalize_result`
(`duels/base_duel.py:242`) and `BaseGame._post_group_result`
(`duels/base_game.py:770`), both of which already receive winner/loser/game.

**Reading the actual cogs (2026-07-19) shows that does not produce a
chokepoint.** Three of the six games don't route their resolution through
those helpers at all:

| Game | Why the shared resolver isn't the seam |
|---|---|
| Hot Potato (duel) | `cog.py:130-171` hand-rolls the whole resolution — disables the view, posts its own `ResultView`, writes state via `hpdb.set_game_state` directly. Never calls `_finalize_result`. |
| Pressure Cooker | Pays inside `handle_interaction` (`cog.py:192`) and *then* returns `("done", loser)`, which is what triggers the result post. Payout happens a layer above the resolver. |
| Quickdraw | `WINNER_FIRED` pays at `cog.py:179` and only calls `_finalize_result` **if the channel is still resolvable** — so today a vanished channel pays but never finalizes. `VOID` (`:145-167`) never pays at all. |

### The seam that would actually hold

`BaseGame._db_set_state` (`base_game.py:933`) is abstract, implemented once per
cog, and *every* terminal transition is supposed to go through it. Making it a
concrete template method on `BaseGame` — write the state via a new abstract
`_db_write_state`, then fire an `_on_terminal_state` hook for
`RESOLVED`/`RESOLVED_NO_NICK`/`ABANDONED`/`VOID`/`EXPIRED_*` — gives exactly
the guarantee escrow needs: no cog can end a game without the economy seeing it.

**But it leaks today.** Across the six cogs there are **30** `set_game_state`
call sites; only 6 are the `_db_set_state` implementations. The other ~24 write
state by calling their `db` module directly, bypassing the base entirely.

So the real stage 4a is:

1. Write the missing tests **first** — there is currently no test for
   `_expire_active`/`ABANDONED`, Quickdraw `VOID`, the Musical Chairs
   `winner=None` branch, or the pressure-cooker payout site. Those are exactly
   the branches a refund will live in, and they're the regression net for
   everything below.
2. Convert the ~24 direct `xxdb.set_game_state(self.db, …)` calls to
   `self._db_set_state(…)`. Mechanical and greppable, but it touches every
   resolution path in six games.
3. Make `_db_set_state` concrete on `BaseGame` (delegating to a new abstract
   `_db_write_state`) and move the payout into its terminal-state branch.
4. Delete the 9 scattered `pay_game_rewards` calls.

This is materially bigger than "move the payout into two helpers". It is still
the right order — escrow on top of the current call graph would be guesswork —
but it should be costed as a refactor of six games' resolution paths, not as a
one-file change.

- Party-suite games are **out of scope** — only 3 of ~50 `end_game` calls pass
  players at all.

**Done (2026-07-19).** Landed exactly in that order:

- Regression net first: new runtime test files for Quickdraw, Pressure Cooker
  and Hot Potato (duel); ABANDONED/wipeout coverage for Chicken; degenerate
  `winner=None` + terminal-payout coverage for Musical Chairs; final-detonation
  payout for Hot Potato group. `FakeEconGamesBot` promoted into `tests/fakes.py`.
- `BaseGame._db_set_state` is now the concrete template method; the 24 direct
  writes were converted, the 6 cog impls renamed to `_db_write_state`, the 9
  scattered `pay_game_rewards` calls deleted. `_on_terminal_state` pays on
  `RESOLVED`/`RESOLVED_NO_NICK` (winner may be None → participation only) and
  merely observes `ABANDONED`/`VOID`/`EXPIRED_*` — the escrow attachment
  points. `BaseDuel._finalize_result` persists `winner_id`/`loser_id` with the
  terminal write so the hook's re-read is always self-sufficient.
- Behavior fix along the way: Quickdraw `WINNER_FIRED` with an unresolvable
  channel used to pay but leave the row ACTIVE (the sweep later abandoned it);
  it now terminalizes to RESOLVED so the seam sees it.
- `tests/test_duels_terminal_seam.py` pins the seam contract itself: every
  terminal state fires the hook exactly once, non-terminal writes don't, and a
  hook failure never breaks game resolution.

## Stage 4b — coin wagers on the duel games

**Built 2026-07-20.** Equal ante, winner takes the pot, no rake.

Locked at build time (user Q&A 2026-07-20): lobby games debit **on join**
(leaving refunds) so a host is never blocked at start by someone else's
wallet; duels debit **both antes at accept**, so a declined or expired
challenge needs no refund path at all; **no forfeit command** — quitting
loses the game normally, and only a genuinely dead game (the abandon sweep)
refunds; amounts are **player-chosen and uncapped**.

Every refund path in the table below is covered and tested. `DECLINED` was
added to `_TERMINAL_STATES` (the base-game comment predicted this) so a
challenger's *declared* — never charged — ante row is cleaned up.

Durability is fine: duel state (state, roster, alive, elimination_order,
winner_id, phase timestamps) is all in SQL, resumable via `on_game_resume`, and
a 1-minute sweep terminalizes anything the resume path can't finish. So escrow
survives a restart **provided** escrow rows live in SQL keyed to the game id.

Refund paths that must all be covered:

| Path | Today |
|---|---|
| `EXPIRED_PENDING` (challenge never accepted) | no economy effect |
| `EXPIRED_LOBBY` (lobby never started / cancelled) | no economy effect |
| `ABANDONED` (`_expire_active`, no cog overrides it) | **silently vanishes** |
| Quickdraw `VOID` (nobody fired) | no `pay_game_rewards` at all |
| Chicken total wipeout (`winner=None`) | participation only |
| Musical Chairs degenerate round (`winner=None`) | row stays ACTIVE, later swept |
| Player leaves the guild mid-game | no listener; id stays in roster, stake would strand |
| Boot-time orphan | escrow whose game row is terminal/missing |

Two design rules this inverts, both deliberate and both needing a note in the
code:

1. `pay_game_rewards` swallows every exception — "economy must never block game
   flow" (`game_rewards.py:148`). An escrow **debit** cannot do that: a failed
   debit must block the game from starting.
2. There is no forfeit/surrender command and the host can't leave a lobby. A
   wagered game needs a defined answer for "I want out", even if that answer is
   "you forfeit the stake".

## Out of scope this round

Carried from the sink research, not started: spotlight slot (already specced at
`economy_spec.md:589`, the best remaining big-ticket item), rotating/retiring
icon drops, private rooms Stage 6 (`price_text_room`/`price_voice_room` are live
settings that **nothing reads**), jail bail/fines, pooled community goals,
gift-any-perk. A coin-bought XP boost is explicitly rejected — it closes a cycle
back into the faucet via XP→coin conversion.
