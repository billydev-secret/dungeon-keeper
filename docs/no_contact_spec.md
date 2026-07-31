# No-Contact List

**Status: Reference** — describes the code as shipped.

Pairs of members the bot will never put in contact, in either direction,
through any feature that can carry contact between two people.

## Why this exists

Blocking someone in Discord stops them messaging you directly. It does not
stop them reaching you *through a bot*: an anonymous whisper, an AMA question
aimed at you, a reply to your confession are all the bot delivering something
on their behalf, and Discord's block never sees them. The motivating incident
was exactly that — a member blocked someone, and he could still reach her
through the anonymous features.

This is a safety feature. Two rules follow from that and are worth stating
before any implementation detail:

1. **Enforcement is not a preference.** Every surface that can relay contact
   is gated. A guarantee that holds in three features and quietly fails in the
   other three is not a guarantee.
2. **The blocked party must never learn an entry exists.** Telling him he has
   been blocked confirms she acted against him, which is the escalation the
   feature exists to prevent. That constraint shapes almost every design
   decision below.

## Storage

`no_contact_pairs` (migration 145), one row per pair:

| Column | Meaning |
|---|---|
| `guild_id`, `user_low`, `user_high` | The pair, order-independent (`user_low = min(a, b)`) |
| `protected_user_id` | Who the entry protects, and the only member who may lift it. `NULL` = mutual separation, liftable only by staff |
| `created_by` | The member, or the mod who acted |
| `reason` | Moderator-visible only; the other party never sees it |
| `created_at` | — |

Also `no_contact_settings` (alert channel + ping role, per guild) and
`no_contact_events` (blocked attempts and mention/reply alerts, staff-visible).

### Why not `dm_consent_pairs`

That table has the same `(user_low, user_high)` shape and was the obvious
candidate. Three reasons it cannot be used:

1. `dm_perms_service.normalize_request_type()` coerces anything that isn't
   `'friend'` to `'dm'`, so a `rel_type='no_contact'` insert would silently
   store `'dm'`.
2. `load_consent_pairs()` ignores `rel_type` entirely and adds every row to
   the mutual-consent set in both orderings. A no-contact row there would not
   merely fail to block — it would **grant the blocked party a DM consent
   pair.** Exactly inverted.
3. Its primary key allows one row per pair, so it cannot represent "these two
   had a consent pair, and now they have a no-contact"; the insert would
   overwrite the consent row and lose it.

The *convention* is reused; the table is not.

### Why not an in-memory cache

`dm_perms` caches consent pairs at boot. That is safe there because a stale
cache fails toward "not connected". A stale no-contact cache fails the other
way — it lets a blocked member through until the next restart. The gates are
low-frequency, so every check is a direct indexed read.

## Gated surfaces

| Surface | What is gated | Where |
|---|---|---|
| Whisper | Send, and replies to whispers sent before the pair existed | `whisper_cog._send_impl`, `WhisperReplyModal` |
| AMA | Directed questions (both modes), the answer-DM back to the asker, and the panelist picker | `games_ama_cog` |
| Confessions | Anonymous replies (`parent_author_id` is the member replied to) | `confessions_cog.ReplyModal` |
| Guess Who | Guesses on the other's rounds, the candidate picker, submissions where `answer_id` differs from `submitter_id`, and the paired reveal | `guess_cog` |
| Pen Pals | Matching, via `_is_blocked_pair` | `pen_pals_cog` |
| Voice Master | Room permissions, via `effective_blocked` | `voice_master_service` |
| DM requests | Consent suppressed; new requests refused | `dm_perms_cog` |

Features that hold their own connection (Pen Pals' matching pass, Voice
Master's permission build) consult the list through
`is_no_contact_conn` / `no_contact_partners_conn` rather than querying
`no_contact_pairs` directly, so the table's shape stays owned by one module —
adding an expiry or soft-delete column is a change in one place, not a grep.

Only the first six surfaces record an **attempt** event. The last three are
gated by extending an existing predicate that runs inside matching loops and
permission syncs; there is no single moment there that means "he tried", and
recording per iteration would bury the real attempts. They are enforced just
as strictly — they simply produce no log lines.

`dm_consent_pairs` rows are **suppressed, not deleted**: `_is_mutual` returns
False for a no-contact pair while the row and its provenance survive for a mod
reviewing the case. Note the consequence — if a no-contact entry is later
lifted, the old consent becomes live again without either party re-consenting.

## The disclosure rules

The blocked party sees a refusal that is indistinguishable from either an
ordinary success or an ordinary failure, chosen per surface by whether
anything would have been *publicly visible*:

- **Fake success** where nothing visible was going to happen: whisper send and
  reply ("Whisper delivered." / "Reply delivered anonymously."), screened AMA
  questions (they would have gone to the host's DMs), confession replies (he
  does not know whose confession it is, so a reply that fails to appear cannot
  be attributed to anyone), and the AMA answer-DM.
- **An ordinary failure** where fake success would leave a hole he could see:
  an *unfiltered* AMA question would have been posted to the channel, and he
  chose the panelist, so "posted!" followed by nothing appearing points
  straight at her. The existing stale-target error is returned instead — a
  believable race that explains the absence without involving her.
- **The ordinary wrong-guess line** for Guess Who, which is not even a lie:
  his partner is filtered out of his candidate picker, so whatever he selected
  really was not the answer.

Three further leaks are closed away from the send paths:

- **`/nocontact list`** hides entries that protect the *other* party
  (`is_visible_to`).
- **`/nocontact remove`** answers "there's no entry between you two" rather
  than "you can't remove this", because a permission refusal confirms
  something exists to remove.
- **Voice Master** splits `list_blocked` (what the owner *sees*) from
  `effective_blocked` (what is *enforced*). Folding no-contact partners into
  the visible list would show him her name in a blocklist he never set.

A duplicate `add_pair` is `ON CONFLICT DO NOTHING` and callers must not
distinguish it from a fresh insert: "that already exists" would tell him she
had added one first. It also stops him rewriting `protected_user_id` to seize
removal rights.

Each of these response strings is a **module-level constant shared by the
ordinary path and the gated one** (`whisper_service.SENT_CONFIRMATION`,
`games_ama_cog.STALE_PANEL_TEXT`, `guess_cog.WRONG_GUESS_TEXT`, …). They were
briefly duplicated literals, which is the wrong shape for this: copy edits are
the most common change in this repo, and rewording one branch and not the other
would silently turn the refusal into a tell with no test failing.

### Known residual: the Guess Who counter

If a blocked member guesses on the other's round, the round's guess counter
does not bump. In principle that is observable. It is not attributable — the
counter moves constantly from other players — and closing it would mean
recording his guesses against her round, which is what the gate exists to
prevent.

## Alerts

Fires when one member of a pair `@mention`s **or replies to** the other. The
reply trigger is not redundant: a Discord reply with the ping switched off
still lands in front of the person replied to and never appears in
`message_mentions`. Read off the live message, with a `messages.reply_to_id`
fallback when Discord did not resolve the reference.

Alerts go to a dashboard-configured channel and ping a dashboard-configured
role. They carry a jump link, channel, and timestamp — **not** message text,
since `messages.content` is nullable and content retention is off by default;
widening retention server-wide to serve this feature would cost every other
member's privacy. Enforcement does not depend on alerts being configured.

**The protected member is never notified** — of alerts or of blocked
attempts. She already receives a real `@mention` by definition, and a
notification on every attempt would hand him an indirect way to distress her.
Attempts are recorded for staff, who can act on a pattern.

## Management

**Member self-service** — `/nocontact add|remove|list`, all ephemeral.

This is a deliberate exception to the project's config-belongs-on-the-web
rule, and the reasoning should survive the next command audit: that rule
governs *admin/server settings*, and the same paragraph of CLAUDE.md reserves
Discord for *member self-service and mod actions*. A member protecting
themselves is self-service. Routing it through a dashboard — or a moderator —
puts the highest barrier exactly where someone is least willing to explain
themselves. `/nocontact add` is two taps from the message that upset them.

**Moderator dashboard** — Moderation → No-Contact List. Adds entries on a
member's behalf (third-party reports, or someone who will not file it
themselves), shows the whole list and the event log, and sets the alert
channel and role. Moderators can remove any entry.

Removal is restricted to the protected member plus staff, deliberately
asymmetric with adding. Adding is the safe direction; removal is the one a
harasser benefits from, and the one he might pressure her into.

## Tests

- `tests/test_no_contact_logic.py` — pure decisions: removal authorisation,
  disclosure, alert triggers, the fake-success contract.
- `tests/test_no_contact_service.py` — storage, the gate helper, and the
  cross-feature enforcement points (Pen Pals, Voice Master, DM consent),
  including that the *visible* voice blocklist stays clean.
- `tests/cogs/test_guess_no_contact.py` — wiring: the gate is actually called,
  and a blocked guess is neither recorded nor distinguishable.
