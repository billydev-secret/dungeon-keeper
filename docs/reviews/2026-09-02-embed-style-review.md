# Embed style review — all 364 call sites (2026-09-02)

Billy: *"review all of the embeds for style."*

Scope: every `discord.Embed(` construction in the repo — **364 across 107
files** — judged against `docs/embed_style_guide.md`. Style only. A parallel
session (`games-deep-review`) owned gameplay, UX and backend for the games at
the same time; per Billy's call this review took **all 364 for style**, so a
games embed got a casing/❌/footer verdict here and a "does it work" verdict
there.

Two of the guide's rules were already machine-enforced and are excluded
throughout: **accent colour** (`test_embed_accent_contract.py`, 94 rows, plus a
repo-wide direct-call guard) and **member names / `name_fn`**. Findings about
either are out of scope by construction, not by oversight.

---

## Headline

The guide was in better shape than it claimed, and worse than it claimed, in
different places.

**Better:** two of its own "Known drift" entries measured **zero** — the
`colour=` kwarg and `█░` progress bars. A third,
"~19 pasted no-permission variants → shared constant", was also already done:
there is one shared `NO_PERMISSION` in `services/replies.py` (29 uses) plus six
feature-specific `MANAGE_DENIED_MSG` constants that each name their own action
("…to review quest claims", "…to award or cancel bounties"). That is the form
the guide *prefers* under "say how to fix it" — not drift awaiting convergence.

**Worse:** the rules nothing enforced had drifted badly.

| Defect | Count | State |
|---|---|---|
| Denial replies missing the `❌ ` prefix (ruling 2026-07-21) | **77** | fixed + gated |
| Multi-section cards with no `apply_section_spacing` | **43** | fixed + gated |
| Footers separating with `·` instead of `•` | 9 | fixed + gated |
| ASCII `...` in member-facing copy | 30 | fixed |
| Select placeholders saying "Select" / missing `…` | 7 | fixed + gated |
| Starboard footer showing a raw `<:emoji:id>` on refresh | 1 | fixed + test |
| Pagination footer reading "Page 2 of 4 · " | 1 | fixed |
| "guild" in member-facing copy | 4 | fixed |
| ALL-CAPS titles (2 built at runtime via `.upper()`) | 3 | fixed + gated |
| Denials hidden behind a local send wrapper | 5 | fixed + gated |
| Footers built in the string layer, joined on `·` | 3 | fixed + gated |

The 77 was the surprise. The guide's Known drift said "**non-game** error
strings missing the `❌ ` prefix" — in fact the misses were overwhelmingly *in*
the games, which is where the ruling originated.

---

## The durable output

`tests/test_embed_style_contract.py` (new) — five repo-wide sweeps, each
citing its guide section and each carrying a "guards the guard" meta-test in
the style of `test_branding.py`:

1. A denial reply opens with `❌ `.
2. A footer separates with `•`, not `·`.
3. A select placeholder says "Pick", not "Select".
4. The kwarg is `color=`, not `colour=`.
5. A pure-stacked multi-section card calls `apply_section_spacing`.

It found three builders on its first run that the ad-hoc scan had missed
(`games_rushmore/embeds.py` was skipped by a file-level "already imports the
helper" check; the sweep is per-function and caught them).

**Both sweeps were then widened after Lane B caught them overclaiming.** The
first version read only literals at `send`-shaped calls and inside
`set_footer`, so six denials behind `role_menus`' local `_reply()` and three
footers assembled in string-layer builders were invisible. They now follow one
level of indirection — a `_SEND_WRAPPERS` set, and any function named `*footer*`
— and the widened footer sweep found `economy/game_rewards` on its first run.

**I also had to retract a claim.** I wrote "games ALL-CAPS titles: verified
zero" into the guide on the strength of a grep for `title="[A-Z]…`. Three had
survived it: one leading with an emoji, and two built at render time with
`.upper()` — including the casino fanfare shouting a guild's own configured
name back at it. The lesson is the one the contract test already embodies and
my measurement did not: a sweep is only trustworthy once you have watched it
catch a known positive.

**Deliberately outside sweep 5:** cards mixing `inline=True` triples. The
helper appends a blank line to every field but the last, which on an inline
triple makes the row taller — a layout judgement, not a mechanical one. See
the open decision below.

---

## Open decisions for Billy

### 1. Section spacing on cards with inline triples — 63 builders, 37 files

`apply_section_spacing` was applied to the **49 pure-stacked** builders (fields
all `inline=False`) — the shape its own docstring describes, and what 8 of the
9 pre-existing adopters look like. The other **63 builders mix `inline=True`
triples**, where the spacer adds height inside each inline box.

There *is* precedent for doing it anyway: `survivor/embeds.py::build_status_embed`
has three inline fields plus one stacked, and it calls the helper — all three
inline boxes get the spacer, growing equally, so the row is taller but not
ragged.

**Options:** (a) leave as-is — the current state; (b) apply to all 63, taller
inline rows repo-wide; (c) teach the helper to skip `inline=True` fields, which
changes `build_status_embed`'s current rendering.

### 2. Two casino pool titles — `·` plus sentence case

```
src/bot_modules/cogs/casino/embeds.py:1382  "📈 Pools — today's market · {spec.label}"
src/bot_modules/cogs/casino/embeds.py:1446  "📈 Pools — the day is in · {spec.label}"
```

Both violate two rules at once: `·` where titles take `—`, and sentence case
where titles take Title Case. Converting the `·` to `—` alone gives a
double-em-dash title, so this wants a copy rewrite rather than a swap. Left
untouched and recorded in the guide's Known drift.

### 3. Double-spaced `•` in field values (`games_ama`)

`"Questions: **12**  •  Answered: **5**  •  Passed: **2**"`. The guide's
separator rule covers **titles and footers**; this is body text, where no rule
has been written. Footers are clean. Decide whether the rule extends to values
before anyone sweeps these.

### 4. "guild" in dashboard route errors

~25 strings in `web_server/routes/*` ("Discord guild not available", "Bot is
not connected to this guild"). The guide says dashboards "should also prefer
'server' in new copy" — these are existing copy, and outside an embed review's
remit. Member-facing Discord copy is now clean.

### 5. Empty states that read like denials — left unprefixed on purpose

```
games_ama_cog.py     "No one is in the hot seat."
games_config_cog.py  "There's no active game in this channel."
games_price_cog.py   "Nobody submitted a price this round. Moving on…"
watch_cog.py         "You are not watching any users."
risky_roll/views.py  "There is no pending winner question for this round."
```

These match a refusal shape but are empty states or narration, which the guide
wants as a plain sentence with **no** emoji. The contract test's `_NOT_A_REFUSAL`
pattern excludes them explicitly so a future sweep can't "fix" them. Two of
them do use uncontracted forms ("You are not", "There is no") against the
guide's contractions rule — a copy call, not a structural one.

---

## Guide changes made

`docs/embed_style_guide.md` § Known drift was rewritten: the closed items moved
to a "verified zero — don't go looking" list with the reason each is closed, the
still-true items restated, and a new "Now gated, not honour-system" subsection
pointing at the contract test. Billy to give `documentation-review` a heads-up,
since that session owns `docs/`.

---

## Verification

- `ruff` clean; `pyright` clean at gate scope (`include = ["src"]`).
- The starboard fix ships with a test proven to fail before it
  (`<:sparkle:…> 4` vs `⭐ 4`) and pass after.
- No test asserted any of the 77 changed denial strings.
- `page_note`'s two assertions in `test_economy_shop.py` updated with it.
- `manual.html` quotes none of the changed copy, so no user-doc update was due.

---

# Lane B — the judgement read

Eight readers, one per feature area, each reading the builders in its area
against the guide's non-mechanical rules (casing, card anatomy, footer job,
timestamps, empty states, currency vocabulary, voice, link-the-message). Every
finding had to cite a guide section and quote real source text.

**Coverage: 188 files, 374 embeds, 212 findings.** Severity: 14 high, 144
medium, 54 low; 210 of 212 are member-visible.

> **Verification status.** Findings were then handed to adversarial verifiers
> prompted to refute by default. A usage limit killed 187 of them mid-run, so
> only part of the set carries a verdict. **Treat an unverified row as a lead,
> not a fact** — the verifiers that did run rejected findings for
> misquoting source, stretching a rule, and flagging already-compliant code,
> so the rejection rate is not zero. Every claim quoted in *this* section I
> checked myself against the source.

The verifiers that ran were worth their cost. One accepted a finding but
rewrote its fix: the proposed change to `_post_audit` would have opened **every**
jail, ticket and policy audit embed to role pings, because that helper is
shared across ~15 call sites. Its corrected version adds opt-in keyword params
instead. That is the kind of error a findings list normally ships with.

## The recurring classes, ranked

Counts are findings, not sites — several findings name a whole class.

| # | Class | Findings | Where it concentrates |
|---|---|---|---|
| 1 | **Title Case** on titles, field names, button/modal labels | 54 | services, economy, member-features, long-tail |
| 2 | **Card anatomy** — title vs `set_author`, thumbnail semantics | 29 | everywhere |
| 3 | **`❌` on denials** the mechanical sweep couldn't see | 25 | duels/chicken/hot-potato, role_grant |
| 4 | **Game signature footer** missing on terminal cards | 15 | games-a, games-b, casino, duels |
| 5 | **`embed.timestamp`** absent on record/audit cards | 13 | mod-admin (17 of ~20 cards), long-tail |
| 6 | **Currency vocabulary** — five implementations, hard-coded "coins" | 12 | economy, games-core |
| 7 | **`escape_markdown`** missing on member text | 11 | **5 of the 14 high-severity** |

### 1. Title Case (54) — the biggest class, and the most gateable

Named independently by five of eight areas as their top drift. It is not
per-file: the split runs *through single cards*. The shop renders "🛡️ Shield
Held" beside "💬 Sponsor a question"; the auction card renders "🏆 Winner"
beside "🔨 Winning bid"; the guide button says "How it Works" while the embed
it opens says "How It Works".

The services reader proposed a concrete, cheap gate: AST-collect every
`Embed(title=…)`, `add_field(name=…)`, `Button(label=…)`, `Modal(title=…)` and
`SelectOption(label=…)` string literal, strip a leading emoji, and flag one
whose second-or-later alphabetic word is lowercase and isn't a stopword. That
single rule covers all 54 — and would have caught the guide's own worked
example drifting in `role_grant_audit_service.py:513`.

### 5. `embed.timestamp` — one helper fixes 17 cards

Of ~20 mod-log record embeds, exactly three set it. Everything posted through
`_post_audit` omits it: Member Jailed, Ticket Opened/Closed/Deleted, all three
Policy outcomes, Member Released, the three jail-state alerts, the warning
builders. The guide names "jail actions" explicitly as record cards.
Cheapest fix is one line in `_post_audit`
(`embed.timestamp = embed.timestamp or datetime.now(timezone.utc)`), not 17 edits.

### 7. `escape_markdown` — the highest-severity class (verified by hand)

Member-typed text reaches embeds unescaped, so `**bold**` or backticks in a
member's input reformat someone else's card. Two findings I confirmed directly:

- **`games_hottakes/embeds.py:131,136`** — `build_vote_embed` in the *same file*
  escapes the identical string at line 78; the recap does not.
  `games_fantasies/embeds.py:118,125,132` is the same shape.
- **The whole mod-admin area** — `grep escape_markdown` across all six jail
  files returns **zero**, while ticket descriptions and jail/warning reasons
  (free text, member- and mod-typed) go straight into audit embeds.

Also `games/utils/audit.py:103`, `duels/base_game.py:733` (stakes text),
`rules_watch/alert.py:64` (a quoted message preview).

## Individual defects worth fixing regardless of class

- **`services/music_now_playing.py:75`** — `title=f"[{title}]({uri})"`. Embed
  titles render as **plain text**; a masked link is not a link there, so every
  music card shows the literal `[Song Name](https://…)` to the room. The slot
  that does this job is `embed.url`.
- **`jail/embeds.py:432`** — the "Warning Threshold Reached" alert builds admin
  role pings into the embed *description*, and `_post_audit` sends with
  `AllowedMentions.none()`. The pings are dead twice over: a mention in an
  embed never notifies, and the send allow-lists nothing. **No admin is
  being alerted.**
- **`jail/apply.py:485`** — the jail DM says "check the jail channel" while
  holding `jail_channel`, giving the member no way back. The guide calls a DM
  with no pointer "the real failure".
- **`games_ttl/embeds.py:106`** — a `▰▱` bar inside a code span placed in a
  *field name*. Field names render no markdown, so members see literal
  backticks around a bar that isn't monospaced. Every sibling game puts the bar
  in the field value.

## Gaps in the guide itself (raised by every area)

These are proposals for you, not findings — the readers were told to keep them
separate.

1. **Titles and field names render as plain text, and the guide never says so.**
   § Footers spells it out for footers only. That silence is exactly where the
   music masked-link and the TTL bar-in-a-field-name both landed. One clause in
   § Card anatomy would close it.
2. **"A footer does one job" cannot be applied as written** — three areas
   independently refused to file footer findings because the guide's *own*
   sanctioned examples stack ("Host: {host} • Need {n}+ players to start.", and
   the game signature's "• extra"). Either say the `" • "`-joined clause is the
   sanctioned way to carry a second thought, or replace the examples.
3. **No slot for a consent/retention notice.** `build_ticket_open_embed` footers
   "When this ticket is closed, the conversation is archived…" — deliberately,
   with a code comment saying so. It is none of the five listed jobs, so a
   literal sweep would delete a data-handling notice. Worth a sixth job.
4. **The game-signature rule reads as a description, not a requirement.** Most
   casino, quickdraw, chicken, musical-chairs, hot-potato and duels cards carry
   no footer at all and technically break nothing.
5. **No plural rule outside currency** — "1 guesses left", "1 teams available",
   "result(s)" have no section to cite. One line under Voice & terminology.
6. **No rule for non-currency numbers** — `fmt_xp()` exists but nothing says XP
   figures must use it, so `xp_service.py:260` prints "31234.57".
7. **§ Builder conventions names `economy_cog.py` as building "eight embeds
   inline".** It now builds four; the wallet, shop, quest board, guide,
   leaderboard and register have all moved to `build_*` modules. The rule is
   still right, the exemplar is stale — and a reader who checks it will
   discount the rule.
8. **`set_author` example is stale** — the guide says "music's requester", but
   the card puts the *artist* there and the requester in a field, which reads
   better and matches the artwork thumbnail.

## What I'd do next, in order

1. **`escape_markdown`** — 11 sites, 5 of them high. A real defect class, not a
   preference, and each fix is one call.
2. **`embed.timestamp` in `_post_audit`** — one line, 17 cards.
3. **The four individual defects above** — the dead admin ping and the music
   masked link are both "this feature silently does not work".
4. **A Title Case contract row** — biggest class, and the proposed rule is
   precise enough to implement without a judgement call per site.

Items 1–3 are bug-shaped and I'd take them now; item 4 changes ~54 strings and
wants your say-so first. None were in the mechanical scope you approved, so
none have been applied.
