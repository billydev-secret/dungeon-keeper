"""Pure quest math — no discord, no database (spec §4).

The claim ``period`` model, the library slot rule, the rotate-pool cursor,
the reward bands, and the trigger-phrase matcher. Everything is deterministic
on its inputs so the ISO week boundaries, slot matrix, rotation cycling, and
phrase-boundary rules stay table-testable.
"""

from __future__ import annotations

import hashlib
import random
import re
import statistics
from datetime import date, timedelta

# Library slot limits per guild. Daily/weekly active quests form a per-cadence
# *pool*: each member is shown/paid a personal subset of N drawn from that pool
# per period (see assigned_quest_ids), so the caps are a sanity ceiling on pool
# size, not a hard "one active" rule. Community goals and monthly goals are
# guild-wide and uncapped (their rotation owns how many run at once — monthly is
# a single lane per calendar month). Event quests are capped at 1 active PER
# TRIGGER KIND — the listener pays every active quest matching its trigger, so
# two same-kind actives would double-pay one occurrence.
POOL_CAP = 25
MAX_ACTIVE_DAILY = POOL_CAP
MAX_ACTIVE_WEEKLY = POOL_CAP
MAX_ACTIVE_EVENT_PER_KIND = 1

# Cadences that draw a *personal board* — a per-member subset of the pool.
# Membership here is the has-a-board predicate, kept separate from the board
# size: a size of 0 means "this guild shows none of this cadence", which is
# the opposite of community/monthly/event's "no board concept, guild-wide or
# every-occurrence". Conflating the two would make a disabled cadence pay
# everything. Monthly left this set when it became a guild-wide community-
# measured goal (migration 125) — it now settles in tiers, not per-member.
BOARD_CADENCES = frozenset({"daily", "weekly"})

# One-time member-setup trigger kinds. These fire once in a member's lifetime
# (setting a bio, saving a birthday) but we still want them to *appear* in the
# random daily board as a subtle welcome guide — a quest you're nudged to do
# once and that then quietly drops off. So a board-cadence quest on one of
# these kinds gets two special-cases in the service layer:
#   • it is claimed on a constant once-ever period (occurrence "set"), not the
#     calendar day, so re-saving a bio tomorrow can't re-earn it, and the
#     completing action always pays even if the quest wasn't drawn that day;
#   • it drops off a member's board once they've done the underlying thing
#     (bio row / birthday row exists) or already claimed it — so only members
#     who *haven't* done it ever see it.
# Kept here (pure) as the single source of truth; the DB-facing completion
# checks live in economy_quests_service.
SETUP_QUEST_KINDS = frozenset(
    {"bio_set", "birthday_set", "role_pick", "shop_purchase"}
)

# Default quests each member draws from each cadence's pool per period, when
# a guild hasn't tuned its own (EconSettings.quest_board_*). The repeat gap
# for a member is ~floor(poolsize / N) periods, so a bigger pool (or a
# smaller N) spaces repeats further apart.
PERSONAL_BOARD_SIZE: dict[str, int] = {"daily": 2, "weekly": 2}

# Ceiling on how many pending setup quests may be pinned onto one board
# (further capped to the board size). The excess isn't dropped — the pinned
# subset rotates through the pending set with the same per-user window walk
# as the draw itself, so every pending setup quest still surfaces within
# ~ceil(pending / cap) periods. Pinning shipped unbounded (2026-07-23) on
# the theory that a swamped board "converts to normal within days"; live
# data three days later said otherwise — 121 of 149 active members had all
# four setups pending, so every daily board was 100% pins and the random
# roll was invisible. The cap keeps the nudge without erasing the board;
# rotation answers the original objection to capping (ranking pins meant
# the last one would reach nobody).
MAX_SETUP_PINS = 2

# Game/module triggers a quest can be auto-completed by (label = how the
# dashboard describes it). On an *event* quest the trigger pays per
# occurrence (period "<kind>:<occurrence>", no time gate); on a daily/weekly
# quest it auto-claims the ordinary calendar period — "do it once today/this
# week". The firing side lives with each module: the photo-post listener in
# EconomyCog, the game-completion hooks in economy/game_rewards.py.
TRIGGER_KINDS: dict[str, str] = {
    "photo_post": "Post a photo in the Photo Challenge channel",
    "party_game": "Finish a party game",
    "game_host": "Host a party game that someone joins",
    "duel": "Finish a duel / PvP challenge",
    "risky_roll": "Take a Risky Roll dare",
    "casino_play": "Place a bet at the casino",
    "guess": "Play a Guess Who round",
    "voice_session": "Be active in voice chat",
    "qotd_reply": "Answer the Question of the Day",
    "starboard": "Get a message on the starboard",
    "invite": "Invite a new member",
    "boost": "Boost the server",
    "bio_set": "Set or update your bio",
    "media_post": "Post an image (optionally scoped to the trigger channel)",
    "pen_pal": "Get matched with a Pen Pal",
    "message_sent": "Send a message",
    "reply_sent": "Reply to someone's message",
    "reaction_given": "React to someone's message",
    "game_win": "Win a party game",
    "duel_win": "Win a duel / PvP challenge",
    "duel_lose": "Lose a duel / PvP challenge",
    "confession": "Post an anonymous confession",
    "ama_ask": "Ask a question in an AMA",
    "whisper": "Send an anonymous whisper",
    "quote": "Turn a message into a quote card",
    "chat_revive": "Answer a Chat Revive prompt",
    "bump": "Bump the server",
    "voice_room_host": "Host a voice room that draws guests",
    "pen_pal_complete": "See a Pen Pals session through to the end",
    "whisper_guess": "Correctly guess who sent a whisper",
    "guess_win": "Win a Guess Who round",
    "guess_post": "Submit a Guess Who round",
    "quoted": "Have your message turned into a quote card",
    "session_join": "Join a scheduled game session",
    "voice_message": "Post a voice message",
    "music_request": "Request a song",
    "birthday_set": "Set your birthday",
    "level_up": "Reach a new level",
    "ama_answer": "Answer a question in your AMA",
    "conversed": "Reply to different members",
    "replied_to": "Have different members reply to you",
    "reacted_to_member": "React to different members' messages",
    "channel_hop": "Talk in different channels",
    "active_day": "Be active on different days",
    "voice_partner": "Share voice with different members",
    "thread_deep": "Be part of a deep thread",
    "welcome": "Welcome a new member",
    "intake_step": "Tick a step on a newcomer's intake card",
    "conversation_starter": "Start a conversation that takes off",
    "cat_catch": "Catch a cat with Cat Bot",
    "mention_award": "Get named in a member-run game",
    "greeting_answered": "Answer someone's hello",
    "birthday_wish": "Wish a member happy birthday",
    "drop_claim": "Catch a coin drop",
    "role_pick": "Pick your roles from a role menu",
    "confession_reply": "Reply to a confession",
    "shop_purchase": "Make your first shop purchase",
    "daily_complete": "Complete daily quests",
}

# Warm one-liners for the leaderboard's community-goals block, shown in
# place of the functional TRIGGER_KINDS label (goal titles there are
# descriptive, so the dry label just repeated them). Display-only flavor:
# everything mechanical (dashboard copy, kickoff beat sheets, income-source
# docs) keeps using TRIGGER_KINDS / TRIGGER_KIND_INFO. A kind missing here
# falls back to its TRIGGER_KINDS label.
TRIGGER_FLAVOR: dict[str, str] = {
    "photo_post": "show us the world through your lens",
    "party_game": "play is better with company",
    "game_host": "every party needs someone to start it",
    "duel": "settle it with style",
    "risky_roll": "fortune favors the bold",
    "casino_play": "the table is always warmer with company",
    "guess": "trust those hunches",
    "voice_session": "your voice makes this place home",
    "qotd_reply": "your take makes the question worth asking",
    "starboard": "greatness gets noticed here",
    "invite": "bring a friend to the fire",
    "boost": "rocket fuel for all of us",
    "bio_set": "tell us who you are",
    "media_post": "brighten the feed",
    "pen_pal": "strangers are friends in waiting",
    "message_sent": "every word keeps the fire crackling",
    "reply_sent": "no message left hanging",
    "reaction_given": "a little love goes a long way",
    "game_win": "glory looks good on you",
    "duel_win": "champions are made here",
    "duel_lose": "losing bravely still counts",
    "confession": "shared secrets weigh less",
    "ama_ask": "curiosity is a gift",
    "whisper": "a little mystery keeps things fun",
    "quote": "immortalize the good stuff",
    "chat_revive": "no silence lasts long around here",
    "bump": "fly the flag for us",
    "voice_room_host": "open a door and see who wanders in",
    "pen_pal_complete": "see a friendship through",
    "whisper_guess": "nobody stays anonymous forever",
    "guess_win": "you know us too well",
    "guess_post": "keep the mystery coming",
    "quoted": "say something worth framing",
    "session_join": "showing up is half the magic",
    "voice_message": "good to hear your voice",
    "music_request": "add your song to our soundtrack",
    "birthday_set": "so we never miss your big day",
    "level_up": "onward and upward",
    "ama_answer": "spill it — we're all ears",
    "conversed": "make the rounds, spread the cheer",
    "replied_to": "write things worth answering",
    "reacted_to_member": "spread the love around",
    "channel_hop": "explore every corner of the map",
    "active_day": "keep showing up — it matters",
    "voice_partner": "new voices become old friends",
    "thread_deep": "down the rabbit hole together",
    "welcome": "first hellos set the tone",
    "intake_step": "walk someone through the front door",
    "conversation_starter": "light a spark, watch it catch",
    "cat_catch": "the cats won't catch themselves",
    "mention_award": "the whole room wants to know",
    "greeting_answered": "no hello goes unanswered",
    "birthday_wish": "make someone's whole day",
    "drop_claim": "quick hands, shiny prizes",
    "role_pick": "fly your colors",
    "confession_reply": "someone needed to hear that",
    "shop_purchase": "treat yourself — you've earned it",
    "daily_complete": "a little every day goes a long way",
}

# Longer per-kind copy for the Income Sources page: what fires it and what
# the event-quest occurrence key means for repeat payouts.
TRIGGER_KIND_INFO: dict[str, str] = {
    "photo_post": "Posting an image in the configured Photo Challenge channel — the post itself pays, no reactions needed. Event cadence: once per guild-local day.",
    "party_game": "Any party game completing with the member in the roster. Event cadence: once per game.",
    "game_host": "Running a party game that at least one other member joins (host of an empty game earns nothing — the anti-farm gate). Drives the host bounty faucet too. Event cadence: once per game hosted.",
    "duel": "A duel/PvP game resolving (chicken, hot potato, musical chairs, pressure cooker, quickdraw). Event cadence: once per match.",
    "risky_roll": "Pressing Roll in a Risky Rolls round. Event cadence: once per round.",
    "casino_play": "Placing any casino bet that is actually charged — every table, and a blackjack double-down as its own wager. Keyed to the stake's ledger row, so a rejected or refunded bet never counts. Event cadence: once per bet.",
    "guess": "Submitting a scored guess in a Guess Who round. Event cadence: once per round.",
    "voice_session": "Earning voice-activity XP (being in VC, not idle-muted). Event cadence: once per guild-local day.",
    "qotd_reply": "Earning the QOTD reward (first message in the QOTD channel that day). Event cadence: once per question.",
    "starboard": "Having a message cross the starboard threshold. Event cadence: once per starred message.",
    "invite": "A member you invited joining the server. Event cadence: once per distinct invitee — alt-farmable, enable with care.",
    "boost": "Starting a server boost. Event cadence: once per day it is detected.",
    "bio_set": "Saving or updating your member bio. Event cadence: once ever.",
    "media_post": "Posting a message with an image attached; set a trigger channel to scope it (e.g. #art). Event cadence: once per message — use daily/weekly for this one.",
    "pen_pal": "Being paired into a Pen Pals session (both members fire). Credited privately — no register-feed entry and no sign-off, since both halves fire together and two adjacent cards would name who was paired with whom. Event cadence: once per session.",
    "message_sent": "Any message in the server. Pair with a target count ('send 20 messages this week') — a target of 1 completes on the first message, and rewarding raw volume invites spam.",
    "reply_sent": "Using Discord's reply on someone ELSE's message (self-replies never count). Best with a target count.",
    "reaction_given": "Reacting to someone else's message — inherits the XP farm guard (one per message per reactor, ever; no self-reacts, no bots). Best with a target count.",
    "game_win": "Winning a party game (only types with a real winner resolve one: NHIE guiltiest, TTL best liar, Hot Takes hottest). Event cadence: once per game.",
    "duel_win": "Winning a duel/PvP match. Event cadence: once per match.",
    "duel_lose": "Not winning a duel/PvP match (every participant who wasn't the winner). Event cadence: once per match.",
    "confession": "Submitting an anonymous confession. The confessor is credited privately — no 'quest complete' message, no register-feed entry, and sign-off can't be turned on; the payout shows only on their own quest log and wallet (the sole other trace is the staff-side ledger row). Event cadence: once per confession — use daily/weekly with a target count.",
    "ama_ask": "Asking a question in an AMA. Unfiltered questions fire on submit; screened questions fire only once the host approves (rejected ones never pay). AMA questions are anonymous, so this is credited privately like `whisper` — no register-feed entry and no sign-off, or the card would name the asker seconds after their question posts. Event cadence: once per question — use daily/weekly with a target count.",
    "whisper": "Sending an anonymous whisper to another member. Credited privately like `confession` — no register-feed entry and no sign-off, so the payout can't be timed against the whisper landing in the feed. Event cadence: once per whisper — use daily/weekly with a target count.",
    "quote": "Turning someone's message into a quote card with the make-it-a-quote role (the quoter who invokes it is credited). Event cadence: once per quoted message — mildly farmable, so use daily/weekly with a target count.",
    "chat_revive": "Responding to a Chat Revive prompt while the lull window is open (the reply the revive service counts as an answer). Event cadence: once per prompt.",
    "bump": "Bumping the server on a listing site (the member who ran the bump command is credited). Event cadence: once per bump — bump cooldowns are the natural rate limit.",
    "voice_room_host": "Your Voice Control room reaching 2+ other members at once (bots and you excluded). Fires once per room lifetime, on the crossing. Event cadence: once per room.",
    "pen_pal_complete": "A Pen Pals session you were in reaching its natural end — both members fire; sessions that end early don't. Credited privately like `pen_pal`, and for the same pair-correlation reason. Event cadence: once per session.",
    "whisper_guess": "Correctly guessing who sent you an anonymous whisper. Event cadence: once per whisper.",
    "guess_win": "Winning a Guess Who round. Event cadence: once per round.",
    "guess_post": "Submitting a Guess Who round for others to solve (confession rounds count too). The submitter IS the answer, so this is credited privately — no register-feed entry, no sign-off; a card naming the earner would solve the round for everyone reading. Event cadence: once per submitted round.",
    "quoted": "Someone ELSE turning your message into a quote card (the quoted author is credited; self-quotes never fire). Event cadence: once per quoted message.",
    "session_join": "Joining a scheduled game session. Event cadence: once per session.",
    "voice_message": "Posting a voice message (the transcription listener is the detector). Event cadence: once per message — use daily/weekly with a target count.",
    "music_request": "Requesting a song in the music player. Capped at once per guild-local day by construction, so raw queue spam never multi-pays.",
    "birthday_set": "Saving your birthday. Event cadence: once ever — the bio_set pattern.",
    "level_up": "Reaching a new XP level. Event cadence: once per level reached.",
    "ama_answer": "Answering a question as the hot seat in your own AMA. Event cadence: once per question answered — use daily/weekly with a target count.",
    "conversed": "Replying to another member's message — each occurrence is that MEMBER, so a counted quest reads 'talk with N different people' (repeat replies to the same person never re-count in a period). Replies only, never bare mentions (mention spam is free; a reply is a real directed interaction).",
    "replied_to": "Someone else replying to YOUR message — the passive twin of conversed; occurrences are the repliers, so counted = 'have N different people reply to you'.",
    "reacted_to_member": "Reacting to a message by someone you haven't reacted to yet this period — occurrences are the message AUTHORS, so counted = 'spread reactions across N different members'. Inherits the reaction XP farm guard.",
    "channel_hop": "Posting in a channel (threads count toward their parent) — occurrences are the CHANNELS, so counted = 'talk in N different channels'. Gets members out of their one home channel.",
    "active_day": "Your first message of a guild-local day — occurrences are the DAYS, so a weekly counted quest reads 'show up any N days this week'. The gentle streak: skipping a day costs nothing but the day.",
    "voice_partner": "Sharing a voice channel with another member while you both earn voice XP (anti-idle rules apply) — occurrences are the PARTNERS, so counted = 'hang out in voice with N different people'.",
    "thread_deep": "Posting in a thread that has reached 20+ messages — once per thread. Rewards sustaining a deep conversation; everyone who posts after the crossing gets their credit.",
    "welcome": "Replying to a member who joined within the last 7 days — occurrences are the newcomers, so counted = 'welcome N new faces'. The retention quest.",
    "intake_step": "Ticking a step on a newcomer's intake card — the greeter who ticks it is credited, whether by the card's buttons or by the greeting that auto-ticks 'Greeted'. Steps that auto-tick from a role change (Verified, role grants) credit nobody, so they never pay. Each step pays once per card no matter how often it is toggled, and a skipped step pays nothing. Event cadence: once per card step. Distinct from 'welcome', which fires on replying to any recent joiner.",
    "conversation_starter": "Your message drawing replies from 3+ distinct members (self-replies and bots never count) — once per message, detected at reply ingest. Event cadence: once per qualifying message — use daily/weekly with a target count.",
    "cat_catch": "Catching a cat with the external Cat Bot in a channel tracked via `/games track watch … kind:Cat Bot`. The catch also pays rarity-tiered coins directly (common→divine); this trigger is the quest hook on top. Event cadence: once per catch (keyed on the catch message).",
    "mention_award": "Being @-mentioned in a message that matches a Mention Awards rule's conditions (trigger text, role ping, specific announcer…) — the watcher pays the member tagged. Built for games the bot does not host (the Hot Seat rotation, where the outgoing contestant announces the next one), so the announcement members already post is the payout event. Pays coins directly beside this trigger (the cat_catch pattern). Who may award is the rule's author chips; without one, anyone in the channel can. Event cadence: once per announcement (keyed on the message).",
    "greeting_answered": "Replying to or @mentioning a member whose greeting is still pending in Greeting Watch (same channel, inside the window). Needs Greeting Watch configured — no watched channels means this never fires. Event cadence: once per greeting.",
    "birthday_wish": "Wishing a member happy birthday on a day their birthday was announced — a reply/mention of the birthday member, or a birthday-wish phrase anywhere. Only publicly-announced birthdays count, so quiet birthdays never become quest bait. Event cadence: once per birthday member per day.",
    "drop_claim": "Winning a coin-drop Claim race. Pays beside the drop itself (the cat_catch pattern); the drop cadence is the natural rate limit. Event cadence: once per drop.",
    "role_pick": "Self-assigning a role via a role menu or an announcement role button. One-time setup quest (the bio_set pattern): claims once ever, drops off the board once done. Event cadence: once ever.",
    "confession_reply": "Posting an anonymous reply to someone ELSE's confession (replying to your own never fires). Credited privately like `confession` — no channel noise, no register-feed entry, no sign-off. Event cadence: once per reply — use daily/weekly with a target count.",
    "shop_purchase": "Making a shop purchase: perk rental, streak shield, emoji or QOTD sponsorship, raffle tickets (automatic renewal billing never fires). One-time setup quest teaching the earn→spend loop. Event cadence: once ever.",
    "daily_complete": "Any of the member's daily quests paying out — occurrences are the (quest, day) of the completed daily, so a weekly counted quest reads 'complete N dailies this week' with a progress bar. The board meta-quest: dailies are the check-offs, this is the progression. Not allowed on daily cadence (a daily that completes itself).",
}


# Suggested reward bands per quest type (community is judged by the author).
# Monthly was lowered from (75, 200) to (50, 90) after live data showed monthly
# quests were the richest per-claim faucet (see migration 103); the old ceiling
# let a single monthly pay ~150, several role-perk rentals from one click.
_REWARD_BANDS: dict[str, tuple[int, int]] = {
    "daily": (10, 20),
    "weekly": (25, 75),
    "monthly": (50, 90),
}


# Kinds whose occurrences are messages landing in a channel, so a
# channel-scoped quest can be sized fairly by scaling kind activity with the
# channel's share of `processed_messages` (media_post rides the same proxy —
# close enough, and errs low for media-heavy channels, the forgiving
# direction). Scoped quests on other kinds keep unscaled sizing.
CHANNEL_SHARE_KINDS = frozenset({"message_sent", "reply_sent", "media_post"})

# Kinds whose member is never named on a public surface, because naming them
# there gives away something the action itself was keeping — either directly,
# or by timing correlation ("X earned Send a Whisper" posted seconds after an
# anonymous whisper appears names the whisperer).
#
# Three surfaces enforce this, and a new one must opt in deliberately:
#   * community quests pay flat tiers only — no top-contributor bonus, no
#     names in the beat sheet (even the owner DM: it's written to be pasted
#     publicly);
#   * the register feed drops these payouts entirely (economy/register.py);
#   * sign-off is refused at config time, since a pending claim names the
#     claimant on the mods' todo board and its outcome would be announced in
#     the register (_check_trigger_config).
#
# Two distinct harms live here, and both are covered:
#
#   * anonymity — the member acted under a name the surface hides
#     (`confession`, `confession_reply`, `whisper`, `ama_ask`);
#   * correlation — the payout is public but pairing it with a second signal
#     reveals something neither shows alone. `pen_pal`/`pen_pal_complete` fire
#     for BOTH halves of a pairing in one transaction, so two adjacent cards
#     name not just who was matched but who *with*, while the room itself
#     defaults to mods-only visibility.
#
# `guess_post` is the sharpest case and is neither: in Guess Who the submitter
# **is** the answer (both call sites pass answer_id=submitter_id), so a card
# reading "**X** earned *Submit a Guess Who Round*" doesn't leak a private act
# — it hands the room the solution before anyone guesses. Missed here until
# 2026-08-17; live rounds were spoiled by it.
#
# Deliberately NOT here, so the next audit doesn't re-litigate them:
#   * `whisper_guess` — the guesser is the whisper's recipient, and the cog
#     already posts "@target solved the whisper!" to the feed, so naming them
#     costs nothing the game hasn't already said. Only the anonymous *sender*
#     needs covering.
#   * `guess` — the guesser is never the answer, and the card names no round,
#     so it reveals participation and nothing about the picture.
#   * `guess_win` — fires on the solve, which simultaneously edits the round
#     into a public embed naming answer, submitter and solver. Nothing left
#     to protect by the time the card lands.
#   * `bio_set` / `birthday_set` — both save data the bot then publishes
#     itself (a bio post, a birthday announcement); the `preference` column on
#     member_birthdays is a gift wish, not a privacy dial.
ANON_KINDS = frozenset({
    "confession",
    "confession_reply",
    "whisper",
    "ama_ask",
    "guess_post",
    "pen_pal",
    "pen_pal_complete",
})

# Community weekly milestone tiers, as fractions of the auto-sized target.
# Tier 1 is sized to be near-certain, tier 3 a genuine stretch; each tier
# crossed pays the quest's flat reward once (research: binary pass/fail
# community goals at small scale just produce attributable disappointment).
COMMUNITY_TIERS: tuple[float, ...] = (0.4, 0.7, 1.0)


def community_tiers_crossed(current: int, target: int) -> int:
    """How many milestone tiers a community counter has crossed (0-3)."""
    if target <= 0 or current <= 0:
        return 0
    frac = current / target
    return sum(1 for t in COMMUNITY_TIERS if frac >= t)


def community_auto_target(trailing_total: int, *, periods_in_window: float = 4.0) -> int:
    """Size a community goal from the guild's trailing-window kind total.

    ``trailing_total`` is the kind's activity over the sizing window (28 full
    days in practice); ``periods_in_window`` is how many *goal periods* that
    window spans — 4 for a weekly goal (28d ≈ 4 weeks, the default), 1 for a
    monthly goal (28d ≈ one month). target ≈ typical period ÷ 0.75, so an
    average period lands at ~75% (tier 2) and a visible push closes tier 3.
    Floor of 10 keeps a cold kind from producing a degenerate one-action goal.
    """
    typical = trailing_total / periods_in_window
    return max(10, round(typical / 0.75))


# thread_deep fires for posts in threads at or past this message count —
# deep enough to feel earned, common enough to happen weekly (2026-07-18
# choice: 20).
THREAD_DEEP_MIN = 20

# welcome fires for replies to members who joined within this window.
WELCOME_WINDOW_SECONDS = 7 * 86400

# conversation_starter fires when a message has drawn replies from this many
# distinct humans.
CONVERSATION_STARTER_REPLIERS = 3

# Personal dynamic-target stretch factor: a member's counted target is
# their own trailing-period median × this, clamped to the author's band —
# ~15% over their normal pace, so effort is comparable across members while
# reward stays flat (paying more for higher output would just re-reward the
# already-active).
DYNAMIC_STRETCH = 1.15


def dynamic_target(median_count: float, target_min: int, target_max: int) -> int:
    """Clamp a member's stretched trailing median into the author's band."""
    return max(target_min, min(target_max, round(median_count * DYNAMIC_STRETCH)))


# Kinds whose personal target resolves at the member's own trailing-period
# p25 instead of the stretched median. Reactions are passive one-click acts
# with a heavy-tailed distribution — the goal is "at least your own
# quiet-week level", so no stretch factor: median × 1.15 would turn the
# freebie fix into a grind on a heavy reactor's off week.
PERSONAL_P25_KINDS = frozenset({"reaction_given"})


def p25_target(counts: list[int], target_min: int, target_max: int) -> int:
    """Clamp a member's trailing-period p25 into the author's band.

    The quantile runs over ALL trailing periods, zeros included — the same
    convention as the median path, so a quiet week drags the target down
    and it stays attainable in a typical-to-slow week. Never below 1.
    """
    if len(counts) >= 2:
        p25 = statistics.quantiles(counts, n=4)[0]
    else:
        p25 = float(counts[0]) if counts else 0.0
    return max(target_min, min(target_max, max(1, round(p25))))


def tier_echo_line(
    tier: int, current: int, target: int, contributors: int
) -> str:
    """The public line for a community goal crossing a milestone tier.

    Not a beat sheet — this one goes out as-is, as the detail line of an Event
    Echo. It is the "Suggested post" the host's tier sheet used to carry, kept
    in the same voice: that sheet existed to hand a human words worth posting,
    so when the bot took the post over (2026-07-29) the move was to keep the
    voice and drop the middleman, not to write a second voice for one event.

    Two things the handover changed. The goal's title is gone — the echo's
    headline carries it, and the sheet only repeated it because a pasted line
    has no headline above it. And the last tier no longer promises a next one:
    a host would have caught "next tier's on the board" under a full clear,
    an unedited bot post would not.
    """
    pct = round(100 * current / target) if target else 0
    if tier >= len(COMMUNITY_TIERS):
        return (
            f"🎉 **Tier {tier} down** — {pct}%, a full clear. "
            f"{contributors} of you got us there, and every tier is banked!"
        )
    return (
        f"🎉 **Tier {tier} down** — {pct}% and climbing, {contributors} of you "
        f"have chipped in. Payout secured for everyone; next tier's on the board!"
    )


# ── Community-weekly beat sheets ──────────────────────────────────────
# DMed to the host (not posted publicly): the numbers plus suggested copy
# they can paste or rewrite in their own voice. Pure string builders so the
# copy stays table-testable. Tier crossings used to be one of these; they are
# now echoed to main chat directly (`tier_echo_line` above), so what is left
# here is the kickoff, the final-24h nudge and the end-of-period resolution —
# the beats that still want a human writing them.


def beat_kickoff(title: str, kind_label: str, target: int, week: str) -> str:
    return (
        f"🎬 **Community weekly kicked off** ({week})\n"
        f"**{title}** — {kind_label}\n"
        f"Target: **{target}** · tiers at 40% / 70% / 100%, each tier pays "
        f"everyone.\n\n"
        f"Suggested post:\n"
        f"> 📣 New community goal this week: **{title}**! Every one of us "
        f"counts toward it — {kind_label.lower()}. Hit {target} together "
        f"and everyone gets paid three times over. Progress lives on the "
        f"leaderboard. Go!"
    )


def beat_final24(title: str, current: int, target: int) -> str:
    pct = round(100 * current / target) if target else 0
    need = max(0, target - current)
    return (
        f"⏳ **Final 24h** — {title}\n"
        f"{current}/{target} ({pct}%) · {need} to go for the full clear\n\n"
        f"Suggested post:\n"
        f"> ⏰ Last day on **{title}** — we're at {pct}%. {need} more and "
        f"it's a full clear. One push!"
    )


def beat_resolution(summary: dict) -> str:
    title = summary["title"]
    crossed = summary["tiers_crossed"]
    current, target = summary["current"], summary["target"]
    contributors = summary["contributors"]
    top = summary["top_contributors"]
    bonus_paid = summary["bonus_paid"]
    pct = round(100 * int(current) / int(target)) if target else 0
    tier_word = f"{crossed}/3 tiers" if crossed else "no tiers"
    if summary.get("anonymous"):
        # Anonymous kind: the sheet is written to be pasted publicly, so it
        # must carry no names — and no bonus was paid to have names for.
        return (
            f"🏆 **Community weekly resolved** — {title}\n"
            f"Final: {current}/{target} ({pct}%) → **{tier_word}** paid to "
            f"every active member ({summary['reward_per_tier']} per tier).\n"
            f"Contributors: {contributors} (anonymous kind — no top list, "
            f"no bonus)\n\n"
            f"Suggested post:\n"
            f"> 🏆 **{title}** is in the books: {pct}% and {tier_word} "
            f"cleared — payouts are in your wallets. No shout-outs on this "
            f"one; you know who you are. 🤫 All {contributors} of you moved "
            f"the bar. Next goal after a breather week. 💰"
        )
    top_lines = "\n".join(
        f"  {i + 1}. <@{uid}> — {n}" for i, (uid, n) in enumerate(top)
    ) or "  (nobody)"
    return (
        f"🏆 **Community weekly resolved** — {title}\n"
        f"Final: {current}/{target} ({pct}%) → **{tier_word}** paid to every "
        f"active member ({summary['reward_per_tier']} per tier).\n"
        f"Contributors: {contributors}\n"
        f"Top contributors{' (bonus paid)' if bonus_paid else ''}:\n"
        f"{top_lines}\n\n"
        f"Suggested post:\n"
        f"> 🏆 **{title}** is in the books: {pct}% and {tier_word} cleared — "
        f"payouts are in your wallets. Shout-out to our top contributors "
        f"{' '.join(f'<@{uid}>' for uid, _ in top) or '…nobody?!'} and all "
        f"{contributors} of you who moved the bar. Next goal after a "
        f"breather week. 💰"
    )


def iso_week_for(local_day: str) -> str:
    """Return the ISO week ("YYYY-Www") a guild-local calendar day falls in.

    Uses the ISO year from ``date.isocalendar()``, not the calendar year, so
    the year-rollover boundary is correct — 2026-12-31 is 2027-W01 and
    2027-01-01 can be 2026-W53.
    """
    iso = date.fromisoformat(local_day).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def previous_local_day(local_day: str) -> str:
    """The guild-local calendar day before ``local_day`` (both "YYYY-MM-DD").

    Used to diff a day's community-progress snapshot against the prior day's
    for the login digest's "biggest movers yesterday" section.
    """
    return (date.fromisoformat(local_day) - timedelta(days=1)).isoformat()


def month_for(local_day: str) -> str:
    """The calendar month ("YYYY-MM") a guild-local day falls in.

    Plain calendar months — a monthly quest's window opens on the 1st at
    guild-local midnight, no ISO-style shifting.
    """
    return local_day[:7]


def quest_period(qtype: str, local_day: str) -> str:
    """The claim period key for a quest type on a given guild-local day.

    Daily → the local day; weekly → its ISO week; community → the constant
    ``'once'`` (a community quest is claimed/settled once, not per period).
    Re-claimability falls straight out of this key — no reset sweeps.
    """
    if qtype == "daily":
        return local_day
    if qtype == "weekly":
        return iso_week_for(local_day)
    if qtype == "monthly":
        return month_for(local_day)
    if qtype == "community":
        return "once"
    # Event quests have no calendar period — the trigger listener supplies a
    # per-occurrence key (see occurrence_period), so a calendar lookup is a bug.
    raise ValueError(f"unknown quest type: {qtype!r}")


def occurrence_period(kind: str, occurrence: str) -> str:
    """The claim period key for one trigger occurrence on an *event* quest.

    Keyed to the occurrence (a photo card, one game, one duel …), not the
    calendar: each occurrence pays each member at most once, forever.
    """
    return f"{kind}:{occurrence}"


def can_activate(existing_active: list[str], qtype: str) -> bool:
    """True if activating one more ``qtype`` quest respects the slot rule.

    ``existing_active`` is the list of qtypes of the guild's currently-active
    quests (excluding the one under consideration). Community and monthly are
    guild-wide goals whose rotation owns concurrency, so they're uncapped here.
    """
    if qtype == "daily":
        return existing_active.count("daily") < MAX_ACTIVE_DAILY
    if qtype == "weekly":
        return existing_active.count("weekly") < MAX_ACTIVE_WEEKLY
    if qtype in ("community", "monthly"):
        return True
    if qtype == "event":
        # Callers gate event quests per trigger kind via can_activate_event;
        # type-level there is no cap (one photo + one duel event is fine).
        return True
    raise ValueError(f"unknown quest type: {qtype!r}")


def can_activate_event(existing_event_kinds: list[str], trigger_kind: str) -> bool:
    """True if activating one more event quest of this kind respects the cap.

    ``existing_event_kinds`` is the trigger kinds of the guild's currently
    active event quests (excluding the one under consideration). One active
    per kind — the listener pays every matching quest, so two same-kind
    actives would double-pay one occurrence.
    """
    return existing_event_kinds.count(trigger_kind) < MAX_ACTIVE_EVENT_PER_KIND


def pick_rotation(pool_ids: list[int], current_id: int | None) -> int | None:
    """The next quest id to activate when cycling a rotate-tag pool.

    Cycles by ascending id: the id after ``current_id`` wrapping around. A
    pool of one (or empty) has nowhere to rotate → None. When ``current_id``
    is not in the pool, start at the first id.
    """
    ordered = sorted(set(pool_ids))
    if len(ordered) <= 1:
        return None
    if current_id is None or current_id not in ordered:
        return ordered[0]
    idx = ordered.index(current_id)
    return ordered[(idx + 1) % len(ordered)]


def reward_band(qtype: str) -> tuple[int, int] | None:
    """The suggested (low, high) reward range for a quest type, or None.

    Advisory only — the dashboard warns out-of-band but saves anyway.
    Community has no band (author's call).
    """
    return _REWARD_BANDS.get(qtype)


# ── per-user quest board (spec §4.6) ──────────────────────────────────


def _seed(*parts: object) -> int:
    """A stable 64-bit seed from the parts — same across processes/versions.

    ``hash()`` is salted per-process and ``random.seed(str)`` isn't guaranteed
    stable, so we hash explicitly. Determinism is what makes the board a pure
    function of ``(user, period)`` with no stored assignment table.
    """
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:16], 16)


def period_index(qtype: str, local_day: str) -> int:
    """A monotonic integer index for the period ``local_day`` falls in.

    One integer per daily/weekly/monthly period, increasing over time — the
    board walks the per-user pool by this index, so a member's set advances
    exactly once per period and never mid-period (counted progress can't
    fragment). Community/event have no calendar period and raise.
    """
    d = date.fromisoformat(local_day)
    if qtype == "daily":
        return d.toordinal()
    if qtype == "weekly":
        iso = d.isocalendar()
        return iso.year * 53 + iso.week
    if qtype == "monthly":
        return d.year * 12 + (d.month - 1)
    raise ValueError(f"quest type has no board period: {qtype!r}")


def assigned_quest_ids(
    pool_ids: list[int], user_id: int, index: int, n: int
) -> list[int]:
    """The ``n`` quest ids a member draws from a cadence pool for a period.

    The pool is shuffled deterministically per member (so two members get
    different sets), then walked ``n``-at-a-time by ``index``, spacing a
    member's repeats roughly ``floor(len/n)`` periods apart — exactly a full
    cycle when ``n`` divides the pool size, approximate otherwise (e.g. len 5,
    n 2 recurs some ids every 2 periods). ``n >= len`` (or a tiny pool)
    degrades gracefully to "the whole pool". Returns sorted ids.
    """
    ordered = sorted(set(pool_ids))
    m = len(ordered)
    if m == 0 or n <= 0:
        return []
    if n >= m:
        return ordered
    # Per-member shuffle: order the pool by a per-(user, quest) hash.
    shuffled = sorted(ordered, key=lambda q: _seed(user_id, q))
    start = (index * n) % m
    picked = [shuffled[(start + i) % m] for i in range(n)]
    return sorted(picked)


def pair_map(tagged: dict[int, str]) -> dict[int, int]:
    """Quest-id → partner-id for tags shared by EXACTLY two quests.

    ``tagged`` maps pool quest ids to their pair_tag ('' = untagged). A tag
    carried by one quest (partner inactive/deleted) or by three-plus is
    inert — a strict rule beats guessing which two of three were meant.
    """
    by_tag: dict[str, list[int]] = {}
    for qid, tag in tagged.items():
        if tag:
            by_tag.setdefault(tag, []).append(qid)
    out: dict[int, int] = {}
    for ids in by_tag.values():
        if len(ids) == 2:
            a, b = sorted(ids)
            out[a], out[b] = b, a
    return out


def apply_pair_bundles(picked: list[int], pairs: dict[int, int]) -> list[int]:
    """Complete pairs on a drawn board: a picked quest pulls in its partner.

    Walking picked ids in sorted order, a quest whose partner is absent
    swaps the partner in for the LAST slot that isn't itself part of an
    honored pair — deterministic, so the board stays a pure function of
    (pool, member, period). A board of one can't hold a pair and is left
    alone; if every other slot is already pair-locked, the odd quest keeps
    its solo slot. Returns sorted ids, same length as ``picked``.
    """
    out = sorted(picked)
    locked: set[int] = set()
    # Pass 1: pairs the draw already completed are untouchable — another
    # quest pulling ITS partner in must never split them.
    for qid in out:
        if qid in pairs and pairs[qid] in out:
            locked.update((qid, pairs[qid]))
    # Pass 2: complete the rest, lowest id first, displacing from the end.
    for qid in list(out):
        if qid in locked or qid not in pairs:
            continue
        partner = pairs[qid]
        for i in range(len(out) - 1, -1, -1):
            if out[i] != qid and out[i] not in locked:
                out[i] = partner
                locked.update((qid, partner))
                break
    return sorted(out)


def has_board(qtype: str) -> bool:
    """Whether this cadence draws a personal board at all.

    True for daily/weekly/monthly regardless of the configured size — a
    guild that set the size to 0 still *has* a board, it's just empty. Gate
    board filtering on this, never on ``board_size(...) > 0``.
    """
    return qtype in BOARD_CADENCES


def board_size(qtype: str, sizes: dict[str, int] | None = None) -> int:
    """How many quests a member draws from this cadence's pool per period.

    ``sizes`` overrides the defaults per cadence (the guild's configured
    board sizes); a cadence absent from it falls back to the default. 0 is a
    meaningful value — the cadence is off for this guild.
    """
    if sizes is not None and qtype in sizes:
        return sizes[qtype]
    return PERSONAL_BOARD_SIZE.get(qtype, 0)


def effective_target(
    target_count: int,
    target_min: int,
    target_max: int,
    *,
    user_id: int,
    quest_id: int,
    period: str,
) -> int:
    """A counted quest's target for one member+period.

    With a band (``0 < target_min < target_max``) the target is drawn from a
    Gaussian anchored in the LOWER third of the band, clamped to ``[min, max]``
    — deterministic on ``(user, quest, period)`` so it's stable all period and
    varies run to run. Without a band it's the fixed ``target_count``. Never
    below 1.

    This is the cold-start path: it only runs for a member with too little
    trailing history to size from their own pace. That member is new or quiet,
    and the warm median/p25 path this stands in for would clamp them toward
    ``target_min`` — so centering the cold-start draw on the band MIDPOINT made
    the fallback harder than the real thing it replaces. Anchoring near the
    floor keeps a newcomer's first target attainable while sigma still spreads
    members apart.
    """
    if not (0 < target_min < target_max):
        return max(1, int(target_count))
    rng = random.Random(_seed(user_id, quest_id, period))
    mu = target_min + (target_max - target_min) / 3
    # ~95% of the mass lands inside the band before clamping; the tails clamp.
    sigma = (target_max - target_min) / 4 or 1
    draw = round(rng.gauss(mu, sigma))
    return max(target_min, min(target_max, draw))


# ── trigger-phrase verification (spec §4.4) ───────────────────────────


def parse_trigger_words(raw: str) -> list[str]:
    """Split a stored ``trigger_words`` value into clean phrases.

    Phrases are separated by commas or newlines; surrounding whitespace is
    stripped, internal runs of whitespace collapse to one space, and
    duplicates (case-insensitive) keep their first occurrence.
    """
    seen: set[str] = set()
    out: list[str] = []
    for chunk in re.split(r"[,\n]", raw or ""):
        phrase = " ".join(chunk.split())
        if not phrase:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(phrase)
    return out


def compile_trigger_pattern(words: list[str]) -> re.Pattern[str] | None:
    """One case-insensitive pattern matching any phrase as a whole word.

    ``(?<!\\w)…(?!\\w)`` instead of ``\\b`` so phrases that start or end with
    non-word characters (e.g. ``:wave:``) still anchor correctly, and "gm"
    never matches inside "dogma". Whitespace inside a phrase matches any
    whitespace run. None when there are no phrases.
    """
    if not words:
        return None
    alternatives = [
        r"\s+".join(re.escape(token) for token in phrase.split())
        for phrase in words
    ]
    return re.compile(
        r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)",
        re.IGNORECASE,
    )


def message_matches_trigger(content: str, pattern: re.Pattern[str] | None) -> bool:
    """True when a message body contains one of the quest's trigger phrases."""
    return bool(pattern is not None and content and pattern.search(content))
