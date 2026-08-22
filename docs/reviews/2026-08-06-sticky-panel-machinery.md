# Sticky / persistent panel machinery — cross-cutting review (2026-08-06)

> **All ten findings below were fixed the same day**, in the commit that follows
> this doc. What shipped for each is recorded under its own heading, and the
> mechanism-level summary is in
> `docs/plans/sticky-panel-extraction.md` → "What the 2026-08-06 cross-cutting
> review changed". The findings text is left as written so the evidence and the
> reasoning survive the fix; F8 is the one whose fix ships **off by default**,
> for the reason given there.
>
> A follow-up code review then found the **F1 fix was in the wrong place** and
> that F2's fix shipped a deploy bug — both corrected, and both recorded under
> their own headings rather than quietly rewritten. One item is left open, under
> F4.

**Lane:** `src/bot_modules/core/sticky.py` reviewed as a *mechanism*, plus its
nine callers. The 2026-08 staged review went feature-by-feature, so each bundle
saw only its own caller; this pass looks at what the callers do to each other.

**Verdict up front: the shared module is sound.** The debounce, the per-guild
placement lock, the shielded placement and the at-the-bottom guard all do what
their docstrings claim, and prod confirms it — see [What is healthy](#what-is-healthy)
for the evidence. Every finding below is either a *config-reachable* interaction
between two callers, or a caller that has not been migrated onto the shared
module.

The one High is a repost storm that is **one dashboard button-press away in a
live guild**, reproduced at the logic layer.

| # | Severity | Finding |
|---|---|---|
| [F1](#f1) | **High** | Two `restick_on_bot` panels in one channel repost forever; live guild is one click away |
| [F2](#f2) | Medium | `guess_cog`'s unmigrated prompt still carries delete-before-post **and** the unrecorded-placement bug |
| [F3](#f3) | Medium | `guess_cog.on_message` does an uncached DB read for every message in every guild |
| [F4](#f4) | Medium | A channel the bot can't post in is retried forever, with no backoff and a log line missing the guild and the error |
| [F5](#f5) | Low | `take_retries()` is drained by 1 of 9 callers, so pen pals' failed edits are never retried |
| [F6](#f6) | Low | The economy cog is the only migrated cog that never publishes `set_known_guilds` |
| [F7](#f7) | Low | The old-panel delete is swallowed with a bare `pass` and no log at all |
| [F8](#f8) | Info | No starvation ceiling: a channel that never falls quiet for 6 s never re-sticks |
| [F9](#f9) | Info | The leaderboard's hourly repaint is a parallel hand-rolled `refresh()` |
| [F10](#f10) | Info | No sticky-id cleanup on channel delete or guild removal |

Already recorded elsewhere, not re-reviewed: the 2026-07-26 casino storm and its
shield fix, the auction card's lifecycle-in-the-callbacks shape, and the
`whisper` / `confessions` launchers being unmigrated and unthrottled — all in
`docs/plans/sticky-panel-extraction.md`. The plan doc names `guess` only as
"missing the per-guild lock"; F2 and F3 are what that understates.

---

<a name="f1"></a>
## F1 — Two `restick_on_bot` panels in one channel repost forever · **High**

### What happens

`on_message` filters bot authors unless `restick_on_bot` is set
(`core/sticky.py:434`). Two panels are opted in:

- the casino hub — `cogs/casino/cog.py:414-428`
- the economy bounty hub — `cogs/economy_cog.py:1290-1296`

Each protects itself against chasing **its own** repost, three ways (cached-id
skip in `should_restick`, the `last_message_id` pre-check at
`core/sticky.py:485`, and `only_if_buried` re-deciding under the lock). None of
those help against *another* sticky panel's repost, which is an ordinary bot
message in the panel's own channel and indistinguishable from a round result.

So panel A reposts → panel B is now buried → B reposts → A is now buried → A
reposts, forever, with **zero human activity**. Neither panel is ever at the
bottom when its own debounce fires, so the guard that stopped the 2026-07-26
storm never engages.

### Evidence — reproduced at the logic layer

Repro (scratchpad, not committed) drives real `StickyPanel` instances against a
`MagicMock(spec=discord.TextChannel)` that advances `last_message_id` on send
and dispatches `MESSAGE_CREATE` as a task *concurrently with* the send
returning — the ordering `core/sticky.py:318-320` says actually happens in prod.
One human message, then silence for 40 debounce periods:

| Configuration | Sends in 40 debounce periods |
|---|---|
| Two `restick_on_bot` panels | **26–27** (never converges) |
| One `restick_on_bot` panel (control) | 0 |
| One `restick_on_bot` + one default panel | 2, then settles |

I verified `discord.py` really does maintain `last_message_id` from the gateway
(`discord/state.py:691`, `parse_message_create`, discord.py 2.7.1), so the
model's fidelity on the load-bearing attribute is not assumed.

### Rate-limit model

0.65 sends per debounce period × `DEFAULT_DELAY = 6.0` → **≈6.5 sends + 6.5
deletes per minute in one channel, indefinitely**. That is *under* the per-channel
message-create bucket (≈5 per 5 s), so it will not 429 and will not self-limit —
it just churns forever. The failure mode is visible channel spam and ~800
wasted REST calls/hour, not an outage. For the general case: one panel costs at
most 10 sends + 10 deletes/min; N panels sharing a channel cost N× that, and the
N near-simultaneous sends are what sit closest to the bucket edge.

### This is reachable in prod today

Queried read-only against `/home/ben/discord-bots/dungeon-keeper/dungeonkeeper.db`
(`mode=ro` URI, no copy). Guild `1476525656115515484` (41,624 messages, last
2026-08-05 22:19):

```
econ_enabled              = 1
casino_panel_channel_id   = 1532304393313980446
econ_bounty_channel_id    = 1532304393313980446   <-- same channel
econ_bounty_panel_message_id   (absent)           <-- hub not posted yet
```

`bounty_enabled()` is just `bounty_channel_id > 0`
(`services/economy_bounty_service.py:49`) → true. `post_bounty_panel` only
checks that the target channel **is** the board channel
(`cogs/economy_cog.py:3801-3806`); it does not ask who else lives there. And
`routes/panels.py:161-194` checks View/Send/Embed permissions and nothing else.

So an admin pressing **Post Bounty Board panel** on that guild starts the loop.
The same guild already has `econ_guide/leaderboard/shop_channel_id` pointing at
that one channel too — four panels posted in it, plus the bounty hub configured
for it.

The guard for exactly this hazard already exists but is wired to one caller:
`_sticky_check` (`economy/auction_views.py:487-503`) **blocks** an auction from a
channel whose resident has `restick_on_bot`, using
`sticky_panel_channels()` (`services/economy_auction_service.py:339-384`). Only
`/bank auction start` calls it. Nothing applies it panel-to-panel.

### Fix

Two layers; the first is the real one.

**1. Make `core/sticky.py` never chase *any* sticky panel's repost.** Keep a
module-level set of message ids that some `StickyPanel` posted, and drop those in
`on_message`:

```python
# module level — bounded; a panel id stops mattering once it is replaced.
_PLACED: set[int] = set()          # or an LRU/deque-backed bounded set

def _remember(self, guild_id, channel_id, message_id):
    _PLACED.add(message_id)
    ...

async def on_message(self, message):
    if message.guild is None:
        return
    if message.id in _PLACED:
        return   # some sticky panel's own repost — never chase it
    ...
```

Verified in the repro: the two-panel case drops from **26 sends to 2** and
settles, while a genuine bot post (a casino round result — a message no panel
placed) still moves the hub exactly once. It needs no cross-cog coupling and no
caller changes.

Note `_remember` is best-effort against the gateway (`core/sticky.py:318-320`),
so on its own this narrows the window rather than closing it — pair it with the
`last_message_id`/`only_if_buried` guards that already exist, which is what the
repro measures. Bound the set (drop an id when `save_ids` supersedes it, or cap
it) so it can't grow across a long uptime.

**2. Refuse the collision at configuration time.** In `post_bounty_panel`, call
`sticky_panel_channels()` and apply `_sticky_check`'s rule — refuse when the
target already hosts a `restick_on_bot` resident, warn when it hosts any other
sticky panel. Better still, hoist that check into `routes/panels.py` so all eight
postable panels get it.

While there: `sticky_panel_channels()` returns a dict keyed by channel id, so
when two panels share a channel it reports **only the last one** in its `named`
tuple (the casino hub). A mod warned about a shared channel is told about one of
the two residents.

### Fixed — and the first attempt was wrong

`core/sticky.py` keeps a process-wide `was_placed` registry of ids some panel
placed. **Where it is consulted turned out to be the whole question**, and the
fix sketched above got it wrong: putting the check in `on_message` does almost
nothing, because `_note_placed` runs inside `_remember`, i.e. *after* `send()`
returns — so it races the gateway in exactly the way this document already
criticises `should_restick`'s id-skip for. Any yield between the frame being
dispatched and `_remember` running loses that race, and the awaited HTTP response
is one.

Caught by a follow-up code review, then confirmed empirically: with only the
`on_message` check, two opted-in panels still storm at **29–30 sends** across 40
debounce periods. The first regression test missed it because its fake dispatched
with `asyncio.create_task` — and since `_place_locked` has no await between
`send()` returning and `_remember`, the registry always won in the harness. It
reported 2 sends for a configuration that really does 29.

The real fix is at the **decision points**, a whole debounce later, where the
registry is reliably populated: `_delayed_restick` (and `_place_locked` under the
lock) return early when `channel.last_message_id` is a message some panel placed.
Whoever placed last holds the slot and the other yields rather than taking it
back — someone has to lose a one-slot contest, and a deterministic loser beats
the two of them alternating forever. The next genuine trigger re-opens it, so
yielding is not permanent (`test_yielding_does_not_stop_the_next_genuine_trigger`).
The `on_message` check stays as a cheap way to skip a doomed debounce, documented
as an optimisation.

The test fake now dispatches to listeners **before** `send()` returns, and the
regression was re-verified failing at 30 sends with the decision-point checks
removed and the `on_message` check left in.

`post_bounty_panel` refuses a channel another bot-chasing panel holds; not
applied to the casino's own channel setting, which belongs to its feature.
`sticky_panel_channels` merges residents instead of overwriting.

**The hoist landed 2026-08-22.** The registry moved to
`services/sticky_registry.py` and grew the six panels it never knew about —
pen pals, DM perms, Voice Control, both todo boards, the Guess Who prompt —
plus the Survivor panel, which is `restick_on_bot` and so was an invisible
*blocking* collision. `routes/panels.py` now runs the block/warn split for
every panel in `panel_registry`, which is what this section recommended;
panels that own their destination (Voice Control, Guess Who) have it looked up
from the registry, and each panel excludes itself so a refresh in place is
never refused. The three panels that post through their own routes — the DM
request panel's `/config/dms/post-panel`, the todo boards, and the Survivor
panel's repost — adopted `panel_posting.sticky_conflict` the same day, so
**every** path that places a sticky panel now runs the split. The todo boards
keep `conflicting_board` ahead of it: that refusal names what clearing the
sibling board would cost, which the generic warning does not.

Three corrections from the review of that work, same day:

* **The check is for sticky panels only.** It ran for every key in
  `panel_registry`, so the support ticket panel and the grant-audit card — both
  posted once and then left to scroll — were being refused a channel outright.
  Neither has a bottom slot to contest; `is_sticky_panel` gates it now.
* **Own-channel panels resolve their real destination.** `own_channel_id` read
  the registry's channel for the panel, i.e. where it *is*. Voice Control posts
  to `voice_master_control_channel_id` but records its location under
  `voice_master_panel_channel_id`, so after a Control Channel move the guard
  judged the old channel's residents — and on a first post, with nothing
  recorded, it skipped the check entirely.
* **Survivor keys off the season's configured channel**, not
  `announcement_channel_id`. The panel is absent from that key until it has
  been posted once, which is exactly the window where something else gets
  placed in the channel unopposed and the Wednesday repost then buries it.

### Test to land with it

`tests/test_core_sticky.py` has 37 tests and no multi-panel scenario — every one
constructs a single `StickyPanel`. Add the two-panel case from the repro
(`sends <= 4` over N debounce periods with no human activity) plus the
round-result regression guard, so the fix can't be undone.

---

<a name="f2"></a>
## F2 — `guess_cog`'s unmigrated prompt carries two prod-proven bugs · Medium

`guess` is the one Group-B site never migrated (blocked on widening
`StickyPanel` to accept `VoiceChannel`/`Thread`). Its hand-rolled re-poster still
has both failure modes the shared module exists to prevent.

**Delete-before-post.** `_repost_prompt` (`cogs/guess_cog.py:1430-1462`) fetches
and deletes the old prompt at 1446-1451, *then* sends the new one at 1452-1458.
If the send raises, it logs and returns — leaving the channel with **no prompt at
all** and `guess_prompt_message_id` still naming the deleted message. The prompt
is gone until someone reposts it by hand. This is precisely the pen-pals bug the
migration fixed ("`channel.send` unguarded after the old panel was deleted, so a
failed send permanently orphaned the panel").

It also uses `fetch_message` + `delete` (two REST calls where
`get_partial_message().delete()` is one) and catches only
`(NotFound, Forbidden)` — a 5xx or a rate-limit on the delete escapes to
`_delayed_repost_prompt`'s broad `except Exception` and aborts the repost, same
end state.

**Unrecorded placement.** `_delayed_repost_prompt`
(`cogs/guess_cog.py:1756-1770`) awaits `_repost_prompt` directly — no
`asyncio.shield`. `on_message` cancel-and-rearms on every message
(`cogs/guess_cog.py:1749-1755`), so the cancelled task can be the one parked in
`channel.send()`. Discord has accepted the message by then, but
`_do_set_config` never runs, so the new prompt's id is never stored: an orphaned
prompt with live buttons, and the stored id still on the already-deleted old
one. That is the exact mechanism of the 2026-07-26 casino storm, fixed in
`6fb53e73` for every migrated caller.

It cannot *storm* here — the trigger is human messages only
(`if message.author.bot: return`) — so the consequence is orphan accumulation,
not a flood. The window is small (a message must land inside the send's HTTP
round trip) but `PROMPT_REPOST_DELAY_SEC = 2.0` (`cogs/guess_cog.py:1161`) is
a third of core's debounce, so the channel re-posts far more often than any
migrated panel and hits the window more.

**Verified by reading the code.** I could **not** confirm orphan accumulation
from prod: deleted messages remain in the `messages` table, so its 1,328 bot
messages in the main guild's guess channel (`1502760619269427292`) cannot be
split into "current prompt" vs "orphan". Confirming this needs a look at the live
channel.

**Fix.** Either finish the migration — widen `StickyPanel._channel` /
`_at_bottom` to accept `TextChannel | VoiceChannel | Thread` and hand `guess` a
`StickyPanel`, which fixes all of this at once — or, as a stopgap, reorder
`_repost_prompt` to send-then-delete, widen the delete catch to
`discord.HTTPException`, and wrap the placement in `asyncio.shield` the way
`place()` does. The widening is the better spend: it retires the last
divergent copy.

---

**Fixed** — migrated, not patched, though the first cut shipped a deploy bug a
follow-up review caught: `guess_prompt_channel_id` was added with no backfill, and
prod has three guilds with a live prompt and **zero** rows for the new key
(verified read-only). Those would have read `(0, live_id)` — the prompt stops
re-sticking entirely, and the first placement cannot resolve a channel to delete
the old prompt through, so the channel ends up with two, the stale one's buttons
still live. `_panel_ids` now falls back to `guess_channel_id` when the prompt has
a message id but no channel id, which is exactly where every legacy prompt is.

`guess` now holds a `StickyPanel` with
`target_types=(TextChannel, VoiceChannel, Thread)`, a **per-panel** widening:
doing it globally would have invalidated the auction card's thread warning, which
relies on threads staying out of the default set. New `guess_prompt_channel_id`
stores where the prompt actually is, so the delete aims at its real channel
rather than whatever the caller passed. `_repost_prompt` survives as a one-line
delegate because four call sites and three test modules speak it. Cog tests
shrank to the callbacks plus one forwarding assertion, per CLAUDE.md — the
placement semantics are covered once in `test_core_sticky.py`.

---

<a name="f3"></a>
## F3 — `guess_cog.on_message` does an uncached DB read per message · Medium

`cogs/guess_cog.py:1740-1741` calls
`await asyncio.to_thread(_load_config, db_path, message.guild.id)` for **every
message in every guild**, before any cheap channel check. `_load_config`
(`cogs/guess_cog.py:146-148`) opens a fresh connection every time — no TTL cache,
no known-guilds fast path.

Avoiding exactly this is why `StickyPanel` has `_cached_ids` (300 s TTL) and
`set_known_guilds`. Prod has ingested 635,643 messages, so this is a connection
open + PRAGMA + query per message on the hottest path in the bot.

The plan doc records `whisper` and `confessions` as "the two worst offenders on
the hot path" and describes `guess` as only lacking a lock. On this axis `guess`
belongs in the same sentence as those two.

**Fix.** Falls out of the migration in F2. If migrating is deferred, add the same
`guild_id → (expiry, channel_id)` TTL cache the economy cog uses for
`_photo_opts` (`cogs/economy_cog.py:1297-1300`), and check
`message.channel.id` against the cached value before touching the DB.

---

**Fixed** by the same migration: `on_message` is now one call into the shared
panel, which has the 300 s TTL cache and the known-guilds fast path.
`_prompt_guilds()` publishes the set at `cog_load`. Regression:
`test_on_message_no_longer_reads_the_db_per_message`.

---

<a name="f4"></a>
## F4 — A channel the bot can't post in is retried forever · Medium

`_place_locked` on a failed send (`core/sticky.py:310-313`):

```python
except discord.HTTPException:
    log.warning("%s: could not post panel in %s", self.name, target.id)
    return None
```

`schedule_restick` (`core/sticky.py:449-456`) then re-arms unconditionally on the
next message. So for a panel whose channel has lost **Send Messages** — a
permission edit, a category resync, a channel converted to announcement-only —
every burst of chat costs one doomed REST call and one warning line, forever.
There is no backoff, no failure counter, and nothing tells the admin: the
dashboard still shows the panel as posted, and `log.txt` is wiped every boot
(see memory: *Discord audit log is the only history*), so the warnings do not
even accumulate into evidence.

The log line itself is thin: no guild id and no exception, so an operator sees
`econ shop: could not post panel in 1526455991514955817` with no status code and
no way to tell 403 from 400-oversized-embed. `economy/bounty_views.py:125,205`
notes that an over-long bounty list is a 400 that "StickyPanel swallows" — that
comment is describing this line.

The dashboard *post* path does fail loudly and usefully
(`routes/panels.py:178-194` names the missing permissions), so this gap is
specific to the restick path.

**Fix.** Three small changes:
1. `log.warning("%s: could not post panel in guild %s channel %s", self.name, guild.id, target.id, exc_info=True)`.
2. Count consecutive placement failures per guild; after ~5, stop re-arming until
   the stored ids change or the process restarts, and log once at `error`.
3. Surface it — reuse `_retry`, or add the guild to a `take_failures()` set the
   dashboard can read, so "the bot can't post this panel" becomes visible
   configuration state rather than a log line nobody sees.

---

**Fixed** — all three points. `_note_failure` counts consecutive failures and
logs with the guild id and `exc_info`; at `max_place_failures` (default 5) it
logs once at `error` and `schedule_restick` stops arming. `failing_guilds()` is
readable state for a dashboard, deliberately not a draining queue. An explicit
`place`/`place_or_refresh` retries and clears the count, and — added after the
follow-up review — so does any **successful in-place edit**, which is proof the
channel works; without that a panel whose edits demonstrably succeed could stay
paused on the strength of five old transient send failures.

**Still open:** nothing in `src/web_server/` reads `failing_guilds()`, so the
`error` line's advice to re-post from the dashboard has no surface prompting the
admin to. The state is there; the panel that shows it is not built.

---

<a name="f5"></a>
## F5 — `take_retries()` is drained by one caller in nine · Low

`refresh()` on a transient `HTTPException` leaves the signature stale and queues
the guild for a retry (`core/sticky.py:416-420`). That queue is drained only by
the todo board loop (`cogs/todo_cog.py:403`). Verified by grep: no other caller
mentions `take_retries`.

Two callers actually use `refresh()` — todo and pen pals
(`cogs/pen_pals_cog.py:1349`, `1829`). So **pen pals populates `_retry` and
nobody ever reads it**: one transient Discord error and its panel stays stale
until something else moves it, with the guild id accumulating in a write-only
set. `_pen_pals_loop` (`cogs/pen_pals_cog.py:1116-1123`) is an existing 
periodic loop that could drain it in one line.

Also dead code: `cogs/pen_pals_cog.py:1830-1831` wraps `self.panel.refresh(...)`
in `except discord.HTTPException`, but `refresh()` catches `HTTPException`
internally and returns False. That handler can only fire if `build` raises.

**Fix.** Drain `take_retries()` in `_pen_pals_loop`. For callers with no loop, the
honest alternative is for core to retry once itself rather than offering a queue
nobody drains.

---

**Fixed** — `_pen_pals_loop` now calls `_retry_failed_panel_edits` each tick,
and the dead `except discord.HTTPException` around `refresh` is gone (`refresh`
catches it internally). Three tests, including one that a single failing guild
does not abort the rest of the batch.

---

<a name="f6"></a>
## F6 — The economy cog never publishes `set_known_guilds` · Low

`docs/plans/sticky-panel-extraction.md` states "every migrated cog now publishes
it". It does not: grep finds `set_known_guilds` in `voice_master`, `pen_pals`,
`dm_perms`, `todo` and `casino` — and in none of the economy cog's **five**
panels.

So `_known` stays `None` for all five, the guild fast path is off, and
`_restick_panels` (`cogs/economy_cog.py:3954-3961`) awaits five `on_message`
calls **sequentially** for every message in every guild. On cache expiry that is
five separate `load_econ_settings` calls in five separate threaded connections
(`cogs/economy_cog.py:3691-3697`) to read one settings row.

Absolute cost is small (five reads per guild per 300 s), which is why this is Low
rather than Medium — but it is the documented fast path simply not wired up, and
the doc claim should not be trusted as written.

**Fix.** Publish the set from `econ_settings` where any panel channel is set, and
re-publish from `_save_panel_ids` / `_save_bounty_panel_ids` (the pattern
`dm_perms._publish_panel_guilds` already uses). Optionally share one
`load_econ_settings` result across the five `load_ids` callbacks.

---

**Fixed** — `_publish_panel_guilds()` at `cog_load`, one query for all four
panels rather than five `load_econ_settings` calls. The **auction card stays
unpublished on purpose**, which is the interesting part: it is posted directly
rather than through `place`, so nothing calls `_remember` to add its guild, and
`auction_views` calls `forget` right after posting — a published set would
*discard* the guild and leave the card un-sticky. Pinned by
`test_publishing_panel_guilds_leaves_the_auction_card_unpublished`. The plan
doc's "every migrated cog now publishes it" claim is corrected.

---

<a name="f7"></a>
## F7 — The old-panel delete is swallowed with no log · Low

`core/sticky.py:325-328` (and `unpost` at `344-346`):

```python
try:
    await old_channel.get_partial_message(old_message_id).delete()
except discord.HTTPException:
    pass
```

A bare `pass`, no log, no retry. The row then points at the new message, so the
old one is unreachable by any later cleanup — it can only be removed by hand.
Because persistent views are registered by `custom_id` (53 `add_view` /
`add_dynamic_items` registrations repo-wide), that orphan **keeps working**: a
duplicate DM-perms panel or a second shop panel stays clickable indefinitely.

Mitigating, and why this is Low: a bot can delete its own message without
`Manage Messages`, so in practice this only fires on a transient 5xx or a
concurrent hand-delete (`NotFound`, which is the common and harmless case). The
`NotFound` case is exactly why the `pass` is there.

The economy cog shows the shape of a real fix for one instance —
`_drop_stale_bounty_hub` (`cogs/economy_cog.py:3750-3777`) deliberately hunts
down an orphaned hub because "its buttons are static custom_ids, so an orphaned
hub keeps working".

**Fix.** Split the cases: `except discord.NotFound: pass` (expected), and
`except discord.HTTPException: log.warning(..., exc_info=True)` plus queue the
`(channel_id, message_id)` for one retry. Do not leave the two
indistinguishable.

---

**Fixed** — `_delete_old` splits the two cases: `NotFound` returns quietly,
anything else retries with backoff on a detached task and logs if it never
succeeds. Deliberately **not** the queue the finding suggested: a queue an owner
has to drain is what F5 was.

---

<a name="f8"></a>
## F8 — No starvation ceiling on the trailing-edge debounce · Info

The debounce is pure trailing-edge: every message cancels the pending repost and
arms a new one (`core/sticky.py:449-456`). There is no maximum staleness, so a
channel that never falls quiet for `DEFAULT_DELAY = 6.0` seconds **never
re-sticks at all**.

Measured in the repro: 45 messages spaced at one third of the debounce, spanning
15 debounce periods → **0 reposts**, then 4 (the four panels) once the channel
fell quiet. The panel stays buried for the whole conversation.

This is a deliberate trade — `dm_perms` was moved from a leading-edge 2 s
cooldown to this debounce on purpose, and the plan doc records it as a
user-visible timing change. Recorded here only because the trade has no cap: the
worst case is unbounded, not `hold_max`-bounded like the `hold` hook is
(`core/sticky.py:493-513`). If it ever bites, the fix is a "re-stick anyway after
N seconds buried" ceiling mirroring `hold_max`.

---

**Fixed, off by default.** `max_burial` re-sticks once the panel has been
buried that long regardless of whether the channel falls quiet — implemented in
`schedule_restick`, which stops *re-arming* past the ceiling rather than letting
`_delayed_restick` decide (the problem is that the task is cancelled before it
ever runs). It defaults to `None`, i.e. exactly today's behaviour, and is wired
to no caller: a timing change across all ten live panels is not a change a review
pass should make silently. Both directions are tested. Say the word and it goes
on for a specific panel.

---

<a name="f9"></a>
## F9 — The leaderboard's hourly repaint is a parallel implementation · Info

`services/economy_loop.py:1476-1549` (`run_guild_leaderboard`) refreshes the
leaderboard panel hourly without going through `StickyPanel.refresh()`. It
diverges on three of the four rows the extraction was written to unify:

| | `StickyPanel.refresh()` | `run_guild_leaderboard` |
|---|---|---|
| REST calls | `get_partial_message().edit()` — one | `fetch_message()` + `.edit()` — two |
| Per-guild lock | held (`place`/`unpost` path) | not taken |
| `NotFound` | reposts, so the feature self-heals | clears the stored ids, retiring the panel |

The `NotFound` divergence is deliberate and documented ("deleting the message is
how staff retire the panel"), and it is **not** config data loss — the
leaderboard channel is not separately configurable, it is just where the panel
was last posted via `routes/panels.py`. I checked: `economy-config.js` has no
leaderboard/guide/shop channel picker. The lock omission is also benign as far as
I can construct: the race resolves to a redundant edit or an extra repost, and
its `NotFound` handler already re-reads the id to distinguish "moved" from
"deleted" (`economy_loop.py:1525-1530`).

So: no bug found, recorded as divergence. It is the fourth panel-refresh
implementation in the repo and the only one still paying two REST calls per
repaint. Folding it into `leaderboard_panel.refresh()` — with an explicit
"retire on NotFound" option if that behaviour is wanted — would retire it.

The same `refresh()`-outside-the-lock note applies to core itself
(`core/sticky.py:386-423`): it reads ids, builds, edits and writes
`_signatures` with no lock, while `place`/`unpost` hold one. I tried to construct
a sequence that strands a *stale* panel through the signature write and could
not — the realistic outcomes are a wasted edit or an extra repost, and only the
todo board supplies a signature at all. Reporting it as suspected-benign rather
than as a finding.

---

**Fixed** — `refresh` gained `repost_if_missing`, and `run_guild_leaderboard` is
now a delegate to `leaderboard_panel.refresh(guild_id, repost_if_missing=False)`.
~70 lines of parallel renderer gone, and the single REST call with it.

**The first cut dropped a guard it should have kept**, caught by the follow-up
review: the old code re-read `leaderboard_message_id` on a 404 and bailed if it
had changed, because a sticky repost deletes the old panel and posts a new one —
so a 404 can mean "it moved", not "it is gone". `refresh` holds no lock (the
suspected-benign note above), so a restick really can land between its id read
and its edit; zeroing the ids there would retire a panel that is live in the
channel with a working `QuestBoardView` and report it unposted on the dashboard.
The retire path now re-reads and only clears when the ids are unchanged
(`test_the_retire_path_does_not_zero_a_panel_that_only_moved`). My docstring had
claimed core did this "under the placement lock", which was simply false.

---

<a name="f10"></a>
## F10 — No sticky-id cleanup on channel delete or guild removal · Info

Repo-wide there are three `on_guild_channel_delete` listeners
(`voice_master_cog.py:357`, `pen_pals_cog.py:1833`, `music_cog.py:808`) and one
`on_guild_remove` (`whisper_cog.py:2319`). **None of them clears sticky panel
ids.** Likewise no cog clears them when a feature is switched off, other than
casino's explicit teardown (`cogs/casino/cog.py:800-802`) and
`todo.unpost_board`.

The consequence is contained, which is why this is Info and not a bug:
`_channel()` returns `None` for a deleted channel (`core/sticky.py:213-214`) and
`_delayed_restick` returns early, so nothing loops or retries. What persists is
stale state — a `(channel_id, message_id)` row naming a channel that no longer
exists, the guild still in `_known`, and a dashboard that reports the panel as
posted. On re-invite or re-creation, `place_or_refresh` edit-404s and posts
fresh, so it heals rather than duplicating.

**Fix (optional).** A shared `on_guild_channel_delete` hook that calls
`forget(guild_id)` and `save_ids(guild_id, 0, 0)` when the deleted channel
matches a panel's stored channel. Worth it mainly so the dashboard stops lying
about panel state.

---

**Fixed** — `StickyPanel.on_channel_delete` clears the ids and cancels any
pending restick, forwarded from `on_guild_channel_delete` in all six cogs that
own a sticky panel (economy forwards to all five of its panels; voice master and
pen pals fold it into their existing listeners). Two corrections from the
follow-up review: it now runs **under the per-guild lock**, because a shielded
placement into a different channel can be in flight and a `(0, 0)` write landing
after that placement's write would forget a panel that had just been posted; and
`guess` also forwards `on_thread_delete`, since Discord dispatches that rather
than `on_guild_channel_delete` for threads — which is precisely the case
`target_types` was widened for.

---

<a name="what-is-healthy"></a>
## What is healthy — and the evidence

Stated plainly because it is a useful result: the four hard parts of this module
work.

**The debounce is per-panel-instance, per-guild** (`_restick_tasks`, keyed by
guild id, on each `StickyPanel`) — not global, not per-channel.
`DEFAULT_DELAY = 6.0`, trailing edge, cancel-and-rearm, so a burst costs one
repost. Verified by `test_restick_debounce_collapses_a_burst` and by the repro.

**The placement lock is held across every await that matters.**
`_place_locked` (`core/sticky.py:288-335`) takes `self._locks[guild.id]` and
*then* re-reads the stored ids inside it, so a caller's pre-lock snapshot can
never be used to delete a live panel. `only_if_buried` is re-decided under that
same lock, so a restick that queued behind another placement sees that
placement's result instead of stacking on top of it.

**Placements are atomic to cancellation.** `place()` runs the work as a shielded
task (`core/sticky.py:249-259`) so `schedule_restick`'s cancel-and-rearm stops
the *waiting*, never the work — the fix for the 2026-07-26 storm. Cancelled
callers still get their failures logged
(`_log_abandoned_placement`, `core/sticky.py:261-269`).

**Restart re-attaches; it does not duplicate.** Every boot path goes through
`place_or_refresh`, which edits in place when the panel is already in the target
channel: casino `_boot` → `ensure_panel` (`cogs/casino/cog.py:511-512`,
`805-808`), `dm_perms._autopost_panels` (`cogs/dm_perms_cog.py:847-887`, whose
docstring says exactly this), and the dashboard poster for the rest. Old
messages' buttons keep working by design — the persistent views are registered by
`custom_id` — which is what makes an *orphan* (F7) matter and a *restart* not.

**Prod confirms the storm is fixed.** In guild `1476525656115515484`'s shared
panel channel (`1532304393313980446`, 543 messages), the inter-arrival pattern is
a triggering bot message followed by exactly **one** repost `+6 s` later — the
`DEFAULT_DELAY` signature — then silence:

```
2026-08-05 19:15:32  id=1534746719366025258
2026-08-05 19:15:38  +6.0s  id=1534746746146521219   <- the repost
2026-08-05 19:39:53  +1455.0s id=1534752850645745766
2026-08-05 19:40:00  +7.0s  id=1534752876902224155   <- the repost
```

Histogram over all 543: 216 gaps in the 2–8 s band (the trigger→repost pairs),
151 gaps over 300 s. No run of ~6 s reposts, which is what the July storm looked
like. `casino_panel_message_id` advances normally (last 2026-08-05 22:15:55), so
the stored id is no longer freezing on a dead message.

One thing that looked alarming and is not: the main guild's
`econ_guide/leaderboard/shop_message_id` are frozen at 2026-07-19 / 07-27 / 07-27.
Those channels' *last recorded message* timestamps match the panel ids exactly
(the `messages` table ingests the bot's own posts), i.e. the panel **is** the last
message and nobody has posted in those channels since. The panels are not stuck;
their channels are quiet.

## Method

- Read `core/sticky.py` in full, plus all nine callers' panel wiring and
  `docs/plans/sticky-panel-extraction.md`.
- Reproduced F1 at the logic layer against real `StickyPanel` instances
  (scratchpad, not committed — the shippable version belongs in
  `tests/test_core_sticky.py`; see F1's last section) and verified the candidate
  fix converges without breaking the round-result chase.
- Verified `discord.py` 2.7.1 maintains `last_message_id` from
  `MESSAGE_CREATE` (`discord/state.py:691`), since the whole at-the-bottom guard
  rests on it.
- Queried prod read-only via a `mode=ro` URI connection (no file copy, no
  backup-file snapshot needed for scalar config reads): `config` keys for all
  five sticky-panel channel/message pairs, guild activity from `messages`, and
  the inter-arrival histogram above.
- Did **not** restart the bot or the dashboard, and made no writes.
