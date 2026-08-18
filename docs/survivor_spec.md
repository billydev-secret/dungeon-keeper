# Survivor — NFL Pick'em Survival Cog for Dungeon Keeper

**Spec v2.2 (final)** · Target: live before NFL Week 1 (Sept 10, 2026)
**Research basis:** report #15 in the bank — *NFL Survivor Pool Rulesets & Play Modes (Aug 2026)*

**The game in one breath:** pick one NFL team to win each week, straight up. No team twice. Your team loses, you're out. Last one standing takes the coin pot.

**The product in one breath:** picks are private acts; results are communal theater. Everything funnels toward one Tuesday-morning post — the Reckoning — where the meadow gathers to find out who lived.

---

## 1. Ruleset

### 1.1 Entry
- Enrollment opens when an admin creates a season and **never closes** — the door is always open; the road is real.
- **One entry per person. Season one is free entry** (`buyin_coins: 0`) with a **house-seeded pot** — the largest audience is coin-poor newcomers, and the entry fee shouldn't gate them. Buy-in remains a config for future seasons.
- **Late entry — the Gauntlet.** Join any week; the bot retroactively plays every eligible missed week, assigning each week's chalk (highest win-probability team by closing odds, no reuse) and grading it against real results. You inherit that line's full fate: those teams are burned from your satchel, one chalk loss burns your strike, two and you arrive dead — landing directly in Ghost Streak, picking from day one like everyone else. *(Clarified 2026-08-18: **the replay ends at death** — replaying chalk past the fatal week would either hand the new ghost an unearned streak of auto-picks or burn teams for a corpse. The fatal line's teams are burned; the weeks after belong to the ghost you now are, live. Unsettled stragglers are never chalk — a pending game can't be inherited without fate changing when it settles.)* The gauntlet's toll is structural: chalk torches the elite teams, so survivors arrive alive but poor.
- **The gauntlet fee (anti-free-option):** the receipt shows your inherited fate *before* you pay — which would make waiting free information. So late entry costs `gauntlet_fee_per_week × weeks elapsed` (default 50 coins/week), **independent of the base buy-in** — this is what closes the exploit even in a free-entry season. *(Clarified 2026-08-17/18: "weeks elapsed" = fully-kicked-off weeks. The partial week a mid-week joiner picks live is free and joining before Week 1 kickoff costs 0. The fee charges **every** elapsed week even when the replay line dies early — waiting is what's priced, not surviving.)* Alive arrivals' fees feed the main pot; **dead-on-arrival fees route to the Ghost Streak side-pot** — you pay into the game you're actually playing.
- **Mid-week joins:** weeks where any games haven't kicked off yet are picked live from the remaining slate; only fully-kicked-off weeks are gauntlet-replayed.

### 1.2 Weekly pick
- One team per week to **win, straight up**. Each team usable once per season.
- **Per-game locking:** your pick locks at *that team's* kickoff. Until then you can change freely. After your team kicks off you're locked, even if other games haven't started.
- **Total darkness, loose lips:** the *bot* never reveals a pick to anyone (admins excepted, mod-log only) until the Tuesday Reckoning — no reactions, no "X has picked" notices. Players, however, may say anything. **Table talk is legal; lying is encouraged.** The bot keeps the secrets; the bluffing is yours.
- **Missed pick → auto-assign:** at the week's final kickoff, anyone pickless is assigned the highest win-probability available team still unplayed (ESPN odds; fallback best record), marked `📎 assigned by the groundskeeper` on reveal. Dignified, mildly embarrassing, fully survivable. *(Clarified 2026-08-17: "still unplayed" means the game has not kicked off — assigning from a game in progress or final would be a pick made with the result known. At the final kickoff that pool is the final game(s) of the week. If every legal team there is already burned for that player, the week is voided for them — survive, mirroring §6.10 — and it does **not** count against the auto-assign cap.)*
- **Auto-assign cap (anti-AFK):** three auto-assigns per season (config), counting one per week even in double-pick weeks and even when only one slot was missing. The fourth time the groundskeeper is needed, he declines — elimination, with its own fixed line (*amended 2026-08-18, just-the-facts*): *"**X** — out of auto-assigns, no pick made. Eliminated."*

### 1.3 Results
- Win → survive. **Loss → strike/elimination. Tie → loss.** ("Your team must win" — one sentence, no asterisks.)
- Postponed/cancelled game → pick voided, player survives, team returns to their pool. *(Clarified 2026-08-17: a postponement announced before the week's final kickoff frees the slot — the player may re-pick from the remaining not-yet-kicked-off slate rather than sitting the week out.)*

### 1.4 Lives
- **One strike** (config: 0–2). First wrong week burns it 💛→🖤; second is the end. Research is unambiguous that sudden death at 20–50 players routinely ends leagues by Halloween — the strike is the longevity buffer.

### 1.5 Escalation
- **Double-pick weeks from Week 14** (config: start week, min-alive trigger, default >4 alive): two teams, both must win, both burn from your pool. One fate, two slots — **each slot locks independently at its own team's kickoff**, so you can still change your Sunday pick after your Thursday pick has locked.

### 1.6 Endgame
- **Sole survivor** wins the pot + `🏈 Sole Survivor` role (held until next season).
- **Wipeout (all remaining eliminated same week):**
  - Through Week 13 → **week annulled**: nobody dies, teams used stay burned.
  - Week 14+ → **equal split** among that week's players.
- **The Accord (legalized collusion):** once ≤6 remain (config), any living player may invoke `/survivor accord` in the Tuesday-to-Thursday-kickoff window. The bot posts a public vote; if **every** living player accepts within 24h, the season ends immediately in an equal split, with its own ceremony: *"the meadow chose peace."* Any decline (or silence) dissolves it — one invocation per player per season, so it can't become a weekly nag. Final tables deal openly here; nobody has to sneak a wipeout.
- **Multiple survivors after the final week** → equal split. No margin-of-victory tiebreakers in v1.
- **Any season end settles both pots** *(decided 2026-08-17)*: sole survivor, final-week split, wipeout split, or Accord — the Ghost Streak side-pot pays out the same day, on that day's standings.

### 1.7 The dead
- Elimination = `👻 Ghost` role, channel access unchanged, one warm condolence DM. Heckling from the graveyard is a feature. *(Decided 2026-08-17: death **swaps** the roles — `🏈 Survivor` comes off, `👻 Ghost` goes on — so the member list shows the state at a glance. Both weekly posts ping `@🏈 Survivor` **and** `@👻 Ghost` (§2.3), so the dead keep hearing about the game they're still playing.)*
- **Ghost Streak (v1 core — promoted):** ghosts keep picking via the identical flow; longest post-death correct streak takes the side-pot (funded by `ghost_pot_pct` of the seed — **20%, decided 2026-08-17** — plus DOA gauntlet fees; streak-length ties split it). **Your satchel follows you into death** — no-reuse continues, so early ghosts are rich in teams and late ghosts scrape. **Ghosts always pick one team**, even in double-pick weeks — the escalation exists to end the main game, and the streak game stays simple for casuals. **The groundskeeper never touches the dead** *(decided 2026-08-17)*: no auto-assign for ghosts, no cap — a missed week simply **breaks the streak** (resets to 0; best-ever streak stays on record), and a voided game leaves it untouched, neither extending nor breaking. The side-pot rewards showing up; auto-assigned streaks would accrue to people who stopped playing. Load-bearing by design: gauntlet joiners who arrive dead land here, so anyone can join any week and always have a live game to play.

---

## 2. Player experience

### 2.1 Journey
```
Season announced → Join (coins) → [Wed slate → pick → sweat → Tue Reckoning] → death → Ghost life → endgame ceremony
```

### 2.2 Season announcement
Pinned embed in #survivor: hero copy (~3 lines), buy-in, live entrant counter, five one-line rules, link to full rules thread, **[🌾 Join the Season]** button → ephemeral confirm (coin balance, debit, one-sentence rules) → role grant. At Week 1 kickoff the button stays live but the copy flips to gauntlet mode: *"the season is underway. the door is open; the road is real. 🌾 N souls walking."* Joins from here run the gauntlet receipt flow (§4.2) before charging the buy-in.

### 2.3 Weekly cadence
- **Wed ~9am — Slate post** (`slate_hour`, guild-local; pings `@🏈 Survivor` + `@👻 Ghost`): the week's games with `<t:...:f>` timestamps (local time for free), plus the **[🏈 Make your pick]** button. Footer: picks close at each kickoff · X of N alive have picked. *(Amended 2026-08-18, twice: first the slate absorbed the join door; then Billy collapsed further — **the channel has exactly ONE updating panel**. Season pitch, current week's slate, standings line (alive/eliminated/pots), the rules, the "New Here?" door, and both buttons live in a single pinned message. The bot edits it in place on joins and settles, and **reposts it to the channel bottom every Wednesday with the week-open ping** — that repost IS the slate moment, so the twice-weekly ping budget holds. Entry-closed seasons drop the door. The §2.2 announcement post and this panel are the same message; §2.6's board auto-post is retired — standings live on the panel, and `/survivor board` remains on demand. The Tuesday Reckoning stays a real post: it is the payoff, not furniture.)*
- **Sat 6pm — Last call:** DM only to the pickless (opt-out honored; fallback channel mention): *"you haven't picked. Make your pick below — or I'll pick for you, and I have terrible taste. 🌙"* **Amended 2026-08-18 (Billy):** the DM — and the closed-DM channel fallback — carries the panel's own persistent 🏈 Make your pick button, so the nudge is the door, not directions to one.
- **Sun–Mon:** the bot posts nothing. The channel does the sweating.
- **Tue 9am — THE RECKONING** (pings both roles; see 2.5).

The roles get pinged exactly twice a week. Restraint is the brand.

### 2.4 Pick flow
- **Primary — `/survivor pick`:** autocomplete filtered to unburned ∩ playing ∩ not-yet-kicked-off, options like `49ers (vs SEA, Sun 1:25)`. Confirmation (ephemeral): team + opponent, lock time `<t:...:R>`, satchel count with wealth signal (`🟢 24 teams left` → `🟡` under 12 — ambient scarcity awareness, never advice), strike status, **[Change pick]** / **[My season]** buttons.
- **Double-pick weeks:** the same command collects two teams in one flow; the confirmation shows both slots with their independent lock times. Changing one slot never unlocks the other.
- **Secondary — slate button:** ephemeral **AFC/NFC dual select** (Discord's 25-option cap vs up to 32 legal teams early season). Keeps casuals off slash-command syntax entirely.
- `/survivor status` (ephemeral): pick state, lock countdown, satchel, strike, ghost stats if dead.

### 2.5 The Reckoning — one post, three acts
0a. **The panel is sticky (added 2026-08-18, Billy's first-look #9):** the
   channel panel rides `core.sticky.StickyPanel` — any message beneath it
   (member or bot; the Reckoning is its main burier) reposts it to the
   channel bottom after the house debounce. The Wednesday `repost_panel`
   stays separate: it carries the week-open ping and the pin, which sticky
   placements don't; the machinery's at-bottom check keeps the two from
   chasing each other. Panel ids live where they always did
   (`announcement_*` in the season config, via
   `survivor_service.panel_ids/set_panel_ids`); no live season → no restick.
0. **The channel panel roster (added 2026-08-18):** the pinned panel lists who is
   alive and who is eliminated **by name**, not just counts — at most
   `ROSTER_DISPLAY_CAP` (30) names per list, dot-separated, with an honest
   "…and N more" tail. Length binds before the count when display names are
   long, so the field can never exceed Discord's 1024-char cap. The graveyard
   field is omitted until someone is in it.
1. **The toll** (*amended 2026-08-18: numbers only — the rotating flavor line went with the corpus*)**:** week number, survivors before → after, pot. Plus **the gate**, when anyone joined since last Tuesday: arrivals announced with their gauntlet fate — *"two souls walked the gauntlet this week. one arrived breathing."*
2. **The ledger:** the only place picks ever appear — every living player → team → `✅ / 💀 / 💛→🖤 / 📎 auto`, deaths sorted first. Ghost Streak standings get a compact strip beneath (current streaks, record holder).
3. **Eliminations** (*amended 2026-08-18: factual, no corpus*)**:** one line per elimination stating team, result and week. Ghost roles applied on post. Zero deaths: no special line — the toll's unchanged survivor count says it.

~~**Flavor corpus:** DB table (~20 eulogies, ~10 no-death, ~10 toll lines), admin CRUD on the dashboard panel, seasonal drift encouraged.~~ *Removed 2026-08-18 (first-look review) — table dropped in migration 172, CRUD and dashboard card deleted.*

> **Copy register (decided 2026-08-18, revised same day).** All hardcoded copy —
> embeds, refusals, buttons, DMs — is **standard sports register**. **Revision
> (first-look review, Billy: "just the facts"):** the flavor corpus is
> **removed entirely** — table (migration 172), CRUD, dashboard card, and
> rotation. The Reckoning's copy is hardcoded and factual: Act 1 is the
> numbers alone (the survivors delta says whether the week took anyone), and
> each elimination states its cause — `FATAL_LINE`
> ("**{name}** — {team} lost. Eliminated in Week {week}.") for football
> deaths, with plain fixed lines for the auto-assign cap, the missed pick,
> and the leaver. This closes the per-guild voice route the corpus carried;
> per-guild personality is the branding kit (accent + bot identity) alone.
> The meadow-voiced sample lines below are historical color, not shipped
> copy. The groundskeeper survives as a mechanic; his line is factual now.

### 2.6 Boards
- `/survivor board` (public; ~~auto-posted after each Reckoning~~ *amended 2026-08-18: the auto-post is retired — the channel panel carries the standings line, and the full board stays on demand*): alive roster with weeks survived + strike hearts, graveyard with week-of-death, pot, most-burned teams meta-stat.
- `/survivor history [@player]`: revealed picks only. Nothing current ever leaks. *(Amended 2026-08-18: the channel panel carries a **[📜 My History]** button — ephemeral and personal, so the clicker's own unrevealed picks show too, tagged as hidden from others. One shared builder renders both faces so the secrecy rule can't drift.)*

### 2.7 Endgame ceremony
Champion gets a dedicated post (never folded into a Reckoning): season stats (weeks survived, closest call, teams ridden), pot receipt, role, optional Bios-cog trophy line. Splits framed as shared victory; annulments as their own mini-event (*"everyone died. therefore nobody did. the week is stricken — but the teams you burned stay burned."*)

### 2.7b Join echo (added 2026-08-18)
A member joining fires an **Event Echo** into main chat — the existing
mirror system, not a new announcement path: silent (no ping), the guild's
configured echo channel, per-member-per-season dedupe, and the shared
cooldowns coalescing a busy join night into one echo. The line is a mini
advertisement — *"🏈 Loaf joined the Survivor pool — 14 players in · pot
8,000 · one team a week, last one standing takes it"* — with the jump link
landing on the channel panel, where the Join button is. A guild with no
echo channel configured simply never echoes.

### 2.8 Notifications & consent
`/survivor notifications`: per-category DM toggles (last call, condolence), default ON. Closed DMs fall back to channel mention.

---

## 3. Admin

> **Amended 2026-08-17.** As drafted, this section put all five admin surfaces in
> Discord, which collides head-on with CLAUDE.md — *"configuration lives on the web
> dashboard, not Discord… don't build slash commands, modals, or button flows for
> admin config."* Raised with Billy before any code; he chose the **strict split**.
> Configuration and state edits go to the dashboard; only the two genuine live mod
> actions stay as commands. **Every rule in §1 is still a setting, not code** (see
> §5) — the setting just lives on a panel instead of a paginated embed.

### 3.1 Dashboard — `src/web_server/`, admin-gated, nav heading **Games**

Route id `survivor` (bare feature name, per CLAUDE.md's frozen-id convention).

- **Season lifecycle** — create a season (name, year, config), and end it
  (archives; history stays queryable). One active season per guild (§6.12).
- **Config** — every dial in §5, laid out as one page rather than paginated.
- ~~**Flavor corpus CRUD**~~ *removed 2026-08-18 with the corpus.*
- **Roster table** — one row per player with per-row **eliminate** / **revive**
  buttons. A table beats a command signature you have to remember, and it shows
  you the state you're editing before you edit it.
- **Role pickers** — Survivor / Ghost / Sole Survivor (see §3.3).

### 3.2 Discord — no admin surface at all

> **Amended 2026-08-18.** The strict split originally kept two live mod actions
> as commands (`settle`, `preview-reckoning`). Billy cut both before stage 4
> built them, on a re-examination of the urgency claim: a stuck game's real
> deadline is *the Tuesday Reckoning*, not minutes-after-final — the Reckoning
> fires regardless, flags stragglers, and late settles post an addendum — so
> the escape hatch is a Monday-evening tool, and the dashboard (phone-usable)
> serves it better than a remembered command signature.

Both land on the dashboard panel instead (stage 4):

- **This Week's Games card** — the week's slate with live status; a stuck game
  offers per-team settle buttons (+ void), feeding the same idempotent settle
  pipeline as the poller, with the usual audit row and mod-log mirror.
- **Preview Reckoning button** — renders Tuesday's post in the panel, any time.

Survivor therefore registers **zero admin commands** in Discord — the member
surface (`/survivor pick|status|board` + the Join button) is the entire command
footprint. All admin actions → DK mod-log, as ever.

### 3.3 Roles

**The bot creates them on first season** (`🏈 Survivor`, `👻 Ghost`,
`🏈 Sole Survivor`) if they don't already exist, matching by name, and stores the
resulting ids in config. Dashboard role pickers let you repoint any of the three
at a role you made yourself, and a role deleted out from under the bot is
recreated rather than crashing the week. Creation requires Manage Roles; if the
bot lacks it, the panel says so and the game runs without role grants rather
than failing.

---

## 4. Data layer

### 4.1 Source
**ESPN unofficial scoreboard API** (free, keyless):
`site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week=N&seasontype=2`
— schedule + kickoff times (lock enforcement), live status, finals, and odds (auto-assign) in one endpoint. Unofficial and unversioned: parse defensively, and every settle path must also work through the panel's manual settle (§3.2). *(Field notes 2026-08-17, pinned by the fixture tests: the season-year selector is `dates=YYYY` — `year=` is silently ignored and serves the current season; kickoffs come as minute-precision Zulu; and **completed games carry no odds at all**, so the frozen-at-last-poll favorite is genuinely unrecoverable after the fact.)*

### 4.2 Bootstrap & polling
- `create-season` ingests the full season schedule into `nfl_games`; daily refresh (flex scheduling moves kickoffs — validate locks against *current* times).
- **Poll every 10 min during game windows** derived from the ingested schedule (not hardcoded days — catches international 9:30am ET games and late-season Saturdays); one daily off-window refresh.
- Settle each game as it goes final (idempotent: result already set → no-op). Week settles when all its games are final; the Reckoning fires Tuesday 9am regardless, flagging stragglers. *(Clarified 2026-08-17: when a flagged straggler settles after the Reckoning has posted, a short addendum goes out and any resulting death applies then — flagged results are neither silently resolved nor held for the next Reckoning.)*
- **Gauntlet replay is deterministic:** it reads only stored `favorite` values (frozen at last pre-kickoff poll) and stored winners — two joiners entering the same week always inherit identical lines, and replays never re-fetch odds. A join posts a private "gauntlet receipt" embed (week-by-week: team, result, strike events) before the confirm button, so nobody buys in blind.

### 4.3 Schema (SQLite, DK conventions)
```sql
CREATE TABLE survivor_seasons (
  id INTEGER PRIMARY KEY,
  guild_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  season_year INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'enrolling',   -- enrolling|active|complete
  config TEXT NOT NULL DEFAULT '{}'           -- json, see §5
);

CREATE TABLE survivor_players (
  season_id INTEGER NOT NULL REFERENCES survivor_seasons(id),
  guild_id INTEGER NOT NULL,                  -- denormalized (stage-4 review): the access
                                              -- export guild-scopes via this column
  user_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'alive',       -- alive|ghost
  strikes_used INTEGER NOT NULL DEFAULT 0,
  eliminated_week INTEGER,
  elimination_source TEXT,                    -- added at stage 4: picks|cap|admin|left.
                                              -- 'picks' deaths are DERIVED and a corrected
                                              -- result resurrects; the rest are decisions
                                              -- and survive recomputation
  joined_at TEXT NOT NULL,
  PRIMARY KEY (season_id, user_id)
);

CREATE TABLE survivor_picks (
  season_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,                  -- denormalized; see survivor_players
  user_id INTEGER NOT NULL,
  week INTEGER NOT NULL,
  slot INTEGER NOT NULL DEFAULT 1,            -- 2 in double-pick weeks
  team TEXT NOT NULL,                         -- ESPN abbr, e.g. 'SF'
  game_id TEXT NOT NULL,
  auto_assigned INTEGER NOT NULL DEFAULT 0,
  locked_at TEXT,
  result TEXT,                                -- null|win|loss|tie|void
  PRIMARY KEY (season_id, user_id, week, slot)
);

CREATE TABLE nfl_games (
  season_year INTEGER NOT NULL,
  week INTEGER NOT NULL,
  game_id TEXT NOT NULL,
  home TEXT NOT NULL, away TEXT NOT NULL,
  kickoff_utc TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled',
  favorite TEXT,                              -- abbr, captured at last poll before kickoff
  favorite_prob REAL,                         -- its win probability (split from `favorite` 2026-08-17:
                                              -- gauntlet determinism + auto-assign ranking need the number)
  winner TEXT,                                -- abbr | 'TIE' | null
  PRIMARY KEY (season_year, game_id)
);

-- survivor_flavor: dropped 2026-08-18 (migration 172) with the corpus feature
```

---

## 5. Config reference (season `config` JSON)

| Key | Default | Notes |
|---|---|---|
| `buyin_coins` | 0 | Season one: free entry |
| `pot_seed` | **10000** | House-seeded pot. **Amended 2026-08-17 from 5000.** See §5.1 |
| `ghost_pot_pct` | **20** | Ghost Streak side-pot's share of the seed. Was unspecified |
| `gauntlet_fee_per_week` | 50 | × weeks elapsed; alive → main pot, DOA → ghost pot |
| `weekly_win_coins` | **25** | *Added 2026-08-18:* paid at the Reckoning to every player whose picks all won that week (ghosts included; a double-pick split week earns nothing). 0 = off. **A faucet**: ~15–20 winners × 25 ≈ 400/week ≈ 60/day, own ledger kind `survivor_weekly_win`, paid in the same transaction that marks the week reckoned so it can never double-pay and the preview can never pay |
| `strikes` | 1 | 0 = sudden death |
| `tie_rule` | `loss` | `loss` \| `survive` |
| `late_entry` | `gauntlet` | `gauntlet` \| `closed` \| `ghost_only` |
| `missed_pick` | `auto_assign` | `auto_assign` \| `eliminate` |
| `max_auto_assigns` | 3 | Per season; 4th = elimination |
| `double_pick_start_week` | 14 | 0 = never |
| `double_pick_min_alive` | 5 | Only escalates if ≥ this many alive |
| `wipeout_annul_through_week` | 13 | After: equal split |
| `accord_max_alive` | 6 | `/survivor accord` available at ≤ this many living |
| `ghost_streak` | on | Side-pot % of main pot + DOA fees |
| `slate_hour`, `lastcall_hour`, `reckoning_hour` | Wed 9 / Sat 18 / Tue 9 | Guild-local, via `tz_offset_hours`. `slate_hour` added 2026-08-17 — §2.3 schedules three tasks; the table only had hours for two |
| `channel_id`, `role_survivor_id`, `role_ghost_id`, `role_sole_survivor_id` | 0 (unset) | Wiring, not rules — the #survivor channel and the three managed roles (§3.3). Added at stage 1; 0 degrades that step to skipped-and-logged, never a crash |

### 5.1 The seed is a faucet — say so out loud

With `buyin_coins: 0` there is **no player money in the main pot**. Every coin the
champion receives is newly minted, so the seed is a faucet and gets counted as
one. Decided 2026-08-17 against the numbers in
`docs/reviews/2026-07-30-economy-health.md`:

| | |
|---|---|
| Seed | **10,000** — main pot 8,000, Ghost Streak side-pot 2,000 |
| Share of the ~74,600 guild float | 13.4% |
| Against net supply growth (+5,221/day) | ~1.9 days, minted once around week 18 |
| Against p90 balance (1,304) / median (186) | 7.7× / 54× |
| Against the largest casino win ever (3,000) | 3.3× |

**Every movement rides `economy_service`** — `apply_credit` / `apply_debit` with
its own ledger kinds, never a bare wallet UPDATE, so the whole feature is visible
in the economy metrics rather than appearing as unexplained mint:

| Ledger kind | Direction | When |
|---|---|---|
| `survivor_buyin` | debit player | join, when `buyin_coins > 0` |
| `survivor_gauntlet_fee` | debit player | late entry, `fee_per_week × weeks` |
| `survivor_payout` | credit player | champion, equal split, or annul-era split |
| `survivor_ghost_payout` | credit player | Ghost Streak side-pot, ties split |
| `survivor_weekly_win` | credit player | weekly all-wins prize, paid at the Reckoning (added 2026-08-18) |

The seed itself is **booked, not minted, at create-season** — the pot displays
truthfully all season and the 10,000 enters supply exactly once, at payout.
Gauntlet fees and buy-ins are recycled player coin: they raise the pot without
raising the float, so a pot that grows past the seed is not extra faucet.

---

## 6. Edge cases
1. **Thursday trap:** unpicked at Thursday kickoff = nothing missed; only Thursday teams leave the menu. Locks are per-game.
2. **Flex scheduling:** validate locks against current ingested kickoff, refreshed daily.
3. **International 9:30am ET games:** last-call DM names any early-window games explicitly.
4. **Byes:** filtered in autocomplete *and* validated server-side.
5. **Opposing picks, same game:** two players on opposite sides is fine — settle both.
6. **Timezones:** store UTC, render `<t:...>` everywhere.
7. **Idempotent settling:** no double eliminations, no duplicate Reckonings.
8. **Double-pick weeks:** two slots, one fate — either loss burns the strike/kills; both teams burn regardless.
9. **Gauntlet joins:** replay uses closing favorites only; a week with a voided/postponed chalk game replays as void (survive, team returns). Joins during a double-pick era replay both slots as top-two chalk.
10. **Joiner with no legal team in their entry week** (e.g., joins Sunday night and every unplayed team is gauntlet-burned): that week is voided for them — survive, pick next week. Rare, but must not crash or auto-kill.
11. **Accord discipline:** invocations outside the Tue–Thu window are rejected with the window shown; a player who leaves the server during a vote counts as a decline; accord during a locked pick voids nothing retroactively — the split is computed on invocation-day standings.
12. **One active season per guild;** archives queryable.
13. **Auto-assign dead end** *(added 2026-08-17)*: at the final kickoff every legal not-yet-kicked-off team is burned for the player → week voided (survive), no auto-assign charged. Mirrors #10; must not crash or auto-kill.
14. **Member leaves the server mid-season** *(added 2026-08-17)*: eliminated at the next Reckoning with its own fixed line (*"left the server mid-season. Eliminated."*); ghost streak frozen at its best; no further picks accepted. Rejoining the server reactivates nothing for the living game — dead is dead — but a rejoined ghost may resume Ghost Streak picking. Inside an Accord vote, leaving still counts as a decline (#11).

---

## 7. Build order
1. Schema + season/config admin commands
2. Schedule ingest + ESPN parser (fixture-based tests on saved JSON)
3. Join / pick / status / board — lock + validation logic
4. Polling loop + idempotent settle engine + manual settle
5. Reckoning, slate, last-call tasks ~~+ flavor corpus CRUD~~ *(corpus removed 2026-08-18)*
5b. Gauntlet replay engine + receipt flow
6. Ghost roles + Ghost Streak (v1 core), wipeout/annul logic, Accord vote flow, endgame + coin payouts (main + ghost pots)
7. **v2 backlog:** buyback window (escalating coin cost — natural sink), dead pool, loser-pool & underdog off-season variants, playoff survivor capstone
8. **Decided 2026-08-17: go live Week 1** and let the meadow learn by dying. No mod
   test league — the Gauntlet means a member who shows up in Week 4 still gets a
   live game, so the usual reason to soft-open a pool (miss the start, miss the
   season) doesn't apply here. Enrollment opens as soon as join/pick/slate is
   testable, ~Sept 3, with real stakes from Sept 10.
9. **Testing rig (added 2026-08-18):** seasons with `season_year >= 2090` are
   **synthetic** — the poller never calls ESPN for them (a nonsense year would
   be served the *current* season and pollute the table), and the dashboard
   grows a Simulator card: generate a compressed schedule (weeks measured in
   minutes), settle kicked games (chalk/random/upset — through the same
   manual-settle pipeline, so grading stays derived), and ▶ Run Weekly Tasks
   forces the Reckoning/panel-repost past their clock gates while the
   once-per-week state keys still prevent double-posts. A whole season runs in
   an evening in a test channel; the rig hard-refuses real season years.
   ▶ Run Weekly Tasks shipped on the Season card by mistake and **moved to the
   Simulator card 2026-08-18**, where this section always placed it: the weekly
   cadence is automatic in production (the poll loop fires each task at or
   after its guild-local hour and catches up if the bot slept), so a manual
   force is a rig affordance, not an operator control. A real season therefore
   has no force button at all.
9b. **Role reconcile (added 2026-08-18, Billy's #10):** every weekly-task
   decision pass also repairs life-state roles — alive players hold 🏈
   Survivor, ghosts hold 👻 Ghost, enforced through the idempotent
   `swap_member_roles`, which checks the gateway role cache first, so a
   no-drift pass costs zero Discord calls. Exists for drift: a join that
   crashed after charging but before its grant, a hand-removed role, a
   rejoin. Best-effort per member, never blocks the pass.
10. **A forced run reports what it did (added 2026-08-18):** every gate routes
   through the week lookup, so a season whose year has no ingested schedule is
   indistinguishable from a quiet week — all three gates return `None` and the
   forced run posts nothing. `run_weekly_tasks` therefore returns one record
   per season (`fired`, `blocked`, `reason`) and the dashboard shows it: what
   posted, or why nothing was due ("no schedule ingested for 2035…", "already
   run for this week", "…was due but couldn't post"). The `post_*` helpers
   return a bool so the report can't claim a task fired when its channel was
   unreachable. The route also **scopes the run to the caller's guild** — a
   forced run is an admin acting on their own server, and the unscoped call
   dragged every other guild's live season past its clock gates too.
