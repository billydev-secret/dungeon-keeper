"""The registry of role dials the bot provisions for itself.

These are guard tests, not behaviour tests: the audit in
``docs/plans/role-autocreate.md`` concluded that most role dials must never be
auto-created, and the cheapest way for that conclusion to rot is for someone to
add an entry here without re-deriving why it's safe. Each assertion below
encodes one of the audit's rules.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.services import feature_roles as fr


def test_every_entry_is_ping_only():
    """A provisioned role must grant nothing — that is the entry requirement."""
    for entry in fr.CONFIG_ROLES:
        assert entry.spec.permissions == discord.Permissions.none(), (
            f"{entry.key} would hand out permissions"
        )
        assert entry.spec.hoist is False


def test_no_entry_is_mentionable():
    """The bot pings these via AllowedMentions; the bit would only let members
    mass-ping the subscriber list by hand."""
    for entry in fr.CONFIG_ROLES:
        assert entry.spec.mentionable is False, f"{entry.key} is mentionable"


@pytest.mark.parametrize(
    "key",
    [
        # Unset means "Guess isn't configured" and the game says so. An empty
        # role turns that into a configured game refusing every member.
        "guess_role_id",
        # Ungated spectate makes @everyone the audience; a gate role denies
        # @everyone Connect. An empty gate role is a room nobody can enter.
        "voice_master_spectator_gate_role_id",
        # NOT NULL DEFAULT 0 — "never set" and "(none)" are the same value.
        "bump_tracker_role_id",
        # Nullable, but the panel's "(none)" saves NULL too.
        "revive_guild_config_role_id",
        # Names authority; an empty one is a silent no-op.
        "mod_role_ids",
        "admin_role_ids",
        # The guild's own membership role — the trap that motivated the audit.
        "opt_in_role_id",
    ],
)
def test_known_hazards_stay_out_of_the_registry(key):
    assert key not in {e.key for e in fr.CONFIG_ROLES}


def test_keys_are_unique():
    keys = [e.key for e in fr.CONFIG_ROLES]
    assert len(keys) == len(set(keys))


def test_role_names_are_distinct():
    """Adopt-by-name keys on these, so two entries sharing a name would have
    them fight over one role."""
    names = [e.spec.name for e in fr.CONFIG_ROLES]
    assert len(names) == len(set(names))


def test_every_entry_reads_as_a_sentence_in_the_mod_log():
    """The recreate notice is prose a mod reads, so both halves must fit it."""
    from bot_modules.core.role_provision import recreate_notice

    for entry in fr.CONFIG_ROLES:
        assert entry.feature.strip(), f"{entry.key} has no feature label"
        line = recreate_notice(entry.spec.name, entry.feature)
        assert entry.spec.name in line and entry.feature in line


def test_econ_prefixed_keys_do_not_use_the_legacy_fallback():
    """``load_econ_settings`` is guild-scoped with no ``guild_id=0`` fallback.

    Reading an econ key *with* the fallback would hand a guild the home guild's
    role id — a role that doesn't exist there — and provision over a guild that
    is actually configured.
    """
    for entry in fr.CONFIG_ROLES:
        if entry.key.startswith("econ_"):
            assert entry.legacy_fallback is False, entry.key


def test_onboarding_blurbs_fit_discord_limits():
    """These become option titles/descriptions in Discord's onboarding, where
    over-length is a 400 with an opaque message."""
    from bot_modules.services.onboarding_service import (
        MAX_OPTION_DESCRIPTION,
        MAX_OPTION_TITLE,
    )

    for entry in fr.CONFIG_ROLES:
        assert entry.blurb, f"{entry.key} has no onboarding blurb"
        assert len(entry.blurb) <= MAX_OPTION_DESCRIPTION, entry.key
        assert len(entry.spec.name) <= MAX_OPTION_TITLE, entry.key


def test_only_the_economy_notify_dial_ignores_a_stored_none():
    """``none_means_off=False`` is a real exception, not a default to spread.

    The dashboard panels save as whole forms and write "0" for any untouched
    picker, so a stored 0 is weak evidence everywhere. It is still a *documented
    wanted state* for the QOTD and promotion-review pings (a silent post), and
    only the economy opt-in role has no coherent "off" at all — the role IS the
    opt-in mechanism.
    """
    exempt = {e.key for e in fr.CONFIG_ROLES if not e.none_means_off}
    assert exempt == {"econ_game_role_id"}
