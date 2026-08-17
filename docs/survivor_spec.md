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
- **Late entry — the Gauntlet.** Join any week; the bot retroactively plays every eligible missed week, assigning each week's chalk (highest win-probability team by closing odds, no reuse) and grading it against real results. You inherit that line's full fate: those teams are burned from your satchel, one chalk loss burns your strike, two and you arrive dead — landing directly in Ghost Streak, picking from day one like everyone else. The gauntlet's toll is structural: chalk torches the elite teams, so survivors arrive alive but poor.
- **The gauntlet fee (anti-free-option):** the receipt shows your inherited fate *before* you pay — which would make waiting free information. So late entry costs `gauntlet_fee_per_week × weeks elapsed` (default 50 coins/week), **independent of the base buy-in** — this is what closes the exploit even in a free-entry season. Alive arrivals' fees feed the main pot; **dead-on-arrival fees route to the Ghost Streak side-pot** — you pay into the game you're actually playing.
- **Mid-week joins:** weeks where any games haven't kicked off yet are picked live from the remaining slate; only fully-kicked-off weeks are gauntlet-replayed.

### 1.2 Weekly pick
- One team per week to **win, straight up**. Each team usable once per season.
- **Per-game locking:** your pick locks at *that team's* kickoff. Until then you can change freely. After your team kicks off you're locked, even if other games haven't started.
- **Total darkness, loose lips:** the *bot* never reveals a pick to anyone (admins excepted, mod-log only) until the Tuesday Reckoning — no reactions, no "X has picked" notices. Players, however, may say anything. **Table talk is legal; lying is encouraged.** The bot keeps the secrets; the bluffing is yours.
- **Missed pick → auto-assign:** at the week's final kickoff, anyone pickless is assigned the highest win-probability available team still unplayed (ESPN odds; fallback best record), marked `📎 assigned by the groundskeeper` on reveal. Dignified, mildly embarrassing, fully survivable.
- **Auto-assign cap (anti-AFK):** three auto-assigns per season (config), counting one per week even in double-pick weeks and even when only one slot was missing. The fourth time the groundskeeper is needed, he declines — elimination, with its own flavor line: *"the groundskeeper stopped covering for X."*

### 1.3 Results
- Win → survive. **Loss → strike/elimination. Tie → loss.** ("Your team must win" — one sentence, no asterisks.)
- Postponed/cancelled game → pick voided, player survives, team returns to their pool.

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

### 1.7 The dead
- Elimination = `👻 Ghost` role, channel access unchanged, one warm condolence DM. Heckling from the graveyard is a feature.
- **Ghost Streak (v1 core — promoted):** ghosts keep picking via the identical flow; longest post-death correct streak takes the side-pot (funded by a % of the main pot + DOA gauntlet fees; streak-length ties split it). **Your satchel follows you into death** — no-reuse continues, so early ghosts are rich in teams and late ghosts scrape. **Ghosts always pick one team**, even in double-pick weeks — the escalation exists to end the main game, and the streak game stays simple for casuals. Load-bearing by design: gauntlet joiners who arrive dead land here, so anyone can join any week and always have a live game to play.

---

## 2. Player experience

### 2.1 Journey
```
Season announced → Join (coins) → [Wed slate → pick → sweat → Tue Reckoning] → death → Ghost life → endgame ceremony
```

### 2.2 Season announcement
Pinned embed in #survivor: hero copy (~3 lines), buy-in, live entrant counter, five one-line rules, link to full rules thread, **[🌾 Join the Season]** button → ephemeral confirm (coin balance, debit, one-sentence rules) → role grant. At Week 1 kickoff the button stays live but the copy flips to gauntlet mode: *"the season is underway. the door is open; the road is real. 🌾 N souls walking."* Joins from here run the gauntlet receipt flow (§4.2) before charging the buy-in.

### 2.3 Weekly cadence
- **Wed ~9am — Slate post** (pings `@🏈 Survivor`): the week's games with `<t:...:f>` timestamps (local time for free), plus the **[🏈 Make your pick]** button. Footer: picks close at each kickoff · X of N alive have picked.
- **Sat 6pm — Last call:** DM only to the pickless (opt-out honored; fallback channel mention): *"you haven't picked. `/survivor pick` — or I'll pick for you, and I have terrible taste. 🌙"*
- **Sun–Mon:** the bot posts nothing. The channel does the sweating.
- **Tue 9am — THE RECKONING** (pings role; see 2.5).

The role gets pinged exactly twice a week. Restraint is the brand.

### 2.4 Pick flow
- **Primary — `/survivor pick`:** autocomplete filtered to unburned ∩ playing ∩ not-yet-kicked-off, options like `49ers (vs SEA, Sun 1:25)`. Confirmation (ephemeral): team + opponent, lock time `<t:...:R>`, satchel count with wealth signal (`🟢 24 teams left` → `🟡` under 12 — ambient scarcity awareness, never advice), strike status, **[Change pick]** / **[My season]** buttons.
- **Double-pick weeks:** the same command collects two teams in one flow; the confirmation shows both slots with their independent lock times. Changing one slot never unlocks the other.
- **Secondary — slate button:** ephemeral **AFC/NFC dual select** (Discord's 25-option cap vs up to 32 legal teams early season). Keeps casuals off slash-command syntax entirely.
- `/survivor status` (ephemeral): pick state, lock countdown, satchel, strike, ghost stats if dead.

### 2.5 The Reckoning — one post, three acts
1. **The toll:** week number, survivors before → after, pot, one rotating flavor line (*"week 7 came for the overconfident. the meadow is quieter now."*) Plus **the gate**, when anyone joined since last Tuesday: arrivals announced with their gauntlet fate — *"two souls walked the gauntlet this week. one arrived breathing."*
2. **The ledger:** the only place picks ever appear — every living player → team → `✅ / 💀 / 💛→🖤 / 📎 auto`, deaths sorted first. Ghost Streak standings get a compact strip beneath (current streaks, record holder).
3. **Eulogies:** one flavor line per elimination, name-slotted (*"pour one out for **@Loaf**, who believed in the Jets the way children believe in summer. 🥀"*). Ghost roles applied on post. Zero deaths: *"everyone lives. suspicious. the meadow remembers."*

**Flavor corpus:** DB table (~20 eulogies, ~10 no-death, ~10 toll lines), admin CRUD via `/survivor admin flavor`, seasonal drift encouraged — same rotating-corpus pattern as the ToD cooldowns.

### 2.6 Boards
- `/survivor board` (public, auto-posted after each Reckoning): alive roster with weeks survived + strike hearts, graveyard with week-of-death, pot, most-burned teams meta-stat.
- `/survivor history [@player]`: revealed picks only. Nothing current ever leaks.

### 2.7 Endgame ceremony
Champion gets a dedicated post (never folded into a Reckoning): season stats (weeks survived, closest call, teams ridden), pot receipt, role, optional Bios-cog trophy line. Splits framed as shared victory; annulments as their own mini-event (*"everyone died. therefore nobody did. the week is stricken — but the teams you burned stay burned."*)

### 2.8 Notifications & consent
`/survivor notifications`: per-category DM toggles (last call, condolence), default ON. Closed DMs fall back to channel mention.

---

## 3. Admin

- `/survivor admin create-season` — modal: name, buy-in, strikes, double-pick start, wipeout boundary.
- `/survivor admin config` — paginated settings embed with toggle buttons; **every rule in §1 is a setting, not code** (see §5).
- `/survivor admin settle <game> <winner>` — the API-failure escape hatch; feeds the normal pipeline silently.
- `/survivor admin preview-reckoning` — renders Tuesday's post to the mod channel Monday night.
- `eliminate` / `revive <player>`, `end-season` (archives; history stays queryable).
- All admin actions → DK mod-log.

---

## 4. Data layer

### 4.1 Source
**ESPN unofficial scoreboard API** (free, keyless):
`site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week=N&seasontype=2`
— schedule + kickoff times (lock enforcement), live status, finals, and odds (auto-assign) in one endpoint. Unofficial and unversioned: parse defensively, and every settle path must also work through `admin settle`.

### 4.2 Bootstrap & polling
- `create-season` ingests the full season schedule into `nfl_games`; daily refresh (flex scheduling moves kickoffs — validate locks against *current* times).
- **Poll every 10 min during game windows** derived from the ingested schedule (not hardcoded days — catches international 9:30am ET games and late-season Saturdays); one daily off-window refresh.
- Settle each game as it goes final (idempotent: result already set → no-op). Week settles when all its games are final; the Reckoning fires Tuesday 9am regardless, flagging stragglers.
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
  user_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'alive',       -- alive|ghost
  strikes_used INTEGER NOT NULL DEFAULT 0,
  eliminated_week INTEGER,
  joined_at TEXT NOT NULL,
  PRIMARY KEY (season_id, user_id)
);

CREATE TABLE survivor_picks (
  season_id INTEGER NOT NULL,
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
  favorite TEXT,                              -- abbr + win prob, captured at last poll before kickoff
  winner TEXT,                                -- abbr | 'TIE' | null
  PRIMARY KEY (season_year, game_id)
);

CREATE TABLE survivor_flavor (
  id INTEGER PRIMARY KEY,
  category TEXT NOT NULL,                     -- eulogy|toll|no_death|annul
  line TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
```

---

## 5. Config reference (season `config` JSON)

| Key | Default | Notes |
|---|---|---|
| `buyin_coins` | 0 | Season one: free entry |
| `pot_seed` | 5000 | House-seeded pot (admin funds at create-season) |
| `gauntlet_fee_per_week` | 50 | × weeks elapsed; alive → main pot, DOA → ghost pot |
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
| `lastcall_hour`, `reckoning_hour` | Sat 18 / Tue 9 | Guild-local |

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

---

## 7. Build order
1. Schema + season/config admin commands
2. Schedule ingest + ESPN parser (fixture-based tests on saved JSON)
3. Join / pick / status / board — lock + validation logic
4. Polling loop + idempotent settle engine + manual settle
5. Reckoning, slate, last-call tasks + flavor corpus CRUD
5b. Gauntlet replay engine + receipt flow
6. Ghost roles + Ghost Streak (v1 core), wipeout/annul logic, Accord vote flow, endgame + coin payouts (main + ghost pots)
7. **v2 backlog:** buyback window (escalating coin cost — natural sink), dead pool, loser-pool & underdog off-season variants, playoff survivor capstone
8. Soft-launch with the mod team as a test league… or go live Week 1 and let the meadow learn by dying
