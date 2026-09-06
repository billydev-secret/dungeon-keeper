# Needle vs. the thread-bot field — gap analysis

**Status:** ANALYSIS ONLY — no code, nothing picked, nothing scheduled.
**Written:** 2026-09-05.
**INDEX.md classification on land:** Implementation plan (analysis; no stages started).
**Deliverable:** the ranked pick-list in §5. Everything above it is the evidence for it.

## 0. The finding that reorders the whole list

**Needle is built but dark in production.** `needle_channels` has **zero rows**
in the live database. The four guild-wide keys were saved once
(`needle_emoji_unanswered` 🔵, `needle_emoji_archived` ✅, `needle_emoji_locked` 🔒,
`needle_default_reply` "Thread created by $USER in $CHANNEL", all on guild
`1469491362444480666`, all at their shipped defaults), so somebody opened the panel —
but no channel was ever added, so the `on_message` listener has never once fired
on a real message.

Read the rest of this document through that. Every candidate below is a
refinement to a feature nobody is using yet, and refinements to a dark feature
are speculative by construction: we would be guessing at which dial chafes
before anyone has felt one chafe. **The highest-value action here is not on the
list — it is switching Needle on in one channel and watching it for a week.**
The list is what to reach for once that has happened.

## 1. How this started, and what was actually compared

Billy found [Thread Bot](https://top.gg/bot/871542167968055418) on top.gg and
asked whether anything in its feature set was worth taking. It turned out to be
a single-command bot (§2), so the comparison was widened, with his agreement, to
the bots that actually lead this category.

| Compared against | What it is | How it was sourced |
|---|---|---|
| **discord-needle** (`MarcusOtter/discord-needle`) | DK's own ancestor — our cog's docstring names it. AGPL-3.0, 229 stars, last pushed 2026-05-29, hosted at needle.gg | **Read directly from source** on `main` via the GitHub API — commands, enums, models, listeners. This is the only column below that is first-hand |
| **EasyThreads** (`992796487048233000`) | "All-in-one bot for everything with Threads and Forum Posts" — autothreading, auto-tags, thread moving, thread panels | Listing-page copy via search-index snapshots |
| **Thread-Watcher** (threadwatcher.xyz) | Keeps threads alive indefinitely; plus a visual-editor ticket system with transcripts and AI summaries | Own site |
| **Thread It** (`wei/thread-it`) | Converts *replies* into threads — a different trigger | GitHub description |
| **Thread Bot** (`871542167968055418`) | The original ask | Listing-page copy via search-index snapshots |

**Sourcing caveat, stated plainly:** top.gg is behind Cloudflare and 403s every
fetch path tried (WebFetch, curl with a browser UA, a text-extraction proxy).
Every top.gg fact in this document therefore comes from search-index snapshots
of those pages, not from the pages themselves. The upstream discord-needle
column has no such caveat — it was read from the repository. Where a
recommendation below depends on a competitor's exact behaviour, it depends on
the upstream column.

## 2. The original ask, answered

Thread Bot is *"a simple bot to manage support ticket threads"* with essentially
one command: `/tcreate [message-id]` — creates a thread on the message with that
ID and adds the invoker plus the original author.

**Nothing to take.** DK already ships the same job done better: jail_cog.py
registers an **"Open Ticket About This Message"** right-click context menu
(`ticket_message_context` → `_TicketFromMessageModal`), which is the same
outcome without asking anyone to copy a message ID out of Discord's developer
mode. The only residue is auto-adding the original author as a participant,
which DK's ticket service already has a seam for (`add_ticket_participant`).

## 3. Feature matrix

DK's column is from the code (`needle_cog.py`, `config-needle.js`,
`routes/config.py`), not from `needle_spec.md`, though the spec is classified
**Reference** and proved accurate throughout.

| Capability | DK Needle | upstream needle | EasyThreads | Thread-Watcher |
|---|---|---|---|---|
| Auto-thread every message in a channel | ✅ | ✅ | ✅ | — |
| **Configured on a web dashboard** | ✅ **only DK** | ✗ `/auto-thread` | ✗ `/addchannel` | partial (visual editor, tickets only) |
| Title styles | 4 (first-50 / first-line / name+date / custom) | 4 (same set) | ✅ | — |
| **Configurable title length** | ✗ **hardcoded 50** | ✅ 1–100 | ? | — |
| **Regex title extraction** | ✗ | ✅ + `safe-regex` ReDoS guard + join text | ? | — |
| Template variables | **3** (`$USER` `$CHANNEL` `$THREAD`) | **14** | ✅ ("variables" in `/help`) | — |
| **Configurable auto-archive duration** | ✗ **hardcoded 24 h** | ✅ | ✅ | its entire product |
| **Rename thread when starter message is edited** | ✗ | ✅ guarded | ? | — |
| Delete-behaviour on starter deletion | 4 modes | 4 modes (identical enum) | `threadautodelete` | — |
| Slowmode inside the thread | ✅ 0–21600 s | ✅ | ✅ | — |
| Include bots | ✅ | ✅ | ✅ | — |
| Status reactions | ✗ **removed 2026-09-06** (was 3-state, emoji configurable) | ✅ toggle | ? | — |
| **Extra reactions on every new message** | ✅ **DK only** | ✗ | ? | — |
| Welcome message + Archive/Edit-title buttons | ✅ | ✅ | ✅ | — |
| Custom button text / colour | ✗ | ✅ (4 styles) | ✅ | — |
| Forum channel support / auto-tags | ✗ | ✗ | ✅ | — |
| Move a thread between channels | ✗ | ✗ | ✅ | — |
| Button panel that spawns threads | ✗ | ✗ | ✅ `/thread-panel` | — |
| Reply-becomes-a-thread trigger | ✗ | ✗ | ✗ | ✗ (Thread It) |
| Keep threads alive indefinitely | ✗ | ✗ | ✗ | ✅ |
| Customise every bot string | ✗ | ✅ `/setting` | ✗ | ✗ |

**Where DK is already ahead:** dashboard configuration (nobody else has it) and
per-channel default reactions. The port did not merely copy upstream — it
improved on it.

> **Amended 2026-09-06.** DK's third advantage used to be configurable status
> emoji. The whole status machine has since been **removed** (migration 215) on
> Billy's call that an auto-reaction should cue people and nothing more, never
> assert a state the bot maintains. DK is now deliberately *behind* upstream on
> that row and intends to stay there; `default_reactions` — the decorative
> half — is untouched and remains a DK-only feature. Read the matrix row
> accordingly: it is a choice, not a gap.

**Open review findings: none.** The S2 in `docs/reviews/2026-07-23-novel-hunt.md`
(#11 / §G) — a member-controlled nickname interpolated into the pinned welcome
message with no mention suppression — **is already fixed**. `_apply_variables`
calls `discord.utils.escape_mentions`, and `_post_welcome` passes
`AllowedMentions.none()`, with a comment explaining both. Nothing to fold in.

## 4. Rejected on house rules, named explicitly

Per CLAUDE.md, admin configuration lives on the dashboard and never in Discord.
These arrive as slash commands in the competitors and would be **reshaped, not
copied**, if ever wanted:

| Competitor surface | If DK wanted it |
|---|---|
| `/auto-thread`, `/addchannel`, `/channelsettings`, `/forum-settings` | already the Auto-Thread dashboard panel |
| `/thread-panel` (EasyThreads) | a DK sticky panel through the existing panel registry |
| `/batch` (Thread-Watcher) | a dashboard multi-select, not a command |
| `/factory-reset`, `/help`, `/info` (upstream) | DK has `/help`; the rest are dashboard state |
| `/setting` — customise every user-facing string (upstream) | out of scope: that is bot-wide localisation, not a Needle feature |
| Thread-Watcher's ticket flows, transcripts, AI summaries | DK already has jail/tickets with transcripts |

## 5. The pick-list

Effort is rough implementation size. Every row assumes the CLAUDE.md tax:
`needle_spec.md` updated in the same commit, `manual.html` §Auto-Threading
updated for anything a member or admin can see, and tests in
`tests/test_needle_logic.py` — which today has **3 test functions in 52 lines**,
thin enough that any of these should widen it rather than sit beside it. None of
these adds a per-user table, so no `data_register.md` row is needed.

### Above the line

**1 — Configurable auto-archive duration.** *Effort: XS.*
DK hardcodes `auto_archive_duration=1440` (24 hours) at `needle_cog.py:354`.
Discord offers 1 hour, 24 hours, 3 days, 7 days. On a slow showcase or intro
channel — exactly where auto-threading earns its keep — a 24-hour timer buries
every thread by the next day. This is the single most consequential hardcoded
value in the cog, it is one column plus one dashboard select, and it is the
honest answer to Thread-Watcher's entire product (see row 9). Recommended first
whenever Needle goes live.

**2 — Rename the thread when the starter message is edited.** *Effort: S.*
Upstream runs a `messageUpdate` listener; DK has `on_message`, `on_message_delete`
and `on_thread_update`, but no edit handler. The upstream guard is the clever
part and should be copied verbatim in spirit: recompute what the thread's name
*would have been* from the old message, and only rename if that matches the
thread's current name — so a thread someone deliberately retitled is never
clobbered. Fixes the common case of posting, noticing a typo, fixing it, and
being stuck with the typo in the thread name forever.

**3 — Richer template variables.** *Effort: S.*
DK has 3, upstream has 14. Not all are worth having; these are:
`$CHANNEL_NAME`, `$DATE`, `$THREAD_NAME`, `$TIME_AGO` (renders as Discord's
relative `<t:…:R>` timestamp), and the `$USER_MENTION` / `$USER_NAME` /
`$USER_NICKNAME` split — DK's single `$USER` is the escaped *nickname*.

> **Handle `$USER_MENTION` carefully.** It is the one variable that must ping,
> and Needle is precisely where a mention-injection S2 already landed once. It
> must resolve to `<@{message.author.id}>` built from the ID — never a
> passthrough of member-controlled text — and the send must move from
> `AllowedMentions.none()` to `AllowedMentions(users=[message.author],
> everyone=False, roles=False)`, which is the ping allow-listing convention in
> `docs/embed_style_guide.md`. Every other variable stays inside
> `escape_mentions`. If that nuance is unwelcome, ship the other four and skip
> this one — they carry no ping risk at all.

**4 — Configurable title length.** *Effort: XS.*
Upstream allows 1–100; DK's `first_fifty` is fixed at 50 while the clamp is
already 100. Natural bundle with row 1 — same migration, same panel section.

### Needs a decision before it is worth costing

**5 — Reply-becomes-a-thread.** *Effort: M–L.* Thread It's trigger, which
nobody else in this field has: rather than threading *every* message, thread a
message the moment someone replies to it in the channel. That suits a busy
general channel where full auto-threading would be oppressive, and it is the
one genuinely novel idea the widened search turned up. But it is a behaviour
members feel immediately and can find intrusive, so it is a design call, not a
backlog item. **Question for Billy in §6.**

**6 — Forum channel support.** *Effort: L.* Needle is text-channel-only by
declared non-goal. EasyThreads covers forums and auto-applies tags. Only worth
building if TGM uses forum channels at all — **question for Billy in §6**. Note
that forum channels already give you a thread per post natively, so the value
here is the *automessage and auto-tagging*, not the threading.

### Below the line

**7 — Custom button text and colour.** *Effort: XS.* Upstream lets you retitle
and recolour the Archive/Edit-title buttons (4 styles). Cheap and
dashboard-shaped, but it is pure cosmetics on a feature that is dark, and
CLAUDE.md's "collapse controls" instinct argues against two more dials earning
their place on the panel. Listed for completeness.

**8 — A plain "thread this message" context menu.** *Effort: XS.* The residue
of the original ask. DK's ticket context menu covers the support case; this
would be the non-ticket version. ~30 lines, low value, and it adds a second
entry to the right-click menu — which is a shared, finite surface.

**9 — Keep-threads-alive. Recommended against.** Thread-Watcher's whole product
bumps or unarchives threads on a timer to defeat Discord's auto-archiving. It
is noisy by construction, and the premise is slightly false: an archived thread
is still readable and revives on the first new message — Discord hides it from
the sidebar, it does not delete it. Row 1 (pick 7 days) gets most of the benefit
with none of the noise.

**10 — Move a thread between channels.** *Recommended against.* Discord has no
native move, so EasyThreads necessarily recreates the thread elsewhere and
replays its messages through a bot or webhook. That is lossy, it rewrites
authorship, and for DK it would collide with the message archive and with
no-contact scoping. Not worth it.

## 6. Open questions for Billy

1. **Is Needle meant to be on?** It has never run. Is that deliberate — TGM does
   not want per-message threads — or did it get configured and forgotten? The
   answer decides whether this list is a roadmap or a curiosity.
2. **If it should be on, which channel first?** A showcase, intro, or Q&A
   channel is the natural fit, and picking one makes row 1's archive duration a
   concrete choice rather than a guess.
3. **Reply-becomes-a-thread (row 5)** — wanted for a busy general channel, or
   too invasive for TGM?
4. **Forum channels (row 6)** — does TGM use any?
5. **`$USER_MENTION` (row 3)** — should the thread's welcome message ping the
   person whose message spawned it, or stay silent?
