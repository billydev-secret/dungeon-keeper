# Embed filler-text review — member-facing copy (2026-09-03)

Billy: *"a large review of the user facing embeds for filler text, let's keep it
clean out there."*

Scope, per Billy's calls at the top of the session:

- **Filler means copy that doesn't earn its space** — a slot restating what
  another slot already said, a field whose whole value is a placeholder, an
  instruction to press a button that is visibly right there, an empty state that
  announces emptiness without a next step. Not literal stub text: `Coming soon`,
  `TBD`, `Lorem` and `Under construction` measure **zero** in this repo.
- **Everything member-facing, games included** — all 363 `discord.Embed(`
  constructions were in scope, filtered to cards a member actually sees.
- **This review edits nothing.** A parallel session (`embed-style-review`) is
  rewriting the same strings for *form*; this one judges *substance* and hands
  over proposals. Every "Proposed" line below is a proposal, not a decision.

Baseline: `main` @ `916f5981`. `embed-style-review`'s four unmerged commits were
diffed against every file cited here — its edits are section spacing, `•`
separators and casing. **No finding below is already fixed on that branch**, and
none of the proposals collide with its changes.

---

## Headline

The copy in this bot is, on the whole, written by someone who cares. The casino,
the rolling playlist, the bounty board and `member_info` write empty states like
*"Nothing on the board yet — post the first one."* and *"No XP yet — chat a
little and it starts counting."* — a sentence plus a real next step, which is
exactly what the guide asks for. There is no sea of padding to drain.

What there is instead is **one systemic habit with two faces**, both of them
concentrated in the same place: the **game lobby card**.

| # | Finding | Sites | Where |
|---|---|---|---|
| 1 | The card says its own name twice — footer is character-for-character its own title | **10** | 8 game modules, 8 of them `build_lobby_embed` |
| 2 | A field whose entire value is `—` / `(empty)` / `(nobody yet)` | **12** | game lobbies + music queue |
| 3 | The AMA recap thanks you twice on one card | 2 | `games_ama` |
| 4 | The XP leaderboard says "no XP" five times on one card | 5 | `cogs/xp_cog.py` |
| 5 | Voice panel puts "join the Hub" in the description *and* the footer | 2 | `voice_master` |
| 6 | DM settings panel: title, description and field name are three synonyms | 3 | `dm_perms` |
| 7 | Non-actionable "check back soon" nudges | 2 | quests, mahjong |
| 8 | A fresh lobby showing `Participants 0` and `Questions Asked 0` | 2 | `games_traditional` |

Findings 1 and 2 together are the review: **~22 of the ~30 findings live on
lobby cards**, and a lobby card is the single most-rendered thing in the bot —
it is what a member stares at while waiting for a game to fill.

---

## 1. The lobby card names itself twice

Ten cards set a footer that is *character-for-character identical to their own
title*. Measured per function (a footer compared only against the title in its
own builder, not any title in the file — the naive file-wide match reports 22
and is wrong):

| Site | Builder | The string, printed twice |
|---|---|---|
| `games_ama/embeds.py:48` | `build_lobby_embed` | `🎙️ Anonymous AMA` |
| `games_ama/embeds.py:114` | `build_main_embed` | `🎙️ Anonymous AMA` |
| `games_ama/embeds.py:197` | `build_panel_embed` | `🎙️ Anonymous AMA` |
| `games_clapback/embeds.py:84` | `build_lobby_embed` | `⚔️ Clapback` |
| `games_compliment/embeds.py:57` | `build_lobby_embed` | `💛 Spin the Compliment` |
| `games_fantasies/embeds.py:29` | `build_lobby_embed` | `✨ Fantasies & Dealbreakers` |
| `games_mfk/embeds.py:74` | `build_lobby_embed` | `💍 {labels}` |
| `games_mlt/embeds.py:74` | `build_join_embed` | `👑 Most Likely To` |
| `games_story/embeds.py:56` | `build_lobby_embed` | `📖 Story Builder` |
| `games_ttl/embeds.py:59` | `build_lobby_embed` | `🤥 Two Truths and a Lie` |

**This is not the game-signature rule misfiring.** `embed_style_guide.md`
§Footers sanctions a *Game signature* footer — `{GAME_ICON} Game Name • extra` —
and gives its reason: *"every game card signs itself so screenshots stay
attributable."* On a card titled `⚔️ Head to Head — Round 2, Matchup 1/3` that
footer earns its keep, because the title alone doesn't name the game. On a card
already titled `⚔️ Clapback`, attribution is **already achieved by the title**,
and the footer spends the card's last line repeating it.

`games_fantasies/embeds.py:25-30` is the starkest version. The whole card is:

```
title       ✨ Fantasies & Dealbreakers
description Submit anonymously each round, then vote!
field       Host — <name>
footer      ✨ Fantasies & Dealbreakers
```

Two of its four lines are the game's name.

**Proposed:** keep the signature, but make it carry the `• extra` the guide's own
format already allows — the thing the title can't say. On a lobby that is the
host and the fill state; mid-game it's the round.

```python
# games_clapback/embeds.py:84
embed.set_footer(text=f"{ICON} Clapback • Hosted by {host_name}")
# games_ttl/embeds.py:59
embed.set_footer(text=f"{GAME_ICONS['ttl']} Two Truths and a Lie • {len(players)} in")
```

Two builders in the repo already do exactly this and are the models to copy:
`games_rushmore/embeds.py:129` footers `_footer(host_name)` — the host, not the
game name — and `games_traditional/embeds.py:73-76` appends `• One category each`
when single-choice is on, so its footer only duplicates the title in the *other*
branch. Neither appears in the table above, because neither repeats itself.

---

## 2. A field whose entire value is a placeholder

Twelve member-facing cards add a field whose whole value is `—`, `(empty)` or
`(nobody yet)`. This one is **deliberate and documented**, which is why it needs
a ruling rather than a patch — four separate docstrings state the intent:

- `games_mfk/embeds.py:45` — *"Empty list renders as `"—"` so the field always has a value."*
- `games_compliment/embeds.py:38` — same sentence.
- `games_mlt/embeds.py:52` — *"An em-dash is shown when no one has joined yet so the field never renders empty."*
- `music/embeds.py:43` — *"list renders `(empty)` so the embed never has no fields."*

The convention is "a field must never be empty, so fill it". The guide's
§Empty states rule is the opposite: *"Empty states are a short plain sentence,
no emoji … Add a nudge when there's an obvious next step."*

The cost is sharpest where **the field name already carries the fact**:

```python
# games_ttl/embeds.py:58
embed.add_field(name="Players (0)", value="—", inline=True)
# games_story/embeds.py:47
embed.add_field(name="Writers (0)", value="—", inline=False)
```

`(0)` already says the list is empty. The `—` says it a second time, and neither
tells the reader that pressing **Join** is what fixes it.

| Site | Currently | Proposed |
|---|---|---|
| `games_ttl/embeds.py:58` | `Players (0)` / `—` | `Players (0)` / `Nobody yet — press Join to play.` |
| `games_story/embeds.py:47` | `Writers (0)` / `—` | `Writers (0)` / `Nobody yet — press Join to write.` |
| `games_mfk/embeds.py:70` | `Pool (0)` / `—` | `Pool (0)` / `Nobody yet — press Join to get your three names.` |
| `games_mlt/embeds.py:71` | `Players (0)` / `—` | `Players (0)` / `Nobody yet — press Join to play.` |
| `games_compliment/embeds.py:53` | `Pool (0)` / `—` | `Pool (0)` / `Nobody yet — press Join to get spun.` |
| `games_clapback/embeds.py:71` | `(nobody yet)` | `Nobody yet — press Join to play.` |
| `games_rushmore/embeds.py:125` | `(nobody yet)` | `Nobody yet — press Join to play.` |
| `games_ama/embeds.py:181` | `🙋 Panel` / `—` | drop the field; the description already says *"Tap 🙋 Volunteer to join the panel"* |
| `music/embeds.py:66` | `Up next` / `(empty)` | drop the field; when nothing is queued *and* nothing is playing, one honest line beats an empty label |

**A constraint any fix must respect** — `games_story` and `games_ttl` edit their
roster **by field index** (`set_field_at(0, …)` at `cogs/games_story_cog.py:160`
and `:179`, and the `games_story/embeds.py:35-38` docstring says so explicitly:
*"the join button updates it in place by editing field index 0"*). So on those
two the field **must keep existing** — change the value, never delete the field,
or the first Join writes the roster into the wrong slot.

`games_ama/embeds.py:181` also renders `🙋 Panel` with **no count** while the
populated branch renders `🙋 Panel (N)` — so the empty card is inconsistent with
its own filled state as well as padded.

---

## 3. The AMA recap thanks you twice

`games_ama/embeds.py:294-311`, one card:

```
title       🎙️ Anonymous AMA — Game Over
description Thanks for playing! Here's how the session went:
… fields …
footer      🎙️ Thanks for playing Anonymous AMA!
```

*"Thanks for playing"* appears twice, the game's name three times, and
*"Here's how the session went:"* is a colon-label introducing fields that are
visibly right underneath it.

**Proposed:** description carries the one fact the fields don't — how it went in
a sentence — and the footer keeps the signature only.

```python
description=f"**{total_q}** questions, **{total_answered}** answered. Thanks for playing!"
embed.set_footer(text=f"{GAME_ICONS['ama']} Anonymous AMA")
```

---

## 4. The XP leaderboard says "no XP" five times

`cogs/xp_cog.py:331-352`. When a server has no XP events, the card renders:

```
description No XP recorded yet.
💬 Text            No tracked text XP yet.
↩️ Replies          No tracked reply XP yet.
🎙️ Voice           No tracked voice XP yet.
🖼️ Image Reacts    No tracked image react XP yet.
footer      Top 5 by XP source and time window
```

Five sentences and a footer to communicate one fact: nothing has happened yet.
The footer describes a table that isn't there.

**Proposed:** when `not has_events`, send the description alone with a nudge and
no fields — *"No XP recorded yet. Chat or hop in voice and it starts counting."*
(matching `member_info/embeds.py:43`, which already words it exactly that way).
Keep the four per-source fields for the populated card, where they're doing real
work.

---

## 5–8. Smaller ones

**5 — Voice Control panel** (`voice_master/embeds.py:106-121`). The description
opens *"Join the Hub voice channel to spin up your own room"* and the footer
closes *"Menus act on the channel you own. Don't own one? Join the Hub."* Both
halves of the footer restate the description — the scope point is already made
in bold at line 110 (*"manage **the channel you currently own**"*). **Proposed:**
drop the footer; the description covers it.

**6 — DM settings panel** (`dm_perms/embeds.py:262-291`). Title *"📬 Your DM
Settings"*, description *"Control how people may request DM access with you."*,
field name *"Your DM Modes"* — three ways of saying the same thing before any
content appears. Separately, the `Connections` field's entire value is an
instruction (*"Pick someone below to see whether you're connected…"*) with no
data in it — a field slot spent on a caption for the select underneath it.
**Proposed:** drop the description (title says it); rename the field to the
member's actual state; fold the Connections caption into the select's own
`placeholder=`.

**7 — Nudges that aren't actionable.** `economy/quest_views.py:272`
*"No active quests right now — check back soon!"* and
`games/mahjong/embeds.py:78` *"No Meadow Card is active right now — check back
soon."* The guide asks for a nudge *"when there's an obvious next step"*; when
there isn't one, "check back soon" is a padded way of ending the sentence.
**Proposed:** either say when (*"the next board posts Monday"*) or stop at the
plain sentence.

**8 — A lobby proud of its zeroes.** `games_traditional/embeds.py:135-137` puts
`Participants 0` and `Questions Asked 0` on a card for a game that has not
started. `Questions Asked` on a fresh lobby can only ever be 0. **Proposed:**
drop `Questions Asked` from the lobby builder — `build_tod_embed` already adds it
once play begins and there is something to count.

**Deixis, minor.** `cogs/guess_cog.py:1428` *"Click below to play."* adds nothing
a visible button doesn't. Contrast `dm_perms_service.py:759` — *"Set your own
preference with the **My DM Settings** button below"* — which names *which*
control, and earns its line. The rule that separates them: naming the control is
information; pointing at it is not.

---

## Considered, and deliberately NOT reported

Listing these because each one looks like filler and isn't, and a future sweep
will find them again:

- **The 70 zero-width spaces.** Mandated by §Section spacing, applied via
  `apply_section_spacing`. Not filler; do not strip.
- **Game-signature footers in general.** Sanctioned by §Footers. Only the **10**
  in Finding 1, where the footer equals its own title exactly, are reported.
- **`—` as a table cell.** `mahjong/sim_logic.py:536-539`, `survivor/embeds.py:35`,
  `member_info/embeds.py:92`, `services/guess_embeds.py:93`. In a column of
  values, `—` is the correct typography for "no data" and keeps rows aligned.
  Only `—` as a card's *entire field value* is reported.
- **The jail policy-vote skeleton** (`jail/embeds.py:97-99`). Yes/No/Abstain all
  render `—` when a vote opens, which establishes the card's shape before the
  first vote so the layout doesn't jump as counts arrive. Deliberate; leave it.
- **`placeholder=`** — 125 uses, the discord.py select/modal API parameter.
  Required UI text.
- **The Todo board** — 82 `TODO` hits are the shipped feature, not code markers.
- **Casino flavor copy.** *"Bright lights, long odds, and questionable financial
  decisions."* is voiced, not padded. `cogs/casino/embeds.py` is the strongest
  copy in the repo and is the standard the rest should be read against.
- **Sanctioned next-step footers.** `economy/shop.py:360` *"Pick one below to buy
  it."* is a Next-step hint, which §Footers explicitly allows.

---

## Open for Billy

**The standing rules blurb.** Several lobbies reprint how-to-play on every
render — `games_clapback/embeds.py:54-56` (*"Join the battle of wits! Write the
funniest answer to each prompt, then vote head-to-head."*),
`games_ttl/embeds.py:47-50`, `games_fantasies/embeds.py:27`. Whether these are
filler depends on a fact only Billy has: **how many first-timers the rooms
actually get.** If the regulars know the games, that's another ~8 findings and
the lobbies get materially shorter. If new members are the point, every one of
them stays. **Not reported as findings pending that call** — they are listed
here, not counted above.

**A guide rule?** Findings 1, 2 and 4 are all one idea: *a card should not spend
a slot saying something another slot already said.* §Card anatomy rules on which
slot does which job but never says slots must not duplicate. If that's worth
writing down, note `documentation-review` currently owns `docs/`.

---

## Method, and what would be missed

Every one of the 363 sites was covered by pattern batteries (deixis, placeholder
values, empty states, footer/title equality, flourish copy) run across all 106
files; the high-traffic member-facing builders were then read in full —
`cogs/casino/embeds.py`, `games_clapback`, `games_ttl`, `games_ama`,
`games_story`, `games_traditional`, `games_mfk`, `games_mlt`, `games_compliment`,
`games_fantasies`, `voice_master`, `dm_perms`, `music`, `music_playlist`,
`cogs/xp_cog.py`, `economy/quest_views.py`, `economy/shop.py`, `jail/embeds.py`.

Taking the sibling review's warning seriously — *"watch the sweep catch a known
positive first"* — the title/footer sweep was validated against the clapback
lobby found by hand before it was trusted, and its first (file-wide) form was
discarded for over-reporting 22 against a true 10.

**What this method would miss:** copy assembled at render time from fragments in
a cog rather than written as a literal in a builder. Findings 4 and 8 were both
found that way and only because those two cogs were read in full; a card built
by string concatenation across several helpers could still hide padding no grep
here would surface.
