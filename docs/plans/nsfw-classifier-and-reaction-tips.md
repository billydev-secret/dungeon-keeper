# Shared NSFW classifier + reaction tips

**Branch:** `nsfw-autoreact-tips` · **Started:** 2026-07-28

## Why

Two requests that turned out to share one dependency:

1. **Tip the poster of an NSFW image when someone reacts.** Auto React
   (`043_auto_react.sql`, `auto_react_service.py`) already places emoji on image
   posts in configured channels — but it has **zero rows in prod**, so it has
   never run anywhere. The genuine gap is "NSFW images" vs its current "any
   image", plus the whole tipping mechanism.
2. **Make spoiler enforcement content-aware.** `enforce_spoiler_requirement`
   currently deletes *any* unspoilered image in a spoiler-required channel — a
   meme, a screenshot, a cat photo. Blunt, and the false positives are the
   complaint.

Both need "is this image explicit?", and a third consumer appeared while
scoping: **preventing nudity in SFW channels**. So classification is not a
feature of auto-react; it is a shared service with three consumers, and the
consumers disagree about which way a failure should fall.

Cost was measured before committing to it (Intel N150, bundled 320n model):

| | |
|---|---|
| cold start (import + model load) | ~470 ms, once per process |
| warm inference, 1536×1024 | **74 ms** median (65–81) |
| resident memory | +132 MB, process lifetime |
| image posts/day, server-wide | 200–500 (~290 avg over 14 d) |
| **CPU/day** | **~21 s** |
| busiest single minute in 14 d | 31 images → 2.3 s, ~4% of one core |

Compute is a non-issue. The real costs are CDN bandwidth (today's auto-react
reads `content_type` and never fetches bytes), event-loop blocking
(onnxruntime is blocking C++ — must go through `run_in_executor`), and
false-negative/false-positive risk, which differs per consumer.

## Decisions (settled with Ben 2026-07-28 — do not re-litigate)

**Funding.** Reactor pays from their own wallet — a transfer, **zero
inflation** — plus a rake taken off the top and **burned**. This makes
reactions a net *sink*, pointing the same way as the retune of two days ago
(casino RTP trim `3488750d`, jackpot skim 25%→5% `bede17e9`, XP-mint ceiling
`5a3e2945`) rather than against it. A house-minted version was considered and
rejected: at ~1,050 reactions/day against a 74,083-coin supply it would have
inflated ~1.4%/day.

**Rake.** `max(1, round(10%))`, burned. A flat percentage was rejected because
5% of a 5-coin tip rounds to zero, making the sink symbolic.

**Ladder.** Per-emoji denominations — the channel's auto-react emoji set *is* a
price ladder, so which emoji you tap is how much you give. Start at **5/25/100**:
a 1-coin rung would deliver the poster nothing after the 1-coin minimum burn.
Grounding: median wallet is 72 coins, 131 of 285 wallets hold under 50, 43 hold
under 10 — these amounts bite harder than the total supply suggests.

**What charges.** Only emoji **the bot itself placed on that specific message**.
Not "the emoji is in the channel's set" — that would let anyone paste a rung
onto a text post, an old message, or an image the classifier rejected, and turn
it into a payment target the bot never sanctioned. The bot's reaction is the
receipt.

**Insufficient funds.** Tip what they have. 0 balance = free no-op. A tap that
would leave the poster 0 after the minimum burn is skipped entirely — no debit,
no burn, nothing.

**Unreact/re-react.** Charge once per `(guild_id, message_id, user_id)`, ever.
Copies `xp_reaction_awards`, which has held over 27,266 rows. Removing refunds
nothing; re-adding is free. A partial tip paid while broke stays partial.

**Excluded.** Bot and webhook reactors (this very feature reacts first, and the
bot has no wallet). Self-tips are ignored entirely — no debit, no credit, no row
— so a self-tap can't inflate the count the emoji is supposed to signal.

**XP untouched.** The same tap still earns `xp_coeff_reaction_given_xp` = 0.25.
Coins therefore buy a trickle of XP; the round trip back to coins is already
bounded by the daily XP-mint ceiling from `5a3e2945`.

**Gating.** `channel.is_nsfw()` stays the rail — CLAUDE.md keys NSFW on
Discord's own age gate, and a classifier is a bot-side judgment call, so it
*narrows* within an age-gated channel and never substitutes for it.

**Explicit set.** Any `*_EXPOSED` label (genitalia, anus, breast, buttocks) plus
the pipeline's synthetic `SEX_ACT`. `*_COVERED` labels do not qualify. Label set
and threshold are dashboard fields, not constants.

**Attachments only.** Embeds are not classified. `_has_image` currently matches
`gifv`/`rich` embeds whose images live on arbitrary external hosts; fetching
those would aim the bot's outbound requests at member-supplied URLs (SSRF
probing, IP-logging pixels, hostile payloads). In a tipping-enabled channel
embeds therefore get **no emoji at all** — since bot-placed emoji are live tips,
reacting to something unclassifiable would create a tip nothing approved.

**Coverage vs recording — these deliberately differ.** Classification runs on
attachments in *every* channel, because SFW prevention needs it everywhere.
Detections are **recorded only for uploads in NSFW channels**, so no dataset is
built out of general chat.

**Retention.** Indefinite, both tables (Ben's call; the growth concern was
raised and declined). Minimizations applied regardless: rows key on
`message_id` and join `messages` for authorship rather than duplicating
`author_id`, and the dashboard view is admin-gated.

**Privacy note, recorded deliberately.** `nsfw_detections` is the most sensitive
table this bot holds — effectively a labelled body-part inventory of members'
uploads. It is derived metadata rather than content, which fits CLAUDE.md's
"derive at ingest" rule, but it should never be surfaced outside an admin-gated
view.

**Incentive note, recorded deliberately.** This pays members to post explicit
images, and the classifier gate sharpens that by paying only for images scoring
as explicit. That is the mechanism working as designed and Ben chose it
knowingly; it is written down so nobody later mistakes it for an oversight.

## Architecture

One service, three consumers, **one classification per message** — spoiler
enforcement and auto-react both fire on the same `on_message`, so the verdict is
computed once and shared (in-process cache keyed by attachment id, backed by the
`nsfw_classifications` row).

Failure direction is per consumer, and is the crux:

| consumer | where | gate | on failure |
|---|---|---|---|
| **Tipping** | `is_nsfw()` channels with a tipping rule | exposed set, standard threshold | **react anyway** — a CDN hiccup must never cost a poster |
| **Spoiler** | `spoiler_required_channels` | exposed set, standard threshold | **delete** — falls back to today's behavior; unreadable is treated as maybe-explicit |
| **SFW prevention** | every other channel | exposed set, **higher** threshold | **do nothing** — never delete on a failed read |

The higher threshold for SFW prevention is deliberate: a false positive there
deletes an innocent photo, so it must demand more certainty than merely
qualifying a post for coins.

## Schema

`141_nsfw_classifier.sql`

```sql
CREATE TABLE nsfw_classifications (
    message_id    INTEGER NOT NULL,
    attachment_id INTEGER NOT NULL,
    guild_id      INTEGER NOT NULL,
    channel_id    INTEGER NOT NULL,
    verdict       INTEGER NOT NULL,   -- 1 = explicit
    top_label     TEXT,
    top_score     REAL,
    model         TEXT NOT NULL,      -- '320n'
    threshold     REAL NOT NULL,      -- what was used, so old rows stay readable
    label_set     TEXT NOT NULL,      -- ditto, after a retune
    inference_ms  INTEGER NOT NULL,
    bytes         INTEGER,
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (message_id, attachment_id)
);

CREATE TABLE nsfw_detections (
    message_id    INTEGER NOT NULL,
    attachment_id INTEGER NOT NULL,
    label         TEXT NOT NULL,
    score         REAL NOT NULL,
    x1 INTEGER, y1 INTEGER, x2 INTEGER, y2 INTEGER
);
```

Recording `threshold` and `label_set` per row is what makes the data survive a
retune — without them, old rows become uninterpretable and the retrospective
"what would 0.4 have changed?" question can't be answered.

Split across two migrations as built, so each stage's commit stands alone:
`142_auto_react_tips.sql` (stage 4 — the rule flag and placement receipts) and
`143_reaction_tips.sql` (stage 5 — rungs and awards). `auto_react_placements`
also carries `channel_id` and `author_id`, so a tip never has to re-fetch the
message to learn who it pays.

```sql
ALTER TABLE auto_react_config ADD COLUMN tips_enabled INTEGER NOT NULL DEFAULT 0;

CREATE TABLE reaction_tip_rungs (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    emoji      TEXT    NOT NULL,
    amount     INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id, emoji)
);

-- what the bot actually placed; the receipt that makes a rung live
CREATE TABLE auto_react_placements (
    guild_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    emojis     TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);

-- one charge per reactor per message, forever (xp_reaction_awards shape)
CREATE TABLE reaction_tip_awards (
    guild_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    amount_paid INTEGER NOT NULL,
    rake        INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id, user_id)
);
```

`auto_react_placements` rather than fetching the message and checking
`reaction.me` — the latter costs an API round trip on every reaction event at
~1,050 events/day, and the table doubles as the record of what qualified.

## Stages

Each stage is one commit. Per CLAUDE.md the matching spec and `manual.html`
update **in the same commit** as the behavior, so docs are listed per stage
rather than saved for the end.

### Stage 1 — classifier service + metrics (`services/nsfw_classifier.py`, migration 141)

The service and its tables, with no consumers wired yet. Download (attachments
only, size-capped), decode, `run_in_executor` around `guess_nudenet.detect`,
label-set + threshold verdict, per-message cache, recording gated to
`is_nsfw()` channels. Reuses the existing lazy `_get_detector()` so the model
loads once and only if something actually classifies.

Tests: verdict for each label set at threshold boundaries; `*_COVERED` does not
qualify; `SEX_ACT` does; recording happens for an NSFW channel and **not** for a
SFW one; cache returns one classification for two consumers; download failure
and inference failure each surface as an explicit "unknown" rather than a
verdict, so callers pick their own fallback.

Docs: new `docs/nsfw_classifier_spec.md` + INDEX.md entry (Reference).

### Stage 2 — spoiler enforcement narrows (`core/post_monitoring.py`)

Only explicit unspoilered images are deleted; everything else is left alone.
Unknown verdict → delete (today's behavior preserved).

Tests: explicit unspoilered → deleted; non-explicit unspoilered → untouched
(the false positive this stage exists to fix); spoilered explicit → untouched;
unknown verdict → deleted; bypass role still bypasses; non-spoiler channel
untouched.

Docs: whichever spec covers post monitoring, + `manual.html` (the rule members
experience changes).

### Stage 3 — SFW nudity prevention

New consumer in the `on_message` path. Delete → short auto-deleting public
notice → mod-log entry with scores for audit → DM the poster their image back so
a wrong call doesn't destroy their file. Separate higher threshold. Fails open.

**Excluded, and this matters:** bot and webhook uploads. `guess_cog.py` posts
`SPOILER_guess_full.jpg`, `SPOILER_guess_crop.jpg` and a confession card — if
the Guess channel isn't Discord-marked NSFW, this stage would delete the bot's
own game content on day one. Also honors a dashboard channel-exclusion list and
the existing `bypass_role_ids`.

Tests: explicit in SFW channel → deleted + logged; bot upload → untouched
(regression for the Guess collision); webhook → untouched; excluded channel →
untouched; bypass role → untouched; unknown verdict → untouched (fail-open);
below the higher threshold but above the tipping one → untouched.

Docs: new spec section + `manual.html`.

### Stage 4 — auto-react gates on the classifier (`cogs/auto_react_cog.py`)

In a tipping-enabled channel: require `is_nsfw()`, attachments only (no
embeds), classify, and place emoji only on an explicit verdict. Record the
placement. Non-tipping rules keep today's behavior exactly, so existing
Auto React semantics are untouched for anyone using it as plain decoration.

Tests: explicit attachment → emoji placed + placement row; non-explicit → no
emoji; embed in a tipping channel → no emoji; non-NSFW channel with a tipping
rule → no emoji; unknown verdict → emoji placed (fail-open); tipping disabled →
unchanged legacy path.

Docs: `auto_react_spec.md`.

### Stage 5 — reaction tips (`services/reaction_tip_service.py`, migration 142)

The money. On `raw_reaction_add`: rung lookup → placement check → dedup check →
skip bot/self → compute `paid = min(rung, balance)`, `rake = max(1, round(paid *
0.1))`, skip if `paid - rake < 1` → `apply_debit(paid)` + `apply_credit(paid -
rake)` in one transaction, rake simply uncredited (that is the burn) → award row.

Not `transfer_currency`, because it credits the recipient the full debited
amount by design and cannot express a rake.

Ledger kinds `tip_out`/`tip_in` with the rake in `meta`, so `economy_loop.py`'s
existing drain narrates tips into the register channel for free — which is the
only feedback a reactor gets that money moved, since there is no confirmation
dialog anywhere in this flow.

Tests: happy path debits, credits and burns the right amounts; second reaction
by the same user on the same message is a no-op; unreact then re-react is a
no-op; self-tip is a no-op; bot reactor is a no-op; partial payment when short;
1-coin balance → skipped entirely (poster would receive 0); 0 balance → free
no-op; emoji not a rung → free; rung on a message with no placement row → free.

Docs: `economy_spec.md` + `manual.html`.

### Stage 6 — dashboard

Under the right nav heading, admin-gated: tipping toggle + per-emoji ladder on
each Auto React rule; classifier thresholds (tipping/SFW), label set, channel
exclusion list, mod-log channel; and a metrics view over the recorded data
(volume, verdict split, latency, label distribution).

Tests: route authz (picked up by the existing sweep), snowflake precision on
channel/message ids, panel-load health + responsive layout via the browser
suite, `npx eslint`/`stylelint` per CLAUDE.md.

Docs: `manual.html` Help panel sections.

## Rough scale once live

A few hundred taps/day moves ~1–2k coins and burns ~100–200 — roughly **0.2% of
supply drained per day**. A real sink, nowhere near disruptive.
`scripts/economy_tuning_report.py` should be run before and after to confirm
against actuals rather than this estimate.

## Risks

- **Classifier accuracy is the whole feature's floor.** A false negative
  silently costs a poster their tips with no signal to them; a false positive in
  a SFW channel deletes an innocent photo. The mod-log audit trail in stage 3
  and the recorded scores from stage 1 are how these get caught and tuned.
- **Latency before enforcement.** Download + inference adds ~1–2 s during which
  an image is visible. This does not make the push-notification-preview problem
  worse — deletion has always happened after the message posts — but it does not
  fix it either.
- **Stage 3 is the only stage that destroys user content.** It ships last of the
  enforcement stages, fails open, and DMs the image back. Worth running with
  deletion disabled (log-only) for a few days first to measure real accuracy
  before trusting it — a config flag, not a code change.
