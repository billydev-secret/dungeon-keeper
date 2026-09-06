"""Member-facing copy for the GDPR disclosure report.

The data register (``docs/data_register.md``) knows *what* every table holds,
how long it is kept and whether erasure clears it. It does not speak to a
member: its cells read ``user_id + amounts + timestamps; meta sampled clean``.

This module supplies the other half — the ~15 categories a member can actually
read, and one authored paragraph each. The report joins the two: register facts,
copy language.

Adding a table to the register therefore needs no change here *unless* it brings
a new ``Feature`` value with it. ``tests/test_disclosure_service.py`` hard-fails
when a register Feature maps to no category, so a new feature cannot slip into a
member's report as an uncategorised table name.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    """One section of the member-facing report."""

    key: str
    title: str
    # What this is, in the member's terms. Second person, no table names.
    blurb: str
    # Why the server holds it at all — the Art 13(1)(c) purpose, plainly put.
    purpose: str
    # What survives an erasure request here, and on what ground. Authored
    # rather than lifted from the register: the register's Purge? cells carry
    # commit hashes, table names, live row counts and notes-to-self, none of
    # which belongs in a document sent to a member.
    preserved: str = ""


# Order is the order they appear in the report. Deliberately front-loaded with
# the categories a member is most likely to have come to ask about.
CATEGORIES: tuple[Category, ...] = (
    Category(
        "messages",
        "Your messages",
        "The text of messages you have sent in this server, along with when and "
        "where you sent them, any attachments or links they carried, who you "
        "mentioned, and the reactions they received. Messages you deleted in "
        "Discord are kept too, and stay readable to moderators.",
        "Message history powers the server's activity and XP features, the "
        "moderation record, and features like quotes and the starboard.",
        "Nothing. Your messages and everything attached to them are erased."
    ),
    Category(
        "activity",
        "Your activity and levels",
        "A running record of the XP you have earned — from messages, from "
        "reactions, and from time spent in voice channels — plus the daily "
        "totals it rolls up into, and your current level.",
        "This is what the levelling system is: without it your level could not "
        "be calculated or shown on a leaderboard.",
        "Nothing. Your XP history and level are erased."
    ),
    Category(
        "economy",
        "Your coins and purchases",
        "Your wallet balance and every transaction behind it: coins earned, "
        "spent, gifted and won, daily login streaks, quests you completed, "
        "items you bought, and any custom shop items or themes you own.",
        "A currency needs a ledger. Both sides of every transfer are kept so "
        "balances stay correct and disputes can be settled.",
        "The transaction ledger. A transfer has two sides, and deleting only yours would corrupt the balance of the person you traded with. It is kept as a financial record, not as a profile of you."
    ),
    Category(
        "games",
        "Games you have played",
        "Records from the games you have taken part in — rounds played, hands "
        "dealt, results, scores and rankings across the casino, duels, Mahjong, "
        "Guess, Risky Rolls, Survivor and the other game features.",
        "Game history keeps scores, leaderboards and statistics working, and "
        "lets a game resume rather than restart.",
        "Records of completed games where a result involved other players, so that their own game history and scores stay intact."
    ),
    Category(
        "social",
        "Who you interact with",
        "A record of who you talk to and react to: message replies, reactions "
        "given and received, mentions, who invited whom, and who followed whom "
        "between voice channels.",
        "These connections drive the server's community and attention reports, "
        "and features such as Mention Awards and the starboard.",
        "Records of who reacted to whom, which the server keeps as the evidence behind its community reports. This is the weakest of the grounds listed here and is under review."
    ),
    Category(
        "voice",
        "Voice channels",
        "Time spent in voice, the rooms you own or are trusted in, your voice "
        "room preferences, and — where a channel has transcription switched on "
        "— transcribed speech.",
        "Voice time earns XP, and room ownership and trust lists are what let "
        "you manage your own voice channel.",
        "Records of who followed whom between voice rooms, on the same basis as the reaction records above."
    ),
    Category(
        "profile",
        "Things you told us about yourself",
        "Information you entered yourself: your bio and its answers, your "
        "birthday if you set one, your music preferences, and settings you have "
        "chosen for individual features.",
        "You provided these so the server could show or use them. Nothing here "
        "was collected without you typing it in.",
        "Nothing. Everything you entered about yourself is erased."
    ),
    Category(
        "anonymous",
        "Anonymous posts you have made",
        "Whispers, confessions and other anonymous posts. These appear to "
        "others without your name — but the server does keep the link between "
        "you and what you posted, so that abuse can be dealt with.",
        "The link exists solely so an anonymous feature cannot be used to "
        "harass people with no accountability. Most of it self-deletes.",
        "The audit record of anonymous posts is kept, so that a report of abuse made against a post can still be investigated. The posts themselves, and your link to them, are removed."
    ),
    Category(
        "penpals",
        "Pen Pals",
        "Whether you opted in, who you were matched with, and any blocks you "
        "set. If you opted out, the record of that opt-out is kept so you are "
        "not matched again.",
        "Matching people needs a record of who is in the pool and who should "
        "not be paired.",
        "Any block you set, and the record that you opted out — both so that you are not matched again, and so a block you placed continues to protect the other person."
    ),
    Category(
        "wellness",
        "Wellbeing tools",
        "Your use of the wellness features: whether you opted in, usage limits, "
        "streaks, quiet periods and weekly summaries.",
        "These features are opt-in and their limits and streaks only work if "
        "your own usage is remembered.",
        "Nothing. Your wellness data is erased."
    ),
    Category(
        "moderation",
        "Your moderation record",
        "Warnings, mutes, jails, tickets you opened or were named in, rule "
        "events, and any automated content checks that flagged something you "
        "posted.",
        "A moderation record is the evidence behind decisions taken about your "
        "account, and the basis on which one could be challenged or reversed.",
        "Warnings, mutes, jails and tickets. These are the evidence behind decisions taken about your account: if a decision were ever challenged, this is the record it would be judged on. It is kept for that reason and no other."
    ),
    Category(
        "roles",
        "Roles and permissions",
        "Roles you have been granted, role menus you have used, permissions "
        "given to you over particular features, and your direct-message "
        "preferences.",
        "The server needs to know what you have access to in order to grant or "
        "refuse it.",
        "The record of permissions granted to you over server features, kept as the counterpart to whatever those permissions were then used to do."
    ),
    Category(
        "joining",
        "Joining and onboarding",
        "When you joined, how your onboarding progressed, greeting checks, and "
        "records relating to periods of inactivity.",
        "Onboarding has to remember where you got to, and inactivity handling "
        "needs to know when you were last seen.",
        "The fact that a join or an inactivity sweep happened, with your identity removed from it."
    ),
    Category(
        "usage",
        "How you use the bot",
        "Which commands and dashboard panels you have used, and when.",
        "Usage figures show which features are worth keeping and which are not "
        "working. They are about the features, not about judging you.",
        "Nothing. Your usage history is erased."
    ),
    Category(
        "admin",
        "Server administration",
        "Records created because of things you did as a moderator or admin — "
        "settings you changed, items you configured, tasks you signed off — "
        "along with small internal records that do not fit the categories above.",
        "These are the record of who configured a shared server surface, and "
        "the counterpart to whatever that surface then did.",
        "Records of settings you changed as a moderator or admin. These are about the server's configuration rather than about you, and they are the counterpart to whatever that setting then did."
    ),
)

CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}

# Register ``Feature`` cell -> category key. Every distinct Feature value in
# docs/data_register.md must appear here; the coverage test enforces it.
FEATURE_TO_CATEGORY: dict[str, str] = {
    "Message archive": "messages",
    "XP/activity": "activity",
    "XP rollup (migration 186)": "activity",
    "Economy": "economy",
    "Economy — Custom shop items": "economy",
    "Economy — Flash Themes": "economy",
    "Economy — emoji & sponsored QOTD": "economy",
    "Economy — live login digest": "economy",
    "Economy — quests & QOTD": "economy",
    "Casino": "games",
    "Games": "games",
    "Games — duels, group Hot Potato": "games",
    "External games": "games",
    "Meadow Mahjong": "games",
    "Guess": "games",
    "Risky Rolls": "games",
    "Survivor": "games",
    "Interaction graph": "social",
    "Reactions": "social",
    "Mention Awards": "social",
    "Starboard": "social",
    "Quote cards": "social",
    "Ping Response report": "social",
    "Voice Master": "voice",
    "Voice follow": "voice",
    "Voice transcription": "voice",
    "Bios": "profile",
    "Birthday": "profile",
    "Music Playlist": "profile",
    "Whisper": "anonymous",
    "Confessions": "anonymous",
    "Anon features": "anonymous",
    "Pen Pals": "penpals",
    "Wellness": "wellness",
    "Mod actions": "moderation",
    "Mod safety": "moderation",
    "Mod watch": "moderation",
    "Mod/audit misc": "moderation",
    "Policy Tickets": "moderation",
    "Rules watch": "moderation",
    "Image Guard": "moderation",
    "Role Grants": "roles",
    "Role menus": "roles",
    "DM perms": "roles",
    "Intake": "joining",
    "Greeting watch": "joining",
    "Inactivity": "joining",
    "Inactivity sweep": "joining",
    "Promotion": "joining",
    "Telemetry": "usage",
    "Admin-authored configuration": "admin",
    "QA tracker": "admin",
    "Todo / Mod Chores": "admin",
    "Small per-member stores": "admin",
    "LegitLibs": "admin",
    "Orphaned": "admin",
}

# Values the member supplied or chose themselves, safe to print back to them.
# (table, column, label). Deliberately narrow: nothing here can name a second
# member, so no Art 15(4) judgement is ever needed before sending the report.
SELF_AUTHORED_VALUES: tuple[tuple[str, str, str], ...] = (
    ("econ_wallets", "balance", "Wallet balance"),
    ("member_xp", "total_xp", "Total XP"),
    ("member_xp", "level", "Level"),
    ("econ_streaks", "current_streak", "Current login streak"),
    ("econ_streaks", "longest_streak", "Longest login streak"),
    ("bio_answers", "answer", "Bio answer"),
    ("bio_field_values", "value", "Bio field"),
    ("mahjong_prefs", "mode", "Mahjong play mode"),
    ("wellness_users", "timezone", "Wellness: your timezone"),
    ("pen_pals_pool", "joined_at", "Pen Pals: joined the pool"),
    ("voice_master_profiles", "saved_name", "Your saved voice room name"),
    ("member_birthdays", "preference", "Birthday: how you asked to be greeted"),
)

# A few values are spread across columns and need composing before they read as
# anything. ``bios`` is absent from this module entirely on purpose: the bio
# text lives in a Discord message, not in the table, so there is nothing here
# to print back.
_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


def composed_values(table: str, row: dict) -> list[tuple[str, str]]:
    """Values that only make sense assembled from more than one column."""
    if table == "member_birthdays":
        month, day = row.get("birth_month"), row.get("birth_day")
        if month and day and 1 <= int(month) <= 12:
            return [("Birthday you set", f"{int(day)} {_MONTHS[int(month) - 1]}")]
    return []
