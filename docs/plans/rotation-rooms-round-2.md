# Rotation Rooms — Round 2: Superlatives, Sealed Envelopes, Second Sight (v2)

**Status:** DESIGN — proposal, no code. Build plan: [rotation-rooms-round-2-build.md](rotation-rooms-round-2-build.md).
**Written:** 2026-09-02. v2 folds in the adversarial review of the same day.
**INDEX.md classification on land:** Design.
**Reading rule:** the text below is the v2 spec as written. The build plan's §2 verifies
every seam named here against `src/` and lists the rows it corrects (C1–C24); **where a
row there disagrees with a line here, the plan wins**, and §4 (build order) and §5
(verify before build) below are superseded by the plan's §4 and §2. In particular:
`/rooms <room>` replaces the three top-level commands; the `*_paid` tables are
`econ_<room>_rewards`; Superlatives has no `season_length_cycles`; `envelope_review`
is not built; §2.7's `/delete_me` row means erasure (`purge_user_data`); and the
`Testing:` rule in §4 is CLAUDE.md's — user-facing commits only, not every behaviour change.

> House-rules reminder for whoever implements this: the code wins over any spec, so every
> existing seam named below (`pay_game_rewards`, `anon_audit_log`, the no-contact service,
> the rotation registry, the question-bank draw, `render_quote_card`, `purge_user_data`,
> the Confessions content filter, the Compliment derangement) must be confirmed in `src/`
> before it is relied on. Where this spec says "existing seam" it means *go look*.

### Changes from v1

| Area | v1 | v2 |
|---|---|---|
| Superlatives voting | one vote per prompt on a random slate, threshold-to-name | **two-phase**: nominations on random slates, then a fixed ballot of top nominees |
| Superlatives consent | global opt-out | opt-in carries a **heat ceiling**; finalists can **withdraw** |
| Cycle boundary | undefined | open-to-open; featured-day actions belong to the cycle that just started |
| Envelope drop cadence | every 4 featured days ("monthly") | **every 8** — at 2 rooms/day over 8 rooms, 4 featured days was 16 calendar days |
| Envelope preview | all pending | **excludes to-me** envelopes |
| Anonymous envelopes | free text | Confessions filter + **mention strip** |
| Jail hold | all envelopes | room envelopes only; to-me DMs proceed |
| Second Sight prompt types | closed + Mirror; open deferred | **open ships in v1** as Tells |
| Second Sight prompts/cycle | 1 | **2** (1–3) |
| Read payout | per attempt | **per correct read, at reveal**, cap 5 |
| Reveal distribution | always | hidden under **8 answers** |
| Who-reads-you | per-person accuracy, opt-in | kept, opt-in, **no number under 3 reads** |
| Mirror coverage | organic | **assigned describers** via the Compliment derangement |
| Answer retention | indefinite | **730 days** + clear-history |
| Dashboard | three top-level entries | grouped under a **Rotation Rooms** heading |

---

## 0. Scope and shared rules

### 0.1 What this adds

Three new rooms for the daily rotating feature channels
(`docs/plans/rotating-feature-channels.md`), taking the pool from five
(confessions / whisper / AMA / guess-who / risky-rolls) to eight. All three enter via one
slash command that opens **one ephemeral panel**, so they work while hidden and their
content accumulates until the room's featured day.

The organising goal is **interpersonal discovery** — members exploring each other and
themselves. Every mechanic below is justified by what it teaches members about one
another; anything that only produces a score has been cut.

### 0.2 Decisions taken (not open)

| Decision | Value |
|---|---|
| Participation pays | every room: a flat faucet once per cycle, plus a quest trigger kind |
| Superlatives | two-phase (nominate → ballot); public tally names winners; bank up to spicy |
| Second Sight | closed, open **and** Mirror prompts in v1; per-person accuracy kept (private, opt-in); public leaderboard off by default |
| Envelopes | mod preview-and-pull accepted; to-a-member addressing deferred to v2 |
| Placement | all three under the NSFW category; heat gates on `channel.is_nsfw()` |
| Dropped | Masks (Confessions already masks per thread); Hot Seat (wants parallel play) |

### 0.3 Rules that apply to all three rooms

1. **Entry survives hiding.** One slash command, one ephemeral panel, buttons and modals.
   No in-channel buttons or message-posting as entry. Non-negotiable for pool membership.
2. **Configuration lives on the dashboard.** Three new route ids, bare feature names —
   `superlatives`, `sealed-envelopes`, `second-sight` — grouped in the sidebar under a
   **Rotation Rooms** heading alongside the existing rooms. Settings live with the data
   they produce. No admin slash commands or modals.
3. **Heat follows the channel.** Banks carry heat tags; draws filter on
   `channel.is_nsfw()`. No bot-side NSFW toggle anywhere in these features.
4. **No-contact is consulted at every point a name is shown or a pair is formed** —
   slates, ballots, read/attribute selects, assigned describers, tally cards. Exclusion is
   silent and slates are reshaped to full size, both directions.
5. **Anonymous actions get an `anon_audit_log` row** under a new slug per room
   (`superlatives`, `envelopes`, `sight`). See §2.5 for the retention subtlety.
6. **Payouts for anonymous or private actions are quiet** — off the public transaction
   feed, never requiring staff sign-off (the AMA-question precedent).
7. **Pass is always a valid answer.** Never penalised, never surfaced, never nagged.
   Opt-out from being a *target* is a first-class toggle in each panel.
8. **Store minimal data; register all of it.** Every table with a member id gets a
   `docs/data_register.md` row with a purge decision in the same commit, a
   `SUBJECT_ID_COLUMNS` entry where the column is non-conventional (`target_id`), and a
   privacy-notice line in `manual.html`.
9. **Seasons, not eternity.** Public numbers reset on a season boundary; private history
   is kept within its retention cap. A closing card marks the end.
10. **Logic-layer tests in the same commit** for every guard named in this document.

### 0.4 Cycles — one definition for all three rooms

A room's **cycle** runs from one open of the room on its featured day to the next open.
Everything a member does during the featured day itself belongs to the cycle that just
started. Jobs that run at open (tally, reveal, drop, ballot build) act on the cycle that
just closed. `cycle_id` is the featured-day date that *opened* it.

With `rooms_per_day: 2` over eight rooms, a cycle is four calendar days. All
"once per cycle" limits and payouts scale with that — change the rotation, they move.

Each room registers with the rotation registry and honours `pause_when_off`: nothing is
ever posted into a hidden channel, because every posting job is keyed to open.

### 0.5 Economy summary

| Room | Faucet | Default | Fires | Ledger | Trigger kind |
|---|---|---|---|---|---|
| Superlatives | `superlative_vote` | 5 | first nomination **or** first ballot vote of a cycle | quiet | `superlative_vote` (fires on both acts) |
| Sealed Envelopes | `envelope_seal` | 5 | first seal of a cycle | quiet | `envelope_seal` |
| Second Sight | `sight_answer` | 5 | first answer of a cycle | quiet | `sight_answer` |
| Second Sight | `sight_read` | 2 | at reveal, **per correct read**, cap `reads_paid_per_cycle` (5) | quiet | `sight_read` (fires on the read, not the payout) |

- Faucets paid on the act, like `photo_post`, not `end_game` — no session, no roster.
  Per-source toggles on the **Income Sources** page; the faucet-scale dial applies.
- Dedup is a `(guild, member, cycle_id)` row, the QOTD shape.
- `sight_read` pays on **correctness** because the incentive study it rests on
  (Klein & Hodges 2001) paid for accuracy, not attempts. Paying attempts made five
  thoughtless hunches worth 10 coins and polluted the who-reads-you data.
- Nothing pays on a reveal, tally or drop. Paying twice makes burn a farm and the tally a
  lottery.
- A fully participating member earns roughly 25 coins per cycle across the three rooms.
  Check that against faucet-scale before launch; QOTD is 10/day for comparison.

### 0.6 Rotation pool changes

- Pool 5 → 8. **Ship at `rooms_per_day: 2`**; consider 3. At 1, Whisper's ~1,050
  messages/month would be throttled to death.
- All four new trigger kinds survive hiding and pair as rotation-room quest triggers.

---

## 1. Superlatives

### 1.1 Concept

An anonymous ballot in the tbh / Gas lineage, run in two phases. Random slates surface
nominees while the room is hidden; a fixed ballot of the top nominees is voted on during
the next cycle; the featured day reveals winners by name. The slate spreads exposure and
makes brigading hard; the ballot produces the consensus a public tally needs.

What it teaches: how the room sees each other — and, through random slates, members
nobody would have thought of.

### 1.2 Why the ancestry matters

Every free-text anonymous-about-a-person product died on cruelty. The survivors shared
one property: members could not author the judgement. **The bank is authored, and that is
the load-bearing safety property.** No free text reaches a target, ever.

### 1.3 Timeline of one prompt slate

| When | What happens to slate S |
|---|---|
| open of cycle N | S is drawn (`prompts_per_cycle`, default 3, round-robin, heat-filtered). **Nominations open.** |
| cycle N (hidden days + featured day) | members nominate on random four-name slates, one nomination per prompt |
| open of cycle N+1 | nominations close. **Ballot built**: top `ballot_size` (3) nominees per prompt with at least `min_nominations` (2). Finalists DM'd. **Ballot opens.** A fresh slate S′ is drawn and its nominations open. |
| cycle N+1 | members vote on S's ballot, one vote per prompt; and nominate on S′ |
| open of cycle N+2 | S's ballot closes. **Tally posts**, naming winners. S′'s ballot opens. |

So every featured day carries a reveal, a live ballot and fresh nominations. No second
clock, no window inside a window.

### 1.4 Nominations

- Four members per prompt, bot-drawn from members active in the last
  `slate_activity_days` (default 14), excluding: self, opted-out, members whose heat
  ceiling is below the prompt's heat, either side of a no-contact pair with the nominator,
  and members currently jailed.
- One nomination per prompt per member per cycle. Final.
- One **🔀 shuffle** per prompt per cycle. Not a nomination; doesn't pay.
- Fewer than four eligible members → prompt silently skipped for that nominator.

### 1.5 Ballot

- Top `ballot_size` nominees per prompt, requiring `min_nominations`. Ties at the cut
  include everyone tied, up to `ballot_size + 2`. Fewer than two qualifying nominees →
  "no ballot this round" for that prompt (panel shows it; nothing posts).
- One vote per prompt per member per cycle. Final. Ballots are the same for everyone —
  no-contact is applied at *display*: a voter never sees a finalist they have no-contact
  with, and that vote option is simply absent for them.
- Ballot build re-checks opt-out, heat ceiling, membership and jail; anyone failing is
  dropped, not replaced.

### 1.6 Consent — three layers

1. **Count me in / Leave me out** — first row of the panel. Out means never on a slate,
   never on a ballot, never named, never DM'd. Takes effect immediately, including for
   nominations and votes already cast (discarded, not refunded).
2. **Heat ceiling** on Count me in: *mild* or *spicy*. Default **mild**. A member is only
   eligible for prompts at or below their ceiling. This is the per-heat consent the v1
   spec lacked and the house rule requires — sensitive exposure is opt-in.
3. **Withdraw** — finalists get a DM when the ballot is built ("you're on the ballot for
   *X* — withdraw?") and a Withdraw button on their panel that works until the tally.
   Withdrawn = removed, no promotion of the next nominee.

Stored in `superlative_prefs` (`opted_out`, `heat_ceiling`).

### 1.7 Tally (at open)

- One card per prompt of the closed ballot, in bank order.
- Plurality wins. **Turnout floor:** fewer than `min_ballot_votes` (default 5) cast on a
  prompt → "no consensus this round", nobody named. Ties at the top name everyone tied.
- **No counts, no runners-up, no ordered board.** A card names a winner or it doesn't.
  Mods see counts on the report; members never do.
- Winners DM'd the card link, best-effort, via the DM-permission service.
- Second-winner rule: two tied winners with a no-contact pair between them are never
  posted on one card; the second is dropped and the report shows why.
- Cards use `resolve_accent_color`; winner mention allow-listed for the ping.

### 1.8 Panel

`/superlatives` → one ephemeral panel:

- **Count me in (mild / spicy) / Leave me out**
- **Nominate** — one row per current prompt: text · **Nominate** (four-name select) ·
  **🔀** · state badge
- **Ballot** — one row per prompt on the live ballot: text · finalist select · state
- **Withdraw** — shown only to current finalists
- **Last round** — link to the latest tally cards
- Footer: next featured day · nominated / voted counts this cycle

### 1.9 Dashboard (`superlatives`)

- **Bank editor**: text, heat, active, weight. Reuse the shared bank editor if one
  exists; otherwise mirror the closest per-feature one. **Verify before stage 1.**
- Dials: `prompts_per_cycle`, `slate_activity_days`, `ballot_size`, `min_nominations`,
  `min_ballot_votes`, `season_length_cycles`.
- **Report**: per-cycle nominations and votes with counts; finalists and withdrawals;
  opt-out and heat-ceiling counts; **coverage** (distinct members who appeared on any
  slate) — the exclusion-risk metric.

### 1.10 Data

| Table | Columns (sketch) | Purge |
|---|---|---|
| `superlative_nominations` | guild, cycle_id, prompt_id, nominator_id, target_id, created_at | **YES** on either party |
| `superlative_ballot_votes` | guild, cycle_id, prompt_id, voter_id, target_id, created_at | **YES** on either party |
| `superlative_finalists` | guild, cycle_id, prompt_id, user_id, withdrawn_at | **YES** |
| `superlative_prefs` | guild, user_id, opted_out, heat_ceiling | **YES** |
| `superlative_paid` | guild, user_id, cycle_id | **YES** |
| `anon_audit_log`, slug `superlatives` | existing | existing |

`target_id` goes in `SUBJECT_ID_COLUMNS`. No member-authored text anywhere.

### 1.11 Failure modes

| Risk | Mitigation |
|---|---|
| Brigading | random slates + shuffle limit; two bars (nominations *and* ballot plurality with a turnout floor) |
| Named on a spicy prompt without consent | heat ceiling, default mild; withdraw |
| Legible bottom | no counts; random slates spread exposure; coverage on the report |
| Dud reveals | the ballot phase concentrates votes; the turnout floor is the honest fallback, not the expected case |
| Naming someone who left / opted out | re-checked at ballot build **and** at tally |

---

## 2. Sealed Envelopes

### 2.1 Concept

Write something now. The bot holds it. It opens on a future **drop** the room shares, and
publishes into the room with everything else that came due. Named or anonymous; to the
room or to future-you. The only room whose structure *is* the rotation's: accumulating
while hidden, revealed on a featured day.

### 2.2 Drops, not dates

- A drop happens every `drop_every_n_featured_days` featured days (**default 8** — about
  a month at four-day cycles).
- At seal time you pick **next drop / +2 / +3** (`max_fuse_drops`, 3); the panel shows
  projected dates.
- The drop job runs at open on a drop day: header card ("N envelopes open today"), each
  envelope as its own message in seal order, nothing else. Named ones post with attribution
  and an allow-listed mention; anonymous ones under the bot with the Confessions card
  shape (no thread mask needed — nothing is threaded).
- **An empty drop posts nothing.** The panel footer shows the count sealed for the next
  drop so members know.
- **Launch seeding:** the first drop is a founders' drop — mods seal a handful before the
  room goes into rotation so the first featured day isn't empty and the room has an
  example of its own voice.

### 2.3 Addressing (v1: two modes)

| Mode | At the drop |
|---|---|
| **To the room** | published in-channel |
| **To me** | DM'd back to the author; never published; never previewed by mods |

**To a member is v2.** It is a delayed message aimed at a person; no-contact would have to
be checked at publish against a pair that may not have existed at seal; and spicy + named
target is the highest-risk combination in the research.

### 2.4 Burn and read-your-own

**My envelopes** lists pending envelopes with projected drops; **read** your own text,
**burn** any pending one at any time before it opens. Burn is a hard delete. Not optional
scope — ninety days is long enough for circumstances to change completely.

### 2.5 Audit retention (must not be missed)

`anon_audit_log` retention defaults to 90 days; a +3 fuse can outlive it. The envelope row
holds `author_id` until it opens (burn and read need it). The audit row for an anonymous
envelope is written **at publication** with the seal timestamp in meta, so the retention
clock starts when the content is public. **Test:** an envelope opened at day 91 has an
audit row aged 0.

### 2.6 Content safety

- All envelopes pass the **Confessions content filter** at seal (same word list, same
  rejection UX).
- **Anonymous envelopes have mentions stripped** at seal — an anonymous message naming a
  member on a ninety-day delay is an anonymous callout. Named envelopes keep mentions;
  they're attributed speech, like any message.
- `body_max_chars` 1000.

### 2.7 Departure, erasure, jail

| Event | Pending room envelopes | Pending to-me envelopes | Published |
|---|---|---|---|
| Leaves guild | **destroyed** at next sweep | **destroyed** | untouched (Discord messages) |
| `/delete_me` | **destroyed** | **destroyed** | row deleted; message stays, as with confessions |
| Jailed | **held** — skipped at the drop, re-queued; destroyed if still jailed at the second consecutive drop | **unaffected** — DM'd on schedule; jail limits the room, not the member's own letter to themselves | n/a |
| Opts out of sealing | unaffected | unaffected | n/a |

### 2.8 Moderation (dashboard)

- **Upcoming drop preview** — the next drop's *room* envelopes, text visible to mods,
  with **Pull** (reason modal → DM to author → destroyed; payout untouched). **To-me
  envelopes never appear here**; they don't publish, so there is nothing to moderate.
  Members are told in panel copy and the privacy notice that mods can read pending room
  envelopes.
- `envelope_review` (default **off**): when on, seals land `pending` and are approved
  from the todo board's 🧾 Approvals section. Ship off.

### 2.9 Panel

`/envelopes` → one ephemeral panel:

- **✉️ Seal one** → modal: body · drop (next / +2 / +3 with dates) · to (room / me) ·
  sign it (named / anonymous)
- **📬 My envelopes** → read / burn per row
- **Stop sealing / Start sealing**
- Footer: next drop date · sealed count for it

### 2.10 Dashboard (`sealed-envelopes`)

Dials: `drop_every_n_featured_days`, `max_fuse_drops`, `max_pending_per_member` (5),
`envelope_review`, `body_max_chars`. Preview + Pull. Report: seals, burns, pulls, drop
sizes, named/anon ratio, to-me share (count only).

### 2.11 Data

| Table | Columns (sketch) | Purge |
|---|---|---|
| `envelopes` | id, guild, author_id, body, addressed, attribution, drop_id, sealed_at, state (pending/held/published/burned/pulled), published_at, published_channel_id, published_message_id | **YES.** `body` is the member's own words. Pending destroyed; published rows deleted (message out of scope). |
| `envelope_prefs` | guild, user_id, opted_out | **YES** |
| `envelope_paid` | guild, user_id, cycle_id | **YES** |
| `anon_audit_log`, slug `envelopes` | existing; written at publication | existing |

Privacy notice: *"If you seal an envelope, the bot stores your text until it opens or you
burn it. Moderators can read envelopes addressed to the room before they open; they cannot
read envelopes addressed to you."*

### 2.12 v2 candidates

To-a-member addressing; **prediction mode** ("I predict…", room votes the call, coin bonus
on a hit); a Drift hook pairing a to-me envelope with a Second Sight prompt id.

---

## 3. Second Sight

### 3.1 Concept

One engine, three modes. A prompt drops. Everyone answers for themselves. Everyone can
perceive any member who has answered — **Read** their closed answer, **Tell** who wrote an
open one, **Describe** them on a Mirror prompt. On the featured day answers are revealed
and perceptions scored. Over a season each member accrues a private record of how well they
read people and how the room reads them.

Interpersonal accuracy is a real, trainable skill with an honest ceiling near 50%
(Ickes; Marangoni et al. 1995); gains are front-loaded and feedback drives them. That is
a low floor and a high cap that never gets solved.

### 3.2 The three rules

1. **Score the skill, never the relationship.** The public number, when there is one, is
   perceptiveness — yours, about people in general. The one dyadic number that exists
   (§3.8, who-reads-you) is private, opt-in, and never shown to anyone but the subject.
2. **Build the response layer.** Being responded to matters more than the disclosure
   (Reis). One-tap, restricted-vocabulary reactions, delivered privately.
3. **Consent falls out of the mechanic.** You become a target only by answering.
   **Answering is visible** — the perceive slate is a list of who has answered, and the
   panel copy says so in plain words before the first answer. Don't answer, you don't
   exist for that round. Pass is a button.

### 3.3 Prompt types and modes

| Type | Answer | Perceive | Scored |
|---|---|---|---|
| **Closed** | one of four bank options | **Read**: pick their option + confidence | Brier |
| **Open** | ≤ 140 chars, **guessable** or **sealed** | **Tell**: attribute an unattributed answer to its author + confidence | Brier |
| **Mirror** | five adjectives for yourself from the curated list | **Describe**: five adjectives for an assigned member | Jaccard overlap |

Open prompts are Tells only — free text can't be predicted, but a voice can be
recognised, which is a different and very real skill.

### 3.4 Cycle

- `prompts_per_cycle` **default 2** (1–3), round-robin, heat-filtered. One cycle at four
  days with a single prompt was too thin for the room to feel alive.
- Answer window: cycle start → open. Perceive window: same, gated per target on having
  answered. Reveal at open.

### 3.5 Answering

- Closed: select. Open: modal text + guessable/sealed select. Mirror: exactly five from
  the adjective list.
- Open answers pass the **Confessions content filter** and have **mentions stripped**;
  they're visible to members unattributed before any mod sees them.
- Answers can be **changed** until reveal; perceptions are scored against the answer as it
  stood at reveal. One truth per cycle.
- **Pass** records nothing, pays nothing, and removes you from the slate.

### 3.6 Perceiving

- **Read / Tell @person** → user select of members who answered this prompt, minus self,
  opted-out, and either side of a no-contact pair (silent, full slate).
  - Read: their option select → confidence.
  - Tell: unattributed guessable answers appear in the panel as they accumulate
    (bot-shuffled order, re-shuffled per viewer); pick an answer → pick its author from the
    slate → confidence. Sealed answers never appear here.
- **Confidence** *hunch / fairly sure / certain* → p ∈ {0.55, 0.75, 0.90}; scored
  `(p − outcome)²`. Certain-and-wrong 0.81; hunch-and-wrong 0.30; certain-and-right 0.01.
  The confidence step rewards knowing what you don't know.
- **Describe** (Mirror cycles): each participant who answered is **assigned
  `mirror_assignments` (3) members to describe**, drawn by the Compliment derangement
  (no self, no no-contact, each subject assigned as evenly as possible). Extra describes
  beyond the assignment are allowed. Assignment is what gets subjects to the delivery
  threshold; organic describing never would.
- One perception per (perceiver, target, prompt, cycle). Final.

### 3.7 Reveal (at open)

Public, in the room:

- **Closed**: results card — prompt, answer distribution as bars with counts and no
  names, **only if at least `min_answers_to_show_distribution` (8) answered**; otherwise
  the card shows the prompt and participation count only. Reads placed. "Best read this
  round: *N%*" with no name unless `perceptiveness_public` is on.
- **Open**: answers card — each **guessable** answer with its author attached; each
  **sealed** answer unattributed. Tells placed. Same best-read line.
- **Mirror**: nothing public.

Private, via **My card**:

- This cycle: each perception you placed → truth, right/wrong, Brier.
- Season: **perceptiveness** (mean Brier, friendly label + number), **breadth** (distinct
  members perceived), **depth** (accuracy on members you've perceived ≥ 3 times, shown
  per-member to you only). Tracked separately on purpose.
- **Who reads you** (§3.8).
- **Your Mirror** — delivered once `mirror_min_describers` (5) have described you: words in
  *Arena* (agreed), *Blind spot* (they saw, you didn't pick), *Façade* (you picked, nobody
  did). Never who, never counts. **Publish my Mirror** posts it to the room if you choose;
  the bot never does.
- `sight_read` payout summary for the cycle.

### 3.8 Who reads you

A per-member toggle, **off by default**: `show_me_who_reads_me`. When on, My card shows
the members who perceive you most accurately, **with their per-person accuracy**, and
only once that member has perceived you at least `min_reads_for_pair_score` (3) times.
One wrong hunch must never render as "Alice reads you at 0%".

This is the one dyadic number in the design. It is visible to exactly one person — the
subject — and to nobody else, ever, including the reader. It does not appear on any
public card or in any report.

### 3.9 Public number switch

`perceptiveness_public` (default **off**): when on, the reveal card names the round's best
perceiver and a season leaderboard appears in the panel. **If the score starts being used
socially — bragging about knowing someone, hurt at being read badly — flip it off.** A
config flip, not a rewrite. Turn it on after a two-week pilot if the room wants it.

### 3.10 Seasons and retention

- `season_length_cycles` (8). Closing card at the boundary: the season's prompts,
  participation, and — if public — the perceptiveness winner. Public numbers reset.
- Private history is kept for `answer_retention_days` (**730**) then purged by the hourly
  sweep. **Clear my history** on My card deletes the member's answers, perceptions placed
  and perceptions received immediately. Season stats are recomputed, never stored.

### 3.11 Drift (v2; schema ready)

A prompt flagged `rerun_after_days` (180) is redrawn when due; a member who answered it
before sees *then* beside *now*, privately. This is why answers are retained across
seasons.

### 3.12 Witness layer (v1-lite)

After reveal, for each member you perceived: one tap — **knew it · surprised me · tell me
more** — delivered to the answerer as a private aggregate ("4 were surprised; 2 want more"),
never who. Restricted vocabulary on purpose. `witness_enabled` default on. v2: a witness
stat, and the same primitive retrofitted onto Confessions and Superlatives.

### 3.13 Panel

`/sight` → one ephemeral panel:

- **Answer / Change answer** · **Pass** — one row per current prompt
- **Read / Tell / Describe someone** — mode follows the prompt type; assigned Mirror
  subjects listed first
- **My card**
- After reveal: **React** · **Publish my Mirror** (Mirror cycles)
- **Leave me out / Count me in** (never a target; you can still perceive)
- Footer: prompt state · answered count · your perceptions this cycle · next featured day

### 3.14 Dashboard (`second-sight`)

- **Bank editor**: type, options (closed), heat, active, weight, `rerun_after_days`,
  teaser. Verify shared-vs-per-feature before stage 2.
- **Adjective list editor** (positive list; Nohari v2, opt-in per subject).
- Dials: `prompts_per_cycle`, `min_answers_to_show_distribution`, `mirror_assignments`,
  `mirror_min_describers`, `reads_paid_per_cycle`, `min_reads_for_pair_score`,
  `perceptiveness_public`, `witness_enabled`, `season_length_cycles`,
  `answer_retention_days`.
- **Report**: answers/perceptions per cycle, distinct perceivers, coverage, mean accuracy,
  pass rate per prompt (a heat or wording signal), Mirror subjects under threshold,
  filter rejections on open answers.

### 3.15 Data

| Table | Columns (sketch) | Purge |
|---|---|---|
| `sight_prompts` | bank | n/a |
| `sight_answers` | guild, cycle_id, prompt_id, user_id, answer (option id / text / adjective ids), sealed, changed_at | **YES**; kept ≤ 730 days for Drift |
| `sight_perceptions` | guild, cycle_id, prompt_id, perceiver_id, target_id, mode, guess, confidence, correct, score, scored_at | **YES on either party.** Recomputing another member's season score after a target purge is accepted and noted in the register. |
| `sight_mirror_assignments` | guild, cycle_id, prompt_id, describer_id, subject_id | **YES on either party** |
| `sight_reactions` | guild, cycle_id, prompt_id, reactor_id, target_id, kind | **YES on either party** |
| `sight_prefs` | guild, user_id, opted_out, show_me_who_reads_me | **YES** |
| `sight_paid` | guild, user_id, cycle_id, kind, count | **YES** |
| `anon_audit_log`, slug `sight` | existing | existing |

`target_id` / `subject_id` go in `SUBJECT_ID_COLUMNS`.

Privacy notice: *"Second Sight stores your answers, the perceptions you place on others
and the ones placed on you, for up to two years, so it can score them and show you your own
history. Answering a prompt is visible to other members. Nobody sees perceptions about you
unless you turn that on, and you can clear your history at any time."*

### 3.16 Failure modes

| Risk | Mitigation |
|---|---|
| Score corrupts intimacy | skill not relationship; public number off; the flip-it-off threshold |
| Small-N deduction from the distribution | distribution hidden under 8 answers; answering disclosed as visible |
| Per-person accuracy as a wound | private, opt-in, minimum 3 reads, seen by the subject only |
| Motivated inaccuracy | known (Simpson et al. 1995); depth is private for exactly this reason |
| Tells exposing someone painfully | sealed toggle; open answers filtered and mention-stripped |
| Mirror never reaching threshold | assigned describers via derangement |
| Exclusion | breadth stat; assignment; low-coverage read bonus in v2 |
| Read farming | paid on correct only, cap 5 |
| Content supply | round-robin; Drift re-runs; member-submitted prompts in v2 |

---

## 4. Build order

| Stage | Contents | Ships with |
|---|---|---|
| **1 — Superlatives** | bank, heat ceiling, nominations, ballot build, tally, withdraw, faucet + trigger, dashboard, report | register rows; manual section; tests: slate filtering, heat ceiling, min_nominations, turnout floor, re-check at build and tally, once-per-cycle |
| **2 — Second Sight core** | closed + open prompts, answer / pass / perceive, Brier, reveal with distribution threshold, My card, who-reads-you, faucets, dashboard | tests: consent-by-answering, no-contact on slate, scoring table (`pytest.param` rows), pair-score minimum, correct-only payout, season reset, retention sweep |
| **3 — Second Sight Mirror + witness** | Mirror type, adjective editor, derangement assignment, threshold delivery, publish, reactions | tests: assignment evenness and exclusions, describer threshold, region computation |
| **4 — Sealed Envelopes** | seal / read / burn, drops, founders' drop, filter + mention-strip, hold-on-jail (room only), destroy-on-leave, audit-at-publish, preview (room only) + pull | tests: burn, destroy-on-leave, to-me unaffected by jail, held-then-destroyed, audit age at publish, preview excludes to-me |
| **5 — Rotation tuning** | register all three; `rooms_per_day` to 2; pair quest triggers; retire plan doc to shipped | rotation plan doc updated |

Stage 1 first because it's smallest and proves the faucet-and-trigger wiring the others
inherit. Commit subjects `Scope: summary`; every behaviour-changing commit ends with a
`Testing:` checklist.

---

## 5. Verify before build

No open design decisions remain. Three things to confirm in `src/` before stage 1:

1. Whether question banks share one editor and draw, or each feature has its own — this
   sets the cost of three new banks.
2. That the rotation registry exposes an **on-open hook** that jobs can attach to; every
   posting job in this spec is keyed to it.
3. That the Compliment derangement is callable as a library function with exclusion sets,
   for Mirror assignment.
