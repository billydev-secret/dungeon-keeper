# Policy Tickets — opening the vote to members

**Status:** **BUILT 2026-09-03** to the *Decisions* section at the foot of this
file, which supersedes the body above wherever they disagree. Migration 202,
`services/policy_ballot_service.py`, `/policy ballot`, and the read-only ballots
section of the frozen `mod-policy-tickets` page. `docs/dungeon_keeper_jail_ticket_spec.md`
§Community ballots is now the reference; read this file for the reasoning, not
the behaviour.

**What the body below describes and the build deliberately does NOT contain**
(the decisions removed them): the five `policy_ballot_*` config dials and their
role multi-picker (§4) — the channel's own permissions are the electorate; the
tallies-only and secret-ballot constructions (§5) — the tally is fully public
and names every voter; quorum and `pass_pct` (§7) — simple majority, ties fail,
no minimum turnout; and any write path into `policies` — a passed ballot is
recorded, not enacted. Stage 0 of §8 shipped as its own commit.

Written 2026-09-03 against branch `website-voice-notes` at `55bf78d4`. Every
claim about current behaviour below was verified by reading `src/`; where
`docs/dungeon_keeper_jail_ticket_spec.md` and the code disagree, the code is
cited.
**Feature spec:** [dungeon_keeper_jail_ticket_spec.md](../dungeon_keeper_jail_ticket_spec.md)
(Reference). **INDEX.md classification when this lands:** Design.
**Billy's ask:** "I do want to make the policy tickets available to users" — e.g. a
veteran-only channel voting on a proposal.

---

## 1. What exists today (verified, not assumed)

### 1.1 One channel does three jobs

`/policy open` (`cogs/jail_cog.py:940`) is admin-gated by a runtime `_is_admin`
check and creates a **private text channel** in the configured ticket category.
Its overwrites deny `@everyone` (`jail_cog.py:984`) and admit the bot plus every
configured mod and admin role (`jail_cog.py:994-1003`). That single channel is:

1. the **deliberation venue** — mods argue in it;
2. the **vote venue** — `/policy vote` (`jail_cog.py:1065`) opens
   `_PolicyVoteModal` (`jail_cog.py:123`) for the exact wording, then posts a
   tally embed with three persistent buttons into *the same channel*
   (`jail_cog.py:175-192`) and pings the mod/admin roles (`jail_cog.py:1040-1048`);
3. the thing that is **transcript-archived and then deleted** on resolution —
   `finalize_policy_vote` archives via `_collect_and_post_transcript` and then
   calls `channel.delete()` (`commands/jail_commands.py:600`, delete at
   `jail_commands.py:758`); `/policy close` does the same
   (`jail_cog.py:1167-1199`).

That coupling is the whole problem. You cannot widen who can see the vote
without widening who can see the deliberation, because they are the same
message list.

### 1.2 The eligible roster is rebuilt three times, and the shared helper is dead

The roster rule is "non-bot member with `guild_permissions.administrator`, or
holding any configured mod/admin role". It is hand-rolled in three places:

| Site | What it feeds |
|---|---|
| `jail_cog.py:166-172` | the initial vote embed's "Awaiting" list |
| `jail_commands.py:833-839` | the running tally after every press |
| `jail_commands.py:2029-2035` | the timeout sweeper's absentee resolution |

`jail/logic.py:138 eligible_voters()` implements exactly this and **is imported by
no production code** — only by `tests/unit/test_jail_logic.py:11`. Same for
`jail/logic.py:165 tally_votes()` and `jail/logic.py:182 resolve_policy_vote()`;
only `vote_outcome()` (`jail/logic.py:202`) is wired up, at `jail_commands.py:2056`.
A member-facing electorate that has to agree with itself across three call sites
is a defect waiting to happen; §8 stage 0 fixes this first.

### 1.3 Votes are attributed and publicly named

```
policy_votes (policy_id, user_id, vote, voted_at, PRIMARY KEY (policy_id, user_id))
```
(`services/moderation.py:252-261`; upsert at `moderation.py:998-1005`.) The
running-tally embed renders every Yes/No/Abstain voter **and everyone still
outstanding** as raw `<@id>` mentions — `_format_mentions` at
`jail/embeds.py:57-59`, used for all four fields at `jail/embeds.py:150-153`.
`cap_mentions` (`jail/logic.py:42`) exists solely because a 25-mention field hits
Discord's 1024-char cap. At member scale this shape is unusable on its own terms.

### 1.4 Resolution arithmetic is unanimity, not majority

`vote_outcome` (`jail/logic.py:202-233`): while anyone is outstanding the result
is `pending`; any `no` rejects; adoption requires **every** eligible voter to have
voted yes. After the deadline (`policy_vote_timeout_hours`, default 72,
`jail_commands.py:1949`), absentees are dropped, a single `no` still rejects, and
a ballot nobody voted on is `rejected_no_quorum`. A sweep every 60 s over every
guild drives it (`jail_commands.py:1991-2015`).

Unanimity is a sane rule for a team of five. It is an absurd rule for a hundred
veterans — one `no` would kill everything. **Member voting cannot reuse this
arithmetic.**

### 1.5 Two live hazards worth naming now

**(a) The finalizer deletes whatever channel it is handed.** `_handle_policy_vote`
passes `interaction.channel` (`jail_commands.py:872`); `_resolve_expired_policy`
passes the channel resolved from `policy["channel_id"]` (`jail_commands.py:2062`).
Today both are the same channel so nothing shows. The moment a vote button lives
in a *second* channel, the button path deletes **that** channel. This is the single
most dangerous integration point in the feature and it is fixed before anything
member-facing exists (§8 stage 0).

**(b) `/policy` has no Discord-side permission gate at all.** The group is
constructed with no `default_permissions` (`jail_cog.py:204-206`), and
discord.py only emits `default_member_permissions` for top-level commands —
`Command.to_dict` guards it behind `if self.parent is None`
(`.venv/.../discord/app_commands/commands.py:788-791`). Every
`@app_commands.default_permissions(...)` decorator on `/policy open|vote|close|list`
is therefore **inert**: every member already sees the whole `/policy` group in the
picker and is stopped only by the runtime `_is_admin` / `_is_mod` checks. Two
consequences: adding a member-facing subcommand costs nothing in visibility, and
there is no Discord gate to lean on — every gate must be a runtime check with a
test behind it.

### 1.6 The dashboard side is read-only

Route id `mod-policy-tickets` (frozen; `static/js/app.js:112`, with the retired
`config-policy-tickets` redirect at `app.js:1311`). `GET /api/moderation/policy-tickets`
(`routes/moderation.py:1063`) is `require_perms({"moderator"})` and selects only
`policy_tickets` columns — **no web route reads `policy_votes` or `policies`**.
The page (`panels/policy-tickets.js`) mounts a queue list
(`panels/mod-policy-tickets.js`) plus a one-field settings half
(`panels/policy-tickets-settings.js`) that writes `PUT /api/config/policy`
(`routes/config.py:4375-4402`, `require_perms({"admin"})`), read back by
`_policy_section` (`routes/config.py:617`).

---

## 2. The shape of the fix: two votes, two venues, one proposal

Do **not** widen `policy_votes`' electorate. Add a second, parallel object:

```
policy ticket  ──┬── mod vote      (policy_votes)        private channel, unanimity, unchanged
                 └── community ballot (policy_ballots)   public channel, majority+quorum, new
```

The private channel keeps every job it has today. A **community ballot** is a
distinct row with its own message in a normal channel, its own electorate, its own
arithmetic, and its own lifetime — it outlives the private channel's deletion
because it never lived there.

The seam between them is deliberately narrow: **the only thing that crosses from
the private channel to the public one is a string a mod typed into a modal.** No
auto-copy of the proposal description, no transcript link, no "as discussed"
quote. That is what makes "widen the vote" not mean "widen the deliberation" —
and it is testable (§5).

**Rejected alternative — one vote, wider electorate.** Move the existing embed out
of the private channel and let `eligible_voters` return members too. It is fewer
tables, and it is wrong on three counts: unanimity at member scale is a veto
machine (§1.4); the embed names every voter and every absentee as `<@id>`
(§1.3), which is both a mass ping and a hard 1024-char wall; and the deliberation
would have to move with the vote or be split across two channels with the
finalizer deleting one of them (§1.5a). The two-object design is more code and
much less surprise.

---

## 3. Where a member casts the vote

### (a) A ballot message with persistent buttons in a normal channel — **recommended**

One message in a configured channel, Yes / No / Abstain as persistent
`DynamicItem` buttons (the exact pattern already in `jail_commands.py:897-966`,
registered in `cog_load` at `jail_cog.py:221-223`, so they survive restarts), an
ephemeral "your vote was recorded" receipt (already the behaviour at
`jail_commands.py:869-871`), and an embed that shows **no names, ever**.

* Highest turnout: it is where members already are, and it announces itself.
* Matches "Discord is for member self-service" and "one panel with buttons over a
  sprawl of subcommands" (CLAUDE.md).
* The message is a durable public artifact — the community's record of its own
  vote, unaffected when the mod channel is deleted.
* Reuses machinery that exists and is already tested at the component level
  (`tests/components/test_jail_views.py`).

Costs, and what they force:

* Anyone who can see the channel sees that a ballot exists and how many votes are
  in. That is fine, and is the point. It does mean **eligibility must be enforced
  on press**, not by channel visibility — a channel the veteran role can see is
  usually visible to more people than that.
* A live running count invites bandwagoning. Mitigated by showing only "N votes
  cast" while open and the breakdown only at close (§4).
* Custom-id prefix must not collide with the existing `policy_vote:` template —
  use `policy_ballot:`.

### (b) An ephemeral panel behind a slash command

`/policy ballot` → ephemeral embed → buttons. No public footprint at all, trivially
role-gated, zero herding.

Discovery is the killer: a member runs a command they were told about, and turnout
collapses to whoever read the announcement. It also produces no shared artifact —
there is nothing to point at afterwards saying "this is what the room decided". Keep
it as the **fallback shape** if Billy wants the ballot invisible to non-eligible
members, in which case the announcement problem has to be solved some other way.

### (c) The dashboard

Real forms, real auth, guild scoping already solved, results trivially renderable.
And it is the wrong venue: CLAUDE.md puts member self-service in Discord and keeps
the dashboard for configuration and mod work. The dashboard is moderator-gated
almost everywhere (Wellness is the lone member-facing section,
`docs/dashboard_ia.md:52`); a member-facing voting page would be a second
exception, behind a login most members have never used. Turnout would be worse than
(b).

**Use the dashboard for what it is good at instead:** the admin dials (§4) and the
moderator-only post-close per-member view (§5).

> **Recommendation: (a).** Public ballot message, persistent buttons, ephemeral
> receipt, no names in the room. (c) is rejected outright; (b) stays on the shelf
> as the private-ballot variant.

---

## 4. Who is eligible, and how it is configured

### The dial

A **multi-role picker**, on the existing **Policy Tickets** page (route id
`mod-policy-tickets` — frozen, label and grouping free), in its Settings half
alongside the voting deadline that already lives there. That page is where a mod
already is when thinking about proposals, and `docs/dashboard_ia.md:19-21` records
that the settings deliberately sit at the bottom of the proposal queue.

New keys on `PUT /api/config/policy` (already `require_perms({"admin"})`,
`routes/config.py:4379`) and `_policy_section` (`routes/config.py:617`):

| Key | Shape | Default | Meaning |
|---|---|---|---|
| `policy_ballot_role_ids` | `config_ids` bucket | *(empty)* | Roles whose holders may vote. Empty = community ballots are **off**. |
| `policy_ballot_channel_id` | int config value | `0` | Where ballot messages are posted. `0` = off. |
| `policy_ballot_hours` | int config value | `72` | How long a ballot runs. |
| `policy_ballot_quorum_votes` | int config value | `0` | Minimum votes cast for a result to count. `0` = no quorum. |
| `policy_ballot_pass_pct` | int config value | `50` | `yes / (yes+no)` percentage needed to pass; `>` not `>=` (see §7). |

### Copy the cleanest existing precedent, don't invent one

DK stores role gates two ways:

* **CSV in a config value** — `mod_role_ids` / `admin_role_ids`, parsed by
  `_parse_id_csv` (`core/app_context.py:428-429`). Legacy shape; the value is an
  opaque string.
* **A `config_ids` bucket** — `auto_role_ids`, `bypass_role_ids`: read with
  `get_config_id_set` (`core/db_utils.py:196`), written wholesale with
  `replace_config_id_bucket` (`core/db_utils.py:179`), surfaced as a list of id
  strings by `_id_str_list` (`routes/config.py:310`).

**Use the bucket.** Its UI precedent is `panels/config-auto-role.js`, which mounts
`mountRoleMultiPicker` (`config-helpers.js:710`) into a `<span data-picker>` slot
and posts `getValues()` as a list of id strings (`config-auto-role.js:57-71`). That
is one function call and it already handles dangling ids, a failed role fetch, and
member-name hydration. Nothing about a ballot roster justifies a new shape.

### How it is enforced (not merely stored)

* **At press time, against live Discord roles.** A snapshot taken at open would let
  a member who lost the role keep voting. The press handler resolves the member's
  current roles and refuses otherwise — the same runtime-check posture forced by
  §1.5b.
* **One roster function.** `jail/logic.py` grows
  `ballot_eligible(members, ballot_role_ids)` next to `eligible_voters`, and stage 0
  first puts the *existing* three call sites onto `eligible_voters`. Two electorates,
  two functions, zero copies.
* **Fail closed.** Empty `policy_ballot_role_ids`, or an unset ballot channel, or a
  roster that resolves to zero members ⇒ the open action refuses with a message
  naming the missing dial. A ballot nobody can vote in is never posted (§7).
* **Never "everyone" by default.** An empty picker means off, not open to all. This
  is the one place a wrong default is a governance incident rather than a bug.

A note on naming: `veteran` already exists in DK as a *grant role* slug
(`core/db_utils.py:242`, `docs/role_grant_spec.md:3`). The ballot dial is
independent of that — it takes whatever role ids Billy picks, and should not be
wired to the grant registry.

---

## 5. Anonymity — the decision with the most consequences

Three honest positions, and what each actually costs.

### Fully public (today's shape, widened)

Names in the embed, live, for yes/no/abstain and for everyone outstanding.

* **Honesty of the vote: worst available.** A member's position on a mod-authored
  proposal becomes a permanent, searchable, public fact next to a running score.
  The bandwagon effect of a live named tally is the whole reason political science
  bothers with secret ballots.
* **Brigading detection: best.** Perfect per-member forensics.
* **It also does not work mechanically:** `<@id>` lists blow the 1024-char field
  (`cap_mentions` exists for exactly this, `jail/logic.py:42`) and read as a mass
  ping at member scale.
* **And it collides with the no-contact list.** See §7 — a public voter roster
  names every no-contact pair to each other in one embed, and unlike Risky Rolls
  (`docs/no_contact_spec.md:206-222`) there is no earlier place to move the gate:
  you cannot refuse someone's vote because their no-contact partner voted. Public
  names and no-contact are incompatible.

### Tallies-only — counts shown, names not — **recommended**

The room sees `Yes 41 · No 12 · Abstain 6` at close and "23 votes cast" while open.
Nobody's name is ever rendered in the channel. Moderators can see who voted what,
on the dashboard, **after the ballot closes**.

* **Honesty: good in practice.** The pressure that actually distorts a community
  vote is peer visibility in the room; that is gone.
* **Brigading detection: fully retained.** A mod can see 30 votes from accounts that
  joined last week.
* **It is not secrecy, and must never be described as such.** This is precisely how
  DK's built anonymity already works: `anon_audit_log.actor_id` "is the real member
  behind an anonymous post — that is the point of the table"
  (`docs/anon_audit_spec.md`, Privacy posture), admin-gated with no member-facing
  read path; Confessions stores `user_id` next to the pseudonym
  (`services/confessions_service.py:156-158`); Survivor's register row says outright
  that pick secrecy "is a *display* rule, not storage" (`docs/data_register.md`,
  `survivor_picks`). The member-facing copy says: **"Your vote is not shown in the
  channel. Moderators can see it."** That sentence is the enforcement of the
  promise, because it is the only promise being made.

### Fully secret — nobody, including mods, can attribute a vote

Achievable, but only one construction actually holds, and it has a price.

* **The obvious construction fails.** `policy_ballot_votes(ballot_id, choice, cast_at)`
  with no `user_id`, plus a separate `policy_ballot_participants(ballot_id, user_id, voted_at)`
  for uniqueness, is trivially re-identified: the two tables are written in the same
  order, so `rowid` (or `cast_at`) joins them back together almost perfectly.
* **The construction that works** replaces the choice log with an **aggregate
  counter** — `policy_ballot_tallies(ballot_id, choice, count)` incremented in place,
  storing no per-vote row at all — alongside `policy_ballot_participants` for the
  one-vote-each guarantee. There is nothing to correlate because there are no
  individual choice rows.
* **The price, and it is not negotiable: a vote cannot be changed.** Decrementing
  the old choice requires knowing the old choice, which requires storing it against
  the member, which is tallies-only wearing a hat. One press, final.
* **A salted hash is not a third option.** `docs/survey_spec.md` proposes
  `hash(salt || user_id)` — and per `docs/INDEX.md:117` it is **zero code**, never
  built. It also would not help here: to answer "has this member already voted" you
  must rehash the presser's id, so the salt has to be retained, and anyone holding
  the salt plus the member list (i.e. anyone with the database) re-identifies every
  row in seconds. Against the only adversary that matters here, it buys nothing over
  storing `user_id` and it *sounds* like it buys everything. Do not ship it.
* **What is lost:** per-member brigading forensics. Aggregate anomalies (a 40-vote
  burst in 90 seconds) stay visible; "which accounts" does not.

> **Recommendation: tallies-only, plus results hidden until close.** Two dials'
> worth of secrecy for none of the operational cost, and honest copy. If Billy wants
> true secrecy, take the counter-table construction and accept "no vote changes" —
> do not take a middle option that only reads as secret.

### Does `policy_votes` need to change?

**No — and the member ballot must not reuse it.** Reasons:

1. Its rows are rendered *with names* into a channel (`jail/embeds.py:150-153`).
   Overloading the table means one query can no longer tell "safe to name" from
   "must never name" without a discriminator column, and the failure mode of getting
   that wrong is publishing a member's vote.
2. It has no `guild_id`, which is why `privacy_service.py:665` has to special-case
   it through a parent join. A new table denormalizes `guild_id` and needs no
   special case — the shape the register already prefers (`survivor_players`,
   `mahjong_seats`).
3. The choice vocabulary and the uniqueness key are the same by coincidence, not by
   design; the arithmetic (§1.4 vs §7) is completely different.

So: `policy_votes` is untouched, its data-register row (`docs/data_register.md:107`)
stands, and the ballot gets its own tables.

---

## 6. Privacy and the data register

Two new tables. Both take `guild_id` directly so the access export's standard guild
scoping works with no parent join, and both use `user_id`, already in
`SUBJECT_ID_COLUMNS` (`services/privacy_service.py:819`) so the export sees them.
`tests/test_privacy_register_coverage.py` hard-fails without the rows below.

```sql
CREATE TABLE policy_ballots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    policy_id          INTEGER NOT NULL REFERENCES policy_tickets(id),
    channel_id         INTEGER NOT NULL DEFAULT 0,   -- the PUBLIC ballot channel
    message_id         INTEGER NOT NULL DEFAULT 0,
    question           TEXT    NOT NULL DEFAULT '',  -- the only string that crosses
    opened_by          INTEGER NOT NULL,
    opened_at          REAL    NOT NULL,
    closes_at          REAL    NOT NULL,
    closed_at          REAL,
    closed_by          INTEGER,                      -- NULL = closed by the sweep
    eligible_at_open   INTEGER NOT NULL DEFAULT 0,   -- frozen denominator, §7
    quorum_votes       INTEGER NOT NULL DEFAULT 0,   -- frozen dials, §7
    pass_pct           INTEGER NOT NULL DEFAULT 50,
    yes_count          INTEGER,                      -- frozen at close
    no_count           INTEGER,
    abstain_count      INTEGER,
    outcome            TEXT                          -- passed|failed|failed_no_quorum|cancelled
);
CREATE INDEX idx_policy_ballots_guild ON policy_ballots (guild_id, outcome);
CREATE INDEX idx_policy_ballots_open  ON policy_ballots (closed_at, closes_at);

CREATE TABLE policy_ballot_votes (
    ballot_id  INTEGER NOT NULL REFERENCES policy_ballots(id),
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    choice     TEXT    NOT NULL,   -- yes|no|abstain
    cast_at    REAL    NOT NULL,
    PRIMARY KEY (ballot_id, user_id)
);
```

The frozen counts on `policy_ballots` are load-bearing for the erasure decision:
**because the outcome is snapshotted at close, deleting a member's vote row cannot
change a decided result.** Same construction as `mahjong_results` (settled hand
preserved, seats purged) and `econ_ledger` (the money record outlives the actor).

### Register rows (paste into `docs/data_register.md`, main table, 7 columns)

```
| policy_ballots | Policy Tickets | one community ballot on a policy proposal: the question a mod typed, opener, window, the frozen tally + outcome at close. Names no voter — `opened_by` and `closed_by` are the only member ids | indefinite | **DECIDED — preserve**, Art 17(3)(e) legal claims — the record of a governance decision and who opened it, the same ground `policy_tickets` is preserved on (row above). The row is about the **server**, not about a member; the frozen counts name nobody | — | `opened_by`/`closed_by` in `SUBJECT_ID_COLUMNS` ✓; guild_id on the row ✓ |
| policy_ballot_votes | Policy Tickets | how one member voted in one community ballot (`yes`/`no`/`abstain`) — attributed, moderator-readable on the dashboard after close, never rendered in Discord | indefinite | **YES — purge**. An activity record with no Art 17(3) ground, the same call as `policy_votes` (row above). Safe because the decision it fed is preserved in aggregate on `policy_ballots`: erasing a vote from a **closed** ballot cannot move a result that was frozen at close. Erasing from an **open** ballot removes it from the live tally, which is correct — an erasure is an out-of-band operator act, and the member is no longer a participant | — | `user_id` in `SUBJECT_ID_COLUMNS` ✓; guild_id denormalized so no parent join is needed (unlike `policy_votes`, privacy_service.py:665) |
```

`purge_user_data` gains one plain guild-scoped `_delete` for
`policy_ballot_votes` — no parent-join block, no `THIRD_PARTY_TABLES` entry (a
ballot vote discloses nothing about anyone else).

Also required in the same commit (CLAUDE.md): a line in
`static/manual.html` §Your Data & Privacy — "how you voted in a community
ballot" — and the member-facing sentence from §5 wherever the ballot is
documented.

---

## 7. Lifecycle, arithmetic, and every edge I could find

### Opening

A mod or admin runs `/policy ballot` **inside a policy proposal channel** — the
same `get_policy_ticket_by_channel` lookup `/policy vote` uses
(`jail_cog.py:1078-1081`, `moderation.py:961`). A modal (shaped like
`_PolicyVoteModal`, `jail_cog.py:123`) takes the exact ballot question. The bot
posts one message into `policy_ballot_channel_id`.

Requiring it to be run from inside the private channel is not ceremony: it is what
guarantees deliberation happened first, and it keeps the string that crosses the
boundary an explicit act.

Refuse, with the reason named, when: no ballot roles configured; no ballot channel
configured; **zero members hold a ballot role**; a ballot is already open for this
policy. All four get tests.

Freeze at open: `eligible_at_open`, `quorum_votes`, `pass_pct`. A dial changed
mid-ballot must not move the goalposts of a vote already in progress.

### Closing

* **Automatically** at `closes_at`, by extending the existing 60-second sweep
  (`policy_vote_timeout_loop`, `jail_commands.py:2002`) with a ballot pass. It
  already iterates every guild for exactly this reason
  (`jail_commands.py:1991-1998`); a second loop would be duplication.
* **Early**, by a fourth mod-gated **Close Ballot** button on the ballot message.
  A button beats a subcommand (CLAUDE.md), and the press is checked with `_is_mod`
  at press time — the message is in a public channel, so the button is visible to
  everyone and must refuse non-mods rather than rely on visibility.
* **Cancelled** if the policy ticket is closed by `/policy close` while a ballot is
  open: outcome `cancelled`, counts frozen anyway, message edited to say so.

On close: write the frozen counts and outcome; edit the ballot message in place to
show the breakdown with buttons removed; post the result back into the private mod
channel **if it still exists**; write an audit row (`ballot_closed`) via
`write_audit`, the same way `policy_passed` / `policy_vote_failed` are recorded
(`jail_commands.py:658-664`).

The ballot message **stays**. It is the public record, and it is in a channel
nothing in this subsystem deletes.

### Surviving the private channel's deletion

`finalize_policy_vote` deletes the deliberation channel (`jail_commands.py:758`).
The ballot lives in `policy_ballots` plus a message in a different channel, so it
survives by construction. `policy_id` keeps the link, and `policy_tickets` is a
preserved table (`docs/data_register.md:73`), so the link never dangles.

An open ballot whose parent ticket was resolved **continues** — it is the
community's vote, not the channel's — and announces into the ballot channel and the
audit channel. (If Billy would rather it be cancelled with the ticket, that is one
branch; see Open Questions.)

### Arithmetic

* **Pass** ⇔ `votes_cast >= quorum_votes` **and** `yes * 100 > pass_pct * (yes + no)`.
* **Ties fail.** Strictly greater, not `>=`: at `pass_pct = 50`, 20–20 is
  `2000 > 2000` = false. Integer arithmetic on both sides, no float rounding, no
  percentage recomputed for display and then re-derived — one function, one
  comparison, in `*_logic.py`, with a parametrised test row per boundary.
* **Abstain counts toward quorum, toward neither side of the threshold.** That is
  what abstaining means and it must be stated in the member-facing copy, because
  the alternative reading (abstain = no) is a common assumption.
* **Below quorum** ⇒ `failed_no_quorum`, reusing the existing vocabulary shape
  (`rejected_no_quorum`, `jail/logic.py:219`).
* **Quorum is an absolute vote count, not a percentage.** A percentage of a role's
  holder count is a percentage of a number dominated by members who have not opened
  Discord in four months; it silently makes every ballot fail. Default `0` (off) so
  the feature cannot ship pre-broken.

### Edge cases

| Case | Behaviour | Why |
|---|---|---|
| **Member leaves mid-ballot** | Their cast vote stands; the denominator does not move | `eligible_at_open` is frozen. Recounting live would let one departure retroactively change a threshold |
| **Member loses the eligible role mid-ballot** | Cast vote stands; they cannot cast or change one afterwards | Eligibility is a press-time check (§4). Symmetric with the leave case and explainable in one sentence |
| **Member *gains* the role mid-ballot** | May vote; turnout can exceed the frozen denominator | Turnout is a good. Clamp the *displayed* percentage at 100% and never let `votes_cast > eligible_at_open` break the quorum comparison |
| **Zero eligible members** | Open refuses | A ballot nobody can vote in resolves `failed_no_quorum` and looks like the community rejected something it never saw |
| **Double voting** | `PRIMARY KEY (ballot_id, user_id)` + upsert, exactly as `cast_policy_vote` (`moderation.py:998`) | Alt accounts are not solvable at this layer. Say so; do not imply otherwise |
| **Ballot message deleted** | Close still runs from the DB; the edit is attempted and its failure swallowed | The existing finalizer already tolerates a missing channel (`channel is not None`, `jail_commands.py:675`) |
| **Ballot channel is age-gated** | Allowed, but the dial's hint warns that only age-verified members will see it | Not an NSFW question — a ballot has no content to gate — but a silent turnout trap |
| **Bot restart mid-ballot** | Buttons keep working | `DynamicItem` + `add_dynamic_items` in `cog_load` (`jail_cog.py:221-223`); custom-id prefix `policy_ballot:` so it cannot collide with `policy_vote:` |
| **Two ballots on one policy** | Refused at open | Ambiguous outcome; a mod who wants a re-vote closes the first |

### The no-contact list — the justification, not a shrug

**It is not consulted, and that is a decision.**

`docs/no_contact_spec.md` governs surfaces that put two members *in contact*. A
community ballot has no contact edge anywhere in it: it is a one-to-many
broadcast; votes are never attributed in the room; there is no pairing, no
directed asker→answerer relationship, no DM, no reply, no member named to another
member at any point. Compare the hardest case the spec had to solve — Risky Rolls,
where "the whole roster of names and numbers" appears in a public embed *and* a
directed edge exists, which is why the gate had to move all the way back into the
dice (`no_contact_spec.md:206-222`). A ballot has neither half of that.

Two designs would reopen the question, and both are forbidden here:

1. **Publicly revealing voter names** (a WYR-style "Reveal Voters"). That publishes a
   roster in which every no-contact pair is named alongside the other. And unlike
   Risky Rolls there is nowhere earlier to move the gate — you cannot refuse a
   member's vote because their no-contact partner voted, and you cannot redact one
   name without the redaction itself being the tell. The only compliant answer is
   *don't publish the roster*, which §5 already recommends for unrelated reasons.
   If Billy later wants public names, the answer is that the two features cannot
   both exist.
2. **Reminder DMs to non-voters authored by the opener.** That is a contact edge
   from one member to every eligible member, and it would need the full
   `no_contact_partners_conn` treatment plus an indistinguishable refusal. Simplest
   fix: no ballot ever sends a DM.

A note in the ballot service saying *why* the list is not consulted is worth more
than the check would be — the next reader's first instinct will be that something
was forgotten.

---

## 8. Build plan

Migration numbering: `201` is **already used twice** on this branch
(`201_newcomer_funnel_indexes.sql`, `201_todo_auto_complete.sql`). Take the next
free number at build time; do not assume `202`.

### Stage 0 — harden what exists (no member-facing change, no migration, no dials)

1. `_handle_policy_vote` resolves the channel from `policy["channel_id"]` instead of
   `interaction.channel` (`jail_commands.py:872`), matching `_resolve_expired_policy`
   (`jail_commands.py:2062`). This is the channel-deletion hazard of §1.5a.
2. The three inline roster rebuilds (`jail_cog.py:166`, `jail_commands.py:833`,
   `jail_commands.py:2029`) call `jail/logic.py:138 eligible_voters()`, which already
   exists and is already tested.

**Tests:** a regression test (write it first, watch it fail) that the vote-button
finalizer targets the policy's own channel when the interaction arrives from a
different channel; a test that all three roster sites return identical sets for the
same member fixture. Ships alone, mergeable on its own merits.

### Stage 1 — schema, service, privacy (one migration)

`policy_ballots` + `policy_ballot_votes` + indexes; `policy_ballot_service.py`
(open / cast / close / recount / outcome); `ballot_eligible()` in `jail/logic.py`;
`purge_user_data` delete; both `docs/data_register.md` rows; the
`docs/dungeon_keeper_jail_ticket_spec.md` update.

> CLAUDE.md **hard-fails** a new `*_service.py` with no mapped test file, so
> `tests/test_policy_ballot_service.py` lands in this commit.

**Tests:** open refuses on each of the four refusal conditions; upsert replaces
rather than duplicates; every arithmetic boundary as `pytest.param` rows (tie at
50%, one over, one under, quorum exactly met, quorum one short, all-abstain,
zero votes); `eligible_at_open` frozen against a mid-ballot roster change; close is
idempotent and status-guarded the way `resolve_policy_vote` is
(`moderation.py:1032-1037`); **`purge_user_data` clears a member's ballot votes and
leaves a closed ballot's frozen counts untouched**; the register-coverage gate passes.

### Stage 2 + 3 — dials and Discord surface, **in one commit**

They cannot be split: shipping the dials first would put five settings on the
dashboard that nothing reads, and CLAUDE.md forbids a preference that is not
enforced.

* Config: `_policy_section` (`routes/config.py:617`) and `PolicyConfigUpdate`
  (`routes/config.py:4375`) gain the five keys; roles via
  `replace_config_id_bucket` / `_id_str_list`; the Settings half of
  `panels/policy-tickets-settings.js` gains a `mountRoleMultiPicker` slot and four
  fields, still inside `guardForm` + `lockUnlessAdmin`.
* Discord: `/policy ballot` + modal; ballot embed builder in `jail/embeds.py` that
  **cannot emit a mention**; `policy_ballot:{yes,no,abstain,close}` DynamicItems
  registered in `cog_load`; the sweep pass for expired ballots.
* Docs: `manual.html` §Policy Voting + §Your Data & Privacy; the spec's command
  table and config table.

**Tests:** `tests/web/test_config_routes.py` rows for each key (admin-gated,
validation bounds, round-trip) — the authz sweep picks the route up for free;
component tests for the four custom_ids and templates, mirroring
`tests/components/test_jail_views.py`; an embed test asserting the ballot builder's
output contains no `<@` substring for any input, and that it is never handed the
proposal description or a transcript reference (the §2 seam, made a test); a cog
test that a press by a member without a ballot role is refused; the browser layout
+ console checks fire automatically for the changed panel.

### Stage 4 — moderator read surface

`GET /api/moderation/policy-ballots` (`require_perms({"moderator"})`) listing
ballots with frozen counts, and per-member votes **only for closed ballots**. Rendered
on the frozen `mod-policy-tickets` page beneath the proposal queue.

**Tests:** unauthenticated and non-moderator both rejected; an open ballot returns
counts but no `user_id`s; a closed one returns both; the snowflake-precision sweep
covers the ids automatically (`docs/web_testing.md`).

### What "done" looks like

A single QA card assembled from the `Testing:` sections by
`scripts/post_testing_docs.py` at ship: pick roles and a channel on the dashboard,
open a proposal, open a ballot from it, vote as an eligible member, be refused as an
ineligible one, close it early, see the frozen result in the channel and the
per-member breakdown on the dashboard, delete the proposal channel and watch the
ballot still be there.

---

## 9. What could go wrong

* **The channel-deletion bug (§1.5a) ships unfixed** and the first early-close press
  deletes a public channel. Stage 0 exists solely to make this impossible; do not
  reorder it.
* **The dials ship before the enforcement.** Five settings that read as promises and
  do nothing. Stages 2 and 3 are one commit for this reason.
* **"Anonymous" gets into the copy.** Tallies-only is not anonymity; if the manual
  says "anonymous" while `policy_ballot_votes.user_id` exists, DK has shipped an
  unenforced promise about the most sensitive thing this feature touches. Copy
  review is part of stage 3, not an afterthought.
* **Someone reuses `policy_votes`** "to save a table" and a member's vote gets
  rendered into a channel by the existing named-tally embed. §5's reason 1.
* **A percentage quorum sneaks in** and every ballot fails on a server where a role
  has 200 holders and 30 actives.
* **A ping is attached to the ballot post.** The existing vote flow pings mod roles
  (`jail_cog.py:1040-1048`); copying that pattern to a member role is a mass ping.
  `allowed_mentions=AllowedMentions.none()` on every ballot send, as
  `jail_cog.py:190` already does once.
* **Turnout is embarrassing.** A first ballot that draws nine votes out of a hundred
  and fails quorum reads as a rejection. Ship with `quorum_votes = 0` and let Billy
  raise it once there is a baseline.
* **Governance drift.** Once members vote, "what happens when a member ballot passes
  and the mod team disagrees" is a real question with no technical answer. It is
  Billy's first Open Question below for a reason.

---

## Open questions for Billy

1. **Anonymity model.** Tallies-only with moderator-visible names after close
   (recommended, §5), or genuinely secret with the counter-table construction —
   which costs you the ability to change a vote and all per-member brigading
   forensics? There is no honest middle.
2. **Binding or advisory.** Does a passed community ballot *adopt* the policy —
   i.e. write a row into `policies`, the table `/policy list` reads
   (`jail_cog.py:1216`, `moderation.py:263`) — or does it come back to the mod
   channel as a recommendation the mods still vote on? If binding, does the mod
   unanimity vote still happen at all, and in which order?
3. **Who may open a member ballot.** Any mod (matching `/policy vote`,
   `jail_cog.py:1072`), or admins only (matching `/policy open`)? Opening one is a
   more public act than starting a mod vote.
4. **Quorum and threshold defaults.** Recommendation: `quorum_votes = 0` and
   `pass_pct = 50` (simple majority, ties fail). What do you actually want for the
   first real ballot?
5. **Which roles vote.** Veteran only? Veteran + Denizen? Note that `veteran` exists
   as a grant-role slug (`core/db_utils.py:242`) but the ballot dial is
   deliberately independent of the grant registry.
6. **One ballot channel or the opener's choice.** A fixed configured channel is one
   dial and one place members learn to look; letting the opener pick at open time is
   more flexible and harder to find.
7. **An open ballot whose proposal is closed.** Does it keep running to its own
   deadline (recommended), or get cancelled with the ticket?
8. **Where results are announced** besides the ballot channel — the audit channel
   only, or a general announcement channel too?
9. **Can members *propose*, or only vote?** This plan gives members the vote and
   leaves `/policy open` admin-only. "Members can raise a proposal" is a separate,
   larger feature (an intake queue with mod triage) and is out of scope here unless
   you want it.

---

## Decisions — Billy, 2026-09-03

These supersede the "Open questions" section below wherever they overlap. The
plan above was written before them; where the two disagree, this section wins.

| Question | Decision |
|---|---|
| Venue | A **thread in the channel the ballot was launched in**. Not the private mod channel — the mod channel is not involved in a community ballot at all. |
| Recorded as | A **policy ticket**, carrying its own tally. |
| On passing | **Result recorded only.** Nothing is written to `policies` automatically; a mod turns a passed ballot into a policy later if they choose. |
| Anonymity | **Fully public** — the running tally names every Yes / No / Abstain, the same way the mod vote does today. |
| Electorate | **Anyone who can see the thread.** No role gate, no eligibility dial. A veteran-only vote is a ballot launched in a veteran-only channel. |
| Who may open | **Admins only**, matching `/policy open`. |
| Pass rule | **Simple majority**, abstentions don't count, **ties fail**. No minimum turnout — a ballot always resolves. |

### What these decisions remove from the plan

- **No `config_ids` role bucket and no `mountRoleMultiPicker`.** §3's eligibility
  design is dropped entirely: the channel's own permissions are the electorate.
  The frozen `mod-policy-tickets` page gains no voting dial.
- **No tallies-only or secret-ballot construction.** §5's counter-table design and
  its salted-hash critique are moot. Votes are attributed and rendered with names,
  so a ballot vote row can reuse the shape `policy_votes` already has.
- **No `policies` write path from a ballot**, and therefore no binding/veto
  states, no cooling-off timer, and no ordering question against the mod vote.

### Consequences that must be carried into the build

- **No-contact cannot be honoured in a public tally, and that is accepted.** Two
  members who have blocked each other will be named in the same list. This was put
  to Billy explicitly as a cost of the fully-public option and taken deliberately.
  It is defensible on the same ground as any public channel message — a ballot is
  a one-to-many broadcast, not a pairing, and both members can already post in the
  channel. **Do not** add a DM, a mention, or any per-pair surface to a ballot;
  those would create a contact edge and reopen the gate for real.
- **The finalizer hazard in §2 becomes load-bearing.** `_handle_policy_vote` hands
  the finalizer `interaction.channel` while the sweeper hands it
  `policy["channel_id"]`. Today those are the same channel so nothing breaks; a
  ballot living in a *different* channel makes the finalizer delete the channel the
  vote was posted in. This must be fixed before a ballot can exist, and it needs a
  test that would fail on today's code.
- **A ballot must outlive the mod ticket.** Resolution deletes the private channel;
  a community ballot's thread and its recorded result are in a different channel
  and must be unaffected.
- `/policy`'s `default_permissions` are inert (§2) — every member already sees
  `/policy open`. Since ballot-opening is admins-only, the runtime `_is_admin`
  check is what enforces it, and that must be true of the ballot command too.

### Still open, deliberately deferred

- **A DK config export/import** ("duplicable template") was not decided and is out
  of scope here — see the role-autocreate round 2 doc, which concludes it is a
  larger piece of work than either round.
- **Ballot duration** was not asked: reuse the existing admin-gated voting-deadline
  dial on `mod-policy-tickets` rather than adding a second one.
