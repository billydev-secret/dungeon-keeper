# Automated moderation — literature review

**Status:** Reference (external evidence). Companion to
`2026-07-20-rules-watch-tuning.md`; read that first for the measurements this
corroborates. Compiled 2026-07-20 from a fan-out research run (26 primary
sources fetched, 127 claims extracted, top 25 adversarially verified 3 votes
each → 22 confirmed, 3 refuted). Every surviving finding rests on a
peer-reviewed or top-venue source (ICLR, NeurIPS, EMNLP, NAACL, CSCW, ICWSM).

**Why this exists.** The tuning spec concluded, from our own data, that
automated detection of the relational patterns is unsolved and that the binding
constraint is labels (~13 positive user-weeks), not compute. That was an
internal empirical finding. This doc asks the external question: *is that a local
result or a structural one?* The literature answers structural, and gives the
citations to say so.

**The one-line conclusion:** the published state of the art independently
reproduces our exact 0.61–0.66 ceiling, needs orders of magnitude more labels to
get there, and hides a false-positive rate that is catastrophic at our base rate.
Thirteen positives is below every workable supervised / weakly-supervised /
PU-learning floor in the literature. The only surviving *constructive*
recommendation is the one the ledger already implements: don't build a
classifier — record concrete acts, defer/abstain, and harvest labels as a
by-product of moderation.

---

## 1. The closest published analogue reproduces our ceiling — with far more data

"Conversations gone awry" (CGA) derailment forecasting is the nearest published
task to ours: predict from an unfolding conversation, *before* the bad turn, so
intervention is still possible.

| system | venue | accuracy | baseline | what it took |
|---|---|---|---|---|
| CRAFT (Wikipedia CGA) | EMNLP 2019 | **66.5%** | 50% | 1M unlabelled pre-train + 4,188 labels |
| CRAFT (Reddit CMV) | EMNLP 2019 | 63.4% | 50% | 600k unlabelled + 6,842 labels |
| best of 13 models (CGA-CMV-large, 19,578 convs) | 2025 | **71.0%** | 50% | only decoder LLMs clear 70% at all |

Three points, all verified:

- **This is our number.** CRAFT's 66.5% balanced accuracy is the same 0.61–0.66
  band our own sweep measured (tuning spec §12). The ceiling is not an artifact
  of our hardware, our prompt, or our 3B model — it is structural to the task.
- **It cost thousands of labels plus a million-conversation pre-train.** CRAFT
  "performs at the level of random guessing" when trained on the labels alone;
  all its lift comes from unsupervised pre-training. There is no version of this
  that learns from 13 labels.
- **Even the newest, largest benchmark barely moves it** — 71.0% with
  billion-parameter decoder LLMs, on a class-balanced, topic-paired,
  length-matched set whose authors concede a label-noise source.

*Sources: arXiv 1909.01362 (CRAFT, EMNLP 2019); arXiv 2507.19470 (CGA-CMV-large,
2025).*

## 2. That accuracy hides a base-rate catastrophe

CGA numbers are reported on **artificially balanced 50/50 test sets**. The
authors of CRAFT say so directly: *"it relies on balanced datasets, while
derailment is a relatively rare event… additional work is needed to establish
whether the recall tradeoff would be acceptable in practice."*

The false-positive rates are the disqualifier:

- CRAFT Wikipedia: **FPR 44.1%**.
- CGA-CMV-large SoTA (Gemma2-9B): **FPR 34.2%**.

At our ~1.1% base rate, a 34% FPR yields **~30 false alarms per true positive**
(`0.342 × 98.9/1.1 ≈ 30.7`). A "surface the pattern in the top 100" objective is
mathematically hopeless for this model class — which is exactly what our §12.2b
replay found empirically (0/13 positives in the top 100). The literature and our
own data agree, by different routes, on the same wall.

⚠️ The 30-per-positive figure is base-rate arithmetic from a balanced-set FPR,
not a reported deployment number. The direction is sound; the exact magnitude
moves with the operating threshold.

*Source: arXiv 1909.01362; arXiv 2507.19470.*

## 3. The relational analogue stays sub-0.5 precision even with everything

Grooming detection is the closest *relational-manipulation* task with a realistic
base rate. The SoTA turn-level detector (SCoRL, NAACL 2025), with full human
turn-level supervision, a high-resource model, and RL optimisation, reaches:

- **Latency-F1 0.365, precision 0.475** on an imbalanced test set (69 positive vs
  11,733 negative, ~0.58% base rate — near our 1.1%).

And a methodological warning we must heed: the same baseline model jumps from
**0.089 turn-level F1 to 0.355 chat-level F1 — a 4× gain — purely by counting
premature firings on non-risky turns as successes.** Conversation-level metrics
systematically inflate apparent performance. Any evaluation of Rules Watch must
be at the **incident/user-week level, not the conversation level**, or it will
flatter itself the same way.

*Source: arXiv 2503.06627 (SCoRL, NAACL 2025).*

## 4. Every low-label escape hatch has disqualifying fine print

The techniques that superficially promise rescue at 13 labels were each run down:

**Positive-Unlabelled (PU) learning.** Published gains are inflated by an
unrealistic protocol — model selection uses a validation set containing labelled
negatives, which by definition do not exist in a true PU setting. Worse, PU's
class-prior estimator has a **directional over-estimation bias precisely when the
positive support is contained in the negative support** (the irreducibility
assumption), and that assumption is not checkable from the data. "New person +
high intensity + low reciprocity" living inside "new person + normal enthusiasm"
is *exactly* a support-containment scenario. PU would fail here silently and in a
predictable direction.

**Weak supervision / Snorkel-style labelling functions.** On realistic,
expertise-heavy tasks the supervised-vs-weak-supervision crossover **exceeds
1,000 labels** (and never crosses at all on a legal task), versus under 200 on
easy benchmarks — so the encouraging benchmark numbers understate real label
needs. Performance is **bounded by labelling-function precision/coverage**, not
the aggregation model, so a domain where nobody can write precise heuristics
("ahead of the rapport curve") gets no benefit. The hypothesis that weak
supervision *wins* in the ~13-label regime was **refuted 0-3** in verification.

**Few-shot active learning.** The headline "170 labels → 90% of full performance"
result is an annotation-*efficiency* finding: it assumes positives are findable
at scale (its pool has ~1,000 abusive instances), its cold-start **breaks under
class imbalance** (a third of runs fail at 10% prevalence, *all* fail at 5%), and
its published fix is a **lexical keyword seed** over an abuse lexicon — a
mechanism unavailable to us, since explicit content is uncorrelated with
complaints here. The authors explicitly scope their result to explicit,
lexically-marked abuse and defer implicit abuse to future work.

**Label-free evaluation** (Fréchet partial-identification bounds) is real but too
coarse to be decision-useful: reported accuracy bounds span ~0.46–0.95. It can't
distinguish a useful detector from a near-random one at the resolution we'd need.

*Sources: arXiv 2509.24228 (PU benchmark, ICLR 2026); OpenReview aYAA-XHKyk /
arXiv 2002.03673 (class-prior estimation bias); arXiv 2501.07727 (BoxWRENCH weak
supervision, 2025); aclanthology 2022.trac-1.7 (active learning); NeurIPS 2024
label-free bounds; arXiv 2208.01704 (WEAPO).*

## 5. What the research did NOT find — and the open leads

This run was scoped to be adversarial, and its most important property is
honesty about scope: **it surfaced no positive existence proof that any technique
works at ~13 positives.** That absence is itself the finding. Research questions
on relational/temporal modelling, deviation framing, label-UI design, early-
intervention ethics, and industry T&S practice produced *zero surviving verified
claims* — the literature that was fetched didn't withstand verification on those
points, so this doc can say what does not work, not what positively does.

Three leads remain genuinely open and are the highest-value follow-ups, because
each attacks the binding constraint (labels) rather than working around it:

1. **Self-censorship / retraction as an auto-mined label** (tuning spec §4.2 —
   a participant retracting within ~2 min of an unwanted interjection). The run
   called this "the single most promising lead in the task framing" — it
   generates labels as a workflow by-product and sidesteps selective-enforcement
   bias — but found *no evidence for or against it*. Needs a dedicated
   CSCW/CHI conversational-repair and deletion-behaviour search. **A separate
   focused run was launched for exactly this.**
2. **Inter-rater agreement among moderators on relational-harassment labels.**
   Unknown, and it bounds any achievable detector regardless of method. Worth
   establishing before trusting even the 13 labels we have.
3. **Label-free recovery of the oracle ensemble gap** (tuning spec §12.3: two
   systems agree on 61% of cases, oracle selector 0.82 vs 0.61–0.66 individually).
   The most actionable *engineering* question; selective-prediction and cascade
   papers were fetched but nothing survived verification on this specific point.

## 6. What this means for Rules Watch

The literature endorses, by elimination, the path already taken:

- **Do not build the classifier.** Not a hardware problem, not a prompt problem,
  not a "we haven't found the right features" problem — a label-count and
  base-rate wall that the published field hits too, with vastly more data.
- **The ledger is the right shape.** Record concrete, citable acts; accuse
  nobody; let labels accrue from normal moderation. This is the run's only
  surviving constructive recommendation, and `rules_watch/ledger.py` already
  implements it (see `rules_watch_cog.md` §12).
- **Keep human reporting primary** (tuning spec §12.2c, unchanged).
- **Evaluate at the incident/user-week level, never the conversation level** — §3
  shows conversation-level metrics inflate ~4×.
- **The `[Looks fine] [Keep watching] [I nudged]` card** (tuning spec §11) is the
  cheapest route to the one thing that *could* move this: more labels, generated
  as a by-product of the workflow rather than a separate annotation effort.

## Sources

All primary / peer-reviewed. Verified claims cite these directly.

- arXiv 1909.01362 — CRAFT, "Trouble on the Horizon" (EMNLP 2019)
- arXiv 2507.19470 — "Conversations Gone Awry, But Then?" (2025)
- arXiv 2503.06627 — SCoRL, turn-level early grooming detection (NAACL 2025)
- arXiv 2509.24228 — PU-learning benchmark (ICLR 2026)
- OpenReview aYAA-XHKyk / arXiv 2002.03673 — class-prior estimation bias
- arXiv 2306.01253 — irreducibility / CPE (ICML 2023)
- arXiv 2501.07727 — BoxWRENCH weak-supervision crossover (2025)
- aclanthology 2022.trac-1.7 — active learning for abuse detection (TRAC 2022)
- NeurIPS 2024 — label-free evaluation via Fréchet bounds
- arXiv 2208.01704 — WEAPO positive-only weak supervision
