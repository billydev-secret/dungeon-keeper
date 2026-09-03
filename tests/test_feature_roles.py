"""The registry of every role the bot provisions for itself.

These are guard tests, not behaviour tests: the audit in
``docs/plans/role-autocreate.md`` concluded that most role dials must never be
auto-created, and the cheapest way for that conclusion to rot is for someone to
add an entry here without re-deriving why it's safe. Each assertion below
encodes one of the audit's rules.

Round 2 (2026-09-03) widened the registry from five entries to sixteen — the
nine roles features used to make on their own, plus the two dials reopened as
*create-on-offer* — so the same guards now cover fourteen dials instead of
five. That widening is most of the value of the refactor: a role outside this
tuple is one the Bot-Managed Roles page cannot show.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.services import feature_roles as fr


def test_every_entry_is_ping_only():
    """A provisioned role must grant nothing — that is the entry requirement."""
    for entry in fr.MANAGED_ROLES:
        assert entry.spec.permissions == discord.Permissions.none(), (
            f"{entry.key} would hand out permissions"
        )
        assert entry.spec.hoist is False


def test_no_ping_entry_is_mentionable():
    """The bot pings these via AllowedMentions; the bit would only let members
    mass-ping the subscriber list by hand."""
    for entry in fr.CONFIG_ROLES:
        assert entry.spec.mentionable is False, f"{entry.key} is mentionable"


@pytest.mark.parametrize(
    "key",
    [
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
    assert key not in {e.key for e in fr.MANAGED_ROLES}


@pytest.mark.parametrize(
    "key",
    [
        # Unset means "Guess isn't configured" and the game says so. An empty
        # role would turn that into a configured game refusing every member —
        # which is why it may only be made while it is being offered.
        "guess_role_id",
        # Ungated spectate makes @everyone the audience; a gate role denies
        # @everyone Connect. An empty gate role is a room nobody can enter.
        "voice_master_spectator_gate_role_id",
    ],
)
def test_the_two_reopened_dials_are_create_on_offer(key):
    """Round 2 reopened these two, and *only* on that condition.

    Dropping ``create_on_offer`` would silently restore the empty-role failure
    they were excluded for, in a way nothing else in the codebase would catch.
    """
    entry = fr.BY_KEY[key]
    assert entry.create_on_offer is True, key
    assert entry.opt_in is True, f"{key} must be offerable, or it can never exist"


def test_create_on_offer_entries_are_all_opt_in():
    """The condition is "created only while being offered to members", so an
    entry nobody can be offered could never legitimately be created at all."""
    for entry in fr.MANAGED_ROLES:
        if entry.create_on_offer:
            assert entry.opt_in, entry.key


def test_keys_are_unique():
    keys = [e.key for e in fr.MANAGED_ROLES]
    assert len(keys) == len(set(keys))


def test_role_names_are_distinct():
    """Adopt-by-name keys on these, so two entries sharing a name would have
    them fight over one role."""
    names = [e.spec.name for e in fr.MANAGED_ROLES]
    assert len(names) == len(set(names))


def test_every_entry_reads_as_a_sentence_in_the_mod_log():
    """The recreate notice is prose a mod reads, so both halves must fit it."""
    from bot_modules.core.role_provision import recreate_notice

    for entry in fr.MANAGED_ROLES:
        assert entry.feature.strip(), f"{entry.key} has no feature label"
        line = recreate_notice(entry.spec.name, entry.feature)
        assert entry.spec.name in line and entry.feature in line


def test_econ_prefixed_keys_do_not_use_the_legacy_fallback():
    """``load_econ_settings`` is guild-scoped with no ``guild_id=0`` fallback.

    Reading an econ key *with* the fallback would hand a guild the home guild's
    role id — a role that doesn't exist there — and provision over a guild that
    is actually configured.
    """
    for entry in fr.MANAGED_ROLES:
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


def test_the_economy_notify_dial_now_honours_none():
    """Billy, 2026-09-03 — and it reverses the 2026-08-22 call deliberately.

    ``economy-config.js`` told the admin "(none)" turned economy notifications
    off while ``economy/guide.py`` passed ``respect_opt_out=False`` and made the
    role anyway on the first 🔔 press. That is a preference the code did not
    enforce, which CLAUDE.md forbids outright. The dial is now real: with a
    stored "(none)" the button says notifications aren't set up here.
    """
    assert fr.ECONOMY_NOTIFY.none_means_off is True


def test_only_the_two_moderation_dials_ignore_a_stored_none():
    """``none_means_off=False`` is a real exception, not a default to spread.

    It survives on exactly two entries, and for one reason that is nothing to
    do with panels writing 0 on save: a jail with no role is not a jail, and an
    inactive sweep with no role strips members and gives them nothing. There is
    no coherent "off" for either, so a stored 0 there means "not set up yet".
    """
    exempt = {e.key for e in fr.MANAGED_ROLES if not e.none_means_off}
    assert exempt == {"jailed_role_id", "inactive_role_id"}


def test_config_roles_is_the_opt_in_subset():
    """``CONFIG_ROLES`` is what Discord onboarding may offer, and onboarding can
    only offer a role a member picks up themselves."""
    assert set(fr.CONFIG_ROLES) == {e for e in fr.MANAGED_ROLES if e.opt_in}
    for entry in fr.CONFIG_ROLES:
        assert entry.source == fr.SOURCE_CONFIG, (
            f"{entry.key} is offerable but its id isn't in the config KV, which "
            "is the only store that can tell 'never set' from '(none)'"
        )


def test_every_entry_names_the_page_that_owns_its_dial():
    """The roster's "Set on X → Y" line and its deep link both read this, and a
    route id that doesn't exist is a dead link on an audit page."""
    import re
    from pathlib import Path

    app_js = Path("src/web_server/static/js/app.js").read_text(encoding="utf-8")
    known = set(re.findall(r'id:\s*"([a-z0-9-]+)"', app_js))
    for entry in fr.MANAGED_ROLES:
        assert entry.panel, f"{entry.key} names no owning page"
        assert entry.panel in known, f"{entry.key} points at unknown page {entry.panel}"


def test_roles_the_bot_hands_out_are_marked():
    """``assigns`` drives two safety behaviours — the hierarchy filter on
    adopt-by-name, and the "out of reach" state — so a mis-set flag is a silent
    hole rather than a cosmetic one."""
    handed_out = {e.key for e in fr.MANAGED_ROLES if e.assigns}
    assert handed_out == {
        "econ_game_role_id",      # the one ping the bot actually grants
        "guess_role_id",          # /guess optin adds it
        "jailed_role_id",
        "inactive_role_id",
        "dm_mode_open_role_id",
        "dm_mode_ask_role_id",
        "dm_mode_closed_role_id",
        "role_survivor_id",
        "role_ghost_id",
        "role_sole_survivor_id",
        "wellness_role_id",
    }
    # The four mention-only pings and the spectate gate are NOT in that set:
    # the bot mentions them or names them in a channel overwrite, and a
    # hierarchy warning on those would be crying wolf.
    assert fr.WELCOME_PING.assigns is False
    assert fr.VOICE_SPECTATE_GATE.assigns is False


def test_the_registry_covers_sixteen_roles_across_fourteen_dials():
    """The corrected figure. "5 of 44" described this file, not the bot.

    Fourteen fixed-name roles were reachable before round 2 (5 ping dials +
    jailed + inactive + the DM trio + Survivor's three + Wellness); reopening
    ``guess_role_id`` and the spectate gate makes it sixteen across fourteen
    dials, since the DM trio and Survivor's three are three dials each.
    """
    assert len(fr.MANAGED_ROLES) == 16
    dials = {
        "config": {e.key for e in fr.MANAGED_ROLES if e.source == fr.SOURCE_CONFIG},
        "dm": {e for e in fr.MANAGED_ROLES if e.source == fr.SOURCE_DM_MODE},
        "survivor": {e for e in fr.MANAGED_ROLES if e.source == fr.SOURCE_SURVIVOR},
        "wellness": {e for e in fr.MANAGED_ROLES if e.source == fr.SOURCE_WELLNESS},
    }
    # 9 config keys + the dm_mode_roles row + the season config row + the
    # wellness_config row = 12 stores, 16 roles, 14 dials an admin can point.
    assert len(dials["config"]) == 9
    assert len(dials["dm"]) == 3
    assert len(dials["survivor"]) == 3
    assert len(dials["wellness"]) == 1


def test_spec_for_reads_the_registry():
    """Call sites take their spec from here so the roster and the provisioner
    can never disagree about what a role is called."""
    assert fr.spec_for("jailed_role_id").name == "Jailed"
    assert fr.spec_for("role_ghost_id", name="👻 Spook").name == "👻 Spook"
    assert fr.dm_mode_role("ask").spec.name == "DMs: Ask"
