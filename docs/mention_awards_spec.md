# Mention Awards

**Reference spec** — matches current behavior.

Pays currency to whoever gets @-mentioned alongside a configured trigger
phrase. Built for games the bot **does not host**: where members run the game
themselves, the announcement they already post is a clean, machine-readable
payout event.

## Why it exists

The prompting case is a Hot Seat rotation run by hand in a channel — one
member in the seat at a time, and the *outgoing* contestant announces the next
one with a card image and a ping:

> **@Hot Seat** your turn **@turbodog8**! Let's all find out more about him!
> What's your favourite thing about yourself?

Dungeon Keeper hosts none of it, so no game hook can pay the contestant. But
the handoff is unambiguous, and the bot already sees it.

Rather than hardcode Hot Seat, a rule is four levers. A second member-run game
needs a second row, not a second feature.

## The four levers

| Lever | Meaning |
|---|---|
| **Channel** | Only messages here can award. |
| **Trigger phrase** | Case-insensitive substring of the message. Empty never matches. |
| **Amount** | Coins paid to the member mentioned. `0` parks the rule without deleting it. |
| **Who can award** | A role the announcer must hold. **Unset means anyone in the channel can hand out currency** — see Anti-farm. |

Configured at **Economy → Mention Awards** (`mention-awards`), admin-gated.
Rules are matched in creation order; the first match wins, because the payout
ledger is keyed on the message and one message can only pay once regardless.

## What counts as an award

A message awards when **all** hold:

1. It's in the rule's channel.
2. The author is not a bot.
3. The rule's amount is at least 1.
4. The content contains the phrase (case-insensitive).
5. The author holds the announcer role, if one is set.
6. The message @-mentions **exactly one** member.
7. That member is not the author.

Rule 6 is deliberate: a message tagging several people is a group shout, and
guessing which of them the phrase referred to would pay the wrong member.
Rule 7 closes the only farm the design is otherwise wide open to.

## Privacy: content is read, never stored

The phrase is matched against `message.content` live off the gateway and
discarded. Nothing in this feature writes content, and the guild's
message-storage level is untouched.

The consequence is that **a phrase rule cannot be replayed over history** —
banked messages have no content to re-match. The one-off backfill
(`scripts/backfill_mention_awards.py`) therefore matches on message *shape*
instead: `media_kind` plus the @-mention edges, both of which survive with
content storage off. That seam is why the backfill is a script rather than a
mode of the feature.

## Payout

`economy/game_rewards.pay_mention_award` — the `pay_cat_catch` shape: coins
credited directly, plus the `mention_award` quest trigger on top. Inherits
every faucet guarantee: no-op when the economy is off or the member is a
bot/unresolvable, booster multiplier applied, failures logged not raised.

**Idempotency** reuses `games_external_payouts` (`message_id` PK, `kind`
discriminator) rather than adding a second ledger — Hot Seat is an external
game in the sense that matters: the bot doesn't host it, and each announcement
pays exactly once. The claim is taken *before* the credit, so an edit
re-firing the listener cannot double-pay.

Dedupe is **per announcement only**. A member who takes the seat again later
is paid again — the right reading of two genuine turns.

## Anti-farm

The honest summary: with **Who can award** unset, any member can pay any other
member by typing the phrase and tagging them. That is the permissive setting,
and it is the correct one for a baton-pass game (the outgoing contestant holds
no special role, and gating on the game's owner would drop about a third of
real announcements — measured on live traffic). Set the role when the game has
fixed hosts.

What is always enforced: no self-awards, no bot authors, exactly one mention,
one payout per message.

## Files

| Path | Role |
|---|---|
| `bot_modules/mention_awards/logic.py` | Pure matching — the whole safety surface |
| `bot_modules/mention_awards/store.py` | Rule CRUD + validation |
| `bot_modules/cogs/mention_awards_cog.py` | Thin `on_message` listener |
| `economy/game_rewards.py` | `pay_mention_award` |
| `web_server/routes/mention_awards.py` | Admin-gated CRUD API |
| `web_server/static/js/panels/config-mention-awards.js` | The panel |
| `scripts/backfill_mention_awards.py` | One-off shape-based replay |
| `migrations/156_mention_awards.sql` | `mention_award_rules` |
| `tests/test_mention_awards_logic.py` | Matcher + store |

## Hot Seat backfill (2026-08-07)

The live channel's history was replayed at 250 coins/turn: **15 turns, 14
members, 3,750 coins** (one member took the seat twice, 3 days apart). The
shape rule was measured at 15/15 precision over 2026-07-23..08-07 — 19 media
messages in the channel, 15 with exactly one mention, all 15 genuine
announcements. Re-running the script is a no-op.
