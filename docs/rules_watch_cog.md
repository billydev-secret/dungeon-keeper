# Dungeon Keeper — Rules Watch

**Alert-only moderation gate with consent-aware priority scoring and label capture.**

Version 0.3 — functional spec. Focuses on what the system *does* and why each signal behaves the way it does. Implementation mechanics (hosting, config, build sequencing) are deliberately out of scope here. This version adds the historical tuning protocol (§5a), the known structural risks surfaced in review (§10), and an honest statement of what the system is actually calibrated to detect (§11).

---

## 1. Purpose & philosophy

Passively watch public-channel messages, run them through layered detection, and **alert a human** — never auto-action. Every alert a mod confirms or dismisses becomes a labeled example, so the system manufactures the ground-truth dataset it currently lacks while being useful immediately.

Four principles, in priority order:

1. **Alert-only, recall-first.** Nothing is auto-actioned. A false positive costs a glance, not a wrongly-punished member, so the system flags generously and lets humans dismiss.
2. **Signals reorder the queue; they never close a case.** Every contextual signal *down-weights* or *up-weights* a flag's priority. None suppress it entirely. The highest-risk events in a consent-centered space — a trusted regular pressuring a quiet member, an established relationship that has curdled — are exactly the ones a naive "popular / they talk a lot → safe" rule would hide.
3. **The text alone can't decide.** Where NSFW language, flirting, and consensual BDSM are normal, the violation signal is *consent and relationship context*, which lives outside any single message. Content/sentiment classifiers detect surface features and will misfire here. They are inputs to a human-reviewed queue, not oracles.
4. **Only act on what is observable.** The system governs public chat, which it can see. It does not inspect DMs or infer private content. Signals derived from consent settings describe *relationship state*, never private message content.

---

## 2. What the system observes vs. what it concludes

The core design move: separate **content signals** (what was said) from **context signals** (who said it to whom, and what's known about their relationship and the recipient's stated boundaries). Content signals are weak and miscalibrated in this space. Context signals are where consent actually shows up.

```
              CONTENT SIGNALS                    CONTEXT SIGNALS
        (weak here — surface features)     (strong here — consent shows up)
        ┌──────────────────────────┐       ┌──────────────────────────────┐
        │ guard model verdict      │       │ relationship history (mutual)│
        │ toxicity / slur lexical  │  ───▶  │ DM-consent pairing (opt-in)  │
        │ sentiment (VADER) + traj │       │ reciprocity in this thread   │
        └──────────────────────────┘       │ persistence after non-resp.  │
                                           │ boundary-token state change  │
                                           │ recipient's declared DM tier │
                                           │ tenure / new-account prior   │
                                           └──────────────────────────────┘
                         │                              │
                         └──────────────┬───────────────┘
                                        ▼
                              PRIORITY SCORE  (reorders queue, never suppresses)
                                        ▼
                          ping now / low-pri digest / logged-only
                                        ▼
                          human ✅/❌  →  labeled training data
```

---

## 3. Content signals (what was said)

These judge the message text. In this community they are **necessary but unreliable**, because the same words can be welcome play or a real violation depending on consent — which the text doesn't carry. They contribute to the base signal; they never decide alone.

**Guard-model verdict (the primary content engine).** A recall-leaning, instruction-tuned guard model given the six rules as its taxonomy. It judges a **conversation window**, not a lone message — because Rule 2's actual trigger ("sustained pressure on someone who has expressed they are not interested") and Rule 6 (escalating public dispute) only exist across multiple turns. It needs to see whether a boundary was set and then crossed. Produces a verdict, a predicted rule, a reason, and a confidence.

**Toxicity / slur lexical (narrow use).** A lightweight toxicity scorer is useful for *one* consent-independent thing: slurs and identity attacks, which are violations regardless of relationship. Its generic "toxic"/"sexual" scores are **not** violation signals here — they'd fire on the whole server. Logged as features; only the slur/identity signal contributes to priority.

**Sentiment + trajectory (VADER).** Sentiment valence is logged but does **not** drive alerts — profanity and rough talk read as "negative" while being perfectly welcome here. The valuable derivative is **trajectory across the thread**: a recipient swinging from neutral/positive to sustained negative is one of the few text-visible hints that something welcome turned unwelcome. Logged as a feature and used as a mild up-weight on the *shift*, never on the absolute score.

---

## 4. Context signals (who, to whom, and what's consented)

These carry the real signal in this space. None inspect private content; all describe observable interaction or self-declared settings.

**Relationship history — mutual and reciprocal.** How much two people have genuinely interacted, using the *reciprocated* count (target engages back, not just sender→target). Strong rapport strongly down-weights a flag. A lopsided ratio — one person repeatedly engaging someone who never reciprocates — does **not** earn the discount and, when severe, *raises* priority, because that asymmetry is the fixation/pressure shape itself.

**DM-consent pairing — the strongest confirmed-relationship signal.** Two members who have opted into a DM-consent relationship have performed a *deliberate, bilateral consent act* — higher-confidence than observed mention frequency, and mutual by construction. A standing pairing strongly down-weights public flags between the pair. Two refinements:
- Captured **as of message time**, not current state.
- A **recently revoked** pairing followed by directed public content is a *boundary-withdrawal* signal — it *raises* priority, like silence/withdrawal does.

This pairing is used **only** as a relationship-confidence input that reduces false positives. It is never a Rule 5 detector (see §6).

**Recipient's declared DM tier — soft public prior only.** The three-tier DM-openness setting describes the recipient's boundary posture. A restricted recipient is *plausibly* more boundary-conscious, so an open sender directing intense content at a restricted recipient (a **tier mismatch**) is a mild up-weight. Weak and clearly bounded: the tier governs a different domain (DMs) than what's being scored (public chat), and is captured as-of message time. Never suppresses; never used to read or infer DM content.

**Reciprocity in this thread.** Live version of relationship history: is the target replying at comparable rate/length, or going one-word / quiet while the sender escalates? One-sided conversation *shape* is among the most trustworthy text-external consent signals. Raises priority when one-sided.

**Persistence after non-response.** Consecutive directed messages to a target who hasn't responded. This *is* Rule 2's "sustained pressure," and it's purely structural — no content judgment, near-zero false positives in this context. Strong up-weight.

**Boundary-token state change.** A narrow detector for explicit stop-signals ("stop," "no," "not interested," recognized safewords). Not a violation by itself, but it's the *state change* that flips subsequent directed content from play to violation. Once seen, directed messages afterward weight heavily. The closest thing to a real consent signal available from public text.

**Withdrawal.** A flag directed at someone who then goes quiet or leaves the channel **gains** priority. Silence is neutral-to-suspicious, never "safe" — the quiet, no-reaction event is both hardest to detect and most serious.

**Tenure / new-account prior.** A brand-new member directing intense content at an established one warrants more weight than two regulars. A standing prior, not a content judgment.

---

## 5. How signals combine (function, not formula)

The guard-model verdict sets a **base signal**. Context signals then move its **priority** up or down. The combination obeys three invariant rules:

- **Down-weighters have a floor.** Relationship history, DM-consent pairing, matched-open tiers, and tenure can sink a flag deep into the queue but can **never** drive priority to zero. Established, consented relationships are precisely where intimate-partner coercion lives; nothing may vanish unseen.
- **Withdrawal and asymmetry are up-weighters.** Silence, target departure, one-sided escalation, persistence after non-response, boundary-token crossings, and recently-revoked consent all *raise* priority. These are the shapes of non-consensual behavior that content scoring misses.
- **Consent artifacts lower the base rate, they don't certify a message.** A DM-consent pairing or strong rapport means "a problem is *less likely* here," not "this message is fine." They adjust priority; they never clear a case.

Down-weighted flags sink to a low-priority digest. Up-weighted flags ping immediately. Everything is logged regardless of where it lands, so nothing is lost and every event is available for labeling.

Early on these weightings are set by hand. Their deeper purpose is that **every signal is logged as a feature on every event**, so a model trained later learns the real weighting from confirmed labels rather than from hand-tuning.

---

## 5a. Tuning protocol — calibrate on history, validate on held-out history

The weights above are not guessed once and left. They are **tuned against historical messages** — the archive is the bench. But tuning against history can overfit (produce something that fits the past perfectly and generalizes poorly), so the protocol has discipline built in:

**Raw model before scorer.** First run the guard model over history with *no* scoring layer and read its output naked. This answers the foundational question — is the content signal catching the right *candidates* at all, or drowning in consensual banter? Only once the raw signal is surfacing sensible candidates do you tune the scoring layer on top. Tuning both at once hides whether a good result came from the model finding the right things or the scorer papering over a weak model.

**Read the false positives, not just the rate.** A flag rate tells you little. What matters is *what kind of thing* got flagged. If every flag is an enthusiastic consensual exchange, that confirms the content miscalibration and tells you the scorer must carry the load. Inspect cases by eye; the categories you see decide the build.

**Tune-slice vs. held-out-slice.** Tune weights on one portion of history, then check the result against a *different* portion you never looked at while tuning. Good behavior on the held-out slice means the tuning generalized; collapse means you overfit to the tuning slice. Without this split, "looks great on the data I tuned on" is the most seductive false signal in the project.

**Trust structural signals over content signals when tuning.** Persistence-after-non-response, reciprocity, withdrawal are mechanical and generalize — "six messages to someone who didn't reply" means the same thing next month. Content/sentiment weights are the ones most likely to overfit to the specific vocabulary of past data. Tune the structural weights with confidence; hold the content weights loosely.

**What backtesting can and can't reconstruct.** History strongly tests the content signal and the *reconstructable* context signals (withdrawal, trajectory — you know what happened next). It under-represents live-only signals (room tone, who was online). Don't conclude a live signal is worthless because it's thin in backfill.

**This is also the go/no-go.** If the raw guard model plus human review already catches what you care about on historical data, that is a legitimate place to stop — the elaborate scoring layer only earns its complexity if the backtest shows it materially improves on raw-model-plus-human. Permission to conclude "the scorer isn't worth building" is part of the protocol, not a failure of it.

---

## 6. Out of scope by design

**Rule 5 (DM consent) is not enforced by the bot.** The system does not see DM content and will not infer it. Rule 5 is handled entirely through **user reports reviewed by a human**. The DM-consent registry contributes to Rules Watch *only* as a relationship-confidence signal (§4) that reduces false positives in public-chat scoring — never as a detector, and never in a way that implies inspecting private messages. The privacy of the DM-consent service is part of its value and is preserved absolutely.

**Rule 1 (21+)** is a membership requirement, not a content rule. It surfaces only on an explicit under-age self-statement; otherwise it is never a content violation and consumes no model judgment.

---

## 7. Data captured per event (functional inventory)

Each observed event records, for the sake of both the live queue and future training:

- **Message identity & context:** message, inferred recipient, the conversation window judged, whether the channel is NSFW-designated.
- **Content signals:** guard verdict + predicted rule + reason + confidence; slur/identity lexical signal; toxicity feature scores (logged, mostly unused); VADER valence + thread trajectory.
- **Context signals:** mutual relationship count and reciprocity ratio; DM-consent pairing state (as-of message time) and recent-revocation flag; recipient DM tier and tier-mismatch flag; in-thread reciprocity; persistence count; boundary-token-crossed flag; withdrawal flag; tenure / account-age prior.
- **Scoring & disposition:** computed priority and a short human-readable "why this priority" (e.g. "strangers, target went quiet after a stop-token").
- **Human label (the payoff):** confirmed real / false positive, corrected rule, who labeled it, when. This is generated as a byproduct of normal moderation — reviewing an alert *is* labeling it.

Message text and conversation windows are retained only as long as needed for labeling and training, then purged, so the system isn't a permanent archive of member chat.

---

## 8. Trajectory of the system

1. **Now:** layered detection feeds a human-reviewed priority queue. Useful immediately; primary hidden value is generating labels.
2. **Accumulating:** confirmed labels build the real-positive dataset the space currently lacks — correctly labeled, drawn from the genuinely hard boundary cases, reflecting *this community's* consent norms.
3. **Later:** a classifier trained on those labels — with all the context signals above available as inputs — learns the weighting directly and becomes the engine, with the guard model kept as a comparison signal. This is the only path to something that actually understands consent in this space, because that understanding exists in no public dataset and has to be learned from here.

---

## 9. Honest limitations

- **No off-the-shelf model understands consent in an adult space.** The guard model with your taxonomy is the best available approximation and will still misfire. Its early job is to surface *candidates*, not to be right.
- **The relationship and consent priors' failure mode is intimate-partner coercion** — which is why every down-weighter floors above zero and nothing is ever suppressed.
- **Silence is the dangerous signal.** The quiet, no-reaction event directed at a withdrawing target is hardest to detect and most serious; the scorer deliberately treats silence as raising priority, not lowering it.
- **Consent artifacts describe relationships, not messages.** A DM-consent pairing tells you two people opted into each other — not that any specific thing said is acceptable. The system uses them only for what they can honestly indicate.

---

## 10. Known structural risks (surfaced in review)

These are not limitations of the *idea* but of the *build*, and each could quietly undermine it if left unexamined. The historical backtest (§5a) is where each gets tested against reality rather than assumed.

**Target identification is the load-bearing assumption.** Nearly every context signal — persistence, reciprocity, withdrawal, one-sided ratio — depends on knowing who a message was aimed *at*. In a busy group channel with no @-mention or reply link and several overlapping conversations, "the target" is often ambiguous or unknowable. If target inference is wrong or absent much of the time, those signals become noise in exactly the high-traffic channels where they're most needed. **The system should know when it can't identify a target and weight its context signals down accordingly** — an unconfident target attribution should not drive confident scoring. The backtest will show how often clean attribution is actually possible here; that number decides how much of the context layer is viable.

**The label loop and the blind spot are the same blind spot.** You only generate labels on events you *review*, and you review what gets surfaced. Anything the scorer down-weights into the digest is less likely to be seen, less likely to be labeled — so a future classifier trains disproportionately on up-weighted events and learns the scorer's existing biases as truth. The quiet down-weighted violation, the one we most want to catch, is the one least likely to ever become a label. **Mitigation: deliberately sample some low-priority/down-weighted events for review**, not just the top of the queue, so the label set reflects the full distribution. Running history (rather than only live traffic) helps here, because you can sample across everything that happened, not only what a live scorer chose to raise.

**The six-head endgame is probably over-scoped.** Rules 4 and 6 are rare; Rule 1 is heuristic; Rule 5 is out of scope. Realistically the label volume a mid-size community produces may only ever support a strong **Rule 2** detector and perhaps Rule 3. The honest endpoint is likely "a well-trained Rule 2 model plus guard-model/heuristics for the long tail," not a full six-head classifier. Let observed label volume — not the original ambition — decide how many heads are worth training.

---

## 11. What this system actually detects

Worth stating plainly, because the sophistication of the scoring can create an illusion the system doesn't earn: **this does not detect consent.** Consent is not observable from outside a relationship, and every signal here — relationship history, DM pairing, boundary tokens, reciprocity — is a *proxy* for it, some strong, some weak. Stacking proxies does not turn into a measurement of the unobservable thing.

What the system honestly is: **triage that surfaces candidates for human judgment, calibrated to match the moderators' own judgment and validated against held-out history so the calibration generalizes.** When you tune weights until the output "looks right," the target you're fitting to is your own moderation intuition made explicit. That is genuinely valuable — it makes your judgment faster, more consistent, and eventually trainable — but it means the system's ceiling is your own consistency, and its role is to *route attention*, never to *assess consent*. The alert-only, never-suppress, human-decides design is what keeps the system inside that honest role.


---

## 12. The ledger (§7.3 / §7.4 of the 2026-07-20 tuning spec)

Everything above this section is *detection*: the guard model plus a context
scorer, producing a priority tier. Three independent attempts to make that
approach discriminate at a realistic base rate failed (see
`docs/reviews/2026-07-20-rules-watch-tuning.md` §12.2b/§12.2c). The ledger is
what survives that result, and it works on a different principle.

**A ledger row is an observation, not an opinion.** It records that a specific,
narrowly-defined thing was said, with a date and the matched phrase. It carries
no score, no tier, no verdict, and no label buttons — there is nothing to agree
or disagree with. It posts nothing to Discord. Its entire value is that when a
human is already reviewing somebody, the prior acts are on record instead of
being reconstructed from memory months later.

Code: `rules_watch/ledger.py`; storage: `rules_ledger` (migration 095); surfaced
on the dashboard's Rules Watch → Ledger tab. It runs from `on_message` on a task
independent of the guard pipeline, so a concrete act is recorded whether or not
the sentiment pre-filter would have let the message through, and it never calls
the model.

### 12.1 What fires, and why it is shaped that way

Both patterns were measured by replaying the whole corpus through the module
(395,095 messages with surviving content, ~163 days) *before* being finalised,
and both were narrowed substantially from their description in §7 as a result.
Combined output is **5 rows / 5.4 months (~0.9/month)**. All three actioned
people surface; no mod, greeter, or ordinary member does.

**DM consent** — a claim that the consent bot is unusable, alongside intent to DM
a specific person.

| filter | hits |
|---|---|
| bot-disclaimer alone | 14 — picks up cat-bot and jail-bot outage chatter |
| **+ DM intent, non-mod channel** | **2 — both Ciccio, 2.5 months before his ban** |

§8.1 is the reason this fires on the *disclaimer* rather than on asking. Dona's
etiquette guide explicitly endorses asking publicly before a DM, so public asking
must never be recorded; the evasion is the claim that the bot can't be used.
DM intent may appear in a nearby message from the same author (5-minute window)
because bigoryx's script split the two across messages.

**Cross-platform** — the author demonstrates he has already viewed the target's
off-platform content.

| filter | hits |
|---|---|
| bare platform mention | 850 |
| + directed at the addressee | 67 |
| of which the intake ritual | ~70 (overlapping) |
| **+ observation, ritual excluded, target hasn't just raised it** | **3 — Burner ×2 (benign) and Whoami23 naming lily's Reddit post, the actioned case** |

### 12.2 Three corrections to §7.4 as written

The spec's §7.4 claims this pattern has a "near-zero false positive rate." That
holds for the *conduct*, not for the *detector* it proposes, which the corpus
shows would have misfired badly:

1. **The dominant hit class is the server's own welcome ritual.** Greeters and
   mods asking new arrivals "What's your Reddit name? I like connecting the
   faces" accounts for essentially all directed platform mentions. Shipping §7.4
   as specified would have built another most-beloved-member detector (§7.7),
   firing mostly on mods doing their job — exactly the failure §6.5 predicted.
   What separates the actioned case is not *naming* a platform but demonstrating
   you already went and looked. So the detector requires observation and
   subtracts the ritual.
2. **`your bio` and `your post` are in-server features here**, not off-platform
   ones. Bios are a Dungeon Keeper feature with an icebreaker pool, and "your
   post" in the photo channels means an in-server post. Both are dropped.
3. **Bare `OF` is unusable** — it matches the word "of". Requires `onlyfans`.

### 12.3 Invariants — do not widen without re-measuring

- Public DM-asking must never fire. It is taught by the etiquette guide.
- The intake ritual must never fire. It is performed by mods dozens of times.
- A cross-platform row requires a resolved target. §11 names the directedness
  filter as what keeps this in the low-risk tier.
- The "she raised it herself" exemption is **same-channel and 6 hours**, and must
  stay that way. See §12.5 — a broader version silently suppressed the one case
  this detector exists to catch.
- Ledger rows store the matched phrase and a 240-character excerpt, never full
  message content.
- Combined volume is ~0.9 rows/month. If it starts filling up, a pattern has been
  widened and the change should be re-measured against the corpus.

### 12.4 What the ledger is not

It is not an early-warning system and does not attempt to be. It would not have
caught velocibaker (no platform reference, no bot disclaimer), and it says
nothing about the rapport-curve pattern that §4.1 identifies as load-bearing.
Detection of that pattern remains unsolved and human reporting stays primary —
§12.2c is unchanged by anything here. What the ledger does is make two specific,
recurring evasions permanently citable at near-zero cost and near-zero risk.

### 12.5 The exemption that nearly buried the case it was built for

Worth recording, because the mistake is subtle and the fix is counter-intuitive.

The cross-platform detector exempts a target who raised the platform herself —
if she just said "posted a new set on my Reddit", someone responding to that is
being responsive, not predatory. The first implementation made that check
**guild-wide over 30 days**, reasoning that a false exemption is cheap and a
false hit is expensive (§11).

Replaying the corpus showed it silently suppressed **the Whoami23 case** — the
one actioned incident the detector exists to catch. lily is a Reddit poster who
talks about Reddit, so she always had a recent mention somewhere. Worse: one of
the mentions granting him immunity was lily *reporting velocibaker for finding
her Reddit profile*, 121 hours earlier.

The general lesson: **an exemption keyed on the target's own behaviour grants the
most immunity to the people with the most exposure** — here, precisely the women
whose off-platform presence is known and who are therefore most pursued. The
window is now same-channel and 6 hours, so it means "she just brought it up" and
nothing more. `tests/test_rules_watch_ledger.py` has two regression cases that
fail against the old window.
