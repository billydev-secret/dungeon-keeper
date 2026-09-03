"""Community ballots on policy proposals — the rules, with no Discord in them.

A **community ballot** is the members' counterpart to the mod team's policy
vote. An admin launches one in an ordinary channel; the bot opens a thread
there and posts a tally card with Yes / No / Abstain buttons. The electorate is
simply *whoever can see that thread* — there is no role dial, because a
veterans-only ballot is a ballot launched in a veterans-only channel and the
channel's own permissions are already the cleanest expression of who is in the
room. It passes on a **simple majority**: abstentions count for neither side,
ties fail, and there is no minimum turnout, so a ballot always resolves.

Deliberate distances from the mod vote in `services/moderation.py`:

* **Its own tables.** The arithmetic is incompatible (unanimity over a fixed
  roster vs. a majority of whoever turned up), and `policy_votes` carries no
  `guild_id`, which forces a parent join in every privacy path. See
  migration 202 for the full argument.
* **Nothing is written to `policies`.** A passed ballot is *recorded*, not
  enacted. Turning one into a policy is a later, human act.
* **It outlives the mod ticket.** Resolving a policy vote deletes the private
  proposal channel; a ballot's thread is somewhere else entirely and its record
  is a row, so neither is touched.

**No-contact is deliberately not consulted here, and that is a decision, not an
omission.** The tally is fully public and names every voter, so two members who
have blocked each other will appear in the same list. That was put to Billy
explicitly and taken: a ballot is a one-to-many broadcast with no pairing, no
directed edge, no DM and no reply — and both members can already post in the
channel it was launched in. What would reopen the question is *adding* a
contact edge, so a ballot must never send a DM, ping a member, or grow any
per-pair surface. Anything of that shape needs the full
`no_contact_partners_conn` treatment before it ships.
"""

from __future__ import annotations

import sqlite3
import time
from typing import TypedDict

# ── Vocabulary ────────────────────────────────────────────────────────

CHOICE_YES = "yes"
CHOICE_NO = "no"
CHOICE_ABSTAIN = "abstain"
#: Every choice a ballot button can record. Order is display order.
BALLOT_CHOICES: tuple[str, ...] = (CHOICE_YES, CHOICE_NO, CHOICE_ABSTAIN)

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
#: The ballot's proposal was closed (or the thread torn down) before its
#: deadline. Counts are still frozen so the record says what the room had said
#: so far, but no result is claimed from it.
OUTCOME_CANCELLED = "cancelled"

#: The `policy_tickets.status` a ballot's own ticket row wears while it runs.
#: Outside the ('open', 'voting') set `get_policy_ticket_by_channel` matches,
#: so `/policy vote` can never start the unanimity mod-vote on a ballot thread
#: — whose finalizer would then archive and delete that thread.
TICKET_STATUS_BALLOT = "ballot"


class BallotRow(TypedDict):
    id: int
    guild_id: int
    policy_id: int
    channel_id: int
    thread_id: int
    message_id: int
    question: str
    opened_by: int
    opened_at: float
    closes_at: float
    closed_at: float | None
    closed_by: int | None
    yes_count: int | None
    no_count: int | None
    abstain_count: int | None
    outcome: str | None


class BallotVoteRow(TypedDict):
    ballot_id: int
    guild_id: int
    user_id: int
    choice: str
    cast_at: float


class BallotTally(TypedDict):
    yes: list[int]
    no: list[int]
    abstain: list[int]


# ── Pure rules ────────────────────────────────────────────────────────


def normalise_choice(choice: str) -> str:
    """Return a canonical ballot choice, or raise ``ValueError``.

    The buttons can only produce the three, but the custom-id they arrive on is
    attacker-supplied text in principle, and the column is a bare TEXT — one
    stray value would sit in the tally counting toward nothing and reading as a
    bug in the arithmetic rather than in its input.
    """
    value = (choice or "").strip().lower()
    if value not in BALLOT_CHOICES:
        raise ValueError(f"not a ballot choice: {choice!r}")
    return value


def tally_choices(votes: list[BallotVoteRow] | list[dict]) -> BallotTally:
    """Bucket cast votes into yes / no / abstain voter-id lists.

    Ids are sorted so a tally card redrawn after every press names people in a
    stable order instead of reshuffling the list under the reader.
    """
    buckets: BallotTally = {"yes": [], "no": [], "abstain": []}
    for row in votes:
        choice = str(row["choice"])
        if choice in buckets:
            buckets[choice].append(int(row["user_id"]))  # type: ignore[literal-required]
    for key in buckets:
        buckets[key].sort()  # type: ignore[literal-required]
    return buckets


def ballot_outcome(yes: int, no: int) -> str:
    """Simple majority of the votes that took a side. **Ties fail.**

    Abstentions are not passed in at all: they count toward neither side, which
    is what abstaining means, and the member-facing copy has to say so because
    the other common reading (abstain = no) is a reasonable guess.

    Strictly greater, never ``>=``: 20-20 fails, and so does 0-0, which is what
    makes "a ballot nobody voted in" resolve rather than hang. There is no
    minimum turnout to check — Billy's call, on the grounds that a first ballot
    drawing nine votes and failing a quorum reads as a rejection of something
    the room never saw.
    """
    return OUTCOME_PASSED if yes > no else OUTCOME_FAILED


def is_open(ballot: BallotRow | dict) -> bool:
    """True while a ballot is still accepting votes."""
    return ballot.get("closed_at") is None


def is_expired(ballot: BallotRow | dict, now: float) -> bool:
    """True when an open ballot has passed its deadline.

    ``closes_at`` of 0 means the guild's voting-deadline dial is 0 — auto
    resolution off, exactly as it is for the mod vote. Such a ballot never
    expires; a moderator's Close press is the only way it ends.
    """
    closes_at = float(ballot.get("closes_at") or 0)
    return is_open(ballot) and closes_at > 0 and now >= closes_at


def can_cast(*, ballot: BallotRow | dict, is_bot: bool, can_view_thread: bool) -> bool:
    """Whether this presser may vote in this ballot, right now.

    Three conditions, each of which has to be checked at *press* time rather
    than trusted from open time:

    * the ballot is still open — a press racing the close must not land;
    * the presser is not a bot;
    * the presser can currently see the thread. Visibility is the whole
      electorate rule, and it moves: a member who loses the role that let them
      into the channel stops being able to vote from that moment (their already
      cast vote stands — see the module docstring's edge cases). A member who
      *gains* access mid-ballot may vote, which is a good: turnout is not
      capped against a frozen denominator because there is no denominator.
    """
    return is_open(ballot) and not is_bot and can_view_thread


# ── Storage ───────────────────────────────────────────────────────────


def open_ballot(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    policy_id: int,
    channel_id: int,
    question: str,
    opened_by: int,
    closes_at: float,
    now: float | None = None,
) -> int:
    """Record a new ballot and return its id."""
    opened_at = time.time() if now is None else now
    cur = conn.execute(
        """
        INSERT INTO policy_ballots (
            guild_id, policy_id, channel_id, question, opened_by,
            opened_at, closes_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (guild_id, policy_id, channel_id, question, opened_by, opened_at, closes_at),
    )
    return int(cur.lastrowid or 0)


def attach_ballot_message(
    conn: sqlite3.Connection, ballot_id: int, *, thread_id: int, message_id: int
) -> None:
    """Record where the ballot's thread and tally card ended up.

    Written after the row, not with it: the row has to exist before the buttons
    can carry its id, and the thread has to exist before the card can be posted
    into it. A ballot whose message send fails therefore still has a row, which
    is the outcome we want — the close path works from the database and only
    *tries* to edit the card.
    """
    conn.execute(
        "UPDATE policy_ballots SET thread_id = ?, message_id = ? WHERE id = ?",
        (thread_id, message_id, ballot_id),
    )


def get_ballot(conn: sqlite3.Connection, ballot_id: int) -> BallotRow | None:
    row = conn.execute(
        "SELECT * FROM policy_ballots WHERE id = ?", (ballot_id,)
    ).fetchone()
    return dict(row) if row else None  # type: ignore[return-value]


def get_open_ballot_for_policy(
    conn: sqlite3.Connection, policy_id: int
) -> BallotRow | None:
    row = conn.execute(
        "SELECT * FROM policy_ballots WHERE policy_id = ? AND closed_at IS NULL "
        "ORDER BY id LIMIT 1",
        (policy_id,),
    ).fetchone()
    return dict(row) if row else None  # type: ignore[return-value]


def list_ballots(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 200
) -> list[BallotRow]:
    """This guild's ballots, newest first — what the dashboard renders."""
    rows = conn.execute(
        "SELECT * FROM policy_ballots WHERE guild_id = ? "
        "ORDER BY opened_at DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[misc]


def cast_ballot_vote(
    conn: sqlite3.Connection,
    *,
    ballot_id: int,
    guild_id: int,
    user_id: int,
    choice: str,
    now: float | None = None,
) -> bool:
    """Record (or change) one member's vote. False if the ballot has closed.

    Changing a vote is a plain upsert on ``(ballot_id, user_id)``, so pressing
    a second button replaces the first rather than stacking. The closed check
    and the write share this connection, so a press that arrives while the
    sweep is closing the ballot either lands before the freeze or is refused —
    it cannot slip in after the counts were frozen.
    """
    value = normalise_choice(choice)
    ballot = get_ballot(conn, ballot_id)
    if ballot is None or not is_open(ballot):
        return False
    cast_at = time.time() if now is None else now
    conn.execute(
        """
        INSERT INTO policy_ballot_votes (ballot_id, guild_id, user_id, choice, cast_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (ballot_id, user_id)
        DO UPDATE SET choice = excluded.choice, cast_at = excluded.cast_at
        """,
        (ballot_id, guild_id, user_id, value, cast_at),
    )
    return True


def get_ballot_votes(conn: sqlite3.Connection, ballot_id: int) -> list[BallotVoteRow]:
    rows = conn.execute(
        "SELECT * FROM policy_ballot_votes WHERE ballot_id = ? ORDER BY cast_at",
        (ballot_id,),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[misc]


def tally_ballot(conn: sqlite3.Connection, ballot_id: int) -> BallotTally:
    """The live tally of an open ballot, or the cast votes of a closed one.

    A *closed* ballot's authoritative result is the frozen count on its own row,
    not this — erasing a member's vote is allowed to change what this returns
    and must never change what was announced.
    """
    return tally_choices(get_ballot_votes(conn, ballot_id))


def frozen_counts(ballot: BallotRow | dict) -> tuple[int, int, int]:
    """The counts recorded at close, with un-closed ballots reading as zeroes."""
    return (
        int(ballot.get("yes_count") or 0),
        int(ballot.get("no_count") or 0),
        int(ballot.get("abstain_count") or 0),
    )


def close_ballot(
    conn: sqlite3.Connection,
    ballot_id: int,
    *,
    closed_by: int | None,
    cancelled: bool = False,
    now: float | None = None,
) -> BallotRow | None:
    """Freeze the tally, decide the outcome, and return the closed row.

    Returns ``None`` when the ballot is already closed or does not exist, which
    is what makes this safe to race: the deadline sweep and a moderator's Close
    press can both fire, and exactly one of them wins. The guard is the
    ``closed_at IS NULL`` clause in the UPDATE, not a read-then-write, so two
    connections cannot both see it open.

    ``closed_by`` is ``None`` when the deadline sweep closed it rather than a
    person. ``cancelled=True`` records the counts but claims no result — used
    when the proposal behind the ballot is closed out from under it.
    """
    closed_at = time.time() if now is None else now
    tally = tally_ballot(conn, ballot_id)
    yes, no, abstain = len(tally["yes"]), len(tally["no"]), len(tally["abstain"])
    outcome = OUTCOME_CANCELLED if cancelled else ballot_outcome(yes, no)
    cur = conn.execute(
        """
        UPDATE policy_ballots
           SET closed_at = ?, closed_by = ?, yes_count = ?, no_count = ?,
               abstain_count = ?, outcome = ?
         WHERE id = ? AND closed_at IS NULL
        """,
        (closed_at, closed_by, yes, no, abstain, outcome, ballot_id),
    )
    if cur.rowcount == 0:
        return None
    return get_ballot(conn, ballot_id)


def find_expired_ballots(
    conn: sqlite3.Connection, guild_id: int, *, now: float | None = None
) -> list[BallotRow]:
    """Open ballots in this guild whose deadline has passed.

    ``closes_at = 0`` (the guild set its deadline dial to 0) is excluded, so
    those wait for a Close press forever, matching the mod vote's behaviour at
    the same setting.
    """
    at = time.time() if now is None else now
    rows = conn.execute(
        "SELECT * FROM policy_ballots WHERE guild_id = ? AND closed_at IS NULL "
        "AND closes_at > 0 AND closes_at <= ? ORDER BY id",
        (guild_id, at),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[misc]
