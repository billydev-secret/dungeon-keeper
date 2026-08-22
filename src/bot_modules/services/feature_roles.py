"""The role dials Dungeon Keeper provisions for itself.

One place naming every role the bot will create rather than ask an admin to
make, so the set is auditable and the Stage 3 dashboard card has something to
read. See ``docs/plans/role-autocreate.md`` for the audit that decided
membership; the short version is that a dial belongs here only if the role
exists *because the feature exists* and holding it grants nothing.

**Being ping-only is the entry requirement, and it is stricter than it looks.**
Three dials that read like ping roles are deliberately absent, because for each
of them an empty role is worse than no role:

* ``guess_role_id`` — an unset dial means "Guess isn't set up" and the game says
  so plainly. Provisioning turns that into a configured game that refuses every
  member with "you need the Guess role", because nobody holds the new one.
* ``voice_master_spectator_gate_role_id`` — ungated spectate makes ``@everyone``
  the audience; a gate role *denies* ``@everyone`` Connect and hands the room to
  the role instead. An empty gate role is a spectate room nobody can enter.
Two more are absent because their storage cannot tell "never configured" from
"the admin picked (none)" — and that distinction is the only thing that makes
provisioning a ping role safe (see ``role_provision.role_dial_opted_out``):

* ``bump_tracker_config.role_id`` — the column is ``NOT NULL DEFAULT 0``, so
  both cases store 0.
* ``revive_guild_config.role_id`` — nullable, but the panel's "(none)" option
  has ``value=""``, which the panel turns into ``null`` on save. Both cases
  store NULL.

Per-instance dials (a scheduled game's announce role, a photo challenge's ping,
a Chat Revive per-channel override) are absent for a third reason: their "unset"
was chosen by an admin filling in a form that offered "(none)", so there is no
never-configured state to detect at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot_modules.core.role_provision import RoleSpec


@dataclass(frozen=True)
class FeatureRole:
    """A ``config`` dial the bot may provision, and what to call the role."""

    #: The ``config`` key holding the role id. Economy settings live in the
    #: same table under an ``econ_`` prefix, so their keys carry it.
    key: str
    #: Human name for the feature, used in the mod-log line on a recreate.
    feature: str
    #: What the role should look like when it has to be made.
    spec: RoleSpec
    #: One line describing the role to a member choosing it in Discord's
    #: onboarding. Kept to Discord's 100-character option-description cap.
    blurb: str = ""
    #: Suggested emoji for the onboarding option.
    emoji: str = ""
    #: Whether a missing row falls back to the legacy ``guild_id=0`` row, which
    #: has to match how the feature itself reads the key or we'd provision a
    #: role for a guild that is really inheriting an answer. Economy settings
    #: are guild-scoped only.
    legacy_fallback: bool = True


def _ping(
    key: str,
    feature: str,
    name: str,
    *,
    blurb: str = "",
    emoji: str = "",
    legacy_fallback: bool = True,
) -> FeatureRole:
    """A ping-only role: no permissions, and not mentionable.

    Not mentionable is deliberate — the bot pings these through
    ``AllowedMentions(roles=[role])``, which does not need the bit, and leaving
    it off stops any member from mass-pinging the subscriber list by hand.
    """
    return FeatureRole(
        key=key,
        feature=feature,
        blurb=blurb,
        emoji=emoji,
        legacy_fallback=legacy_fallback,
        spec=RoleSpec(
            name=name,
            reason=f"Dungeon Keeper {feature} setup",
            mentionable=False,
        ),
    )


WELCOME_PING = _ping(
    "welcome_ping_role_id", "welcome messages", "Welcome Ping",
    blurb="Get a nudge when somebody new arrives, so you can say hello.",
    emoji="👋",
)
QOTD_PING = _ping(
    "econ_qotd_ping_role_id", "the question of the day", "QOTD",
    blurb="Be told when the question of the day goes up.",
    emoji="💬",
    legacy_fallback=False,
)
RISKY_PING = _ping(
    "risky_ping_role_id", "the Risky Rolls round ping", "Risky Rolls",
    blurb="Get pinged when a Risky Rolls round opens.",
    emoji="🎲",
)
PROMOTION_REVIEW_PING = _ping(
    "promotion_review_ping_role_id", "promotion reviews", "Promotion Reviewers",
    blurb="For role managers: be told when someone needs reviewing.",
    emoji="📋",
)
ECONOMY_NOTIFY = _ping(
    "econ_game_role_id", "economy notifications", "Economy Notifications",
    blurb="Get your streak digest and event alerts in your DMs.",
    emoji="🔔",
    legacy_fallback=False,
)

#: Every dial the bot provisions. All five live in the ``config`` KV — that is
#: not a coincidence: it is the only store where a never-set dial is
#: distinguishable from one an admin set to "(none)".
CONFIG_ROLES: tuple[FeatureRole, ...] = (
    WELCOME_PING,
    QOTD_PING,
    RISKY_PING,
    PROMOTION_REVIEW_PING,
    ECONOMY_NOTIFY,
)
