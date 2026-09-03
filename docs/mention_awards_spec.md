# Mention Awards

**Reference spec** — matches current behavior.

Pays currency to whoever gets @-mentioned in a message that matches a rule's
**conditions**. Built for games the bot **does not host**: where members run
the game themselves, the announcement they already post is a clean,
machine-readable payout event.

## Why it exists

The prompting case is a Hot Seat rotation run by hand in a channel — one
member in the seat at a time, and the *outgoing* contestant announces the next
one with a card image and a ping:

> **@Hot Seat** your turn **@turbodog8**! Let's all find out more about him!
> What's your favourite thing about yourself?

Dungeon Keeper hosts none of it, so no game hook can pay the contestant. But
the handoff is unambiguous, and the bot already sees it.

## Rule anatomy

A rule is a **channel**, an **amount**, and a list of **condition chips** that
must *all* match (AND). A second member-run game needs a second rule, not a
second feature. Configured at **Economy → Mention Awards** (`mention-awards`),
admin-gated. Rules are matched in creation order; the first match wins,
because the payout ledger is keyed on the message and one message can only pay
once regardless.

| Chip | Matches when | Value |
|---|---|---|
| `contains_text` | Content contains the text (case-insensitive substring), or matches the pattern when the chip's `regex` flag is set (`re.search`, IGNORECASE) | free text / pattern, ≤200 chars |
| `mentions_role` | The message pings that role | role id |
| `from_user` | The author is that member | user id |
| `author_has_role` | The author holds that role — the anti-farm chip (the old "who can award" lever) | role id |

Chips are stored as a JSON array on the rule row (migration 157, which also
converted the original phrase/announcer-role columns losslessly). Ids inside
the JSON are strings — the panel reads it, and a snowflake past 2^53 loses
precision in JavaScript.

**Raw content caveat:** chips match against *raw* gateway content, where a
role ping is `<@&id>` markup — `hot seat` as a *text* chip can never match the
rendered `@Hot Seat`. That is exactly what `mentions_role` is for.

## What counts as an award

A message awards when **all** hold:

1. It's in the rule's channel — **threads count toward their parent**, the
   same convention as the photo-challenge trigger and trigger quests.
2. The author is not a bot.
3. The rule's amount is at least 1.
4. The rule has at least one chip, and **every** chip matches.
5. The message @-mentions **exactly one** member (role pings ride separately
   and don't count toward this).
6. That member is not the author.

Rule 5 is deliberate: a message tagging several people is a group shout, and
guessing which of them the chips referred to would pay the wrong member.
Rule 6 closes the only farm the design is otherwise wide open to.

**Fail-closed everywhere:** an unknown chip kind never matches, a rule with no
chips matches nothing, malformed conditions JSON parses to no chips, and a
regex that breaks at match time doesn't match — a bad row can park a rule but
can never open a faucet.

## Privacy: content is read, never stored

Chips are matched against `message.content` live off the gateway and
discarded. Nothing in this feature writes content, and the guild's
message-storage level is untouched.

The consequence is that **text chips cannot be replayed over history** —
banked messages have no content to re-match. The one-off backfill
(`scripts/backfill_mention_awards.py`) therefore matches on message *shape*
instead: `media_kind` plus the @-mention edges, both of which survive with
content storage off. That seam is why the backfill is a script rather than a
mode of the feature. The backfill's mention *source* also differs from the
live path's (`message_mentions` banks the gateway payload — reply pings
included, self-mentions dropped — where the live matcher reads raw content
mentions), so its 15/15 measured precision is per-channel evidence, not a
guarantee; its docstring says to spot-check a new channel's dry run.

**`from_user` chips hold a member's id** (as a string inside the conditions
JSON — the export's "list-column blind spot"). On hard erasure,
`purge_user_data` strips that member's `from_user` chips; a rule left with no
chips is deleted outright (empty chips = the fail-closed "matches nothing"
state). The column is registered in `LIST_VALUED_MEMBER_COLUMNS` so an access
request's export discloses the gap for hand-review.

Regex chips are admin-authored behind the admin gate and validated
(`re.compile`) at save time; a pathological pattern is bounded by Discord's
message length and is the admin's own footgun, not member-reachable.

## Payout

`bot_modules/economy/game_rewards.pay_mention_award` — the `pay_cat_catch` shape: coins
credited directly, plus the `mention_award` quest trigger on top. Inherits
every faucet guarantee: no-op when the economy is off or the member is a
bot/unresolvable, booster multiplier applied, failures logged not raised.

**Idempotency** reuses `games_external_payouts` (`message_id` PK — the
contract is one payout per message across *all* kinds; `kind` labels a claim,
it doesn't scope it) rather than adding a second ledger. The claim is taken
*before* the credit, so an edit re-firing the listener cannot double-pay —
and because `pay_mention_award` reports whether the credit actually landed,
a claim whose payout no-ops (economy off, member unresolvable) is **released**
so a retry (an edit, or the backfill) can pay it rather than the member being
silently shorted forever.

**Rule changes are live immediately.** The listener caches each channel's
rules (60s TTL, negative results included, so unwatched channels cost no DB
work), and every dashboard write invalidates that cache in-process (the
games_external refresh pattern). The TTL is only the backstop for
out-of-band DB edits or a mid-restart dashboard write.

Dedupe is **per announcement only**. A member who takes the seat again later
is paid again — the right reading of two genuine turns.

## Anti-farm

The honest summary: with no author chip (`author_has_role` / `from_user`),
any member can pay any other member by posting a matching message. That is
the permissive setting, and it is the correct one for a baton-pass game (the
outgoing contestant holds no special role, and gating on the game's owner
would drop about a third of real announcements — measured on live traffic).
Add an author chip when the game has fixed hosts.

What is always enforced: no self-awards, no bot authors, exactly one mention,
one payout per message.

## Files

| Path | Role |
|---|---|
| `bot_modules/mention_awards/logic.py` | Pure chip matching — the whole safety surface |
| `bot_modules/mention_awards/store.py` | Rule CRUD + validation + conditions JSON |
| `bot_modules/cogs/mention_awards_cog.py` | Thin `on_message` listener |
| `bot_modules/economy/game_rewards.py` | `pay_mention_award` |
| `web_server/routes/mention_awards.py` | Admin-gated CRUD API |
| `web_server/static/js/panels/config-mention-awards.js` | The chip-builder panel |
| `scripts/backfill_mention_awards.py` | One-off shape-based replay |
| `migrations/156_mention_awards.sql`, `157_mention_award_conditions.sql` | Table; chips conversion |
| `tests/test_mention_awards_logic.py` | Matcher + store |

## Hot Seat backfill (2026-08-07)

The live channel's history was replayed at 250 coins/turn: **15 turns, 14
members, 3,750 coins** (one member took the seat twice, 3 days apart). The
shape rule was measured at 15/15 precision over 2026-07-23..08-07 — 19 media
messages in the channel, 15 with exactly one mention, all 15 genuine
announcements. Re-running the script is a no-op.
