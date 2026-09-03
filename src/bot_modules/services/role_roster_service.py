"""What state each bot-managed role is in, and what to say about it.

The Bot-Managed Roles page (``bot-roles``) is an audit surface, not a form: an
admin opens it to answer "what has this bot done to my role list, is any of it
broken, and what do I do about it". So the interesting work is deciding which
of nine states a role is in and writing one true sentence about each — which is
exactly the kind of thing that belongs in a tested service rather than in a
panel's template.

Everything here is pure. Discord objects are flattened to :class:`LiveRole` by
the route before they arrive, which is what lets the whole state table be
tested without a gateway.

**Provenance beats inference.** ``bot_managed_roles`` (migration 203) records
what the provisioner actually did, so "I made this role" and "I adopted a role
you already had" are facts. Every role provisioned before that table existed —
and the DM trio, whose call site can never write one — has no row, and those
degrade to the old inference and say so rather than guessing confidently.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from bot_modules.services.feature_roles import FeatureRole
from bot_modules.services.role_provenance import RoleProvenance

#: The badge on the card. One primary state per role, chosen by the precedence
#: in :func:`describe_role` — an admin acts on one thing at a time, and a card
#: showing four pills is a card nobody reads.
IN_USE = "in_use"
OUT_OF_REACH = "out_of_reach"
DELETED = "deleted"
INHERITED = "inherited"
TURNED_OFF = "turned_off"
NOT_MADE = "not_made"
ADOPTABLE = "adoptable"
OFFER_FIRST = "offer_first"


@dataclass(frozen=True)
class LiveRole:
    """One Discord role, flattened to the five things this module needs."""

    id: int
    name: str
    position: int
    managed: bool = False
    member_count: int = 0


@dataclass(frozen=True)
class DialReading:
    """What the stores say about one dial, before any judgement."""

    #: The id the feature would read today (legacy fallback included).
    stored_id: int = 0
    #: Whether that id came from *this guild's* own row. False means it was
    #: inherited from the legacy ``guild_id = 0`` row and names a role in
    #: another server — which is why it never resolves here, and why calling
    #: that a deletion was a false accusation.
    stored_is_own: bool = True
    #: The admin explicitly chose "(none)" and the dial honours that.
    opted_out: bool = False
    #: The role ``stored_id`` resolves to, if any.
    live_role: LiveRole | None = None
    #: Every role in the guild whose name matches the spec exactly, lowest
    #: position first — the adopt-by-name candidates.
    named_matches: tuple[LiveRole, ...] = ()
    #: The bot's own top role position, or None when it can't be read.
    bot_top_position: int | None = None
    #: What the provisioner recorded, if anything.
    provenance: RoleProvenance | None = None


@dataclass(frozen=True)
class RoleCard:
    """One card on the roster: a state, a sentence, and what can be done."""

    key: str
    name: str
    emoji: str
    feature: str
    state: str
    #: The sentence under the role name. Says the consequence, not the state —
    #: a word in a pill tells an admin what is true and not what happens next.
    headline: str
    role_id: int = 0
    current_name: str = ""
    member_count: int = 0
    #: True when the bot hands this role out, so hierarchy matters and the
    #: "Roles I hand out" group is where it belongs.
    assigns: bool = False
    #: Where the dial lives, for the deep link.
    panel: str = ""
    panel_label: str = ""
    dial_label: str = ""
    #: Provenance, when we have it: "created" / "adopted" / "" for unknown.
    origin: str = ""
    can_create: bool = False
    can_adopt: bool = False
    can_stop: bool = False
    #: Extra facts the primary state doesn't carry.
    notes: tuple[str, ...] = field(default_factory=tuple)


def _reach_ok(entry: FeatureRole, role: LiveRole, top: int | None) -> bool:
    """Can the bot actually hand this role out?

    Only asked for roles the bot assigns. Four of the pings are never given to
    anybody — a position warning on those would be crying wolf, and the group
    heading on the page says so in as many words.
    """
    if not entry.assigns or top is None:
        return True
    return role.position < top


def describe_role(entry: FeatureRole, reading: DialReading) -> RoleCard:
    """:func:`_describe_role`, with the buttons clamped to what is safe.

    Two clamps, both of which would otherwise be easy to forget on one branch
    out of nine:

    * **Only ``config``-KV dials get write buttons.** The DM trio, Survivor's
      three and the Wellness role are stored by their own features, in their
      own tables, and a second writer reaching into those is how a repoint gets
      silently undone by the owning page's next whole-form save. Those cards
      are read-only and link to the page that owns them.
    * **A create-on-offer role is never created from here.** Creating it alone
      is precisely the empty-role failure that kept these two dials out of the
      registry for a year; the button says "offer it in onboarding" instead.
    """
    card = _describe_role(entry, reading)
    if entry.source != "config":
        card = replace(card, can_create=False, can_adopt=False, can_stop=False)
    if entry.create_on_offer:
        card = replace(card, can_create=False)
    return card


def _describe_role(entry: FeatureRole, reading: DialReading) -> RoleCard:
    """The card for one role: which state, which sentence, which buttons.

    Precedence, and why it is this way:

    1. **Turned off** — an admin's explicit "(none)" beats every observation,
       because nothing else on the card is going to happen while it stands.
    2. **Deleted / inherited** — a stored id resolving to nothing. Which of the
       two it is turns on ``stored_is_own``, and getting that wrong is what
       posted "⚠️ **Jailed** was deleted" to a server that never had one.
    3. **Out of reach** — the role exists but the bot can't hand it out, so the
       feature is broken in a way no amount of configuration here will fix; the
       fix is in Discord and the sentence says where.
    4. **In use** — with a rename or a duplicate carried as a note rather than
       a state, since neither stops anything working.
    5. **Not made yet**, or **adoptable** when a role of the right name is
       already sitting there waiting to be picked up.
    """

    def card(
        *,
        state: str,
        headline: str,
        can_create: bool = False,
        can_adopt: bool = False,
        can_stop: bool = False,
        role_id: int = 0,
        current_name: str = "",
        member_count: int = 0,
        notes: tuple[str, ...] = (),
    ) -> RoleCard:
        """One card, with the eight fields every branch shares filled in.

        A shared kwargs dict would do the same job and type as `str | bool`,
        which is how a wrong field reaches a template instead of a type
        checker.
        """
        return RoleCard(
            key=entry.key,
            name=entry.spec.name,
            emoji=entry.emoji,
            feature=entry.feature,
            assigns=entry.assigns,
            panel=entry.panel,
            panel_label=entry.panel_label,
            dial_label=entry.dial_label,
            origin=reading.provenance.origin if reading.provenance else "",
            state=state,
            headline=headline,
            role_id=role_id,
            current_name=current_name,
            member_count=member_count,
            can_create=can_create,
            can_adopt=can_adopt,
            can_stop=can_stop,
            notes=notes,
        )

    where = (
        f"Set on {entry.panel_label} → {entry.dial_label}"
        if entry.panel_label and entry.dial_label
        else (f"Set on {entry.panel_label}" if entry.panel_label else "")
    )
    # A dial can only be switched back on where "(none)" is honoured at all.
    can_stop = entry.none_means_off and entry.source == "config"

    if reading.opted_out:
        return card(
            state=TURNED_OFF,
            headline=(
                f'You picked "(none)"{" on " + entry.panel_label if entry.panel_label else ""}, '
                "so I won't make one."
            ),
            can_create=not entry.create_on_offer,
            can_adopt=True,
            can_stop=False,
            notes=(where,) if where else (),
        )

    role = reading.live_role
    duplicates = tuple(r for r in reading.named_matches if role is None or r.id != role.id)

    if role is None and reading.stored_id and not reading.stored_is_own:
        return card(
            state=INHERITED,
            headline=(
                "This is another server's setting showing through — no role "
                "here yet. I'll make one when it's needed."
            ),
            can_create=not entry.create_on_offer,
            can_adopt=True,
            can_stop=can_stop,
        )

    if role is None and reading.stored_id:
        made = reading.provenance is not None and reading.provenance.origin == "created"
        return card(
            state=DELETED,
            headline=(
                ("The role I made is gone. " if made else "The role this points at is gone. ")
                + (
                    f"I'll make a replacement {entry.made_when}, and it will "
                    "start empty."
                    if entry.made_when and not entry.create_on_offer
                    else "Offer it to members again and I'll make a replacement."
                )
            ),
            can_create=not entry.create_on_offer,
            can_adopt=True,
            can_stop=can_stop,
        )

    if role is not None:
        notes: list[str] = []
        if where:
            notes.append(where)
        if role.name != entry.spec.name:
            notes.append(
                f"It's called @{role.name} now. That's fine — I go by id, not name."
            )
        if duplicates:
            notes.append(
                f"There are {len(duplicates) + 1} roles called @{entry.spec.name}. "
                "I'm using this one."
            )
        if reading.provenance is None:
            notes.append("Made before I started keeping track, so I can't say "
                         "whether I created it or adopted it.")
        elif reading.provenance.origin == "adopted":
            notes.append("This was already your role — I adopted it rather than "
                         "making a second one.")

        if not _reach_ok(entry, role, reading.bot_top_position):
            return card(
                state=OUT_OF_REACH,
                role_id=role.id,
                current_name=role.name,
                member_count=role.member_count,
                headline=(
                    f"@{role.name} sits above my own role, so I can't add or "
                    "remove it. Move Dungeon Keeper above it in Server "
                    "Settings → Roles."
                ),
                can_create=False,
                can_adopt=True,
                can_stop=can_stop,
                notes=tuple(notes),
            )
        return card(
            state=IN_USE,
            role_id=role.id,
            current_name=role.name,
            member_count=role.member_count,
            headline=(
                f"{role.member_count} members have it."
                if role.member_count != 1
                else "1 member has it."
            ),
            can_create=False,
            can_adopt=True,
            can_stop=can_stop,
            notes=tuple(notes),
        )

    if reading.named_matches:
        candidate = reading.named_matches[0]
        return card(
            state=ADOPTABLE,
            current_name=candidate.name,
            member_count=candidate.member_count,
            headline=(
                f"You already have a role called @{candidate.name}. I'll use "
                "that one rather than making a second."
            ),
            can_create=not entry.create_on_offer,
            can_adopt=True,
            can_stop=can_stop,
            notes=(where,) if where else (),
        )

    if entry.create_on_offer:
        return card(
            state=OFFER_FIRST,
            headline=(
                "Not made yet — and I won't make one until you offer it to "
                "members, because a role nobody holds would leave this feature "
                "switched on and refusing everybody."
            ),
            can_create=False,
            can_adopt=True,
            can_stop=can_stop,
            notes=(where,) if where else (),
        )

    return card(
        state=NOT_MADE,
        headline=(
            f"Not made yet — I'll create it {entry.made_when}."
            if entry.made_when
            else "Not made yet."
        ),
        can_create=True,
        can_adopt=True,
        can_stop=can_stop,
        notes=(where,) if where else (),
    )


def summary_line(cards: list[RoleCard]) -> str:
    """The one sentence at the top of the page, set at display size.

    Counts, not tiles: "14 managed / 12 healthy / 1 missing" is three numbers a
    person reads faster in a sentence, and the sentence can carry which role is
    broken, which no tile can.
    """
    made = [c for c in cards if c.role_id]
    broken = [c for c in cards if c.state in (DELETED, OUT_OF_REACH)]
    if not made:
        return "Dungeon Keeper hasn't made any roles in this server yet."
    n = len(made)
    head = (
        f"Dungeon Keeper is using {n} role{'s' if n != 1 else ''} in this server."
    )
    if not broken:
        return head + " They're all in working order."
    names = ", ".join(f"@{c.current_name or c.name}" for c in broken[:3])
    more = "" if len(broken) <= 3 else f" and {len(broken) - 3} more"
    verb = "needs" if len(broken) == 1 else "need"
    return f"{head} {names}{more} {verb} your attention."
