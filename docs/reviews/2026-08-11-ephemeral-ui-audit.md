# Ephemeral UI audit — which games, which moments, which modals (2026-08-11)

Todo #96: *"review other games that could have ephemeral UI instead of what
they're using now"*, widened on request to cover two more axes — private
moments inside **multiplayer** games, and places where an **ephemeral panel
would beat a modal**.

This was a recommendation, not a change. **Every finding it raised has since
been worked** — see the "Shipped" notes under E3, M1, M2, M3 and M4, all
2026-09-01; E1 and E2 needed no work. Nothing here is outstanding.

**Verdict up front.** The solo-play question is closed: every one-person game in
the bot is in the casino, and the five that were still public are being moved
now. The multiplayer question is close to closed too, but for a reason worth
recording — submission confirmations are *already* ephemeral almost everywhere,
and the public one-person messages that remain are **turn pings, which cannot
become ephemeral because an ephemeral message does not notify anybody**. The
modal axis is where the real remaining wins are, and the best of them is not in
a game at all.

| # | Axis | Finding | Cost |
|---|---|---|---|
| [E1](#e1) | Solo | Every solo game is in the casino; five are moving now, four already moved | — (this round) |
| [E2](#e2) | Multiplayer | Submission confirmations already ephemeral across the games — no work needed | none |
| [E3](#e3) | Multiplayer | The remaining public one-person messages are turn pings and **must stay public** — but Story leaves a dead turn panel behind every turn | S |
| [M1](#m1) | Modal | Fantasies asks members to **type** one of two categories, and a reject discards their entry | S |
| [M2](#m2) | Modal | Every casino bet costs a modal round-trip to type a number | M |
| [M3](#m3) | Modal | Roulette makes you type a number 0–36 | S–M |
| [M4](#m4) | Modal | Two more constrained-value asks typed as free text (birthday month, voice user limit) | S |

---

<a name="e1"></a>
## E1 — The solo-play question is closed

Searched every game cog for one-person play. The result is cleaner than
expected: **there is no solo game outside the casino.**

- **Already ephemeral** (shipped before this round): Coinflip, Slots, Blackjack,
  War. Each renders a private machine that edits itself in place.
- **Moving now** (#94/#95): Roulette, Derby, Baccarat, Dice, Keno.
- **Everything else is genuinely multiplayer**: Risky Rolls, Quickdraw, Chicken,
  Hot Potato, Musical Chairs, Pressure Cooker, LegitLibs, Duels, Guess, Price is
  Right, and the `games_*` family. These have real spectators and should stay
  public.

Worth noting for the record: todo #94 also named "Betflip", which does not exist
under that name anywhere in the repo. It is almost certainly Coinflip, which had
already been ephemeral since 2026-07-24.

<a name="e2"></a>
## E2 — Multiplayer submission confirmations are already ephemeral

The obvious candidate inside a group game is the moment one player submits
something private — an answer, a statement, a hot take. Checked all of them, and
the hygiene is already good:

| Game | Confirmation | Already ephemeral? |
|---|---|---|
| Fantasies | "Your … has been submitted!" | yes |
| Hot Takes | "✅ Hot take submitted! Total submissions: N" | yes |
| Two Truths & a Lie | "✅ Your statements have been submitted!" | yes |
| AMA | "✅ Your question has been submitted for host review." | yes |
| Price is Right | "✅ Scenario submitted!" | yes (+ `delete_after=5`) |
| Clapback | "Answer submitted! …" | yes |

No work needed. This axis is done and was done before anyone asked.

<a name="e3"></a>
## E3 — The public one-person messages that remain are pings, and must stay

After E2, the public messages aimed at a single member are these:

- `games_ttl_cog.py:487` — `"{member} It's your turn!"` (`delete_after=15`)
- `games_story_cog.py:410` — `"{mention} — it's your turn! You have N minutes…"`
- `games_rushmore_cog.py:945` — `"{m} ⏱️ Time's up! Your pick was skipped."`
  (`delete_after=10`)
- `games_price_cog.py:739` — `"<@{host}> — write this round's scenario!"`

These look exactly like the clutter #95 is about. **They still cannot move.**

An ephemeral message produces no notification — Discord delivers it only into
the open client of someone already looking at the channel. A turn nudge whose
entire job is to reach someone who is *not* looking is the one message type
ephemerality actively breaks. Converting these would silently stall games
whenever the pinged player had the tab closed.

Cleanup is a different question from ephemerality, though, and checking each one
individually is what turned up the one real finding here. Three of the four are
already tidy:

- **TTL** and **Rushmore** are bare pings with `delete_after` — correct as-is.
- **Price is Right** attaches a view, then explicitly `prompt_msg.delete()`s it
  when the host is done (`games_price_cog.py:749`, `:774`) — also correct.
- **Story** attaches a view and, when the turn resolves, disables the buttons
  and edits the message (`games_story_cog.py:420-424`) — and then **leaves it in
  the channel forever**.

So Story accumulates one dead "it's your turn!" panel per player per round for
the life of the game. In a 5-player, 4-round story that is 20 spent panels
interleaved with the story itself, which is a bigger contribution to channel
mess than anything the casino was doing.

**Recommendation: delete Story's turn panel when the turn resolves**, the way
Price is Right already deletes its host prompt. The story text itself is posted
separately, so nothing is lost with the panel.

Note that `delete_after` is **not** the fix for Story or Price — both messages
carry the interactive view the player is meant to click, and a timer would take
the button away mid-turn. Delete-on-resolve is the pattern; `delete_after`
belongs only to the two bare pings that already use it.

**Cost: S** — one delete call plus its failure branch, in a file no other
session is touching.

**Shipped 2026-09-01.** `games_story_cog.py` now deletes `turn_msg` when the
turn resolves, before the game-closed break, so a story closed mid-turn leaves
nothing behind either.

<a name="m1"></a>
## M1 — Fantasies asks members to type one of two words · **best win in the audit**

`cogs/games_fantasies_cog.py:49-59`:

```python
class SubmitEntryModal(discord.ui.Modal, title="Submit a Fantasy or Dealbreaker"):
    category = discord.ui.TextInput(
        label='Type "Fantasy" or "Dealbreaker"',
        max_length=20,
        placeholder="Fantasy",
    )
    entry = discord.ui.TextInput(..., max_length=500)
```

A **binary choice collected as free text**, next to a 500-character entry box.
`normalize_category` (`games_fantasies/logic.py:37`) is lenient — anything
starting with `f` or `d`, case-insensitive, stripped — so most typos survive.
But when it does return `None`, `on_submit` returns early with an ephemeral
error and **the modal is already closed, so the member's 500-character entry is
gone.** They retype the whole thing.

The loss window is narrow (an empty category, or a word starting with some other
letter — "I think…", "erotic…", "no idea"), but it is entirely self-inflicted:
the field has two valid values and Discord has had buttons and selects the whole
time.

**Fix:** two buttons — *Fantasy* / *Dealbreaker* — each opening the modal with
only the entry box, category already decided. One tap replaces a typed word,
`normalize_category` and its failure branch disappear, and the data-loss path
disappears with them. **Cost: S.** One cog, one logic function deleted, its
tests fold into the button test.

**Shipped 2026-09-01**, one better than proposed: rather than a picker step
before the modal, the round's submit message carries *Submit a Fantasy* and
*Submit a Dealbreaker* directly, so the fix costs **no** extra tap.
`normalize_category` is deleted.

<a name="m2"></a>
## M2 — Every casino bet costs a modal round-trip

Every wager in the casino goes through `_AmountBetModal`
(`cogs/casino/views.py:60`): press the game button, a modal opens, type a number,
submit. That is one modal per bet, and prod says a single player often places
several bets in one round (roulette 16 of 61 player-rounds, derby 16/64,
baccarat 7/22, dice 6/18).

To be fair to it, the modal is well built — the label carries live limits
(`"Your bet (5–100 · 340 left today)"`) and the box pre-fills the member's last
bet on that game from `_last_bets`. Nobody learns a limit from an error.

But the pre-fill is the tell: the code already knows the answer is usually "the
same as last time". An ephemeral panel could offer *Last bet (25) · Min · 50 ·
Max · Custom…* as buttons, with **Custom…** opening today's modal for the rare
free-form case. Most bets become one tap; the typing path survives for anyone
who wants it.

**Cost: M**, and it should **wait for the #94/#95 rewrite to land** — that work
is already restructuring how these games open and repaint, and the bet-entry
surface is the natural next layer on top of it rather than a competing edit to
the same file.

**Shipped 2026-09-01**, once that rewrite had landed. The ladder is
`casino_logic.bet_amount_options` — **Last · Half · Double · Max** off the
remembered stake (Billy's pick over a fixed 10/25/50/100), falling back to
**Min · a round middle · Max** on a first bet. Every rung is capped by the
table maximum, the balance *and* the daily-cap headroom together, so a tap
can never be refused; with no legal stake at all the surface falls back to
the modal, whose service call gives the real reason. A press inside a private
surface replaces it in place rather than opening a second message, and on the
five private-round tables the step carries **Back** (and restores the board on
timeout) because it covers the round's own board.

<a name="m3"></a>
## M3 — Roulette makes you type a number 0–36

`RouletteBetModal` (`cogs/casino/views.py:141`) has a second field labelled
`"Your number (0–36)"`. Red/black and the dozens are already buttons
(`RouletteBetButton`); only the straight-up number is typed.

37 values does not fit one Discord select (25-option cap), which is presumably
why it is a text box. Two selects (0–18 / 19–36), or a select for the tens and a
second for the unit, would both work — but this is genuinely more fiddly than M1
and the payoff is smaller, since straight-up bets are a minority of roulette
action.

**Cost: S–M.** Reasonable to fold into M2 when the bet surface is rebuilt;
not worth a standalone change.

**Shipped 2026-09-01** with M2, as suggested. Two selects split the wheel
0–18 / 19–36, and `RouletteBetModal` lost its number box along with the
0–36 validation branch behind it — by the time that modal opens the bet is
fully decided and only the stake is open.

<a name="m4"></a>
## M4 — Two more constrained values typed as free text

Swept every `TextInput` label in the bot. Beyond the casino and Fantasies, two
ask for a value from a small fixed set:

- **`"Month (1–12)"`** (`cogs/birthday_cog.py`) — 12 values, fits one select
  comfortably. A select also kills the "is 03 valid?" question and the parse
  branch behind it.
- **`"User limit (0–99)"`** (`commands/voice_master_commands.py`) — 100 values,
  but the useful ones are few. Buttons for *None / 2 / 4 / 6 / 10* plus
  *Custom…* would cover nearly all real use.

Everything else that is typed genuinely wants free text — confessions, whispers,
questions, topics, nicknames, story text, "Never have I ever…", search boxes.
Those are correct as modals and should stay.

**Cost: S each**, independent of the casino work, and both sit in features
nobody is currently editing.

**Both shipped 2026-09-01.** The birthday month is a **select inside the
modal** (discord.py 2.7.1 supports one via `ui.Label`, which this audit assumed
was unavailable), so it costs no extra step and the "must be between 1 and 12"
branch is gone; the day stays typed because 31 values overflow the select cap
and its bound depends on the month. The voice limit became the ladder above.

---

## Suggested order

1. **E3** (Story's leftover turn panels) — the largest actual reduction in
   channel mess in this document, and a one-line-ish fix.
2. **M1** (Fantasies) — small, removes a data-loss path, touches a file no
   other session is in.
3. **M4** (birthday month, voice user limit) — small, independent, safe.
4. **M2 + M3** together, *after* the #94/#95 casino rewrite lands, as one
   deliberate pass over the bet-entry surface.

E1 and E2 need no work. The "must stay public" half of E3 is a do-not-attempt
note rather than a backlog item — recorded so the next person scanning for
public one-person messages doesn't spend the afternoon I just spent concluding
they are movable.
