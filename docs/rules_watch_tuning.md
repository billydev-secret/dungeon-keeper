# Rules Watch — tuning spec

**Status:** Design, partially implemented. Written 2026-07-19 from a full audit of
guild `1469491362444480666` history (393k messages with content, 2025-04 → 2026-07),
all moderation tickets, `💛│golden-girls`, and `🏢│mod-chat`; reviewed and corrected
against the DB, with model/prompt/context tuning measured on a GPU host.

**Why this exists.** Rules Watch flagged 98.7% of everything it evaluated and every
human-labeled event was a false positive. This records *why*, what the real bannable
patterns look like, and what to change.

| | |
|---|---|
| ✅ **Done** | §2.1 boundary-gate fix — 45.4 → **7.7 alerts/day** (−82.9%), `tests/test_rules_watch_scorer.py` |
| 📊 **Measured** | §12 model/prompt/context sweep — ceiling **0.66** (Nemo-12B Q4_K_M); **0.61** on a 4060-sized build (Nemo-12B IQ4_XS + `baseline_plus`), vs 0.52 today. Noise floor ±0.02 |
| ⬜ **Not started** | everything in §7, the §11 card, and the §2.2 model decision |

**Companions:** `tests/data/rules_watch_eval.jsonl` (57 labeled real messages — read
§13 on what it does and doesn't measure), `scripts/rules_watch_tune.py` (sweep
harness), `scripts/rules_watch_model_swap.sh` (remote server swap/restore).

**The one-line conclusion:** the LLM cannot do detection at any size tested; the
arithmetic can. Ship deterministic detection with the model as a false-positive
suppressor and card-phraser, never as the decider.

---

## 1. Current state

| metric | value |
|---|---|
| events logged (2026-06-21 → 07-19) | 1,294 |
| flagged | **1,277 (98.7%)** |
| human-labeled | 277 — **all 277 `is_violation=0`** |
| distinct authors flagged | 93 |
| `guard_confidence` == exactly 0.90 | 1,112 of 1,277 (87%) |
| top reason | "slur" / "identity attack" — **946 (74%)** |

`guard_confidence` is a constant, not a probability. Every downstream priority
score built on it (`priority_score`, `priority_tier` — 1,110 of 1,294 events are
`immediate`) is sorting noise.

The problem users **were** flagged (velocibaker 31×, ciccio 11×, whoami23 9×) at
confidence 0.80–0.99 — statistically indistinguishable from the 90 people who were
not. The signal existed and was drowned.

Roster correction: the users actually **banned** are ciccio, whoami23 and Rere34.
velocibaker was not banned — he got a soft private redirect from a friend, then
left on his own (`current_member=0` for that reason; Billy: *"Hope he takes a
breath and comes back"*). Rere34 predates Rules Watch entirely (last message
05-15; the system started 06-21), so he could never have been flagged.

**No alert has ever been posted to Discord.** `rules_watch_channel_id` is unset for
the guild and `alert_message_id` is NULL on all 1,294 events (`monitor.py:401-405`
bails). All 277 labels came through the dashboard, from a single labeler. Nothing
has interrupted anyone yet — so whatever is wired to a channel first will set the
lasting impression of this feature.

---

## 2. Root-cause bugs

### 2.1 The boundary gate reads the wrong person's message — `scorer.py:111`, `monitor.py:128`

`check_boundary_token(content)` runs on **the message being judged**, i.e. the
author's own words. A boundary event means *the target* said no and the author
continued. As written it fires when the author says "no".

Token frequency across all 1,292 evaluated messages with content:

| token | events | share |
|---|---|---|
| **`no`** | 924 | **71.5%** |
| `stop` | 108 | 8.4% |
| **`red`** | 82 | 6.3% |
| `go away` | 11 | 0.9% |
| `yellow` | 9 | 0.7% |
| (no token) | 170 | 13.2% |

Consequences:

- `boundary_token_crossed` is set on **87%** of rows. The field is noise and the
  scorer weights it.
- Bare `red`/`yellow` as safewords match colour words. Real example, logged as
  *"slur and identity attack"*: `The red and black combo 😍 omg! And your liiiiips 😍`
- Other real flags: `no milk, extra butter in the pan`, `No thanks 😂😂😂`, `Oh no I'm poor`.

The token check is also the **pre-filter** (`monitor.py:144-149`) — it decides which
messages reach the LLM at all. So this one bug sets the entire system's volume.

**✅ FIXED 2026-07-19.** `detect_boundary_crossing(conn, guild, channel, author,
target, ts)` replaces `check_boundary_token(content)` in the gate. A boundary event
now requires the **target** to have signalled stop **to this author**: either
replying directly to them, or following something the author said in-channel within
a 6h window. A stop-signal replying to somebody else is attributed to that person.
Bare `no` is honoured only as a whole-message refusal in a direct reply
(`_SOLO_NO_RE`); `red`/`yellow` are removed. Covered by
`tests/test_rules_watch_scorer.py` (27 cases, written to fail first).

**Measured effect** — all 1,318 historical events replayed through both gates:

| | events | per day |
|---|---|---|
| old gate | 1,318 | 45.4 |
| **new gate** | **225** | **7.7** |
| reduction | | **82.9%** |

`boundary_token_crossed` drops from 1,144 events to 34. Survivors now enter via
**vader 170**, boundary 34, slur 11, persistence 10 — so negative sentiment is now
the dominant gate and is the next thing to examine, not the boundary logic.

### 2.2 The guild forces a 4-bit 3B model — `config` table

```
guild_id=0                    ai_mod_model = claude-sonnet-4-6
guild_id=1469491362444480666  ai_mod_model = Llama-3.2-3B-Instruct-Q4_K_M.gguf   ← override
```

The global default is Sonnet; the main guild overrides to a 4-bit 3B running
CPU-only on a 2-core R1600 at ~68 s/check. That model cannot do the judgment this
task needs — it emits `{"verdict":"flag","rule":"2","confidence":0.9}` for
essentially any input, and labels the result "slur".

**Decide deliberately.** Removing the override sends member message content to an
external API. That is a privacy posture change and is the owner's call, not a
default. If it stays local, §7 (deterministic features) carries the load and the
LLM should be a narrow confirmer, not the primary engine.

**Two refinements from the measured sweep (§12), both of which cut against an
earlier draft of this section:**

- The prompt is *not* independently at fault. `_RULES_WATCH_SYSTEM` does say "flag
  generously", anchor on "slur" as the first criterion, and embed `0.85`/`0.9` in
  its JSON examples — and rewriting it helped the 3B and 8B. But on Mistral-Nemo-12B
  the same rewrite **collapsed recall to 0.03–0.11**, and the *original* prompt
  scored best. Prompt and model must be tuned together; never ship a prompt change
  without re-running the sweep on the target model.
- Model **character beats size**. Nemo-12B (permissive) beat Qwen2.5-14B
  (safety-tuned) by +0.08 with fewer parameters; Qwen repeatedly declined to engage
  with explicit content. For this server, prefer a permissive base model. Size
  itself plateaus after ~8B.

The **privacy-safe way to settle the ceiling question** ("is this task hard, or are
local models weak?") is `scripts/rules_watch_seeds.json` — 15 labelled *synthetic*
windows. Those can be run against Sonnet with zero real member content leaving the
network. Do that before deciding on the override.

### 2.3 What is NOT broken

`detect_slur` (`scorer.py:128-145`) is well built. It deliberately excludes
`slut`/`whore`/`cunt` with a comment explaining that they are consensual
vocabulary here. **Leave it alone.** The "slur" reasons in the log come from the
LLM, not from this regex.

---

## 3. The finding that reorders everything

**Explicit content is uncorrelated with complaints.** Every ban and revocation in
the dataset was driven by SFW or near-SFW conduct.

| person | action | cited conduct explicit? |
|---|---|---|
| Rere34 | **banned** | No — pretextual DM request, channel-following |
| Ciccio | **banned** ("coercive behavior") | No — reporter: *"it's been sfw the entire time"* |
| Whoami23 | spicy revoked | No — the word *"Wow"*, and naming her Reddit |
| ds_01 | warned + promotion frozen | No — a parenthetical about a voice note |
| Eye of 2wilight | promotion withheld | No — *"Small brains big boobs"* in a SFW channel |
| velocibaker | soft private redirect | Yes — but rate/breadth, no single line |
| Defwolf420 | **nothing** | (unsolicited DM) |

In the same eleven minutes, same channel, same woman as velocibaker's flagged line,
these drew **zero** complaints:

- `Lemme boop those booba`
- `You look so fucking good.. as I am getting in the shower 😉`
- `They look perfect!!`

A classifier keyed on explicitness inverts this dataset.

The community has its own name for the distinction. mimi, twice, independently:
> *"I feel a bit sexually objectified **and not in a fun sexy way**"*
> *"Got my eyes on that one, **and not in the fun sexy way!**"*

---

## 4. The five discriminators (in members' own words)

### 4.1 Rapport / familiarity — the load-bearing variable

> **bagel:** *"That's always fucking weird when dudes I don't know participate in a
> conversation they're not included in salaciously. **Like guys I know the two of us
> are friendly with? Okay that can be funny. There's a familiarity.**"*
> **lily:** *"And like bro I don't know you that way."*
> **mimi:** *"uhhh I don't know ypu and now I feel a bit sexually objectified."*
> **Billy:** *"He's just a little **ahead of the rapport curve**"* / *"being salacious and
> **we don't have that relationship**."*
> **Chi-Gal:** *"I'm sure he's seeing other people do it but… **some people have known
> each other a long time**."*

Same sentence, different relationship history, different verdict. This is *the* feature.

**It is computable, and it works.** Pair reciprocity for velocibaker's targets,
from `reply_to_id` edges:

| target | his | hers | reciprocity | outcome |
|---|---|---|---|---|
| juliet | 9 | 2 | **0.22** | deleted her post — *"I can't take it sorry"* |
| Olivia | 16 | 6 | **0.38** | — |
| Skittly | 15 | 7 | **0.47** | solicited on camera at 48h |
| Lucia | 77 | 38 | **0.49** | his #1 target, met 7 days earlier |
| Cat Bae | 31 | 27 | **0.87** | consensual, no complaint ✅ |
| Little Loaf | 148 | 95 | 0.64 | 2-year friend ✅ |

Two caveats, both important:

- **bagel scores 0.66** — inside the healthy band. The static ratio would *not* have
  caught the person who actually reported him. Her reply *length* did not drop
  (25.7 → 28.3 chars), so "shorter replies" is the wrong frame. Nor is it gradual
  decay: **27 of her 36 early replies came on a single day (07-08)**. From 07-09 she
  replied ≤3/day to him while replying 24–117/day to everyone else, and his volume
  quintupled. The pattern is **one burst of engagement, then sustained non-response
  under rising volume** — simpler than trend estimation, and close to
  `persistence_count` / `thread_reciprocity`, which already exist but are computed
  over a 30-message window rather than per day.
- **`reply_to_id` coverage is fine** — an earlier draft claimed otherwise and was
  wrong. 90% of text comments in the photo channels carry `reply_to_id`
  (19,791/22,069); every heavy commenter is ≥77%; bigprop03 has 726 resolvable
  edges. Replies are the spine. Building pairs by time-window proximity *instead*
  actively degrades the signal: it drops Little Loaf (2-year friend) 0.64 → 0.35
  and bagel → 0.31, i.e. it misreads healthy pairs as predatory. Use proximity only
  to fill the ~10% gap.

### 4.2 Being uninvited to the exchange

Highest-signal single behavior in the corpus — it produced complaints from four
different women, from **one word** each time.

> **Chi-Gal** on *"Small brains big boobs"*: *"**you weren't even talking to him**"*
> **mimi**, on whoami23's *"WOW"* into her flirt with bagel — she self-censored 24
> seconds later: *"Oof okay maybe I shoulda rembered I am in public after all 😅"*

That retraction is a **machine-readable ground-truth label**: a participant
retracting or self-censoring within ~2 min of an uninvited salacious interjection.
Mine it historically to build training data.

### 4.3 Sexualizing content she didn't put in front of you

whoami23 naming lily's Reddit post in-server (*"stalker-ish"* — Dona); Rere34 using
her bio as a DM pretext; velocibaker finding lily's Reddit via another server and
DMing her there undisclosed, which lily read as *"basically to bypass the DM bot"*.
Dona's rule: *"keep reddit compliments there and server compliments here."*

Every instance of this in the corpus produced a ticket. Near-zero false positive rate.

### 4.4 Attention-contingent pressure — the ban tier

Not sexual at all, and it is what actually gets people removed.

> **lily:** *"if I interact with anyone there **he immediately messages me within a
> minute or two**"* … *"He will send a message then follow up later with **'have I made
> you mad'**"* … *"he would **hound me to send him selfies**… **he said it's because I
> needed help with my self-confidence when like… I never made that request of him**"* …
> *"**It kinda feels like being watched.**"*
> **Birdie:** *"he came at me about not messaging him again"* … *"**the way he makes me
> feel bad if I don't respond to him** really got to me today. **It's draining. It's like
> I can't move in there without being made to feel guilty.**"*
> **Little Loaf:** *"**manipulative with his emotions**"* / *"a redirection **'pay
> attention to me'** kind of feeling."*

### 4.5 Response to correction — real, but confounded (and not an early signal)

> **bagel:** *"**if a man does well with redirection and is respectful, I think that is
> worth more than just a guy who seems normal and cool.**"*

| subject | response to correction | outcome |
|---|---|---|
| bigprop03 | respected an SFW-only boundary, disengaged | cleared by the women over a mod's suspicion |
| velocibaker | one gentle DM → apologized profusely, left | soft redirect only |
| ds_01 | *"seemingly unwelcome"*, *"what I propose is…"*, *"the **intent** of rules"* | warned, promotion frozen permanently |
| whoami23 | *"I'm not what you're describing"*, *"I didn't think it was that much of a deal"* | access revoked |
| Ciccio | kept going after two warnings | **banned** |

**Justify-before-acknowledging** is the escalation signature — but do not overstate
it, as an earlier draft did by calling it "the strongest predictor".

Only **8 cases** in the whole corpus have both a confrontation and a recoverable
response. Within those it separates outcomes 4/4 in each direction, but response
class is **perfectly collinear with complainant count and prior record**: ds_01,
whoami23 and Ciccio all had priors; Burner and velocibaker had none. This data
cannot separate the two variables. The *"accepts → cleared"* arm is well supported
(two subjects were factually in the right and still led with acknowledgment; in
ticket #7 the moderator apologised to the accused). The *"justifies → escalates"*
arm is **not identified** — those three may have escalated on record alone.

**Rere34 breaks it outright**: banned with no ticket and no chance to respond. So it
is not a necessary condition. The better-supported measurable predictor is
**complainant count** (Ciccio: Chi 06-15 → Lucia+lily 06-29 → lily 06-30 → Birdie
07-16 → ban).

**Do not train on ticket close reasons.** They are misleading in both directions:
#23 closed *"Ciccio understands and got the message"* → banned 31 days later; #34
(whoami23) closed warmly *"we're solid 😊"* while mod-chat that same day already read
*"Don't promote"* / *"Ban and call it a day"*. Ground truth lives in mod-chat and in
`known_users.current_member`.

---

## 5. Failure modes, and which are detectable in public

| mode | exemplar | public trace? |
|---|---|---|
| **A — ahead of the rapport curve** | velocibaker | **Yes.** Rate, breadth, reciprocity, want-verbs. |
| **B — cross-platform pursuit** | whoami23, Rere34 | **Yes.** One message is enough. |
| **C — attention-contingent DM coercion** | Ciccio | **Barely.** See below. |
| **D — consent-bot evasion w/ rehearsed excuse** | bigoryx | **Partly** — the excuse is often said in public. |
| **R — imported reputation** | ryan8102, brizillaking1, ghostkiller1034 | **No, and correctly so.** Decided on cross-server history that never touched this server. A classifier should see nothing here. |

**Mode C is the hard one.** Ciccio's 3,165 public messages are essentially clean —
his public relationship with lily scored as one of the healthiest on the server *on
the day she filed the ticket about him*. Rate, breadth and reciprocity all fail.
Two catchable public surfaces exist:

1. **Consent-gate negotiation, in the open, on day 8** — to Birdie, 2026-04-30:
   *"I don't know how to use the ask bot, or have access to it. But if I did I'd ask
   it if it was ok to dm you."* → Birdie: *"Idk how to use it either, lol - Yes we
   can DM."* **That is the exact channel the ban came out of 2.5 months later.**
   He used the DM bot 3 times in 3 months.
2. **Withhold-after-offer** — 2026-05-16: *"had a bit of a sad night last night"* →
   *"wanna talk about it?"* → *"**I do, but I don't wanna drag anyone down with my
   stupid depresh**"* → she offers DMs unprompted two minutes later. Same play on
   Birdie the next day, ending in *"I gotchu too. Promise promise 😘😘"* — a pledge he
   collected on two months later.

Mode D has the same shape: bigoryx used a **reusable script** — *"the request bot
wasn't working"* / *"I couldn't figure it out"* / *"I hate the bot"* — against three
women across two servers and ten months. Little Loaf: *"'I hate the bot' means he
hates having a trail of consent and DMed without it."*

---

## 6. Must-not-fire (encoded as negatives in the eval set)

### 6.1 Reciprocal play
`Pleeeeease 🫠` → *"You keep begging like that and I could absolutely be convinced"* /
*"You're doing so good"*. Repeated asks after a non-answer — and the target is
directing it. **The target's own escalation is the exempting signal.**

Cat Bae ↔ velocibaker is the critical pair: **lexically identical** to his flagged
material, but she proposed every step (*"Let's get together and count freckles"*),
reciprocity 0.87, no complaint. Any rule that catches *"can I please taste?"* must
leave this alone.

### 6.2 Refusal phrases are ~90% non-refusals
Hits on *"I said no"* / *"not comfortable"* are overwhelmingly narration or game
answers — *"Have you ever been asked to do something you weren't comfortable with —
'Yes of course I have and I said no'"*; Chi-Gal's *"I SAID NO QUESTIONS"*.
**Require a request from a different author in the preceding window.**

### 6.3 A clean no, handled correctly
> **Burner:** *"If I asked you for a pic of titty taco, would you send it to me"*
> **Amberosia:** *"No"*
> **Burner:** *"-# I just made it up but I think titty taco is your boob but inside a tortilla"* / *"# DEVASTATING"*

Solicitation → flat refusal → immediate de-escalation, zero re-ask. This is the
shape of a non-violation.

### 6.4 Per-user priors must not leak into per-message labels
velocibaker also wrote *"Not without consent, don't jinx my dice! 😜"* and
*"Aw you're sweet, I'm he/him."* Ciccio wrote hundreds of ordinary compliments
nobody objected to. **Compliment density alone must not fire.**

### 6.5 The intake ritual would trip the classifier
2026-03-27: a new member said *"I'm not really interested in nsfw conversations…"*
The owner correctly said the questions were optional — then posted the turn-ons list
anyway, and she left: *"I'm not comfortable talking about what turns me on."* Any
"asks about turn-ons after a stated boundary" rule needs an intake exemption, or the
ritual needs a rethink.

### 6.6 Same vocabulary, female subject → no harm complaint
Heli was described with the identical *"zoomie energy" / "over eager"* vocabulary
used for velocibaker, and it produced watchfulness, not a complaint. Useful control.

---

## 7. Recommended changes

Ordered by (value ÷ effort).

1. **Fix the boundary gate** (§2.1). Check the *target's* recent messages, not the
   author's. Drop bare `red`/`yellow`. Expect flag volume to fall by most of the 71.5%
   currently driven by the word "no".
2. **Stop trusting `guard_confidence`.** It is a constant. Remove it from
   `priority_score` until a model produces a real distribution.
3. **DM-consent tripwire.** Flag any public message containing DM-access intent
   (`can I dm you`, `is it ok to message you`) and/or a bot-disclaimer
   (`don't know how to use the bot`, `couldn't figure it out`, `the bot wasn't
   working`, `I hate the bot`). Record the (author, target) pair as an off-bot DM
   channel. Low volume, near-zero false positives. **Would have put ciccio → Birdie on
   record 2026-04-30, and matches bigoryx's reusable script.**
4. **Cross-platform reference detector.** Fires when A names B's off-platform
   presence (`reddit`, `insta`, `OF`, `snap`, `telegram`, `your post`, `your bio`)
   and B has not mentioned it in the preceding window. **Escalate hard on ≥2 distinct
   targets** — that is what separated whoami23 from a one-off warning.
5. **Third-party salacious insertion.** Salacious reply into an active two-person
   exchange the author isn't addressed in, where he has no rapport with either.
   Standalone signal per the owner. Label positive when a participant self-censors
   within ~2 min.
6. **Pair rapport, computed properly.** `exchanges`, `days_known`, `reciprocity`, built
   from channel+time-window proximity to the poster (not `reply_to_id` — §4.1).
   Gate intensity on it.
7. **Rate and breadth per user-day — but MUST be constrained by pair-newness.**
   velocibaker: >15 directed comments/day across >8 recipients, hitting 58% of images
   within an hour; verified to first trip on **2026-07-14, four days before bagel
   reported him.** ⚠️ Unconstrained, this same threshold trips on **280 user-days**
   historically — led by lily (56), Lucia (42), Chi-Gal (22), Birdie (13) and Billy
   (13). Raw rate/breadth is a *most-beloved-member* detector and is the highest
   downside-risk item in this document. What separated velocibaker was rate/breadth
   **plus** every target being known <14 days. Gate on median pair-age (or a
   per-user z-score against their own baseline) and re-measure before shipping.
8. **Reply-rate decay.** Target's reply rate to *this* author falling while the
   author's volume rises — normalized against the target's baseline reply rate to
   *other* commenters in the same threads. Catches the bagel case, which §6 static
   reciprocity misses.
9. **Want-verb / imperative feature.** Grammatical shift from *she is X* (describing
   the image) to **I want / let me / can I / show me / get back here** (first-person
   want-verb with the recipient as object). Cheapest reliable lexical feature here.
10. **Endearment-vs-acquaintance-age.** `babe`/`hun`/`darling`/`gorgeous` directed at
    someone known <72h.
**Not a detector — ticket-time decision support only:** *correction-response scoring*
(§4.5). It fires **inside tickets**, i.e. after a human is already engaged, so by
definition it cannot produce an early nudge. It is also statistically confounded
(§4.5). Useful as a note on a ticket; not a trigger.

**Deprioritize entirely** — effectively uncorrelated with violations here:
explicitness, `please`/`pretty please`, `send me`, `show me`, `need`, `I said no`,
`not comfortable`.

---

## 8. The canonical taxonomy — Dona's guides

**This section supersedes anything derived in §4/§7 that conflicts with it.** Dona
wrote two guides in `🏢│mod-chat` *before* this spec existed, and they encode the
same distinctions more cleanly. The guard prompt should quote these rather than the
generic server rules.

- **"🔞 SERVER INTERACTION & ETIQUETTE GUIDE"** — 2026-07-07
- **"✧ HOW TO FLIRT & CHAT RESPONSIBLY ✧"** — 2026-07-08

**§1 is the rapport curve, stated as policy:** *"RAPPORT BEFORE FLIRTING… Jumping
straight into highly explicit or aggressive flirting with someone you don't know
feels intrusive and uncomfortable."* The progression is explicit and is effectively
a state machine — **casual chat → light flirting → explicit**, the last *"only after
mutual interest is crystal clear"*. Skipping a stage is the detectable event.

**Compliment rubric** (use verbatim in the prompt — better than the want-verb
feature in §7.9, and it comes with the server's own examples):

| 💚 green — fine | 🚨 red — concerning |
|---|---|
| style, energy, confidence, general aesthetic | specific anatomy; demands a reaction; assumes immediate sexual access |
| *"You look absolutely incredible in that outfit!"* | *"I want to see what's under that outfit, slide into my DMs."* |
| *"Your confidence in these photos is amazing. Love your vibe!"* | *"You have the perfect body for [act], let me show you."* |
| *"That colour looks stunning on you, wow."* | *"Damn, your [body part] is driving me crazy, send more."* |

Those three red examples are near-templates for velocibaker's actual messages
(*"the rare and elusive Bagel boobs"*, *"all the touching you could take"*,
*"Reeeeally hoping you demonstrate how well you take it"*).

**Traffic lights — this should be the classifier's output vocabulary**, replacing
the binary flag/ok. A binary verdict can't tell the owner *how hard to land it*:

| light | Dona's definition | intervention |
|---|---|---|
| 🟢 | fast replies, matching the flirtatious tone, asking questions back | none |
| 🟡 | *"short/one-word answers, taking hours to reply while active in public chat, or ignoring the flirty parts of your text while only replying to casual parts"* | the public nudge |
| 🔴 | *"saying stop, ignoring you entirely, dry/cold responses, profile says No DMs"* | mod action |

The **yellow definition is the §4.1 bagel feature, written by the community first**,
and all three clauses are computable. The middle one — replying slowly *while active
elsewhere* — is better than a raw reciprocity ratio because it self-controls for the
target simply being busy.

**Correction-response is written policy**, which settles the §4.5 confound for
practical purposes: *"Accept 'No' Gracefully… Pushing for explanations, getting
defensive, or whining will result in a ban."* It need not predict outcomes
statistically to be worth encoding — it is a stated rule.

### 8.1 Two conflicts to resolve (one is actively exploitable)

1. **The guide says ask publicly; Rule 5 says use the bot.** *"Always ask publicly
   first: 'Hey, is it cool if I DM you about your latest post?'"* That gap is exactly
   the cover story Ciccio used — *"I don't know how to use the ask bot… but if I did
   I'd ask if it was ok to dm you"* — and Birdie said yes. bigoryx ran the same
   script across two servers for ten months. **Consequence for §7.3: public
   DM-asking is endorsed and must NOT fire.** Fire only on the *bot-disclaimer*
   ("don't know how to use it", "it wasn't working", "I hate the bot"). Narrower rule,
   fewer false positives, targets the actual evasion.
2. **Slur vocabulary.** The guide prohibits `bitch`/`slut`/`whore`/`cunt` *"used to
   demean, insult, or objectify users against their will… only allowed if explicitly
   and enthusiastically consented to within specific, designated kink or roleplay
   channels"*. `scorer.py:133-136` excludes them outright as consensual vocabulary.
   Policy is context-dependent; the code is a blanket allow. Neither matches.

Also worth noting Dona already identified the top risk: *"Unsolicited Direct Messages
are the #1 reason members leave."*

## 8.2 Vocabulary the community actually uses

Ordered by how load-bearing each term was in producing action. Directly usable as
guidance text.

| term | means |
|---|---|
| *"not in the fun sexy way"* | **the problem is not explicitness** — best single heuristic in the corpus |
| *"thirsty"* / *"here for the NSFW access"* / *"looking for one thing"* | present for access, not the person |
| *"stalker-ish"* / *"creeping on Reddit"* | cross-platform pursuit |
| *"it kinda feels like being watched"* | surveillance affect |
| *"a bit much"* / *"a lot"* / *"big energy"* | rate complaint |
| *"zoomies"* / *"over eager"* / *"excessive"* | rate complaint, milder |
| *"whiny"* / *"not remorseful"* | correction-response complaint |
| *"draining"* / *"made to feel guilty"* / *"pushy"* | **coercion tier** |
| *"manipulative with his emotions"* | **coercion tier** |
| *"off"* | low-confidence unease, still registered |

---

## 9. Observed threshold map

What actually happens at each level. Any classifier is approximating this.

| signal | outcome observed |
|---|---|
| suspicious surface pattern, no conduct (JayGuerrero) | promotion withheld, no contact |
| one awkward/anatomical line to a stranger (Eye of 2wilight, Nismowood) | promotion withheld, informal pushback |
| high rate/breadth in *permitted* channels (velocibaker) | private friend-to-friend DM |
| boundary respected but motive suspect (bigprop03) | nothing — women overrode mod suspicion |
| unsolicited DM, isolated (Defwolf420) | **nothing at all** |
| unsolicited DM + arguing with enforcement (ds_01) | warning + permanent promotion freeze |
| cross-platform naming + prior declined contact (whoami23) | access revoked |
| attention-monitoring + guilt pressure, repeated after warnings (Ciccio) | **ban** |
| pretextual consent + channel-following, target went invisible (Rere34) | **ban** |

Since 2026-07-12, Chi-Gal polls `💛│golden-girls` before granting NSFW access
(*"if I had checked with women first I wouldn't have made the mistake with Who Am I"*).
**That poll is what the classifier is really being asked to approximate.**

---

## 10. Data limits

- **Content blackout 2026-05-29 → 06-18.** 23,498 rows have NULL content; timestamps
  and authors survive. Ciccio's first strike ticket (`ticket-chigal76-0615-1906`) is
  inside it and is unrecoverable.
- **Synthetic rows 2026-05-29 → 06-09** in `processed_messages`/`xp_events` are
  fabricated — do not train on them.
- **Spicy channels are wiped periodically.** `🫦│spicy-chat` content only begins
  2026-03-21. The highest-risk channels are the least observable. bigoryx has *zero*
  surviving messages despite a fully documented ban.
- **`known_users` was backfilled late.** Anyone banned before ~April has no row;
  resolve them via `🏢│leave-join-log`, whose webhook rows carry the joiner's real
  `author_id`.
- **A second blackout**, not previously recorded: ticket channels created on/after
  2026-07-14 that are absent from `known_channels` are also fully NULL — #35, #37,
  #41, #42, #43, #44. #37 is the substantial loss (60 messages, Billy + Chloee).
- **The jail and warnings subsystems have never been used on a real offender.**
  All 7 `jails` rows and 4 of 5 `warnings` rows are bot tests and jokes (*"being
  gorgeous"*, *"forgot Brandon's birthday"*, *"1m test jail to see if bot works"*).
  Only warning id 5 (ds_01) is real. There is **no ban/kick table at all** — bans
  exist only as mod-chat narration and ticket close reasons. Any design that treats
  those as live enforcement paths is wrong.
- **`iluvyerflaps` could not be identified** — zero hits anywhere in the DB.
- **`noz86` is not a Meadow member** — his rows belong to guild `1358148226850492618`,
  which stores no content at all.

---

## 11. Alert budget and card copy

The owner's stated goal is narrower than a verdict engine:

> *"I don't need it to be perfect, but I do want it to be useful to triggering a
> human review… if I can jump in early with a public nudge, it's helpful."*

That reframes everything below. **Earliness beats precision**, and precision can be
low *provided the card is safe to be wrong about* — which is a copy problem, not a
threshold problem.

**Budget: 1–5 cards/day.** Above ~10/day it gets ignored; the evidence is already in
the DB — 1,017 of 1,294 events sit unlabelled in the dashboard queue. The §2.1 fix
lands volume at **7.7/day**, which is inside range before any detector improves.

**Earliness, measured:** the rate/breadth detector first trips on velocibaker
**2026-07-14 — four days before bagel reported him**. The DM-consent tripwire hits
Ciccio on **04-26 and 04-30**, ~11 weeks before his ban, at a corpus-wide cost of
0.17 hits/day.

**Downside risk — the metric that actually matters here**, since a wrong nudge
damages a relationship the owner relies on:

| risk | detector |
|---|---|
| low | DM-consent ledger; cross-platform *with* the directedness filter — both cite a concrete act |
| medium | rate/breadth **with** pair-newness |
| **high** | rate/breadth **unconstrained** (would nudge lily, Chi-Gal, Burner, and Billy); third-party insertion standalone; endearment standalone |

⚠️ Third-party insertion is riskier than §7.5 implies: in the same window as
velocibaker's *"Get back here and let me adore you"*, another member had said
*"No need to run, stayyyyy"* seconds earlier. Bystanders chiming in is normal here —
only rapport separates the two, so this detector inherits the full difficulty of the
rapport feature rather than sidestepping it.

**And note the precedent that even a correct nudge has a cost:** velocibaker got the
gentlest possible intervention — a private DM from a personal friend — apologised
profusely, and **left the server**. Billy: *"Hope he takes a breath and comes back."*
Cards must therefore bias toward *watch* over *act*.

### Proposed card

Pattern-level (user-week), never message-level. Leads with the observation, includes
the counter-evidence, uses the mods' own vocabulary, and states plainly that nothing
has been alleged.

> **Rules Watch — heads-up (pattern, not incident)**
> **Raptor** may be getting ahead of the rapport curve in #flash-channel.
>
> Last 3 days: 53 directed comments to 15 members; joined the thread on 59% of
> photos within an hour. Most of these members he's known under two weeks.
> Balanced back-and-forth with Cat Bae; near-zero response from bagel and juliet
> despite continued comments.
>
> Nobody has complained and no boundary has been crossed. This card exists so a
> light public redirect can happen *before* anyone has to file anything. If he's
> just enthusiastic, dismiss it — that's a valid outcome.
>
> Suggested ladder: keep watching → have a friend say hi → soft DM
> (*"you're coming in a little hot with folks who don't know you yet"*).
>
> **[Looks fine] [Keep watching] [I nudged]**

**Relabel the buttons.** The current verdict-shaped pair ("Confirmed violation /
False positive") forces exactly the framing to avoid; the withdrawal-escalation
string in `monitor.py:479` (*"Target went silent after the flagged message"*) has the
same accusatory tone. The three buttons above still capture labels — *fine* →
negative, *nudged* → positive, *watching* → defer.

## 12. Tuning results (measured 2026-07-19)

Four models × four prompts × three context levels, replayed over the 57-case eval
set against a GPU llama-server (~0.2 s/call vs ~68 s on the prod CPU). Harness:
`scripts/rules_watch_tune.py`; server swap/restore: `scripts/rules_watch_model_swap.sh`.

**Metric note.** F1 and precision are useless here: the eval set is 65% violation,
so a flag-everything policy scores P=0.65 for free, and the live guard "wins" on F1
purely by never saying ok (TNR = 0.00 across 57 cases). Use **balanced accuracy**
= (TPR + TNR) / 2, where 0.50 is a coin flip.

| model | params | best BalAcc | best config |
|---|---|---|---|
| Llama-3.2-3B-Q4 (current) | 3B | 0.52 | any — all at chance |
| Llama-3.1-8B-Q4 | 8B | 0.58 | neutral + none |
| Qwen2.5-14B-Q4 | 14B | 0.57 | neutral + full |
| **Mistral-Nemo-12B-Q4** | 12B | **0.65** | **baseline + full** |

1. **Parameter count plateaus.** 3B→8B = +0.06; 8B→14B = −0.01. Scaling is not
   the lever.
2. **Alignment matters more than size.** Nemo-12B beat Qwen-14B by +0.08 with
   fewer parameters. Qwen is safety-tuned and repeatedly declined to engage with
   explicit content — it also failed to emit parseable JSON on 24 of 57 rubric
   cases (so its `rubric+full` "0.58" is scored on 33 cases and is not
   comparable). For a sex-positive server, prefer a permissive base model.
3. **Context injection is the largest single lever.** Nemo, same prompt:
   no context 0.50 → pair rapport 0.61 → + daily activity 0.65. **+0.15** from
   supplying reciprocity and days-known. This validates §4.1's rapport feature as
   consumable by the LLM, not just by SQL. Context is computed strictly from
   messages *before* each case (no lookahead).
4. **The prompt must be tuned against the model, not in the abstract.** §2.2 calls
   the "flag generously" instruction a co-defendant. True for 3B/8B, where
   rewriting it improved specificity. **False for Nemo**, where the rewrite
   collapsed recall to 0.03–0.11 and the *original* prompt scored best. Do not
   ship a prompt rewrite without re-running this sweep on the target model.
5. **No model, at any size, caught the relational patterns.** Every one of the
   four Ciccio attention-pressure cases, all three third-party insertions, and the
   consent-bot evasion were missed by all four models. Every model also flagged
   the calibration negatives (*"Lemme boop those booba"*, Cami's correct DM
   request, Chi-Gal's joking *"I SAID NO QUESTIONS"*); 8B and Nemo additionally
   flagged Cat Bae's *"Let's get together and count freckles"* — the consensual
   case this whole spec is built around not flagging.

### 12.1 Round 2 — sizing for the 4060, and prompt tuning (2026-07-20)

**Noise floor first.** Four runs of an identical config gave 0.68 / 0.66 / 0.65 /
0.66 — temperature 0 is not deterministic here (batching + quantized KV).
**Run-to-run variance is ~±0.02, so differences under ~0.05 at n=57 are not
meaningful.** Any single-run comparison in this document should be read with that
in mind.

**A methodology trap worth recording.** llama-server splits `-c` across `--parallel`
slots. At `-c 4096 --parallel 4` each slot gets 1,024 tokens, and the longer prompts
plus a real window overflow it — the model returns unparseable output and the run
silently scores on 1–3 of 57 cases while still printing a plausible balanced
accuracy. Always check that TP+FP+TN+FN equals the case count.

**Quantization curve** (Mistral-Nemo-12B, best prompt per row, 4060 budget = 8 GB,
weights + KV at `q8_0`):

| model | quant | weights | +KV @4k q8 | fits 4060? | best BalAcc |
|---|---|---|---|---|---|
| Llama-3.2-3B (current prod) | Q4_K_M | 2.0 | 2.3 | yes | 0.52 |
| Llama-3.1-8B | Q4_K_M | 4.9 | 5.2 | yes | 0.58 |
| Qwen2.5-14B | Q4_K_M | 9.0 | — | no | 0.57 |
| Mistral-Nemo-12B | Q3_K_M | 6.1 | 6.4 | yes | 0.60 |
| **Mistral-Nemo-12B** | **IQ4_XS** | **6.7** | **7.1** | **yes — largest that fits** | **0.61** |
| Mistral-Nemo-12B | Q4_K_M | 7.5 | 7.8 | no (needs the 5080) | **0.66** |

Q4_K_S (7.12 GB) nominally fits but leaves no room for compute buffers. **IQ4_XS is
the largest usable model on an 8 GB 4060**, and the step down from Q4_K_M costs
~0.05 — right at the edge of significance, but consistent across prompts. Note the
4060 must run `--parallel 1`: 16k total context would need 1.3 GB of KV and blow the
budget. At ~7 evaluations/day, serialised inference is irrelevant.

**Prompt tuning results** (8 variants × context levels):

| prompt | what it is | outcome |
|---|---|---|
| **`baseline_plus`** | the **original** prompt + two surgical fixes: demote the slur criterion, state that explicitness is permitted | **best or tied-best on every model** |
| `baseline` | shipped prompt, unchanged | competitive but higher flag rate |
| `traffic` | green/yellow/red output instead of flag/ok | competitive on the quantized models |
| `dona`, `cot`, `fewshot` | full etiquette rubric / reasoning field / worked examples | **worse** — flag rates collapse to 11–23%, recall craters |

The lesson is counter-intuitive and worth keeping: **elaborate rubrics hurt.** A 12B
follows "flag only if…" too literally and goes quiet. The winning edit was small —
keep the prompt that works and remove its two specific pathologies.

**Net for a 4060 deployment:** Nemo-12B IQ4_XS + `baseline_plus` + pair-rapport
context ≈ **0.61**, against 0.52 for the current 3B. Model choice bought ~+0.09;
prompt tuning bought ~+0.03. Real, but still far short of alert-grade on its own —
§12's conclusion is unchanged.

**Conclusion.** The LLM cannot perform detection. It *can* rank and suppress:
Nemo at `neutral+*` reaches TNR 0.90–0.95, and Qwen `neutral+full` TNR 0.85. So the
supported architecture is deterministic detection (§7.6–7.8) with the LLM as a
false-positive suppressor and card-phraser — never as the decider.

**Infrastructure note.** `LLAMA_SERVER_URL` currently points at the RTX 5080
desktop (16 GB), not the planned 4060 box. Nemo-12B-Q4 needs ~7.5 GB + KV and
coexists with the prod 3B; a 14B does not. Windows Firewall only allows 8080, so
tuning servers on other ports need an SSH tunnel. Detached servers must be created
via WMI `Win32_Process` — `Start-Process` dies with the SSH session.

## 13. Eval set

`tests/data/rules_watch_eval.jsonl` — 57 real messages resolved to genuine
`message_id`s, so they can be replayed through the guard exactly as they ran live.
37 violation / 20 ok.

```json
{"message_id": ..., "ts": ..., "channel": "🔥│flash-channel", "author": "...",
 "author_id": ..., "content": "...", "label": "violation|ok",
 "pattern": "rapport_curve|cross_platform|third_party_insertion|attention_pressure|
             correction_response|conditional_worth|consent_bot_evasion|access_seeking|
             age_signal|none",
 "why": "the mod or target quote that justifies this label"}
```

**What this set measures — and what it does not.** It is a regression set for
**alert timing** ("would we have surfaced this pattern before a human had to act?").
It is **not** ground truth for conduct. Three reasons:

1. **Labels inherit selective enforcement.** "Traces to a mod decision" bakes in who
   complained and who had friends. Defwolf420 and jga are both still
   `current_member=1` for same-class conduct as ticketed/banned users; velocibaker's
   own 07-06 Reddit flag died because two mods vouched for him personally. A model
   fit to "who got actioned" learns *who complained and who knew whom* as much as
   what was done.
2. **Four rows don't meet the stated provenance bar** and should be treated as weak:
   whoami23's *"Thank you and I'm not under 21"* (labelled on hindsight — answering a
   broadcast reminder), the whisper-joke row (the rationale is inference, with no
   mod or target quote), and two rows whose author resolves to a raw ID with no
   complaint cited.
3. **It is unbalanced** — 65% violation overall and ~23% of rows are velocibaker.
   That base rate is nothing like live traffic, where violations are rare. So
   precision measured here is optimistic, and **F1/precision must not be used**
   (see §12): use balanced accuracy.

Usage: run a candidate over it, change **one** thing, re-run
(`scripts/rules_watch_tune.py`). Without this, any change looks like an improvement,
because 98.7% is the floor.

The pairs that matter most:

- velocibaker *"the rare and elusive Bagel boobs"* (**violation**) vs Tim_jonson20
  *"You look so fucking good.. as I am getting in the shower 😉"* (**ok**) — same
  channel, same woman, same eleven minutes. Only rapport differs.
- velocibaker → Lucia *"can I please taste?"* (**violation**) vs velocibaker → Cat Bae
  freckle thread (**ok**) — same author, same register, reciprocity 0.49 vs 0.87.
- Cami *"May I DM you? I know your DMs are closed"* (**ok**) vs Ciccio *"I don't know
  how to use the ask bot"* (**violation**) — same topic, opposite behavior.
